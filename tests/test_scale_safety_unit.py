from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.responses import JSONResponse

from library.capacity import CapacityExceeded, enforce_upload_capacity
from library.config import Settings


def test_section_embedding_limit_supports_zero_small_and_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from library.semantic import index

    description = {
        "sections": [
            {"id": f"s{i}", "title": f"Topic {i}", "summary": f"Evidence {i}."}
            for i in range(5)
        ]
    }
    assert index._section_embedding_inputs(  # noqa: SLF001
        description, summary="Document", max_sections=0,
    ) == []
    assert len(index._section_embedding_inputs(  # noqa: SLF001
        description, summary="Document", max_sections=2,
    )) == 2
    monkeypatch.setattr(
        index,
        "get_settings",
        lambda: SimpleNamespace(section_embedding_max_sections=3),
    )
    assert len(index._section_embedding_inputs(  # noqa: SLF001
        description, summary="Document",
    )) == 3


def test_pagination_cursor_is_opaque_strict_and_keeps_tie_breaker() -> None:
    from datetime import datetime, timezone

    from fastapi import HTTPException

    from library.api.pagination import decode_desc_cursor, encode_desc_cursor

    timestamp = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    cursor = encode_desc_cursor(timestamp, "row-b")
    decoded_time, decoded_id = decode_desc_cursor(cursor)
    assert decoded_time == timestamp
    assert decoded_id == "row-b"
    with pytest.raises(HTTPException) as exc_info:
        decode_desc_cursor("not a cursor!")
    assert exc_info.value.status_code == 422


@pytest.mark.asyncio
async def test_session_keyset_pages_do_not_skip_equal_timestamps() -> None:
    from datetime import datetime, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from library.api.pagination import decode_desc_cursor, encode_desc_cursor
    from library.db.models import Base, Session
    from library.repositories import sessions

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    timestamp = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as db:
            for row_id in ("row-a", "row-b", "row-c"):
                db.add(Session(
                    id=row_id,
                    started_at=timestamp,
                    initiating_user_message=row_id,
                    turn_count=0,
                    total_input_tokens=0,
                    total_output_tokens=0,
                    total_cache_read=0,
                    total_tool_calls=0,
                    total_llm_calls=0,
                    total_duration_ms=0,
                ))
            await db.commit()
        async with factory() as db:
            first = await sessions.list_sessions(db, limit=2)
            cursor = decode_desc_cursor(
                encode_desc_cursor(first[-1].started_at, first[-1].id)
            )
            second = await sessions.list_sessions(db, limit=2, before=cursor)
        ids = [row.id for row in first + second]
        assert ids == ["row-c", "row-b", "row-a"]
        assert len(ids) == len(set(ids))
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_task_claim_exclusion_and_terminal_pruning_are_bounded() -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from library.db.models import Base, Task
    from library.repositories import tasks

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)

    def row(
        row_id: str,
        *,
        kind: str,
        status: str,
        finished_at=None,  # noqa: ANN001
        priority: int = 100,
    ) -> Task:
        return Task(
            id=row_id,
            kind=kind,
            payload={},
            status=status,
            priority=priority,
            attempts=0,
            max_attempts=5,
            scheduled_at=now - timedelta(days=60),
            created_at=now - timedelta(days=60),
            finished_at=finished_at,
        )

    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add_all([
                row("periodic", kind="periodic_tick", status="pending", priority=1),
                row("ordinary", kind="ingest_file", status="pending", priority=10),
                row(
                    "old-done",
                    kind="ingest_file",
                    status="done",
                    finished_at=now - timedelta(days=45),
                ),
                row(
                    "old-dead",
                    kind="ingest_file",
                    status="dead",
                    finished_at=now - timedelta(days=40),
                ),
                row(
                    "recent-done",
                    kind="ingest_file",
                    status="done",
                    finished_at=now - timedelta(days=2),
                ),
            ])
            await db.commit()
        async with factory() as db:
            claimed = await tasks.claim_pending_ids(
                db,
                now=now,
                limit=10,
                exclude_kinds=("periodic_tick",),
            )
            assert claimed == ["ordinary"]
            assert await tasks.delete_terminal_batch_before(
                db,
                cutoff=now - timedelta(days=30),
                limit=1,
            ) == 1
            await db.commit()
        async with factory() as db:
            assert await db.get(Task, "old-done") is None
            assert await db.get(Task, "old-dead") is not None
            assert await db.get(Task, "recent-done") is not None
            assert await db.get(Task, "periodic") is not None
            assert await db.get(Task, "ordinary") is not None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_agent_event_retention_deletes_only_one_oldest_batch() -> None:
    from datetime import datetime, timedelta, timezone

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from library.db.models import AgentEvent, Base, Conversation, Session
    from library.repositories import agent_events

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with factory() as db:
            db.add(Session(
                id="session",
                started_at=now,
                initiating_user_message="question",
                turn_count=1,
                total_input_tokens=0,
                total_output_tokens=0,
                total_cache_read=0,
                total_tool_calls=0,
                total_llm_calls=0,
                total_duration_ms=0,
            ))
            db.add(Conversation(
                id="conversation",
                session_id="session",
                turn_index=0,
                started_at=now,
                ended_at=now,
                user_message="question",
                agent_response="answer",
                tool_calls=[],
                llm_calls=[],
                total_input_tokens=0,
                total_output_tokens=0,
                total_cache_read=0,
                total_tool_calls=0,
                total_llm_calls=0,
                total_duration_ms=0,
            ))
            await db.flush()
            db.add_all([
                AgentEvent(
                    id="old-1",
                    conversation_id="conversation",
                    cursor=1,
                    event="thinking",
                    data="{}",
                    created_at=now - timedelta(days=60),
                ),
                AgentEvent(
                    id="old-2",
                    conversation_id="conversation",
                    cursor=2,
                    event="answer",
                    data="answer",
                    created_at=now - timedelta(days=45),
                ),
                AgentEvent(
                    id="recent",
                    conversation_id="conversation",
                    cursor=3,
                    event="done",
                    data="{}",
                    created_at=now - timedelta(days=2),
                ),
            ])
            await db.commit()
        async with factory() as db:
            assert await agent_events.delete_batch_before(
                db,
                cutoff=now - timedelta(days=30),
                limit=1,
            ) == 1
            await db.commit()
        async with factory() as db:
            assert await db.get(AgentEvent, "old-1") is None
            assert await db.get(AgentEvent, "old-2") is not None
            assert await db.get(AgentEvent, "recent") is not None
    finally:
        await engine.dispose()


def test_postgres_transaction_pool_disables_statement_cache_with_unique_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from library.db import engine

    captured: dict[str, object] = {}
    sentinel = object()

    def fake_create(url: str, **kwargs):  # noqa: ANN003, ANN202
        captured["url"] = url
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(engine, "create_async_engine", fake_create)
    result = engine._build_engine(Settings(  # noqa: SLF001
        db_backend="postgres",
        postgres_prepared_statement_cache_size=0,
    ))
    assert result is sentinel
    connect_args = captured["connect_args"]
    assert isinstance(connect_args, dict)
    assert connect_args["prepared_statement_cache_size"] == 0
    name_func = connect_args["prepared_statement_name_func"]
    first = name_func()
    second = name_func()
    assert first.startswith("__asyncpg_")
    assert first != second


@pytest.mark.asyncio
async def test_runtime_schema_bootstrap_can_be_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from library.db import bootstrap

    monkeypatch.setattr(
        bootstrap,
        "get_settings",
        lambda: Settings(runtime_schema_bootstrap_enabled=False),
    )

    def unexpected_engine():  # noqa: ANN202
        raise AssertionError("disabled startup bootstrap must not open the database")

    monkeypatch.setattr(bootstrap, "get_engine", unexpected_engine)
    await bootstrap.bootstrap_schema()


def test_db_prepare_resolves_explicit_migration_assets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:  # noqa: ANN001
    from library.db.cli import _alembic_configuration

    migrations = tmp_path / "migrations"
    migrations.mkdir()
    config_path = tmp_path / "managed.ini"
    config_path.write_text(
        "[alembic]\nscript_location = migrations\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ALEMBIC_CONFIG", str(config_path))
    configuration = _alembic_configuration()
    assert configuration.config_file_name == str(config_path.resolve())
    assert configuration.get_main_option("script_location") == str(
        migrations.resolve()
    )


class _CountResult:
    def __init__(self, file_count: int, storage_bytes: int) -> None:
        self._row = (file_count, storage_bytes)

    def one(self) -> tuple[int, int]:
        return self._row


class _CapacityDB:
    def __init__(self, *, file_count: int, storage_bytes: int, backlog: int = 0) -> None:
        self.file_count = file_count
        self.storage_bytes = storage_bytes
        self.backlog = backlog

    async def execute(self, _statement):  # noqa: ANN001, ANN202
        return _CountResult(self.file_count, self.storage_bytes)

    async def scalar(self, _statement):  # noqa: ANN001, ANN202
        return self.backlog


@pytest.mark.asyncio
async def test_upload_capacity_is_opt_in_and_reports_429() -> None:
    db = _CapacityDB(file_count=2, storage_bytes=90)
    await enforce_upload_capacity(
        db,  # type: ignore[arg-type]
        incoming_bytes=20,
        settings=Settings(),
    )
    with pytest.raises(CapacityExceeded) as exc_info:
        await enforce_upload_capacity(
            db,  # type: ignore[arg-type]
            incoming_bytes=20,
            settings=Settings(library_storage_bytes_limit=100),
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "5"}
    assert exc_info.value.detail["resource"] == "storage_bytes"


@pytest.mark.asyncio
async def test_chat_capacity_is_rejected_before_stream_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from library.api import routes_chat

    class ChatDB:
        async def get(self, _model, _row_id):  # noqa: ANN001, ANN202
            return SimpleNamespace(deleted_at=None, ended_at=None)

        async def scalar(self, _statement):  # noqa: ANN001, ANN202
            return 1

        def get_bind(self):  # noqa: ANN201
            return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    monkeypatch.setattr(
        routes_chat,
        "get_settings",
        lambda: Settings(chat_concurrency_limit=1),
    )
    with pytest.raises(CapacityExceeded) as exc_info:
        await routes_chat.post_chat(
            "session",
            routes_chat.ChatBody(query="hello"),
            db=ChatDB(),  # type: ignore[arg-type]
        )
    assert exc_info.value.status_code == 429
    assert exc_info.value.headers["Retry-After"] == "5"


@pytest.mark.asyncio
async def test_chat_background_turn_does_not_depend_on_stream_consumption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from library.api import routes_chat

    completed = asyncio.Event()

    class ChatDB:
        async def get(self, _model, _row_id):  # noqa: ANN001, ANN202
            return SimpleNamespace(deleted_at=None, ended_at=None)

    async def fake_durable_turn(**_kwargs) -> None:  # noqa: ANN003
        completed.set()

    monkeypatch.setattr(routes_chat, "_run_durable_turn", fake_durable_turn)
    monkeypatch.setattr(routes_chat, "get_settings", lambda: Settings())
    response = await routes_chat.post_chat(
        "session",
        routes_chat.ChatBody(query="continue in background"),
        db=ChatDB(),  # type: ignore[arg-type]
    )
    assert response.media_type == "text/event-stream"
    await asyncio.wait_for(completed.wait(), timeout=1)


@pytest.mark.asyncio
async def test_readiness_returns_503_when_a_dependency_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from library import main

    async def database_failure() -> None:
        raise RuntimeError("database unavailable")

    class Storage:
        async def check_ready(self) -> None:
            return None

    monkeypatch.setattr(main, "_database_ready", database_failure)
    monkeypatch.setattr(main, "get_storage", lambda: Storage())
    monkeypatch.setattr(
        main,
        "get_settings",
        lambda: SimpleNamespace(readiness_timeout_seconds=0.1),
    )
    response = await main.ready()
    assert isinstance(response, JSONResponse)
    assert response.status_code == 503
    assert json.loads(response.body)["checks"] == {
        "database": "error",
        "storage": "ok",
    }


@pytest.mark.asyncio
async def test_readiness_timeout_is_bounded() -> None:
    import asyncio

    from library import main

    assert await main._bounded_readiness(  # noqa: SLF001
        asyncio.Event().wait(),
        timeout_seconds=0.001,
    ) == "error"


@pytest.mark.asyncio
async def test_scheduler_disabled_skips_bootstrap_but_starts_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from library.tasks.handlers import periodic_tick
    from library.tasks.runner import TaskRunner

    calls = {"bootstrap": 0, "run": 0}

    async def fake_bootstrap() -> None:
        calls["bootstrap"] += 1

    async def fake_sweep(self) -> None:  # noqa: ANN001
        return None

    async def fake_run(self) -> None:  # noqa: ANN001
        calls["run"] += 1

    monkeypatch.setattr(periodic_tick, "bootstrap_periodic_tick", fake_bootstrap)
    monkeypatch.setattr(TaskRunner, "_sweep_llm_dependent_if_no_key", fake_sweep)
    monkeypatch.setattr(TaskRunner, "_run", fake_run)
    runner = TaskRunner(Settings(worker_scheduler_enabled=False))
    await runner.start()
    assert runner._loop_task is not None  # noqa: SLF001
    await runner._loop_task  # noqa: SLF001
    assert calls == {"bootstrap": 0, "run": 1}


def test_public_tool_call_id_is_stable_and_provider_independent() -> None:
    from library.agent.runtime import _public_tool_call_id

    assert _public_tool_call_id(turn=0, tool_index=0) == "turn-1-tool-1"
    assert _public_tool_call_id(turn=3, tool_index=2) == "turn-4-tool-3"


@pytest.mark.asyncio
async def test_tool_events_use_public_id_while_model_pairing_keeps_provider_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from library.agent import runtime
    from library.agent.tools import ToolContext
    from library.llm import ToolCall

    persisted: list[dict[str, object]] = []

    async def fake_run_tool(_registration, _ctx, _tool_call):  # noqa: ANN001, ANN202
        return 1, {"value": 1}, None

    async def fake_persist(**kwargs) -> None:  # noqa: ANN003
        persisted.append(kwargs)

    monkeypatch.setattr(runtime, "get_tool", lambda _name: object())
    monkeypatch.setattr(runtime, "_run_tool", fake_run_tool)
    monkeypatch.setattr(runtime, "_persist_tool_call", fake_persist)
    monkeypatch.setattr(
        runtime,
        "get_settings",
        lambda: SimpleNamespace(agent_max_parallel_tool_calls=1),
    )
    result_blocks = []
    events = [
        event
        async for event in runtime._dispatch_tool_calls(  # noqa: SLF001
            tool_calls=[ToolCall(
                id="provider-call-9",
                name="bounded_unit_tool",
                arguments={"value": 1},
            )],
            ctx=ToolContext(session_id="s", conversation_id="c"),
            conversation_id="c",
            result_blocks=result_blocks,
            guard=runtime._CallGuard(),  # noqa: SLF001
            turn=2,
        )
    ]
    payloads = [json.loads(event.data) for event in events]
    assert [payload["tool_call_id"] for payload in payloads] == [
        "turn-3-tool-1",
        "turn-3-tool-1",
    ]
    assert result_blocks[0].tool_call_id == "provider-call-9"
    assert persisted[0]["tool_call_id"] == "turn-3-tool-1"
    assert persisted[0]["tool_index"] == 0
    assert persisted[0]["turn"] == 2


@pytest.mark.asyncio
async def test_prune_batch_loop_honors_max_batches() -> None:
    from library.tasks.handlers.prune import _prune_in_batches

    calls = 0

    async def delete_batch() -> int:
        nonlocal calls
        calls += 1
        return 10

    deleted = await _prune_in_batches(
        delete_batch,
        batch_size=10,
        max_batches=3,
    )
    assert deleted == 30
    assert calls == 3


def test_migration_head_contains_durable_event_ledger() -> None:
    from library.db.bootstrap import ALEMBIC_HEAD_REVISION, SCALE_SAFETY_INDEXES

    assert ALEMBIC_HEAD_REVISION == "0017_cancelled_ingest_tasks"
    assert {name for name, _table, _columns in SCALE_SAFETY_INDEXES} == {
        "ix_files_capacity_active",
        "ix_conversations_active_started",
        "ix_sessions_deleted_started_id",
        "ix_tasks_status_finished_id",
        "ix_audit_events_occurred_id",
        "ix_task_outcomes_completed_id",
        "ix_agent_events_created_id",
    }
