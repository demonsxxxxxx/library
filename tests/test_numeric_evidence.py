"""Numeric evidence must survive delivery and match its original physical page."""
from __future__ import annotations

import base64
import hashlib
import io
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from PIL import Image

from library.agent import runtime
from library.agent.numeric_evidence import attach_numeric_claims, receive_numeric_evidence
from library.agent.read_delivery import ReadDelivery
from library.agent.tools import ToolContext
from library.agent.tools import read_files as read_tool
from library.llm import ToolCall
from library.pipelines import pdf
from library.repositories import entries


@pytest.mark.parametrize(('page', 'expression', 'altered'), [
    (2, '3.5 × 10⁸ ≤ N_cell', '3.5 × 10⁸ ≥ N_cell'),
    (3, '7.2 × 10⁻³ mg', '7.2 × 10³ mg'),
])
async def test_dispatch_and_render_original_page(monkeypatch, page, expression, altered):
    # Use varying source IDs, pages and expressions; only the visual reader
    # and external repository/storage boundaries are replaced.
    stream = io.BytesIO()
    im = Image.new('RGB', (80, 100), 'white')
    im.save(stream, format='PDF', save_all=True, append_images=[im, im])
    await exercise_numeric_binding(monkeypatch, page, expression, altered, stream.getvalue())


async def exercise_numeric_binding(monkeypatch, page, expression, altered, body, *,
                                   region=(0, 0, .5, .5), expected_crop=None):
    eid = str(uuid4())
    f = SimpleNamespace(id=str(uuid4()), storage_key='pdf', sha256=hashlib.sha256(body).hexdigest(),
                        mime_type='application/pdf', original_ext='.pdf', deleted_at=None, kind='text', ingest_status='done')
    e = SimpleNamespace(id=eid, display_name='Source.pdf', deleted_at=None)

    class Store:
        async def get(self, key):
            assert key == 'pdf'
            yield body

    @asynccontextmanager
    async def session_scope():
        yield SimpleNamespace(commit=AsyncMock())

    @asynccontextmanager
    async def tool_lock(*args, **kwargs):
        yield

    saved = []

    async def persist(**call):
        saved.append(call)

    live = AsyncMock(return_value=[(e, f)])
    monkeypatch.setattr(entries, 'list_with_file_by_ids_any', live)
    monkeypatch.setattr(entries, 'list_live_with_file_by_ids', live)
    monkeypatch.setattr(entries, 'resolve_entry_id_prefix', AsyncMock(return_value=(eid, None)))
    monkeypatch.setattr(read_tool, 'get_storage', lambda: Store())
    monkeypatch.setattr(pdf, 'has_vision_profile', lambda: True)
    async def transcribe(request):
        if expected_crop is not None:
            assert base64.b64decode(request.messages[0].content[1].data_b64) == expected_crop
        return SimpleNamespace(text=json.dumps({'status': 'clear', 'text': expression}), stop_reason='end_turn')

    vision = AsyncMock(side_effect=transcribe)
    monkeypatch.setattr(pdf, 'get_chat_client', lambda profile: SimpleNamespace(complete=vision))
    monkeypatch.setattr(runtime, 'session_scope', session_scope)
    monkeypatch.setattr(runtime, 'tool_execution_lock', tool_lock)
    monkeypatch.setattr(runtime, '_persist_tool_call', persist)
    monkeypatch.setattr(runtime, '_load_prior_tool_calls', AsyncMock(side_effect=lambda cid: list(saved)))
    quote_lookup = AsyncMock(return_value=1)
    label_lookup = AsyncMock(return_value=1)
    monkeypatch.setattr(runtime, '_locate_pdf_quote_page', quote_lookup)
    monkeypatch.setattr(runtime, '_resolve_pdf_page_locator', label_lookup)
    ctx = ToolContext('session', 'conversation', read_delivery=ReadDelivery())
    guard = runtime._CallGuard()

    async def dispatch(name, arguments):
        turn = len(saved)
        blocks, stats = [], runtime._DispatchStats()
        async for _ in runtime._dispatch_tool_calls(
            tool_calls=[ToolCall(str(turn), name, arguments)], ctx=ctx, conversation_id=ctx.conversation_id,
            result_blocks=blocks, guard=guard, stats=stats, turn=turn,
        ):
            pass
        return json.loads(blocks[0].content), stats

    def read_args(candidate):
        return {'requests': [{'entry_id': eid, 'reads': [{'page_start': page, 'numeric_check': {
            'candidate': candidate, 'locator': 'the indicated field', 'region': list(region)}}]}]}

    good, _ = await dispatch('read_files', read_args(expression))
    evidence = good['results'][0]['reads'][0]
    ref = evidence['evidence_id']
    assert ctx.numeric_evidence[ref]['page'] == page
    bad, _ = await dispatch('read_files', read_args(altered))
    assert not bad['ok'] and 'evidence_id' not in bad['results'][0]['reads'][0]
    assert vision.await_count == 2
    assert not ToolContext('session', 'another-turn').numeric_evidence

    claim = {'evidence_id': ref, 'entry_id': eid, 'page': page, 'expression': expression}
    result, stats = await dispatch('finish_research', {'evidence_status': 'sufficient', 'numeric_claims': [
        claim, dict(claim, expression=altered), dict(claim, page=page - 1),
        dict(claim, entry_id=str(uuid4())), {'expression': altered}, dict(claim, evidence_id='unknown'),
    ]})
    assert stats.finish_research_requested and stats.evidence_status == 'partial'
    checked = result['numeric_results']
    assert checked == stats.numeric_results
    assert checked[0]['status'] == 'verified'
    assert [v['reason'] for v in checked[1:]] == [
        'expression_mismatch', 'source_location_mismatch', 'source_location_mismatch',
        'missing_pixel_evidence', 'missing_pixel_evidence',
    ]
    assert all('quote' not in v and 'expression' not in v for v in checked[1:])
    body = attach_numeric_claims('其他结论保留。', checked)
    shown = await runtime._rewrite_footnotes_for_display(body, numeric_results=checked)
    assert '其他结论保留' in shown and expression in shown and altered not in shown
    assert shown.count('未核实') == 5 and shown.count(f'(entry:{eid}?page={page})') == 1
    assert quote_lookup.await_count == label_lookup.await_count == 0
    assert 'original PDF version unavailable' in await runtime._rewrite_footnotes_for_display(body)
    f.sha256 = '0' * 64
    stale, _ = await dispatch('finish_research', {'evidence_status': 'sufficient', 'numeric_claims': [claim]})
    assert stale['evidence_status'] == 'insufficient'
    assert stale['numeric_results'][0]['reason'] == 'source_version_unavailable'
    assert f'entry:{eid}' not in await runtime._rewrite_footnotes_for_display(body, numeric_results=checked)
    assert vision.await_count == 2
    return {'verified': checked[0]['status'], 'rejected': [v['reason'] for v in checked[1:]],
            'physical_page_link': f'entry:{eid}?page={page}' in shown,
            'stale_rejected': stale['numeric_results'][0]['reason']}


def test_missing_or_truncated_pixels_do_not_create_evidence():
    ledger = {}
    for payload in ('{"results": []}', '[{"evidence_id": "123"}]', '{"evidence_id":...'):
        receive_numeric_evidence(payload, ledger)
    assert not ledger
