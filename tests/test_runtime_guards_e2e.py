"""Focused tests for the runtime guards added 2026-05-24.

Covers additions on top of the plan-execute loop:
  1. NO_PLAN fast-path — planner can skip execute by emitting `NO_PLAN: ...`
  2. tool-call dedup — repeat (name, args) returns prior result without
     re-running the handler
  3. doom-loop guard — same key crossing threshold within the rolling
     window appends a STOP nudge to the *current* tool message (no
     mutation of prior messages, so prefix cache stays valid)
  4. final-answer continuation — max_tokens fragments are buffered
     server-side and emitted as one answer event

Strategy: drive runtime.run_turn against a scripted fake chat client and a
scripted fake tool. Avoids real LLM/HTTP cost while exercising the actual
production code path.

Run:
    .venv/Scripts/python tests/test_runtime_guards_e2e.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

_TEST_PARENT = Path(os.environ.get("LIBRARY_TEST_TMP", Path(__file__).resolve().parent))
_TEST_ROOT = _TEST_PARENT / f"_runtime_guards_e2e_data_{os.getpid()}_{uuid4().hex[:8]}"
_TEST_ROOT.mkdir(parents=True)
os.environ["LIBRARY_HOME"] = str(_TEST_ROOT)
os.environ["STORAGE_BACKEND"] = "local"
os.environ["WORKER_ENABLED"] = "false"
os.environ["LLM_DEFAULT_API_KEY"] = "sk-fake"
os.environ["LLM_DEFAULT_MODEL"] = "fake-model"

from library.config import get_settings
get_settings.cache_clear()  # type: ignore[attr-defined]

from sqlalchemy import select

from library.db.engine import get_engine, get_session_factory
from library.db.models import Base, Conversation, File, FileEntry, Session
from library.llm.types import (
    ChatRequest, ChatResponse, TokenUsage, ToolCall,
)
from library.utils.ids import new_id
import library.agent.runtime as runtime
from library.agent import tools as tools_pkg
from library.agent.stable_context import PLAN_PHASE_PROMPT


def _stored_plan_text(conv: Conversation) -> str:
    first = conv.llm_calls[0]
    return first.get("plan_text") or first.get("extra", {}).get("plan_text") or ""


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_schema() -> None:
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def _open_session(initiating: str) -> str:
    factory = get_session_factory()
    sid = new_id()
    now = _now()
    async with factory() as s:
        s.add(Session(
            id=sid, started_at=now,
            initiating_user_message=initiating,
            turn_count=0,
            total_input_tokens=0, total_output_tokens=0,
            total_cache_read=0, total_tool_calls=0, total_llm_calls=0,
            total_duration_ms=0,
        ))
        await s.commit()
    return sid


async def _seed_citation_entry() -> str:
    factory = get_session_factory()
    now = _now()
    file_id = new_id()
    entry_id = new_id()
    async with factory() as s:
        s.add(File(
            id=file_id,
            storage_key=f"citation/{file_id}.txt",
            sha256=(file_id.replace("-", "") * 2)[:64],
            size_bytes=80,
            mime_type="text/plain",
            original_ext=".txt",
            kind="text",
            summary="Evidence about medieval merchants",
            description={"sections": []},
            extra=None,
            ingest_status="done",
            ingested_at=now,
            created_at=now,
            updated_at=now,
        ))
        s.add(FileEntry(
            id=entry_id,
            folder_id=None,
            file_id=file_id,
            display_name="evidence.txt",
            lifecycle="active",
            catalog_id=None,
            extra=None,
            created_at=now,
            updated_at=now,
        ))
        await s.commit()
    return entry_id


class _ScriptedChat:
    profile_name = "chat"
    model = "fake-chat"

    def __init__(self, responses: list[ChatResponse]) -> None:
        self._responses = responses
        self._i = 0
        self.requests: list[ChatRequest] = []

    async def complete(self, request: ChatRequest) -> ChatResponse:
        self.requests.append(request)
        if self._i >= len(self._responses):
            raise RuntimeError(
                f"fake LLM script exhausted at call #{self._i + 1}; "
                "loop should have stopped earlier"
            )
        r = self._responses[self._i]
        self._i += 1
        return r


def _install_chat(client) -> None:
    runtime.get_chat_client = lambda profile="chat": client  # type: ignore


# ---- fake tool that counts how many times its handler ran ------------------

class _CountingTool:
    """Drop-in replacement for a registered tool. Each .handler() call
    increments .call_count so the test can verify dedup actually skipped
    the handler dispatch."""

    def __init__(self, name: str = "echo_tool") -> None:
        self.name = name
        self.call_count = 0

    async def handler(self, db, ctx, arguments):
        self.call_count += 1
        return {"echo": arguments, "n": self.call_count}


class _CitingReadTool(_CountingTool):
    def __init__(self, entry_id: str) -> None:
        super().__init__(name="read_files")
        self.entry_id = entry_id

    async def handler(self, db, ctx, arguments):
        self.call_count += 1
        return {
            "ok": True,
            "results": [{
                "ok": True,
                "entry_id": self.entry_id,
                "display_name": "evidence.txt",
                "reads": [{
                    "ok": True,
                    "text": "Merchant classes expanded with organized trade routes in the eleventh century.",
                }],
            }],
        }


def _install_tool(
    tool: _CountingTool,
    *,
    explicit_finalization: bool = False,
) -> None:
    """Replace get_tool/all_tool_defs to return our scripted tool only."""
    fake_def = {
        "name": tool.name,
        "description": "test echo",
        "input_schema": {"type": "object", "properties": {}},
    }

    class _Reg:
        handler = tool.handler

    finish_registration = (
        tools_pkg.get_tool("finish_research") if explicit_finalization else None
    )
    finish_def = (
        {
            "name": finish_registration.name,
            "description": finish_registration.description,
            "input_schema": finish_registration.input_schema,
        }
        if finish_registration is not None
        else None
    )

    def fake_get_tool(name: str):
        if name == tool.name:
            return _Reg
        if finish_registration is not None and name == "finish_research":
            return finish_registration
        return None

    runtime.get_tool = fake_get_tool  # type: ignore
    runtime.all_tool_defs = lambda: [
        fake_def,
        *([finish_def] if finish_def is not None else []),
    ]  # type: ignore


# ---- collectors ------------------------------------------------------------

async def _drive(session_id: str, user_message: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    async for ev in runtime.run_turn(
        session_id=session_id, user_message=user_message,
    ):
        out.append((ev.event_type, ev.data))
    return out


# ---- 1. NO_PLAN fast-path --------------------------------------------------

async def test_no_plan_fast_path() -> None:
    sid = await _open_session("hi")
    chat = _ScriptedChat([
        ChatResponse(
            text="NO_PLAN: You're welcome; standing by.\nSession name: Quick thanks",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        # No second response scripted — if execute fires, the fake will
        # raise "script exhausted" and the test fails.
    ])
    _install_chat(chat)

    events = await _drive(sid, "谢谢")
    seq = [e[0] for e in events]
    assert "planning" in seq
    assert "plan" in seq
    plan = json.loads(next(d for ev, d in events if ev == "plan"))["text"]
    assert "Session name:" not in plan, plan
    # No execute phase: no `thinking` event.
    assert "thinking" not in seq, seq
    assert "tool_call" not in seq
    answer = next(d for ev, d in events if ev == "answer")
    assert "You're welcome" in answer, answer
    assert "Session name:" not in answer, answer
    done = next(d for ev, d in events if ev == "done")
    assert '"session_name": "Quick thanks"' in done, done
    factory = get_session_factory()
    async with factory() as s:
        row = await s.get(Session, sid)
        assert row and row.initiating_user_message == "Quick thanks"
        conv = (
            await s.execute(select(Conversation).where(Conversation.session_id == sid))
        ).scalar_one()
        stored_plan = _stored_plan_text(conv)
        assert "Session name:" not in stored_plan, stored_plan
    # Exactly one LLM call (the plan).
    assert len(chat.requests) == 1, len(chat.requests)
    print("[1] NO_PLAN fast-path: 1 LLM call, no execute")


async def test_no_plan_local_query_is_repaired_before_execute() -> None:
    sid = await _open_session("doc summary")
    tool = _CountingTool()
    _install_tool(tool)
    chat = _ScriptedChat([
        ChatResponse(
            text=(
                "NO_PLAN: I cannot inspect that document here.\n"
                "Session name: Doc summary"
            ),
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="c1", name=tool.name, arguments={"q": "current document"},
            )],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=500, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text="Answer from the local document.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=600, output_tokens=20),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    events = await _drive(sid, "总结这份文档")
    seq = [e[0] for e in events]
    assert seq.count("thinking") == 2, seq
    assert seq.count("tool_call") == 1, seq
    plan_payload = json.loads(next(d for ev, d in events if ev == "plan"))
    assert not plan_payload["text"].lstrip().startswith("NO_PLAN:"), plan_payload
    assert plan_payload["budget"]["tier"] == "quick", plan_payload
    answer = next(d for ev, d in events if ev == "answer")
    assert "local document" in answer
    assert tool.call_count == 1
    assert len(chat.requests) == 3, len(chat.requests)

    factory = get_session_factory()
    async with factory() as s:
        conv = (
            await s.execute(select(Conversation).where(Conversation.session_id == sid))
        ).scalar_one()
        stored_call = conv.llm_calls[0]
        stored_plan = _stored_plan_text(conv)
        assert stored_plan.startswith("BUDGET: quick"), stored_plan
        assert "NO_PLAN:" not in stored_plan
        assert stored_call["no_plan_repaired"] is True
        assert "NO_PLAN:" in stored_call["raw_plan_text"]
    print("[1a] local-query NO_PLAN repaired before execute")


# ---- 2. tool dedup ---------------------------------------------------------


def test_no_plan_prompt_excludes_factual_questions() -> None:
    assert (
        "answered directly without Library's local" in PLAN_PHASE_PROMPT
    )
    assert "weather, news, prices" in PLAN_PHASE_PROMPT
    assert "Do not invent the fact" in PLAN_PHASE_PROMPT
    assert "Never use `NO_PLAN` for requests about the user's library" in (
        PLAN_PHASE_PROMPT
    )
    assert "knowledge-base contents" in PLAN_PHASE_PROMPT


def test_library_tool_requirement_heuristic() -> None:
    assert runtime._requires_library_tools("summarize this PDF")
    assert runtime._requires_library_tools("总结这份文档")
    assert runtime._requires_library_tools("总结这篇")
    assert runtime._requires_library_tools("我的知识库里有 Raft 笔记吗")
    assert not runtime._requires_library_tools("这份怎么样")
    assert not runtime._requires_library_tools("今天天气怎么样")
    assert not runtime._requires_library_tools("谢谢")


async def test_execute_repairs_premature_no_tool_answer_for_library_query() -> None:
    sid = await _open_session("local note")
    tool = _CountingTool()
    _install_tool(tool)
    chat = _ScriptedChat([
        ChatResponse(
            text=(
                "BUDGET: quick\n"
                "1. Locate the relevant local note.\n"
                "2. Verify the answer from local evidence.\n"
                "Session name: Local note"
            ),
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text="Raft is a consensus algorithm.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=500, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="c1", name=tool.name, arguments={"q": "raft note"},
            )],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=600, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text="Answer from local evidence.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=700, output_tokens=20),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    events = await _drive(sid, "summarize my raft note")
    seq = [e[0] for e in events]
    assert seq.count("thinking") == 3, seq
    assert seq.count("tool_call") == 1, seq
    assert seq.count("tool_result") == 1, seq
    answer = next(d for ev, d in events if ev == "answer")
    assert "Answer from local evidence" in answer
    assert tool.call_count == 1
    assert len(chat.requests) == 4, len(chat.requests)
    repair_req = chat.requests[2]
    assert any(
        "research phase is still active"
        in str(message.content)
        for message in repair_req.messages
    )
    print("[1b] no-tool local-library answer repaired into agent loop")


async def test_tool_dedup() -> None:
    sid = await _open_session("dedup test")
    tool = _CountingTool()
    _install_tool(tool)

    chat = _ScriptedChat([
        # plan
        ChatResponse(
            text="先 echo 看看，再 echo 同一参数（应被 dedup）。",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=500, output_tokens=30),
            parsed_json=None,
        ),
        # execute 0: echo({"q":"hi"})
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="c1", name="echo_tool", arguments={"q": "hi"},
            )],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=600, output_tokens=20),
            parsed_json=None,
        ),
        # execute 1: identical args — should be deduped
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="c2", name="echo_tool", arguments={"q": "hi"},
            )],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=700, output_tokens=20),
            parsed_json=None,
        ),
        # execute 2: final answer
        ChatResponse(
            text="done.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=800, output_tokens=20),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    events = await _drive(sid, "走两次同样的工具调用")
    tool_results = [d for ev, d in events if ev == "tool_result"]
    assert len(tool_results) == 2, tool_results
    # First call ran; second was deduped → handler ran exactly once.
    assert tool.call_count == 1, tool.call_count
    # Second tool_result frame should carry the deduped flag.
    assert '"deduped": true' in tool_results[1], tool_results[1]
    print("[2] tool dedup: handler ran 1x for 2 identical calls")


# ---- 3. doom-loop guard ----------------------------------------------------

async def test_doom_loop_nudge() -> None:
    sid = await _open_session("doom test")
    tool = _CountingTool()
    _install_tool(tool)

    # Same name, near-duplicate args (each subtly different so dedup
    # does NOT collapse them) — three calls trip the threshold.
    chat = _ScriptedChat([
        ChatResponse(  # plan
            text="测试 doom-loop。",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(  # execute 0
            text=None,
            tool_calls=[ToolCall(id="c1", name="echo_tool",
                                 arguments={"q": "a"})],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=500, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(  # execute 1
            text=None,
            tool_calls=[ToolCall(id="c2", name="echo_tool",
                                 arguments={"q": "a"})],  # dup → counts in seen
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=550, output_tokens=20),
            parsed_json=None,
        ),
        # By the third call to the same key the doom-loop counter trips.
        ChatResponse(  # execute 2
            text=None,
            tool_calls=[ToolCall(id="c3", name="echo_tool",
                                 arguments={"q": "a"})],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=600, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(  # execute 3: final answer
            text="ok.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=700, output_tokens=20),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    events = await _drive(sid, "doom")
    # Inspect the fourth chat request (the one AFTER doom-loop tripped):
    # its messages must contain the STOP nudge text appended to the last
    # tool_result block. Append-only: nothing else mutated.
    last_req = chat.requests[-1]
    nudge_seen = False
    for msg in last_req.messages:
        if isinstance(msg.content, list):
            for block in msg.content:
                content = getattr(block, "content", "") or ""
                if "runtime guard" in content and "repeatedly called" in content:
                    nudge_seen = True
    assert nudge_seen, (
        "doom-loop nudge not appended to last tool_result. "
        f"messages={[m.role for m in last_req.messages]}"
    )
    # The execute prompt now starts with a cacheable snapshot prefix. The
    # live user message must still be byte-identical and appended after
    # that stable prefix, so doom-loop nudges never mutate the cached part
    # or the original user turn.
    assert last_req.cache_breakpoints == [0]
    original_user_indices = [
        i for i, m in enumerate(last_req.messages)
        if m.role == "user" and m.content == "doom"
    ]
    assert original_user_indices, [
        (m.role, m.content if isinstance(m.content, str) else "<blocks>")
        for m in last_req.messages
    ]
    assert original_user_indices[0] > 0
    print("[3] doom-loop nudge appended; original user msg unchanged")


# ---- 4. final-answer max_tokens continuation ------------------------------

async def test_final_answer_continuation_is_buffered() -> None:
    sid = await _open_session("long answer")
    chat = _ScriptedChat([
        ChatResponse(
            text="1. Write the researched answer.\nSession name: Long answer",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text="Part A ",
            tool_calls=[], stop_reason="max_tokens",
            usage=TokenUsage(input_tokens=500, output_tokens=2048),
            parsed_json=None,
        ),
        ChatResponse(
            text="Part B.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=550, output_tokens=20),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    events = await _drive(sid, "make it long")
    plan = json.loads(next(d for ev, d in events if ev == "plan"))["text"]
    assert "Session name:" not in plan, plan
    answers = [d for ev, d in events if ev == "answer"]
    assert answers == ["Part A Part B."], answers
    done = json.loads(next(d for ev, d in events if ev == "done"))
    assert done["truncated"] is False, done
    assert done["llm_calls"] == 3, done
    assert len(chat.requests) == 3, len(chat.requests)
    assert chat.requests[2].tools == chat.requests[1].tools
    assert chat.requests[2].tool_choice == "auto"
    assert chat.requests[2].tool_choice == "auto"

    factory = get_session_factory()
    async with factory() as s:
        conv = await s.get(Conversation, done["conversation_id"])
        assert conv and conv.agent_response == "Part A Part B."
        stored_plan = _stored_plan_text(conv)
        assert "Session name:" not in stored_plan, stored_plan
    print("[4] final-answer continuation: buffered into one answer event")


# ---- 5. explicit research finalization + citation closure -----------------


async def test_finalizing_attaches_validated_citation_manifest() -> None:
    entry_id = await _seed_citation_entry()
    sid = await _open_session("citation finalization")
    tool = _CitingReadTool(entry_id)
    _install_tool(tool, explicit_finalization=True)
    quote = "organized trade routes in the eleventh century"
    chat = _ScriptedChat([
        ChatResponse(
            text="1. Read the source and answer with citations.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=400, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="read-1",
                name="read_files",
                arguments={"requests": [{"entry_id": entry_id}]},
            )],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=500, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text=None,
            tool_calls=[ToolCall(
                id="finish-1",
                name="finish_research",
                arguments={
                    "evidence_status": "sufficient",
                    "reason": "The requested source passage was read.",
                    "citations": [{
                        "entry_id": entry_id,
                        "quote": quote,
                        "reason": "the source dates the organized trade expansion",
                    }],
                },
            )],
            stop_reason="tool_use",
            usage=TokenUsage(input_tokens=600, output_tokens=20),
            parsed_json=None,
        ),
        ChatResponse(
            text="Merchants expanded through organized trade.",
            tool_calls=[], stop_reason="end_turn",
            usage=TokenUsage(input_tokens=700, output_tokens=40),
            parsed_json=None,
        ),
    ])
    _install_chat(chat)

    events = await _drive(sid, "When did organized merchant trade expand?")
    answers = [data for event, data in events if event == "answer"]
    assert len(answers) == 1, answers
    assert f"[evidence.txt](entry:{entry_id}?q=organized+trade+routes" in answers[0]
    assert "entry_id=" not in answers[0]
    assert tool.call_count == 1

    thinking = [
        json.loads(data)
        for event, data in events
        if event == "thinking"
    ]
    assert [item["answer_phase"] for item in thinking] == [
        "researching",
        "researching",
        "finalizing",
    ]
    assert chat.requests[3].tool_choice == "auto"
    assert any(
        "citation_manifest markers" in str(message.content)
        for message in chat.requests[3].messages
    )

    done = json.loads(next(data for event, data in events if event == "done"))
    assert done["llm_calls"] == 4
    assert done["tool_calls"] == 2
    assert done["truncated"] is False
    factory = get_session_factory()
    async with factory() as s:
        conv = await s.get(Conversation, done["conversation_id"])
        assert conv is not None
        assert conv.agent_response is not None
        assert f"entry_id={entry_id}" in conv.agent_response
        assert [call["name"] for call in conv.tool_calls] == [
            "read_files",
            "finish_research",
        ]
        manifest = conv.tool_calls[1]["result"]["citation_manifest"]
        assert manifest[0]["entry_id"] == entry_id
        assert manifest[0]["quote"] == quote
    print("[5] finalizing attached validated citation definitions before display")


async def test_final_request_recovers_dropped_read_and_exact_current_question(monkeypatch) -> None:
    from library.config import ModelCapabilities
    from library.llm.types import ToolResultBlock, ToolUseBlock

    question = 'Read the document. ' + 'Background detail. ' * 160 + 'Report the quartz gauge reading.'
    measured = 'The quartz gauge reading was 76 units at inspection.'
    sid = await _open_session(question)
    eid = await _seed_citation_entry()

    class LongRead(_CitingReadTool):
        async def handler(self, db, ctx, arguments):
            packet = await super().handler(db, ctx, arguments)
            packet['results'][0]['reads'][0]['text'] = 'ordinary material. ' * 1200 + measured
            return packet

    _install_tool(LongRead(eid), explicit_finalization=True)
    from library.llm.types import ToolDef
    defs = [ToolDef(**item) for item in runtime.all_tool_defs()]
    monkeypatch.setattr(runtime, 'all_tool_defs', lambda: defs)
    def reply(text=None, calls=()):
        return ChatResponse(text=text, tool_calls=list(calls), stop_reason='tool_use' if calls else 'end_turn', usage=TokenUsage())
    chat = _ScriptedChat([
        reply('Read the source.'),
        reply(calls=[ToolCall('r1', 'read_files', {'requests': [{'entry_id': eid}]})]),
        reply(calls=[ToolCall('f1', 'finish_research', {'evidence_status': 'insufficient', 'reason': 'test'})]),
        reply('Bounded answer.'),
    ])
    chat.capabilities = ModelCapabilities(context_window=23_000, tokenizer='utf8_upper_bound')
    _install_chat(chat)
    events = await _drive(sid, question)
    assert any(e == 'answer' for e, _ in events), [e for e, _ in events]
    final = chat.requests[-1]
    assert any(m.role == 'user' and m.content == question for m in final.messages)
    calls = [b.id for m in final.messages if isinstance(m.content, list) for b in m.content if isinstance(b, ToolUseBlock)]
    reads = [b for m in final.messages if m.role == 'tool' for b in m.content if isinstance(b, ToolResultBlock) and b.tool_call_id == 'r1']
    assert calls.count('r1') == len(reads) == 1
    assert measured in reads[0].content
    assert len(reads[0].content) < 8000  # bounded excerpts, not the original 22k result
    # Serialize the actual final request using each production adapter, offline.
    from library.llm.openai_adapter import OpenAIChatClient
    from library.llm.anthropic_adapter import AnthropicChatClient
    wire = OpenAIChatClient.__new__(OpenAIChatClient)._render_messages(final)
    assert any(m.get('tool_call_id') == 'r1' and measured in m['content'] for m in wire if m['role'] == 'tool')
    wire = AnthropicChatClient.__new__(AnthropicChatClient)._render_messages(final)
    assert any(b.get('tool_use_id') == 'r1' and measured in b['content']
               for m in wire for b in m['content'] if b['type'] == 'tool_result')


# ---- 6. canonical args (json.dumps sort_keys) ------------------------------

def test_canonical_args() -> None:
    a = runtime._canonical_args({"a": 1, "b": 2})
    b = runtime._canonical_args({"b": 2, "a": 1})
    assert a == b, (a, b)
    # Distinct values must produce distinct keys.
    c = runtime._canonical_args({"a": 1, "b": 3})
    assert c != a
    print("[5] _canonical_args is order-stable")


def test_public_plan_text_strips_numbering() -> None:
    plan = (
        "BUDGET: standard\n"
        "1. 定位案件材料和适用规则。\n"
        "2. 核验证据材料与庭审陈述。\n"
        "3. 分项分析诉讼请求是否支持。\n"
    )
    public = runtime._public_plan_text(plan)
    assert public.splitlines() == [
        "定位案件材料和适用规则。",
        "核验证据材料与庭审陈述。",
        "分项分析诉讼请求是否支持。",
    ]
    print("[6] public plan strips numbering")


async def main() -> None:
    await _create_schema()
    test_canonical_args()
    test_public_plan_text_strips_numbering()
    test_no_plan_prompt_excludes_factual_questions()
    test_library_tool_requirement_heuristic()
    await test_no_plan_fast_path()
    await test_no_plan_local_query_is_repaired_before_execute()
    await test_execute_repairs_premature_no_tool_answer_for_library_query()
    await test_tool_dedup()
    await test_doom_loop_nudge()
    await test_final_answer_continuation_is_buffered()
    await test_finalizing_attaches_validated_citation_manifest()
    from pytest import MonkeyPatch

    with MonkeyPatch.context() as monkeypatch:
        await test_final_request_recovers_dropped_read_and_exact_current_question(monkeypatch)
    print("\nALL RUNTIME-GUARD TESTS PASSED")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except AssertionError as e:
        print("FAIL:", e, file=sys.stderr)
        raise
