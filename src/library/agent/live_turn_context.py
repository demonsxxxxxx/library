"""Keep the live user message and bounded read excerpts through request compaction."""
from __future__ import annotations

import json
import re
from typing import Any

from library.agent.conversation_compaction import (
    ConversationCheckpoint,
    TokenCounter,
    fit_messages_to_token_budget,
)
from library.llm import ChatMessage, ToolResultBlock, ToolUseBlock

# A replay is a view of an existing tool exchange, never a new invocation.
_WINDOW = 800
_MAX_REPLAYS = 8
_WORD = re.compile(r'[a-zA-Z0-9_]{2,}|[\u3400-\u9fff]+')


def _terms(text: str) -> set[str]:
    terms = set()
    for word in _WORD.findall(text.lower()):
        if '\u3400' <= word[0] <= '\u9fff':
            terms.update(word[i:i + 2] for i in range(max(1, len(word) - 1)))
        else:
            terms.add(word)
    return terms


def _score(text: str, terms: set[str], focus: set[str]) -> int:
    found = _terms(text)
    return len(found & terms) + 3 * len(found & focus)


def _priority(row: dict, terms: set[str], focus: set[str]) -> tuple[int, bool, int]:
    # An explicitly narrowed page read with a topical match is stronger
    # evidence than another global search window repeating the document title.
    found = _terms(row['text'])
    matches = len(found & focus)
    place = row.get('read_range') or {}
    page = place.get('page_start')
    targeted = type(page) is int and page > 0 and page == place.get('page_end')
    return matches, bool(matches and targeted), len(found & terms)


def _excerpt(block: ToolResultBlock, terms: set[str], focus: set[str]) -> tuple[tuple[int, bool, int], ToolResultBlock] | None:
    if block.is_error:
        return None
    try:
        packet = json.loads(block.content)
    except (ValueError, RecursionError):
        return None
    if not isinstance(packet, dict) or packet.get('ok') is not True:
        return None
    if packet.get('view') == 'read_excerpt':
        # Our own bounded view may need to survive several checkpoints.
        rows = packet.get('excerpts')
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
            return None
        text = rows[0].get('text')
        if not isinstance(text, str) or not text or len(text) > 2 * _WINDOW:
            return None
        return _priority(rows[0], terms, focus), block
    best: dict[str, Any] | None = None
    best_score = -1
    entries = packet.get('results')
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get('entry_id') or not isinstance(entry.get('reads'), list):
            continue
        for read_index, read in enumerate(entry['reads']):
            if not isinstance(read, dict) or read.get('ok') is not True:
                continue
            body = read.get('text')
            if not isinstance(body, str) or not body.strip():
                continue
            extra = read.get('extras')
            extra = extra if isinstance(extra, dict) else {}
            args = read.get('args')
            if (extra.get('numeric_status') or extra.get('source') == 'pdf_page_pixels'
                    or (isinstance(args, dict) and 'numeric_check' in args)):
                # Pixel claims have a stricter, version-bound finish path. Never
                # turn an incomplete or conflicting check into an ordinary quote.
                continue
            origin = read.get('char_start', 0)
            if type(origin) is not int or origin < 0:
                continue
            # Copy provenance/uncertainty, not arbitrary metadata, tool instructions,
            # cursor contents or a validated numeric claim. Offset is into this wire text.
            loc = {k: extra[k] for k in ('page_start', 'page_end', 'numeric_warning') if k in extra}
            for start in range(0, len(body), _WINDOW // 2):
                # Rank the matching window, but retain adjacent text as well.
                # A window boundary frequently bisects a sentence or table row.
                piece = body[start:start + _WINDOW]
                score = _score(piece, terms, focus)
                if score > best_score:
                    left = max(0, start - _WINDOW // 2)
                    best_score = score
                    best = {
                        'entry_id': entry['entry_id'], 'read_range': loc,
                        'read_index': read.get('read_index', read_index),
                        'compressed_read': bool((extra.get('read_compression') or {}).get('compressed'))
                        if isinstance(extra.get('read_compression'), dict) else False,
                        'wire_offset': left + origin,
                        'text': body[left:start + _WINDOW + _WINDOW // 2],
                    }
                if start + _WINDOW >= len(body):
                    break
    if best is None:
        return None
    # Keep one window per call so independent reads can share the reserve.
    view = {'ok': True, 'view': 'read_excerpt', 'coverage': 'partial; not proof of absence',
            'excerpts': [best]}
    content = json.dumps(view, ensure_ascii=False)
    return _priority(best, terms, focus), ToolResultBlock(tool_call_id=block.tool_call_id, content=content)


def fit_live_turn(messages: list[ChatMessage], question: ChatMessage, *, token_budget: int,
                  counter: TokenCounter) -> tuple[list[ChatMessage], ConversationCheckpoint, int]:
    """Invoked only on overflow. Never synthesize a user message from source text."""
    at = next(i for i, msg in enumerate(messages) if msg is question)
    budget = token_budget - counter.message(question)
    if budget < 128:
        raise ValueError('model context window cannot retain the current user request')
    rest = [*messages[:at], *messages[at + 1:]]
    fitted, checkpoint = fit_messages_to_token_budget(rest, token_budget=budget, counter=counter)
    assert checkpoint is not None  # Removing the question also removed its exact budget.
    kept = {id(msg) for msg in fitted[1:]}
    content = question.content
    text = content if isinstance(content, str) else '\n'.join(getattr(b, 'text', '') for b in content)
    terms = _terms(text)
    focus = _terms(text.rstrip().rsplit('\n', 1)[-1])
    # Inspect only omitted, paired read_files blocks from this live turn. Do
    # not use the raw DB payload, replayed session history or other tools.
    ranked = []
    calls = {}
    for index, msg in enumerate(messages[at + 1:]):
        if msg.role == 'assistant':
            blocks = msg.content if isinstance(msg.content, list) else []
            calls = {b.id: b for b in blocks if isinstance(b, ToolUseBlock) and b.name == 'read_files'}
        elif msg.role == 'tool' and id(msg) not in kept:
            for block in msg.content:
                if not isinstance(block, ToolResultBlock) or block.tool_call_id not in calls:
                    continue
                item = _excerpt(block, terms, focus)
                if item:
                    score, view = item
                    pair = [ChatMessage(role='assistant', content=[calls[block.tool_call_id]]),
                            ChatMessage(role='tool', content=[view])]
                    ranked.append((score, index, pair))
    remaining = min(6000, budget // 3, max(0, budget - 512))
    selected = []
    cost = 0
    for _, index, pair in sorted(ranked, key=lambda item: (item[0], item[1]), reverse=True):
        size = counter.messages(pair)
        if size <= remaining - cost:
            selected.append((index, pair))
            cost += size
            if len(selected) == _MAX_REPLAYS:
                break
    if selected:
        try:
            fitted, checkpoint = fit_messages_to_token_budget(rest, token_budget=budget - cost, counter=counter)
        except ValueError:
            # A bounded excerpt is optional; it must not invalidate a request
            # that already fits with its question and the original checkpoint.
            selected = []
        assert checkpoint is not None
    prior = {id(msg) for msg in messages[:at]}
    result = [fitted[0], *(msg for msg in fitted[1:] if id(msg) in prior), question]
    # At a smaller budget, the tail cannot grow; still deduplicate by call ID
    # rather than relying on a particular checkpoint selection algorithm.
    live = [msg for msg in fitted[1:] if id(msg) not in prior]
    present = {b.id for m in live if isinstance(m.content, list) for b in m.content if isinstance(b, ToolUseBlock)}
    n = 0
    for _, pair in sorted(selected, key=lambda item: item[0]):
        key = pair[0].content[0].id
        if key not in present:
            result.extend(pair)
            present.add(key)
            n += 1
    result.extend(live)
    return result, checkpoint, n
