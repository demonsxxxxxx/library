from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
import uuid
from datetime import datetime, timedelta, timezone

from library.config import LlmConfigError, Settings, get_settings, validate_llm_config
from library.db.session import session_scope
from library.repositories import tasks as tasks_repo
from library.repositories import task_outcomes as outcomes_repo
from library.services.ingest_status import mark_file_failed_for_dead_ingest_task
from library.tasks import handlers as _handlers_pkg  # noqa: F401  (register)
from library.tasks.ingest_guard import IngestCancelled
from library.tasks.kinds import KIND_PERIODIC_TICK, LLM_DEPENDENT_KINDS, get_handler
from library.tasks.usage import (
    bind_accumulator, unbind_accumulator, UsageCounters,
)

log = logging.getLogger(__name__)


_NO_LLM_KEY_ERROR = (
    "skipped: no LLM api_key configured. "
    "Set LLM_DEFAULT_API_KEY in .env or in Settings → LLM Profile, then restart."
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _backoff(attempts: int, settings: Settings) -> timedelta:
    exponent = min(max(0, attempts - 1), 63)
    seconds = min(
        settings.worker_retry_base_seconds * (2**exponent),
        settings.worker_retry_max_seconds,
    )
    return timedelta(seconds=seconds)


class TaskRunner:
    """In-process async worker. Polls `tasks` table, claims rows, runs handlers."""

    def __init__(self, settings: Settings | None = None, worker_id: str | None = None) -> None:
        self.settings = settings or get_settings()
        self._static_settings = settings is not None
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}"
        self._stop = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._inflight: set[asyncio.Task[None]] = set()
        # A process may reclaim a task while the coroutine from an expired
        # delivery is still winding down.  Give every delivery its own owner
        # token so the stale coroutine cannot heartbeat or finish the new
        # lease merely because both deliveries ran in this process.
        self._claim_owners: dict[str, str] = {}

    async def start(self) -> None:
        if self._loop_task is not None:
            return
        await self._sweep_llm_dependent_if_no_key()
        if self._current_settings().worker_scheduler_enabled:
            from library.tasks.handlers.periodic_tick import bootstrap_periodic_tick
            await bootstrap_periodic_tick()
        self._stop.clear()
        self._loop_task = asyncio.create_task(self._run(), name="library.task_runner")

    @property
    def is_running(self) -> bool:
        """True while the polling loop task is alive.

        The lifecycle manager and status endpoints use this to distinguish
        "configured on but not actually running" (e.g. the loop died or was
        never started) from a healthy worker.
        """
        return self._loop_task is not None and not self._loop_task.done()

    def _has_llm_key(self) -> bool:
        try:
            validate_llm_config(self._current_settings())
        except LlmConfigError:
            return False
        return True

    async def _sweep_llm_dependent_if_no_key(self) -> None:
        """At startup, if no api_key is configured, mark all pending
        LLM-dependent tasks dead. Otherwise the runner picks them up
        within seconds and they fail one by one with OpenAIError noise.

        Runs once per start(). The user's path back is: set a key,
        restart the app — bootstrap_periodic_tick will then re-enqueue
        the periodic kinds normally on the next startup."""
        # Reconcile legacy orphan ingests before the no-key failure sweep;
        # deletion is cancellation, never a missing-key parsing failure.
        async with session_scope() as session:
            await tasks_repo.cancel_orphan_ingests(session, now=_now())
            await session.commit()
        if self._has_llm_key():
            return
        async with session_scope() as session:
            pending = await tasks_repo.list_pending_by_kinds(
                session,
                kinds=sorted(LLM_DEPENDENT_KINDS),
            )
            n = await tasks_repo.mark_pending_dead_by_kinds(
                session,
                kinds=sorted(LLM_DEPENDENT_KINDS),
                now=_now(),
                error=_NO_LLM_KEY_ERROR,
            )
            for task in pending:
                await mark_file_failed_for_dead_ingest_task(
                    session,
                    task_id=task.id,
                    kind=task.kind,
                    payload=task.payload or {},
                    reason=_NO_LLM_KEY_ERROR,
                )
            await session.commit()
        if n:
            log.warning(
                "marked %d pending LLM-dependent task(s) dead: no api_key configured",
                n,
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._loop_task:
            await self._loop_task
            self._loop_task = None
        if self._inflight:
            await asyncio.gather(*self._inflight, return_exceptions=True)

    async def _run(self) -> None:
        log.info("TaskRunner %s starting", self.worker_id)
        while not self._stop.is_set():
            settings = self._current_settings()
            max_concurrent = max(1, int(settings.worker_batch_size or 1))
            available = max_concurrent - len(self._inflight)
            if available <= 0:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=settings.worker_poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            try:
                claimed = await self._claim_batch(available)
            except Exception:
                log.exception("claim batch failed")
                claimed = []
            if not claimed:
                try:
                    await asyncio.wait_for(
                        self._stop.wait(),
                        timeout=settings.worker_poll_interval_seconds,
                    )
                except asyncio.TimeoutError:
                    pass
                continue
            log.info("claimed %d task(s): %s", len(claimed), ", ".join(claimed))
            for task_id in claimed:
                owner_id = self._claim_owners.pop(task_id, None)
                if owner_id is None:
                    # Keep test/custom claimers that return plain ids
                    # compatible with the runner's original extension seam.
                    t = asyncio.create_task(self._process(task_id))
                else:
                    t = asyncio.create_task(self._process(task_id, owner_id))
                self._inflight.add(t)
                t.add_done_callback(self._inflight.discard)
        log.info("TaskRunner %s stopped", self.worker_id)

    def _current_settings(self) -> Settings:
        if self._static_settings:
            return self.settings
        return get_settings()

    async def _claim_batch(self, limit: int) -> list[str]:
        now = _now()
        settings = self._current_settings()
        lease_until = now + timedelta(seconds=settings.worker_lease_seconds)
        async with session_scope() as session:
            rows = await tasks_repo.claim_pending_ids(
                session,
                now=now,
                limit=limit,
                exclude_kinds=(
                    ()
                    if settings.worker_scheduler_enabled
                    else (KIND_PERIODIC_TICK,)
                ),
            )
            if not rows:
                await session.commit()
                return []
            claimed: list[str] = []
            owners: dict[str, str] = {}
            for task_id in rows:
                owner_id = self._new_claim_owner()
                changed = await tasks_repo.mark_running(
                    session,
                    ids=[task_id],
                    now=now,
                    lease_until=lease_until,
                    worker_id=owner_id,
                )
                if changed:
                    claimed.extend(changed)
                    owners.update(dict.fromkeys(changed, owner_id))
            await session.commit()
            self._claim_owners.update(owners)
            return claimed

    def _new_claim_owner(self) -> str:
        # Task.locked_by is VARCHAR(64).  Retain a readable worker prefix and
        # reserve 33 characters for ':' plus the UUID delivery nonce.
        return f"{self.worker_id[:31]}:{uuid.uuid4().hex}"

    async def _process(self, task_id: str, owner_id: str | None = None) -> None:
        owner_id = owner_id or self._claim_owners.pop(task_id, None) or self.worker_id
        async with session_scope() as session:
            task = await tasks_repo.get(session, task_id)
            if (
                task is None
                or task.status != "running"
                or task.locked_by != owner_id
            ):
                return
            handler = get_handler(task.kind)
            payload = dict(task.payload or {})
            attempts = task.attempts
            max_attempts = task.max_attempts
            kind = task.kind
            # Handlers normally consume only their public payload. Reserved
            # runtime fields let resumable work bind checkpoints to the exact
            # task row without changing every handler signature.
            payload["_task_id"] = task_id
            payload["_task_owner"] = owner_id
            payload["_task_attempt"] = attempts

        if handler is None:
            await self._fail(
                task_id,
                attempts,
                max_attempts,
                f"no handler registered for {kind!r}",
                owner_id,
            )
            return

        log.info(
            "task %s (%s) started attempt=%d/%d",
            task_id,
            kind,
            attempts,
            max_attempts,
        )

        # Guard: if this kind needs LLM and no key is configured, mark
        # dead immediately with a clean message instead of letting the
        # handler crash with `OpenAIError: Missing credentials`. Catches
        # rows queued before bootstrap was guarded, plus anything
        # enqueued by future code paths that forget to check first.
        if kind in LLM_DEPENDENT_KINDS and not self._has_llm_key():
            await self._fail(
                task_id,
                max_attempts,
                max_attempts,
                _NO_LLM_KEY_ERROR,
                owner_id,
            )
            return

        token = bind_accumulator()
        started = time.monotonic()
        handler_task = asyncio.create_task(
            handler(payload),
            name=f"library.task_handler.{kind}.{task_id}",
        )
        heartbeat = asyncio.create_task(
            self._heartbeat(task_id, owner_id, handler_task)
        )
        try:
            await handler_task
        except IngestCancelled as exc:
            unbind_accumulator(token)
            async with session_scope() as session:
                await tasks_repo.mark_cancelled(
                    session, task_id=task_id, now=_now(),
                    reason=str(exc), worker_id=owner_id,
                )
                await session.commit()
            return
        except asyncio.CancelledError:
            # A failed fenced heartbeat means another delivery now owns this
            # row. Stop cooperative handler work promptly and leave all queue
            # transitions to the current owner. Any already-committed handler
            # effects must remain idempotent, but this prevents further calls
            # after ownership loss.
            unbind_accumulator(token)
            log.warning(
                "task %s (%s) cancelled after delivery ownership was lost",
                task_id,
                kind,
            )
            return
        except Exception as exc:
            heartbeat.cancel()
            log.exception("task %s (%s) failed", task_id, kind)
            duration_ms = int((time.monotonic() - started) * 1000)
            counters = unbind_accumulator(token) or UsageCounters()
            changed = await self._fail(
                task_id,
                attempts,
                max_attempts,
                repr(exc),
                owner_id,
            )
            if changed:
                await self._record_outcome(
                    task_id=task_id, kind=kind, outcome="error",
                    counters=counters, duration_ms=duration_ms,
                )
            return
        finally:
            heartbeat.cancel()

        duration_ms = int((time.monotonic() - started) * 1000)
        counters = unbind_accumulator(token) or UsageCounters()
        async with session_scope() as session:
            changed = await tasks_repo.mark_done(
                session,
                task_id=task_id,
                now=_now(),
                worker_id=owner_id,
            )
            if changed:
                await outcomes_repo.record_outcome(
                    session,
                    task_kind=kind,
                    object_kind="task",
                    object_id=task_id,
                    outcome="applied",
                    detail=counters.to_detail(duration_ms=duration_ms),
                )
                log.info("task %s (%s) done duration_ms=%d", task_id, kind, duration_ms)
            await session.commit()

    async def _record_outcome(
        self, *, task_id: str, kind: str, outcome: str,
        counters: UsageCounters, duration_ms: int,
    ) -> None:
        try:
            async with session_scope() as session:
                await outcomes_repo.record_outcome(
                    session,
                    task_kind=kind,
                    object_kind="task",
                    object_id=task_id,
                    outcome=outcome,
                    detail=counters.to_detail(duration_ms=duration_ms),
                )
                await session.commit()
        except Exception:
            log.exception("failed to record task_outcome for %s", task_id)

    async def _heartbeat(
        self,
        task_id: str,
        owner_id: str | None = None,
        handler_task: asyncio.Task[None] | None = None,
    ) -> None:
        owner_id = owner_id or self.worker_id
        try:
            while True:
                settings = self._current_settings()
                interval = settings.worker_heartbeat_seconds
                await asyncio.sleep(interval)
                async with session_scope() as session:
                    now = _now()
                    changed = await tasks_repo.heartbeat(
                        session,
                        task_id=task_id,
                        lease_until=now + timedelta(
                            seconds=settings.worker_lease_seconds,
                        ),
                        now=now,
                        worker_id=owner_id,
                    )
                    await session.commit()
                    if not changed:
                        log.warning(
                            "task %s lost delivery lease; heartbeat stopped",
                            task_id,
                        )
                        if handler_task is not None and not handler_task.done():
                            handler_task.cancel("task delivery lease lost")
                        return
        except asyncio.CancelledError:
            return

    async def _fail(
        self,
        task_id: str,
        attempts: int,
        max_attempts: int,
        error: str,
        owner_id: str | None = None,
    ) -> bool:
        owner_id = owner_id or self.worker_id
        async with session_scope() as session:
            task = await tasks_repo.get(session, task_id)
            if task is not None and task.kind == "ingest_file":
                file_id = (task.payload or {}).get("file_id")
                if file_id:
                    await tasks_repo.lock_ingest_file(session, str(file_id))
                    await tasks_repo.cancel_orphan_ingests(
                        session, now=_now(), file_ids=[str(file_id)],
                    )
            if attempts >= max_attempts:
                changed = await tasks_repo.mark_dead(
                    session,
                    task_id=task_id,
                    now=_now(),
                    error=error,
                    worker_id=owner_id,
                )
                if changed and task is not None:
                    await mark_file_failed_for_dead_ingest_task(
                        session,
                        task_id=task.id,
                        kind=task.kind,
                        payload=task.payload or {},
                        reason=error,
                    )
                if changed:
                    log.warning(
                        "task %s (%s) marked dead after %d/%d attempts: %s",
                        task_id,
                        task.kind if task is not None else "?",
                        attempts,
                        max_attempts,
                        error,
                    )
            else:
                next_run_at = _now() + _backoff(
                    attempts,
                    self._current_settings(),
                )
                changed = await tasks_repo.reschedule_for_retry(
                    session,
                    task_id=task_id,
                    error=error,
                    next_run_at=next_run_at,
                    worker_id=owner_id,
                )
                if changed:
                    log.warning(
                        "task %s (%s) scheduled for retry attempt=%d/%d next_run_at=%s: %s",
                        task_id,
                        task.kind if task is not None else "?",
                        attempts,
                        max_attempts,
                        next_run_at.isoformat(),
                        error,
                    )
            await session.commit()
            return changed


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    runner = TaskRunner()
    await runner.start()
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await runner.stop()


if __name__ == "__main__":
    asyncio.run(main())
