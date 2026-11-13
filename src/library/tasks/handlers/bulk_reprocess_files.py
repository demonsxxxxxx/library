from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from library.config import get_settings
from library.db.models import File, Task
from library.db.session import session_scope
from library.repositories import audit_events as audit_events_repo
from library.repositories import tasks as tasks_repo
from library.services.reprocess import (
    bulk_reprocess_file_ids_statement,
    reprocess_file,
)
from library.tasks.kinds import KIND_BULK_REPROCESS_FILES, task_handler


@task_handler(KIND_BULK_REPROCESS_FILES)
async def handle_bulk_reprocess_files(payload: Mapping[str, Any]) -> None:
    task_payload = dict(payload)
    page_size = max(10, min(
        5_000,
        int(task_payload.get("page_size") or get_settings().bulk_reprocess_page_size),
    ))
    async with session_scope() as session:
        await _dispatch_bulk_reprocess_pages(
            session,
            task_payload=task_payload,
            page_size=page_size,
        )


async def _dispatch_bulk_reprocess_pages(
    session: AsyncSession,
    *,
    task_payload: dict[str, Any],
    page_size: int,
) -> None:
    dispatcher_task_id = str(task_payload.get("dispatcher_task_id") or "")
    if not dispatcher_task_id:
        raise ValueError("bulk_reprocess_files payload missing dispatcher_task_id")

    checkpoint = dict(task_payload.get("checkpoint") or {})
    cursor = str(checkpoint.get("last_file_id") or "") or None
    files_matched = int(checkpoint.get("files_matched") or 0)
    tasks_created = int(checkpoint.get("tasks_created") or 0)
    tasks_reused = int(checkpoint.get("tasks_reused") or 0)
    skipped = int(checkpoint.get("skipped") or 0)
    pages_completed = int(checkpoint.get("pages_completed") or 0)
    page_size = max(10, min(5_000, int(page_size)))

    while True:
        file_ids = list((await session.execute(
            bulk_reprocess_file_ids_statement(
                payload=task_payload,
                after_file_id=cursor,
                limit=page_size + 1,
            )
        )).scalars().all())
        has_more = len(file_ids) > page_size
        page_file_ids = file_ids[:page_size]
        if not page_file_ids:
            break

        for file_id in page_file_ids:
            file_row = await session.get(File, file_id)
            if file_row is None or file_row.deleted_at is not None:
                skipped += 1
                continue
            existing = await tasks_repo.find_pending_or_running_by_dedup(
                session,
                f"ingest_file:{file_id}",
            )
            task_id = await reprocess_file(
                session,
                file_row,
                scheduled_by=str(task_payload.get("scheduled_by") or "bulk_reprocess"),
            )
            if task_id is None:
                skipped += 1
            elif existing is None:
                tasks_created += 1
            else:
                tasks_reused += 1

        files_matched += len(page_file_ids)
        pages_completed += 1
        cursor = str(page_file_ids[-1])
        checkpoint = {
            "last_file_id": cursor,
            "files_matched": files_matched,
            "tasks_created": tasks_created,
            "tasks_reused": tasks_reused,
            "skipped": skipped,
            "pages_completed": pages_completed,
        }
        dispatcher = await session.get(Task, dispatcher_task_id)
        if dispatcher is None:
            raise ValueError(f"dispatcher task {dispatcher_task_id!r} not found")
        dispatcher.payload = {
            **task_payload,
            "checkpoint": checkpoint,
            "page_size": page_size,
        }
        await session.commit()
        if not has_more:
            break

    await audit_events_repo.append(
        session,
        kind="bulk_reprocess_completed",
        task_id=dispatcher_task_id,
        payload={
            **checkpoint,
            "dispatcher_task_id": dispatcher_task_id,
            "page_size": page_size,
            "status_filter": task_payload.get("status"),
        },
    )
    await session.commit()
