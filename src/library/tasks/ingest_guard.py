"""Context-local cancellation checkpoints for an ingest delivery and its child calls."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from library.db.models import File, Task
from library.db.session import session_scope
from library.repositories import tasks as tasks_repo


class IngestCancelled(asyncio.CancelledError):
    """A deleted source or stale delivery is not a retryable parsing failure."""


@dataclass(frozen=True)
class IngestDelivery:
    file_id: str
    task_id: str | None
    owner_id: str | None


_delivery: ContextVar[IngestDelivery | None] = ContextVar("ingest_delivery", default=None)


async def check_ingest_delivery(session: AsyncSession | None = None) -> None:
    delivery = _delivery.get()
    if delivery is None:
        return
    if session is None:
        async with session_scope() as db:
            await check_ingest_delivery(db)
        return
    live = await session.scalar(select(tasks_repo.live_ingest_file_ids().where(
        File.id == delivery.file_id,
    ).exists()))
    if not live:
        raise IngestCancelled("no_live_entry")
    if delivery.task_id is not None:
        owned = await session.scalar(select(Task.id).where(
            Task.id == delivery.task_id, Task.status == "running",
            Task.locked_by == delivery.owner_id,
        ).with_for_update())
        if owned is None:
            raise IngestCancelled("ingest delivery ownership lost")


@asynccontextmanager
async def ingest_publication_fence():
    """Keep deletion serialized until synchronous index publication finishes."""
    delivery = _delivery.get()
    if delivery is None:
        yield
        return
    async with session_scope() as db:
        await tasks_repo.lock_ingest_file(db, delivery.file_id)
        await check_ingest_delivery(db)
        yield


def guard_ingest_delivery(handler):
    @wraps(handler)
    async def guarded(payload):
        token = _delivery.set(IngestDelivery(
            file_id=str(payload.get("file_id") or ""),
            task_id=payload.get("_task_id"),
            owner_id=payload.get("_task_owner"),
        ))
        try:
            return await handler(payload)
        finally:
            _delivery.reset(token)
    return guarded
