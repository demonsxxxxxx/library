"""Reprocess primitive — clear a File's ingest state and re-enqueue it.

Used by:
  - POST /v1/files/{file_id}/reprocess        (user-driven, single)
  - POST /v1/files/reprocess                  (user-driven, bulk)
  - periodic_tick._dispatch_reprocess_low_quality  (self-heal, low-summary)

The mental model: "AI got smarter, redo this." The handler does all the
real work — reprocess just unblocks its write-once gate by clearing
`ingested_at`, purges entry_tags so the new run's tags fully replace
the old, and drops entry_relations touching the file's live entries so
derived recommendation edges can be rebuilt. dedup_key matches upload.py:318
so a stale pending/running
ingest_file row short-circuits cleanly.

Caller owns the transaction; this function never commits.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from library.db.models import EntryTag, File, FileEntry
from library.repositories import audit_events as audit_events_repo
from library.repositories import entries as entries_repo
from library.repositories import entry_relations as entry_relations_repo
from library.repositories import entry_tags as entry_tags_repo
from library.repositories import files as files_repo
from library.repositories import tasks as tasks_repo
from library.tasks.enqueue import enqueue
from library.tasks.kinds import KIND_INGEST_FILE


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def bulk_reprocess_file_ids_statement(
    *,
    payload: dict[str, object],
    after_file_id: str | None = None,
    limit: int | None = None,
):
    """Build a stable, paged file-id query for a bulk reprocess scope."""
    stmt = select(File.id).where(File.id.in_(tasks_repo.live_ingest_file_ids()))
    requires_entry_join = bool(
        payload.get("catalog_ids")
        or payload.get("folder_ids")
        or payload.get("tag_id")
    )
    if requires_entry_join:
        stmt = stmt.join(FileEntry, FileEntry.file_id == File.id).where(
            FileEntry.deleted_at.is_(None),
        )
    if payload.get("tag_id"):
        stmt = stmt.join(EntryTag, EntryTag.entry_id == FileEntry.id).where(
            EntryTag.tag_id == str(payload["tag_id"]),
        )
    if payload.get("catalog_ids"):
        stmt = stmt.where(FileEntry.catalog_id.in_(payload["catalog_ids"]))
    if payload.get("folder_ids"):
        stmt = stmt.where(FileEntry.folder_id.in_(payload["folder_ids"]))
    if payload.get("file_ids"):
        stmt = stmt.where(File.id.in_(payload["file_ids"]))
    if payload.get("status"):
        stmt = stmt.where(File.ingest_status == str(payload["status"]))
    if after_file_id:
        stmt = stmt.where(File.id > after_file_id)
    stmt = stmt.distinct().order_by(File.id.asc())
    return stmt.limit(limit) if limit is not None else stmt


async def reprocess_file(
    session: AsyncSession,
    file_row: File,
    *,
    scheduled_by: str = "reprocess",
) -> str | None:
    """Clear ingest state for one file and enqueue ingest_file.

    Returns the new task_id, or None if dedup short-circuited (a
    pending/running ingest_file row already covers this file).

    `scheduled_by` is recorded in the task_enqueued audit so we can
    distinguish user-driven reprocess from periodic self-heal in logs.
    """
    await tasks_repo.lock_ingest_file(session, file_row.id)
    await session.refresh(file_row)
    entry_ids = await files_repo.list_live_entry_ids_for_file(session, file_row.id)
    if file_row.deleted_at is not None or not entry_ids:
        return None
    now = _utcnow()
    for eid in entry_ids:
        await entry_tags_repo.delete_all_for_entry(session, eid)
    relation_count = await entry_relations_repo.delete_all_touching_entries(
        session, entry_ids,
    )

    seed = await entries_repo.find_seed_by_file_id(session, file_row.id)
    display_name = seed.display_name if seed is not None else None

    file_row.ingested_at = None
    file_row.ingest_status = "pending"
    file_row.updated_at = now

    await audit_events_repo.append(
        session,
        kind="reprocess_requested",
        payload={
            "file_id": file_row.id,
            "entry_count": len(entry_ids),
            "relation_count": relation_count,
            "scheduled_by": scheduled_by,
        },
    )

    task = await enqueue(
        session,
        kind=KIND_INGEST_FILE,
        payload={
            "file_id": file_row.id,
            "display_name": display_name,
            "scheduled_by": scheduled_by,
        },
        dedup_key=f"ingest_file:{file_row.id}",
    )
    if task is None:
        return None
    await audit_events_repo.append(
        session,
        kind="task_enqueued",
        task_id=task.id,
        payload={
            "task_id": task.id,
            "kind": KIND_INGEST_FILE,
            "file_id": file_row.id,
            "scheduled_by": scheduled_by,
        },
    )
    return task.id
