"""Task introspection HTTP routes.

These endpoints expose minimal task-queue state for CLI bookkeeping —
e.g. the embedded REPL checks `running-count` before exit so the user
can choose to wait for in-flight ingest work to finish before the
TaskRunner dies with the process. `/tasks/active` returns a small
listing (kind + payload preview + age) for the `/background` command,
so users can see what the worker is actually doing instead of just a
count.

These are not the worker's RPC surface; the worker reads the queue
directly from the DB.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from library.db.models import Task
from library.db.session import get_session
from library.api.pagination import decode_desc_cursor, encode_desc_cursor
from library.repositories import tasks as tasks_repo

router = APIRouter(tags=["tasks"])
INGEST_TASK_KINDS: tuple[str, ...] = ("ingest_file", "reprocess_file")


@router.get("/tasks/running-count")
async def running_count(
    db: AsyncSession = Depends(get_session),
) -> dict[str, int]:
    """Count tasks currently in `running` or `pending` status.

    Returned counts include both states because in embedded mode,
    pending tasks won't progress once the CLI exits either — the user
    cares about everything still on the queue, not just the in-flight
    rows.
    """
    return await tasks_repo.count_running_and_pending(db)


_PAYLOAD_KEYS_FOR_LABEL = ("display_name", "entry_id", "file_id", "session_id", "conversation_id", "path")


def _payload_label(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    name = payload.get("display_name")
    if name:
        return str(name)
    for key in _PAYLOAD_KEYS_FOR_LABEL:
        if key == "display_name":
            continue
        v = payload.get(key)
        if v:
            s = str(v)
            return f"{key}={s[:24] + ('...' if len(s) > 24 else '')}"
    # fall back to first key=value pair for visibility
    for k, v in payload.items():
        s = str(v)
        return f"{k}={s[:24] + ('...' if len(s) > 24 else '')}"
    return ""


@router.get("/tasks/active")
async def list_active(
    db: AsyncSession = Depends(get_session),
    limit: int = 30,
) -> dict[str, list[dict]]:
    """Compact listing of running + pending tasks for the `/background` CLI."""
    running = await tasks_repo.list_by_status(db, status="running", limit=limit)
    pending = await tasks_repo.list_by_status(db, status="pending", limit=limit)
    now = datetime.now(timezone.utc)

    def _row(t) -> dict:
        ref = t.started_at or t.scheduled_at
        if ref is not None and ref.tzinfo is None:
            # SQLite strips tzinfo on round-trip; the column stores UTC.
            ref = ref.replace(tzinfo=timezone.utc)
        age_s = int((now - ref).total_seconds()) if ref else 0
        payload = t.payload or {}
        return {
            "id": t.id,
            "kind": t.kind,
            "label": _payload_label(t.payload),
            "file_id": payload.get("file_id"),
            "entry_id": payload.get("entry_id"),
            "attempts": t.attempts,
            "age_s": max(age_s, 0),
        }

    return {
        "running": [_row(t) for t in running],
        "pending": [_row(t) for t in pending],
    }


@router.get("/tasks/recent")
async def list_recent(
    db: AsyncSession = Depends(get_session),
    limit: int = Query(default=30, ge=1, le=200),
    cursor: str | None = Query(default=None),
) -> dict[str, Any]:
    """Recently-finished tasks (done + dead + cancelled), newest first, with the
    per-run usage detail captured by the runner. Powers the StatusBar
    Activity popover so users can see how long ingest / reflect / embed
    took and how many tokens each call burned."""
    from library.repositories import tasks as tasks_repo
    before = decode_desc_cursor(cursor) if cursor is not None else None
    fetched = await tasks_repo.list_recent_with_usage(
        db,
        limit=limit + 1,
        before=before,
    )
    rows = fetched[:limit]

    def _row(r: dict) -> dict:
        payload = r["payload"] or {}
        detail = r["detail"] or {}
        return {
            "id": r["id"],
            "kind": r["kind"],
            "status": r["status"],
            "label": _payload_label(payload),
            "file_id": payload.get("file_id"),
            "entry_id": payload.get("entry_id"),
            "started_at": (
                r["started_at"].isoformat() if r["started_at"] else None
            ),
            "finished_at": (
                r["finished_at"].isoformat() if r["finished_at"] else None
            ),
            "last_error": r["last_error"],
            "duration_ms": detail.get("duration_ms"),
            "tokens_in": detail.get("tokens_in"),
            "prompt_tokens": detail.get("prompt_tokens"),
            "tokens_out": detail.get("tokens_out"),
            "cache_read": detail.get("cache_read"),
            "cache_creation": detail.get("cache_creation"),
            "llm_calls": detail.get("llm_calls"),
            "stages_ms": detail.get("stages_ms") or {},
        }

    next_cursor = (
        encode_desc_cursor(rows[-1]["finished_at"], rows[-1]["id"])
        if len(fetched) > limit and rows and rows[-1]["finished_at"] is not None
        else None
    )
    return {"items": [_row(r) for r in rows], "next_cursor": next_cursor}


@router.get("/tasks/throughput")
async def task_throughput(
    window_minutes: int = 60,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Report ingest queue backlog and recent completion throughput."""
    bounded_window = min(max(window_minutes, 1), 24 * 60)
    now = datetime.now(timezone.utc)
    since = now - timedelta(minutes=bounded_window)
    active = (
        await db.execute(
            select(Task).where(
                Task.kind == "ingest_file",
                Task.status.in_(("pending", "running")),
            )
        )
    ).scalars().all()
    terminal = (
        await db.execute(
            select(Task).where(
                Task.kind == "ingest_file",
                Task.status.in_(("done", "dead")),
                Task.finished_at >= since,
            )
        )
    ).scalars().all()
    return _task_throughput_payload(
        active=list(active),
        terminal=list(terminal),
        now=now,
        window_minutes=bounded_window,
    )


def _task_throughput_payload(
    *,
    active: list[Task],
    terminal: list[Task],
    now: datetime,
    window_minutes: int,
) -> dict[str, Any]:
    by_kind: dict[str, dict[str, Any]] = {
        kind: {
            "kind": kind,
            "pending": 0,
            "running": 0,
            "done": 0,
            "failed": 0,
            "oldest_pending_age_seconds": 0,
            "average_duration_seconds": None,
        }
        for kind in INGEST_TASK_KINDS
    }
    durations: dict[str, list[float]] = {kind: [] for kind in INGEST_TASK_KINDS}
    for task in active:
        throughput_kind = _throughput_kind(task)
        row = by_kind.get(throughput_kind)
        if row is None or task.status not in {"pending", "running"}:
            continue
        row[task.status] += 1
        if task.status == "pending" and task.scheduled_at is not None:
            scheduled_at = _as_utc(task.scheduled_at)
            row["oldest_pending_age_seconds"] = max(
                int(row["oldest_pending_age_seconds"]),
                max(0, int((now - scheduled_at).total_seconds())),
            )
    for task in terminal:
        throughput_kind = _throughput_kind(task)
        row = by_kind.get(throughput_kind)
        if row is None or task.status not in {"done", "dead"}:
            continue
        if task.status == "done":
            row["done"] += 1
        elif task.status == "dead":
            row["failed"] += 1
        if task.started_at is not None and task.finished_at is not None:
            durations[throughput_kind].append(max(
                0.0,
                (_as_utc(task.finished_at) - _as_utc(task.started_at)).total_seconds(),
            ))

    for kind, row in by_kind.items():
        terminal_count = int(row["done"]) + int(row["failed"])
        row["success_rate"] = (
            int(row["done"]) / terminal_count if terminal_count else None
        )
        row["completed_per_minute"] = int(row["done"]) / window_minutes
        if durations[kind]:
            row["average_duration_seconds"] = sum(durations[kind]) / len(durations[kind])

    kind_rows = list(by_kind.values())
    pending = sum(int(row["pending"]) for row in kind_rows)
    running = sum(int(row["running"]) for row in kind_rows)
    done = sum(int(row["done"]) for row in kind_rows)
    failed = sum(int(row["failed"]) for row in kind_rows)
    terminal_count = done + failed
    return {
        "window_minutes": window_minutes,
        "since": (now - timedelta(minutes=window_minutes)).isoformat(),
        "queue": {
            "pending": pending,
            "running": running,
            "total": pending + running,
            "oldest_pending_age_seconds": max(
                (int(row["oldest_pending_age_seconds"]) for row in kind_rows),
                default=0,
            ),
        },
        "completed": {
            "done": done,
            "failed": failed,
            "success_rate": done / terminal_count if terminal_count else None,
            "files_per_minute": done / window_minutes,
        },
        "by_kind": kind_rows,
    }


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _throughput_kind(task: Task) -> str:
    """Split real ingest tasks into ordinary and reprocess activity."""
    if task.kind == "reprocess_file":
        return "reprocess_file"
    raw_payload = getattr(task, "payload", None)
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    return "reprocess_file" if payload.get("scheduled_by") else "ingest_file"
