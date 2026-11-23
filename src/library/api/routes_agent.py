"""Session HTTP routes — DESIGN.md §12.2.

  POST /sessions               — open a new session
  POST /sessions/{id}/close    — close a session, return totals
  GET  /sessions               — list sessions (chat sidebar)
  GET  /sessions/{id}/messages — replay turns for a session

Chat (per-turn agent execution) lives in routes_chat.py as
`POST /chat/{session_id}` with SSE streaming.

Sessions are server-managed containers; clients keep a session_id and
post chat turns into it; reflect_turn is enqueued per turn (in
agent.runtime), not on close.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from library.agent.cache_metrics import summarize_llm_calls
from library.agent.runtime import _public_plan_text, _rewrite_footnotes_for_display
from library.agent.runtime import _strip_session_name_line
from library.agent.runtime import TOOL_RESULT_PREVIEW_LEN
from library.agent import tool_display
from library.api.pagination import decode_desc_cursor, encode_desc_cursor
from library.config import get_settings
from library.db.models import Session as SessionRow, TaskOutcome
from library.db.session import get_session
from library.repositories import audit_events as audit_events_repo
from library.repositories import catalogs as catalogs_repo
from library.repositories import entries as entries_repo
from library.repositories import folders as folders_repo
from library.repositories import sessions as session_service
from library.repositories import tags as tags_repo
from library.services.attachments import (
    list_turn_attachments,
    read_attachment,
)

router = APIRouter(tags=["sessions"])


class CreateSessionBody(BaseModel):
    initiating_user_message: str | None = None


def _cache_metric_fields(llm_calls: list[dict[str, Any]]) -> dict[str, Any]:
    summary = summarize_llm_calls(llm_calls)
    settings = get_settings()
    return {
        "prompt_tokens": summary.prompt_tokens,
        "cache_read": summary.cache_read_tokens,
        "cache_creation": summary.cache_creation_tokens,
        "cache_eligible_prompt_tokens": summary.eligible_prompt_tokens,
        "cache_eligible_read_tokens": summary.eligible_read_tokens,
        "cache_eligible_estimated_tokens": summary.eligible_estimated_tokens,
        "cache_eligible_requests": summary.eligible_requests,
        "cache_prompt_coverage_ratio": summary.prompt_coverage_ratio,
        "cache_eligible_hit_ratio": summary.eligible_hit_ratio,
        "cache_eligible_reuse_ratio": summary.eligible_reuse_ratio,
        "prompt_prefix_breaks": summary.prefix_breaks,
        "cache_slo": summary.slo_payload(
            minimum_hit_ratio=settings.agent_cache_slo_min_hit_ratio,
            minimum_eligible_requests=(
                settings.agent_cache_slo_min_eligible_requests
            ),
        ),
    }


def _conversation_cache_metric_fields(conversations: list[Any]) -> dict[str, Any]:
    calls = [
        call
        for conversation in conversations
        for call in (conversation.llm_calls or [])
        if isinstance(call, dict)
    ]
    return _cache_metric_fields(calls)


def _session_totals_payload(session: Any, conversations: list[Any]) -> dict[str, Any]:
    return {
        "turn_count": session.turn_count,
        "input_tokens": session.total_input_tokens,
        "output_tokens": session.total_output_tokens,
        "tool_calls": session.total_tool_calls,
        "llm_calls": session.total_llm_calls,
        **_conversation_cache_metric_fields(conversations),
    }


@router.post("/sessions", status_code=201)
async def create_session(
    body: CreateSessionBody | None = None,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    init = (body.initiating_user_message if body else None) or ""
    s = await session_service.create_session(db, initiating_user_message=init)
    await db.commit()
    return {
        "session_id": s.id,
        "started_at": s.started_at.isoformat() if s.started_at else None,
    }


@router.post("/sessions/{session_id}/close", status_code=200)
async def close_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    s = await session_service.get_live(db, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")
    if s.ended_at is not None:
        conversations = await session_service.list_for_session(db, session_id)
        return {
            "session_id": s.id,
            "ended_at": s.ended_at.isoformat(),
            "end_reason": s.end_reason,
            "totals": _session_totals_payload(s, conversations),
        }
    closed = await session_service.close_session(
        db, session_id=session_id, end_reason="normal"
    )
    conversations = await session_service.list_for_session(db, session_id)
    await db.commit()
    return {
        "session_id": closed.id,
        "ended_at": closed.ended_at.isoformat() if closed.ended_at else None,
        "end_reason": closed.end_reason,
        "totals": _session_totals_payload(closed, conversations),
    }


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_session(
    session_id: str,
    db: AsyncSession = Depends(get_session),
) -> None:
    """Soft-delete a session — hides it from the sidebar but leaves
    `conversations` and `journal` rows intact. Journal is the agent's
    first-class memory across sessions; UI delete must not erase it.

    If the session has never been explicitly closed (`ended_at IS NULL`)
    we auto-close it as part of the delete: in practice the GUI never
    calls /close, so every session in the user's DB looks "active"
    forever otherwise. The trash icon clearly means "I am done with
    this", so closing on the user's behalf is what they want.
    """
    s = await db.get(SessionRow, session_id)
    if s is None or s.deleted_at is not None:
        raise HTTPException(status_code=404, detail="session not found")
    if s.ended_at is None:
        await session_service.close_session(
            db, session_id=session_id, end_reason="deleted"
        )
    await session_service.soft_delete(db, session_id)
    await audit_events_repo.append(
        db, kind="session_deleted", session_id=session_id, payload={},
    )
    await db.commit()


@router.get("/sessions")
async def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    cursor: str | None = Query(default=None),
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """List sessions for the chat sidebar, ordered most-recent first.

    Each row carries enough to render a clickable list entry:
    session-title preview, turn count, started_at / ended_at, and the close
    reason if any.
    """
    before = decode_desc_cursor(cursor) if cursor is not None else None
    fetched = await session_service.list_sessions(
        db,
        limit=limit + 1,
        offset=offset,
        before=before,
    )
    rows = fetched[:limit]
    next_cursor = (
        encode_desc_cursor(rows[-1].started_at, rows[-1].id)
        if len(fetched) > limit and rows
        else None
    )
    latest_modes = await session_service.latest_session_modes(
        db, [s.id for s in rows],
    )

    # Legacy sessions opened before write-side backfill have an empty
    # `initiating_user_message`. Fill those previews from the first
    # conversation's user_message in one batched query.
    needs_fallback = [
        s.id for s in rows if not (s.initiating_user_message or "").strip()
    ]
    fallbacks = await session_service.first_user_messages(db, needs_fallback)

    def _preview(s: SessionRow) -> str:
        text = (s.initiating_user_message or "").strip()
        if not text:
            text = (fallbacks.get(s.id) or "").strip()
        return text[:160]

    return {
        "sessions": [
            {
                "session_id": s.id,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                "end_reason": s.end_reason,
                "preview": _preview(s),
                "mode": latest_modes.get(s.id, "auto"),
                "turn_count": s.turn_count or 0,
                "total_input_tokens": s.total_input_tokens or 0,
                "total_output_tokens": s.total_output_tokens or 0,
                "total_tool_calls": s.total_tool_calls or 0,
            }
            for s in rows
        ],
        "limit": limit,
        "offset": offset,
        "next_cursor": next_cursor,
    }


@router.get("/conversations/{conversation_id}/attachments/{name}")
async def get_conversation_attachment(
    conversation_id: str,
    name: str,
) -> Response:
    """Serve one stored chat image for UI re-display.

    Returns the raw bytes with the correct Content-Type. 404 when the name
    fails strict validation (``^\\d+\\.(png|jpe?g|gif|webp)$``), attempts
    path traversal, or the file is absent. These files are UI-only — they
    are never fed back to the LLM (see services.attachments docstring).
    """
    result = read_attachment(conversation_id, name)
    if result is None:
        raise HTTPException(status_code=404, detail="attachment not found")
    data, media_type = result
    return Response(content=data, media_type=media_type)


@router.get("/sessions/{session_id}/messages")
async def session_messages(
    session_id: str,
    db: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Replay the transcript of a session for the GUI.

    Returns turns in turn_index order. Each turn carries the
    user_message, the final agent_response, and a denormalized list of
    tool_calls (name, arguments, optional preview, ok flag, duration).
    The GUI uses this to rebuild the same `Turn[]` shape it builds from
    a live SSE stream — without re-executing anything.

    `plan_text` is best-effort: planners record their internal plan text
    in `llm_calls[*]['extra']['plan_text']` when phase=='plan'. We surface
    cleaned display text if present.
    """
    s = await session_service.get_live(db, session_id)
    if s is None:
        raise HTTPException(status_code=404, detail="session not found")

    convs = await session_service.list_for_session_ordered(db, session_id)
    session_mode = "auto"
    for c in reversed(convs):
        mode = session_service.conversation_mode(c)
        if mode:
            session_mode = mode
            break

    error_by_conversation: dict[str, str] = {}
    conv_ids = [c.id for c in convs]
    if conv_ids:
        rows = (
            await db.execute(
                select(TaskOutcome.object_id, TaskOutcome.detail)
                .where(
                    TaskOutcome.task_kind == "run_turn",
                    TaskOutcome.object_kind == "conversation",
                    TaskOutcome.object_id.in_(conv_ids),
                    TaskOutcome.outcome == "error",
                )
                .order_by(TaskOutcome.completed_at.desc())
            )
        ).all()
        for conversation_id, detail in rows:
            if conversation_id in error_by_conversation:
                continue
            if isinstance(detail, dict):
                error = detail.get("error")
                if isinstance(error, str) and error.strip():
                    error_by_conversation[conversation_id] = error

    # Pre-resolve every id referenced by any tool_call in this session
    # so the replay payload mirrors the live SSE shape (each call gets a
    # ready-to-render `display` string). Four batched lookups, regardless
    # of turn count.
    all_eids: set[str] = set()
    all_tids: set[str] = set()
    all_fids: set[str] = set()
    all_cids: set[str] = set()
    for c in convs:
        for tc in c.tool_calls or []:
            if not isinstance(tc, dict):
                continue
            args = tc.get("arguments") or {}
            name = tc.get("name") or ""
            all_eids.update(tool_display.collect_entry_ids(name, args))
            all_tids.update(tool_display.collect_tag_ids(name, args))
            all_fids.update(tool_display.collect_folder_ids(name, args))
            all_cids.update(tool_display.collect_catalog_ids(name, args))

    entry_names: dict[str, str] = {}
    tag_names: dict[str, str] = {}
    folder_names: dict[str, str] = {}
    catalog_names: dict[str, str] = {}
    if all_eids:
        rows = await entries_repo.list_live_with_file_by_ids(db, list(all_eids))
        entry_names = {entry.id: entry.display_name for entry, _ in rows}
    if all_tids:
        tag_names = await tags_repo.name_by_ids(db, list(all_tids))
    if all_fids:
        folder_names = await folders_repo.name_by_ids(db, list(all_fids))
    if all_cids:
        catalog_names = await catalogs_repo.name_by_ids(db, list(all_cids))

    turns: list[dict[str, Any]] = []
    for c in convs:
        replay_error = error_by_conversation.get(c.id)
        if c.ended_at is None and replay_error is None:
            replay_error = (
                "This turn did not finish. It was likely interrupted before "
                "Library could persist a final response."
            )
        plan_text: str | None = None
        for call in c.llm_calls or []:
            if isinstance(call, dict) and call.get("phase") == "plan":
                pt = call.get("plan_text") or call.get("extra", {}).get("plan_text")
                if isinstance(pt, str) and pt.strip():
                    plan_text = _public_plan_text(_strip_session_name_line(pt))
                    break

        tool_calls = []
        for tc in c.tool_calls or []:
            if not isinstance(tc, dict):
                continue
            name = tc.get("name") or ""
            args = tc.get("arguments") or {}
            display = tool_display.format_tool_call(
                name, args,
                resolver=entry_names.get,
                tag_resolver=tag_names.get,
                folder_resolver=folder_names.get,
                catalog_resolver=catalog_names.get,
            )
            # Mirror the live SSE shape: a one-line preview of the tool's
            # result (truncated to TOOL_RESULT_PREVIEW_LEN), so replayed
            # transcripts show the same expandable result body the user
            # saw during the original turn.
            result = tc.get("result")
            error = tc.get("error")
            if error:
                preview: str | None = f"ERROR: {error}"
            elif result is not None:
                p = tool_display.format_tool_result_preview(name, result)
                if len(p) > TOOL_RESULT_PREVIEW_LEN:
                    p = p[:TOOL_RESULT_PREVIEW_LEN] + "..."
                preview = p
            else:
                preview = None
            tool_calls.append({
                "tool_call_id": tc.get("tool_call_id"),
                "tool_index": tc.get("tool_index"),
                "turn": tc.get("turn"),
                "name": name,
                "arguments": args,
                "display": display,
                "ok": tc.get("error") is None,
                "error": tc.get("error"),
                "duration_ms": tc.get("duration_ms"),
                "preview": preview,
            })

        # UI re-display of pasted images. Gate the directory scan cheaply on
        # the persisted placeholder marker (see runtime._persisted_user_message,
        # which writes "[image attached]" or "[N images attached]" — both end
        # in "attached]") so text-only turns cost nothing.
        attachments: list[dict[str, Any]] = []
        if "attached]" in (c.user_message or ""):
            attachments = list_turn_attachments(c.id)

        numeric_results = next((
            call["result"].get("numeric_results", [])
            for call in reversed(c.tool_calls or [])
            if isinstance(call, dict) and call.get("name") == "finish_research"
            and not call.get("error") and isinstance(call.get("result"), dict)
            and call["result"].get("ok") is True
        ), [])
        cache_metrics = _cache_metric_fields([
            call for call in (c.llm_calls or []) if isinstance(call, dict)
        ])

        turns.append({
            "conversation_id": c.id,
            "turn_index": c.turn_index,
            "mode": session_service.conversation_mode(c) or "auto",
            "started_at": c.started_at.isoformat() if c.started_at else None,
            "ended_at": c.ended_at.isoformat() if c.ended_at else None,
            "user_message": c.user_message,
            "attachments": attachments,
            "agent_response": (
                await _rewrite_footnotes_for_display(
                    c.agent_response,
                    locate_pdf_quotes=False,
                    resolve_pdf_page_labels=False,
                    numeric_results=numeric_results,
                )
                if c.agent_response else c.agent_response
            ),
            "error": replay_error,
            "plan_text": plan_text,
            "tool_calls": tool_calls,
            "metrics": {
                "tokens_in": c.total_input_tokens or 0,
                "tokens_out": c.total_output_tokens or 0,
                **cache_metrics,
                "tool_calls": c.total_tool_calls or 0,
                "llm_calls": c.total_llm_calls or 0,
                "duration_ms": c.total_duration_ms or 0,
            },
        })

    return {
        "session_id": s.id,
        "started_at": s.started_at.isoformat() if s.started_at else None,
        "ended_at": s.ended_at.isoformat() if s.ended_at else None,
        "end_reason": s.end_reason,
        "mode": session_mode,
        "metrics": {
            "tokens_in": sum(c.total_input_tokens or 0 for c in convs),
            "tokens_out": sum(c.total_output_tokens or 0 for c in convs),
            **_conversation_cache_metric_fields(convs),
            "tool_calls": sum(c.total_tool_calls or 0 for c in convs),
            "llm_calls": sum(c.total_llm_calls or 0 for c in convs),
            "duration_ms": sum(c.total_duration_ms or 0 for c in convs),
        },
        "turns": turns,
    }
