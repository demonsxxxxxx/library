from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from library.db.bootstrap import bootstrap_schema_sync
from library.db.models import File, FileEntry, Task
from library.tasks.handlers import bulk_reprocess_files as module
from library.utils.ids import new_id


@pytest.mark.asyncio
async def test_bulk_reprocess_dispatcher_pages_and_checkpoints(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bulk.db'}")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    dispatcher_id = new_id()
    file_ids = [new_id() for _ in range(12)]
    called: list[str] = []

    async def fake_reprocess(session, file_row, *, scheduled_by):  # noqa: ANN001
        del session
        assert scheduled_by == "all"
        called.append(file_row.id)
        return new_id()

    monkeypatch.setattr(module, "reprocess_file", fake_reprocess)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(bootstrap_schema_sync)
        async with factory() as session:
            for index, file_id in enumerate(file_ids):
                session.add(File(
                    id=file_id,
                    storage_key=f"bulk/{index}",
                    sha256=f"{index:064x}",
                    size_bytes=1,
                    mime_type="text/plain",
                    original_ext=".txt",
                    kind="text",
                    summary="ready",
                    description={"sections": []},
                    extra="",
                    ingest_status="done",
                    ingested_at=now,
                    deleted_at=None,
                    created_at=now,
                    updated_at=now,
                ))
                session.add(FileEntry(
                    id=new_id(), file_id=file_id, display_name=f"{index}.txt",
                    lifecycle="active", created_at=now, updated_at=now,
                ))
            session.add(Task(
                id=dispatcher_id,
                kind="bulk_reprocess_files",
                payload={"dispatcher_task_id": dispatcher_id, "scheduled_by": "all"},
                dedup_key="bulk:test",
                status="running",
                priority=55,
                attempts=1,
                max_attempts=20,
                scheduled_at=now,
                created_at=now,
            ))
            await session.commit()

        async with factory() as session:
            await module._dispatch_bulk_reprocess_pages(
                session,
                task_payload={
                    "dispatcher_task_id": dispatcher_id,
                    "scheduled_by": "all",
                },
                page_size=10,
            )

        assert called == sorted(file_ids)
        async with factory() as session:
            dispatcher = await session.get(Task, dispatcher_id)
            assert dispatcher is not None
            checkpoint = dispatcher.payload["checkpoint"]
            assert checkpoint["files_matched"] == 12
            assert checkpoint["tasks_created"] == 12
            assert checkpoint["tasks_reused"] == 0
            assert checkpoint["pages_completed"] == 2
            assert checkpoint["last_file_id"] == sorted(file_ids)[-1]
    finally:
        await engine.dispose()
