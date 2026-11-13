from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from library.config import Settings
from library.db.models import Base, File, FileEntry, Folder, Task, TaskOutcome
from library.model_rate_limit import acquire_model_call_slot
from library.pipelines.base import PipelineResult
from library.repositories import tasks as tasks_repo
from library.services.reprocess import bulk_reprocess_file_ids_statement, reprocess_file
from library.tasks import ingest_guard, runner as runner_module
from library.tasks.handlers import ingest_file as ingest_module
from library.tasks.ingest_guard import IngestCancelled
from library.tasks.runner import TaskRunner
from library.services.entries import soft_delete_entry
from library.services.folders import soft_delete_folder
from library.tasks.enqueue import enqueue


@pytest_asyncio.fixture
async def sessions(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'cancel.db'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    await engine.dispose()


async def seed(sessions, *, shared=False, running=False, folder=False):
    now = datetime.now(timezone.utc)
    async with sessions() as db:
        if folder:
            db.add(Folder(id="folder", name="docs", created_at=now, updated_at=now))
        db.add(File(
            id="file", storage_key="original.txt", sha256="a" * 64, size_bytes=1,
            ingest_status="processing" if running else "pending",
            created_at=now, updated_at=now,
        ))
        await db.flush()
        db.add(FileEntry(
            id="entry", file_id="file", folder_id="folder" if folder else None,
            display_name="original.txt", lifecycle="active", created_at=now, updated_at=now,
        ))
        if shared:
            db.add(FileEntry(
                id="shared", file_id="file", display_name="copy.txt", lifecycle="active",
                created_at=now, updated_at=now,
            ))
        await db.flush()
        task = await enqueue(db, kind="ingest_file", payload={"file_id": "file"})
        if running:
            task.status = "running"
            task.locked_by = "owner"
            task.attempts = 1
        await db.commit()
        return task.id


@pytest.mark.parametrize("running", [False, True])
@pytest.mark.parametrize("folder", [False, True])
async def test_deleting_last_entry_cancels_ingest_atomically(sessions, running, folder):
    task_id = await seed(sessions, running=running, folder=folder)
    async with sessions() as db:
        if folder:
            await soft_delete_folder(db, folder_id="folder")
        else:
            await soft_delete_entry(db, entry_id="entry")
        await db.commit()
    async with sessions() as db:
        task = await db.get(Task, task_id)
        assert task.status == "cancelled"
        assert task.locked_by is None and task.lease_expires_at is None
        assert task.finished_at is not None
        entry = await db.get(FileEntry, "entry")
        assert entry.purge_after > entry.deleted_at
        file = await db.get(File, "file")
        assert file.deleted_at is None and file.storage_key == "original.txt"
        assert file.ingest_status != "failed"


async def test_deleting_one_shared_entry_keeps_task(sessions):
    task_id = await seed(sessions, shared=True)
    async with sessions() as db:
        await soft_delete_entry(db, entry_id="entry")
        await db.commit()
    async with sessions() as db:
        assert (await db.get(Task, task_id)).status == "pending"
        await soft_delete_entry(db, entry_id="shared")
        await db.commit()
    async with sessions() as db:
        assert (await db.get(Task, task_id)).status == "cancelled"


async def test_delete_rollback_keeps_entry_and_task(sessions):
    task_id = await seed(sessions)
    async with sessions() as db:
        await soft_delete_entry(db, entry_id="entry")
        await db.rollback()
    async with sessions() as db:
        assert (await db.get(FileEntry, "entry")).deleted_at is None
        assert (await db.get(Task, task_id)).status == "pending"


def bind_sessions(monkeypatch, sessions):
    @asynccontextmanager
    async def scope():
        async with sessions() as db:
            yield db
    for module in (ingest_module, ingest_guard, runner_module):
        monkeypatch.setattr(module, "session_scope", scope)
    return scope


def result():
    return PipelineResult(
        summary="synthetic", description={"sections": []}, kind="text", extra=None,
        entry_extra=None, entry_catalog_path=[], entry_tags=[],
    )


@pytest.mark.parametrize("restore", [False, True])
async def test_deleted_delivery_cannot_call_model_or_persist_after_restore(
    sessions, monkeypatch, restore,
):
    task_id = await seed(sessions, running=True)
    bind_sessions(monkeypatch, sessions)
    async with sessions() as db:
        await soft_delete_entry(db, entry_id="entry")
        await db.commit()
    if restore:
        async with sessions() as db:
            entry = await db.get(FileEntry, "entry")
            entry.deleted_at = entry.purge_after = None
            await db.flush()
            fresh = await enqueue(db, kind="ingest_file", payload={"file_id": "file"})
            await db.commit()
            assert fresh.id != task_id

    @ingest_guard.guard_ingest_delivery
    async def stale_delivery(_payload):
        with pytest.raises(IngestCancelled):
            await acquire_model_call_slot(
                kind="test", provider="synthetic", base_url=None, model="fake", tps=100,
            )
        async with sessions() as db:
            with pytest.raises(IngestCancelled):
                await ingest_module._persist(db, file_id="file", entry_id="entry", result=result())
        with pytest.raises(IngestCancelled):
            async with ingest_guard.ingest_publication_fence():
                pytest.fail("deleted delivery must not publish index")

    await stale_delivery({"file_id": "file", "_task_id": task_id, "_task_owner": "owner"})
    async with sessions() as db:
        assert (await db.get(File, "file")).summary is None


async def test_no_live_entry_is_not_requeued_by_enqueue_reprocess_or_bulk(sessions):
    await seed(sessions)
    async with sessions() as db:
        await soft_delete_entry(db, entry_id="entry")
        await db.commit()
    async with sessions() as db:
        assert await enqueue(db, kind="ingest_file", payload={"file_id": "file"}) is None
        file = await db.get(File, "file")
        assert await reprocess_file(db, file) is None
        assert not list((await db.scalars(bulk_reprocess_file_ids_statement(payload={}))).all())
        assert await tasks_repo.claim_pending_ids(
            db, now=datetime.now(timezone.utc), limit=10,
        ) == []


@pytest.mark.parametrize("shared", [False, True])
async def test_final_persistence_reselects_live_shared_entry_or_discards(sessions, shared):
    await seed(sessions, shared=shared)
    async with sessions() as db:
        await soft_delete_entry(db, entry_id="entry")
        await db.commit()
    async with sessions() as db:
        if shared:
            await ingest_module._persist(db, file_id="file", entry_id="entry", result=result())
            await db.commit()
        else:
            with pytest.raises(IngestCancelled):
                await ingest_module._persist(db, file_id="file", entry_id="entry", result=result())
    async with sessions() as db:
        assert (await db.get(File, "file")).summary == ("synthetic" if shared else None)


@pytest.mark.parametrize("heartbeat", [False, True])
async def test_running_pipeline_stops_without_failure_outcome(sessions, monkeypatch, heartbeat):
    task_id = await seed(sessions, running=True)
    bind_sessions(monkeypatch, sessions)
    entered, release, stopped = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = []

    class Pipeline:
        async def run(self, **_kwargs):
            entered.set()
            try:
                await release.wait()
                await acquire_model_call_slot(
                    kind="test", provider="synthetic", base_url=None, model="fake", tps=100,
                )
                calls.append("model")
                return result()
            finally:
                stopped.set()

    async def context(*_args, **_kwargs):
        return None

    async def refresh(_file_id):
        calls.append("embedding")

    monkeypatch.setattr(ingest_module, "get_storage", lambda: object())
    monkeypatch.setattr(ingest_module, "resolve_pipeline", lambda *_a, **_kw: Pipeline())
    monkeypatch.setattr(ingest_module, "_build_context", context)
    monkeypatch.setattr(ingest_module, "_refresh_semantic_index", refresh)
    runner = TaskRunner(Settings(llm_default_api_key="test", worker_heartbeat_seconds=1))
    monkeypatch.setattr(runner, "_has_llm_key", lambda: True)
    process = asyncio.create_task(runner._process(task_id, "owner"))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        async with sessions() as db:
            await soft_delete_entry(db, entry_id="entry")
            await db.commit()
        if not heartbeat:
            release.set()
        await asyncio.wait_for(process, 4)
        assert stopped.is_set() and calls == []
        async with sessions() as db:
            task = await db.get(Task, task_id)
            assert task.status == "cancelled" and task.locked_by is None
            assert (await db.get(File, "file")).summary is None
            assert (await db.get(File, "file")).ingest_status != "failed"
            assert await db.scalar(select(func.count()).select_from(TaskOutcome)) == 0
    finally:
        if not process.done():
            process.cancel()
            await asyncio.gather(process, return_exceptions=True)


async def test_cancelled_row_cannot_retry_finish_or_recover_and_metrics_ignore_it(sessions):
    from library.api.routes_tasks import _task_throughput_payload
    task_id = await seed(sessions, running=True)
    now = datetime.now(timezone.utc)
    async with sessions() as db:
        await soft_delete_entry(db, entry_id="entry")
        await db.commit()
    async with sessions() as db:
        assert not await tasks_repo.mark_done(db, task_id=task_id, now=now, worker_id="owner")
        assert not await tasks_repo.reschedule_for_retry(
            db, task_id=task_id, error="late", next_run_at=now, worker_id="owner",
        )
        assert not await tasks_repo.revive_running_to_pending(
            db, task_id=task_id, now=now, previous_worker_id="owner",
        )
        rows = await tasks_repo.list_recent_with_usage(db, limit=10)
        assert rows[0]["status"] == "cancelled" and rows[0]["detail"] == {}
        task = await db.get(Task, task_id)
        payload = _task_throughput_payload(active=[], terminal=[task], now=now, window_minutes=60)
        assert payload["completed"]["done"] == payload["completed"]["failed"] == 0
        assert payload["by_kind"][0]["average_duration_seconds"] is None


def test_migration_keeps_history_and_cancels_legacy_orphans(tmp_path):
    from library.db.bootstrap import _ensure_cancelled_tasks
    engine = create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as db:
            # Mimic the old task CHECK, including all real columns and indexes.
            from alembic.migration import MigrationContext
            from alembic.operations import Operations
            op = Operations(MigrationContext.configure(db))
            with op.batch_alter_table("tasks") as batch:
                batch.drop_constraint(op.f("ck_tasks_status"), type_="check")
                batch.create_check_constraint(
                    op.f("ck_tasks_status"), "status IN ('pending','running','done','dead')",
                )
            now = datetime.now(timezone.utc)
            db.execute(File.__table__.insert(), {
                "id": "file", "storage_key": "keep.txt", "sha256": "a" * 64,
                "size_bytes": 1, "ingest_status": "pending", "created_at": now, "updated_at": now,
            })
            for status in ("pending", "running", "done", "dead"):
                db.execute(Task.__table__.insert(), {
                    "id": status, "kind": "ingest_file", "payload": {"file_id": "file"},
                    "status": status, "scheduled_at": now, "created_at": now,
                })
            _ensure_cancelled_tasks(db)
            _ensure_cancelled_tasks(db)
            rows = dict(db.execute(select(Task.id, Task.status)).all())
            assert rows == {"pending": "cancelled", "running": "cancelled", "done": "done", "dead": "dead"}
            assert db.scalar(select(File.storage_key)) == "keep.txt"
            assert not db.execute(text("PRAGMA foreign_key_check")).all()
    finally:
        engine.dispose()


async def test_queued_ingest_never_reads_storage_after_delete(sessions, monkeypatch):
    task_id = await seed(sessions, running=True)
    bind_sessions(monkeypatch, sessions)
    async with sessions() as db:
        await soft_delete_entry(db, entry_id="entry")
        await db.commit()

    def forbidden_pipeline(*_args, **_kwargs):
        pytest.fail("deleted task must not resolve a pipeline or read the original")

    monkeypatch.setattr(ingest_module, "get_storage", lambda: object())
    monkeypatch.setattr(ingest_module, "resolve_pipeline", forbidden_pipeline)
    with pytest.raises(IngestCancelled):
        await ingest_module.handle_ingest_file({
            "file_id": "file", "_task_id": task_id, "_task_owner": "owner",
        })


@pytest.mark.parametrize("delete_first", [False, True])
async def test_concurrent_delete_and_persist_are_serialized(sessions, delete_first):
    await seed(sessions, running=True)
    locked = asyncio.Event()

    async def first():
        async with sessions() as db:
            if delete_first:
                await soft_delete_entry(db, entry_id="entry")
            else:
                await ingest_module._persist(db, file_id="file", entry_id="entry", result=result())
            locked.set()
            await asyncio.sleep(0.02)
            await db.commit()

    async def second():
        await locked.wait()
        async with sessions() as db:
            if delete_first:
                with pytest.raises(IngestCancelled):
                    await ingest_module._persist(db, file_id="file", entry_id="entry", result=result())
            else:
                await soft_delete_entry(db, entry_id="entry")
                await db.commit()

    await asyncio.wait_for(asyncio.gather(first(), second()), 5)
    async with sessions() as db:
        assert (await db.get(FileEntry, "entry")).deleted_at is not None
        assert (await db.get(File, "file")).summary == (None if delete_first else "synthetic")
        assert (await db.scalar(select(Task.status))) == "cancelled"


async def test_sdk_internal_retry_is_blocked_after_deletion(sessions, monkeypatch):
    import httpx
    from openai import AsyncOpenAI
    from library.provider_clients import _check_ingest_request

    task_id = await seed(sessions, running=True)
    bind_sessions(monkeypatch, sessions)
    requests = []

    async def transport(request):
        requests.append(request)
        async with sessions() as db:
            await soft_delete_entry(db, entry_id="entry")
            await db.commit()
        return httpx.Response(500, json={"error": {"message": "synthetic transient failure"}})

    @ingest_guard.guard_ingest_delivery
    async def call(_payload):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(transport),
            event_hooks={"request": [_check_ingest_request]},
        ) as http:
            sdk = AsyncOpenAI(api_key="test", base_url="https://synthetic.invalid/v1", http_client=http)
            with pytest.raises(IngestCancelled):
                await sdk.chat.completions.create(model="fake", messages=[{"role": "user", "content": "test"}])

    await call({"file_id": "file", "_task_id": task_id, "_task_owner": "owner"})
    assert len(requests) == 1


async def test_folder_delete_keeps_shared_file_outside_subtree(sessions):
    task_id = await seed(sessions, folder=True, shared=True)
    async with sessions() as db:
        await soft_delete_folder(db, folder_id="folder")
        await db.commit()
    async with sessions() as db:
        assert (await db.get(Task, task_id)).status == "pending"
        assert (await db.get(FileEntry, "shared")).deleted_at is None


async def test_worker_start_cancels_legacy_orphan_before_no_key_sweep(sessions, monkeypatch):
    task_id = await seed(sessions)
    bind_sessions(monkeypatch, sessions)
    async with sessions() as db:
        # Legacy delete bypassed service coordination.
        entry = await db.get(FileEntry, "entry")
        entry.deleted_at = datetime.now(timezone.utc)
        await db.commit()
    runner = TaskRunner(Settings(llm_default_api_key=""))
    monkeypatch.setattr(runner, "_has_llm_key", lambda: False)
    await runner._sweep_llm_dependent_if_no_key()
    async with sessions() as db:
        assert (await db.get(Task, task_id)).status == "cancelled"
        assert (await db.get(File, "file")).ingest_status != "failed"
