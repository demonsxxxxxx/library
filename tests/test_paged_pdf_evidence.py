"""Regressions for turn-scoped evidence delivery and pixel-only numeric checks."""
import base64
import io
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from PIL import Image

from library.agent.citation_manifest import _matches_read
from library.agent.read_delivery import MAX_TOOL_RESULT_LEN, ReadDelivery
from library.agent.runtime import _structured_truncate
from library.agent.tools import ToolContext, get_tool
from library.pipelines import pdf
from library.repositories import entries


def packet(reads):
    return {'ok': all(r['ok'] for r in reads), 'results': [
        {'entry_id': 'entry1', 'ok': all(r['ok'] for r in reads), 'reads': reads}], 'count': 1}


async def test_continuation_and_statuses(monkeypatch):
    live = AsyncMock(return_value=[(SimpleNamespace(id='entry1'), None)])
    monkeypatch.setattr(entries, 'list_live_with_file_by_ids', live)
    ctx = ToolContext('s', 'c', read_delivery=ReadDelivery())
    huge = 'mathematics "α"\n' * 9000
    reads = [{'ok': False, 'error': 'no matches'}, {'ok': True, 'text': huge},
             {'ok': True, 'text': 'unrelated'}, {'ok': False, 'error': 'unavailable'}]
    first = ctx.read_delivery.start(packet(reads))
    assert first['subreads'] == {'hit': [1, 2], 'no_match': [0], 'failed': [3]}
    page = first
    seen = []
    parts = []
    handler = get_tool('read_files').handler
    for _ in range(10):
        text, marker = _structured_truncate(page, MAX_TOOL_RESULT_LEN)
        assert marker is None and len(text) <= MAX_TOOL_RESULT_LEN and page['results']
        for row in page['results']:
            for read in row['reads']:
                seen.append((read['read_index'], read['status']))
                if read['read_index'] == 1:
                    assert read['char_start'] == sum(map(len, parts))
                    parts.append(read['text'])
        if not page['next_cursor']:
            break
        page = await handler(None, ctx, {'cursor': page['next_cursor']})
    assert page['delivery'] == 'complete' and ''.join(parts) == huge
    assert set(seen) == {(0,'no_match'), (1,'hit'), (2,'hit'), (3,'failed')}
    focused = await handler(None, ctx, {'cursor': first['by_read']['2']})
    assert focused['results'][0]['reads'][0]['text'] == 'unrelated' and not focused['next_cursor']
    alien = ToolContext('other', 'c2', read_delivery=ReadDelivery())
    assert not (await handler(None, alien, {'cursor': first['next_cursor']}))['ok']
    live.return_value = []
    assert not (await handler(None, ctx, {'cursor': first['next_cursor']}))['ok']


def test_small_partial_keeps_success_and_failure():
    result = ReadDelivery().start(packet([{'ok': True, 'text': 'hit'}, {'ok': False, 'error': 'no matches'}]))
    assert result['partial_success'] and result['ok'] and result['results'][0]['ok']
    assert not result['results'][0]['reads'][1]['ok']


class Storage:
    async def get(self, key):
        stream = io.BytesIO()
        Image.new('RGB',(90,120),'white').save(stream, format='PDF')
        yield stream.getvalue()


@pytest.mark.parametrize(('candidate','status','transcription','expected'), [
    ('10⁻²', 'clear', '10⁻²', 'pixel_agreement'),
    ('10²', 'clear', '10⁻²', 'disagreement'), ('102', 'clear', '10²', 'disagreement'),
    ('x ≤ 8', 'clear', 'x ≥ 8', 'disagreement'), ('8', 'unclear', '8', 'unverified'),
])
async def test_visual_agreement_or_abstention(monkeypatch, candidate, status, transcription, expected):
    client = SimpleNamespace(complete=AsyncMock(return_value=SimpleNamespace(
        text=json.dumps({'status': status,'text': transcription}), stop_reason='end_turn')))
    monkeypatch.setattr(pdf, 'has_vision_profile', lambda: True)
    monkeypatch.setattr(pdf, 'get_chat_client', lambda profile: client)
    result = await pdf.PdfPipeline().read_segment(storage=Storage(), file_row=SimpleNamespace(storage_key='pdf'),
        args={'page_start':1,'numeric_check':{'locator':'equation at left','candidate':candidate,'region':[0,0,.5,.5]}})
    req = client.complete.await_args.args[0]
    blocks = req.messages[0].content
    assert [b.kind for b in blocks] == ['text','image'] and blocks[0].text == 'equation at left'
    assert base64.b64decode(blocks[1].data_b64).startswith(b'\xff\xd8')
    assert result.extras['numeric_status'] == expected
    assert bool(result.error) == (expected != 'pixel_agreement')
    if result.error:
        assert not result.text
    else:
        assert _matches_read({'text':result.text,'extras':result.extras}, transcription)
        assert not _matches_read({'text':result.text,'extras':result.extras}, '102')


async def test_missing_vision_does_not_verify_text(monkeypatch):
    monkeypatch.setattr(pdf, 'has_vision_profile', lambda: False)
    result = await pdf.PdfPipeline().read_segment(storage=Storage(), file_row=None,
        args={'page_start':1,'numeric_check':{'locator':'first row','candidate':'42'}})
    assert result.error and result.extras['numeric_status'] == 'unverified' and not result.text
