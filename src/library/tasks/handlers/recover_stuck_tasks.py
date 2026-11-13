"""recover_stuck_tasks — DESIGN.md §9.1.

Self-healing: when a worker dies mid-task (process crash, OOM, lease expired
without heartbeat reaching DB), the row stays at status='running' forever
unless something resurrects it. This handler does that.

Strategy: any row at status='running' AND lease_expires_at < now (with a tiny
grace window) is assumed dead. We:
  - reset status='pending', clear locked_by + lease_expires_at
  - leave attempts unchanged (the runner already incremented on claim — it's
    "fair" to count this as one used attempt; backoff is not extended since
    we don't know how the worker died)
  - if attempts >= max_attempts, mark dead instead

Audit: one `task_recovered` event per recovered row, plus one `task_marked_dead`
per row that exhausted retries.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from library.repositories import audit_events as audit_events_repo
from library.db.session import session_scope
from library.repositories import tasks as tasks_repo
from library.services.ingest_status import mark_file_failed_for_dead_ingest_task
from library.tasks.kinds import KIND_RECOVER_STUCK_TASKS, task_handler

log = logging.getLogger(__name__)

GRACE_SECONDS = 10  # treat anything past lease+grace as definitively dead

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)

@task_handler(KIND_RECOVER_STUCK_TASKS)
async def handle_recover_stuck_tasks(payload: Mapping[str, Any]) -> None:
    now = _utcnow()
    cutoff = now - timedelta(seconds=GRACE_SECONDS)
    try:
        limit = max(1, min(int(payload.get("limit") or 500), 5000))
    except (TypeError, ValueError):
        limit = 500

    async with session_scope() as session:
        rows = await tasks_repo.list_stale_running(
            session,
            now=cutoff,
            limit=limit,
        )

        file_ids = sorted({
            str(t.payload["file_id"]) for t in rows
            if t.kind == "ingest_file" and (t.payload or {}).get("file_id")
        })
        for file_id in file_ids:
            await tasks_repo.lock_ingest_file(session, file_id)
        await tasks_repo.cancel_orphan_ingests(session, now=now, file_ids=file_ids)

        recovered = 0
        marked_dead = 0
        for t in rows:
            previous_locked = t.locked_by
            previous_lease = t.lease_expires_at
            if t.attempts >= t.max_attempts:
                changed = await tasks_repo.mark_running_dead(
                    session,
                    task_id=t.id,
                    now=now,
                    error="recover_stuck_tasks: lease expired beyond max_attempts",
                    previous_worker_id=previous_locked,
                    previous_lease_expires_at=previous_lease,
                )
                if not changed:
                    continue
                await mark_file_failed_for_dead_ingest_task(
                    session,
                    task_id=t.id,
                    kind=t.kind,
                    payload=t.payload or {},
                    reason="recover_stuck_tasks: lease expired beyond max_attempts",
                )
                await audit_events_repo.append(
                    session,
                    kind="task_marked_dead",
                    task_id=t.id,
                    payload={
                        "kind": t.kind,
                        "attempts": t.attempts,
                        "max_attempts": t.max_attempts,
                        "previous_locked_by": previous_locked,
                        "previous_lease_expires_at": (
                            previous_lease.isoformat() if previous_lease else None
                        ),
                        "reason": "lease_expired_max_attempts",
                    },
                )
                marked_dead += 1
            else:
                changed = await tasks_repo.revive_running_to_pending(
                    session,
                    task_id=t.id,
                    now=now,
                    previous_worker_id=previous_locked,
                    previous_lease_expires_at=previous_lease,
                )
                if not changed:
                    continue
                await audit_events_repo.append(
                    session,
                    kind="task_recovered",
                    task_id=t.id,
                    payload={
                        "kind": t.kind,
                        "attempts": t.attempts,
                        "previous_locked_by": previous_locked,
                        "previous_lease_expires_at": (
                            previous_lease.isoformat() if previous_lease else None
                        ),
                    },
                )
                recovered += 1

        if recovered or marked_dead:
            log.info(
                "recover_stuck_tasks: recovered=%d marked_dead=%d", recovered, marked_dead
            )
        await session.commit()
