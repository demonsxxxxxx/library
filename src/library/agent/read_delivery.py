"""Bounded, turn-local delivery of read_files evidence; no second storage read."""
from __future__ import annotations

import json
import secrets
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

MAX_TOOL_RESULT_LEN = 50_000
_MAX_CACHED_CHARS = 4_000_000


def _size(value: Any) -> int:
    # Match runtime._structured_truncate's actual serialization, not token estimates.
    return len(json.dumps(value, ensure_ascii=False, default=str))


@dataclass
class _Item:
    entry: dict
    read: dict
    index: int
    status: str


@dataclass
class ReadDelivery:
    # Owned by ToolContext (one agent execution), never global or shared across users.
    batches: OrderedDict[str, tuple[list[_Item], int]] = field(default_factory=OrderedDict)
    cached_chars: int = 0

    def start(self, payload: dict) -> dict:
        size = _size(payload)
        if size <= MAX_TOOL_RESULT_LEN:
            if payload.get('ok'):
                return payload
            # A failed sibling is not evidence against a successful read. Keep
            # both their statuses, including for small batches that do not page.
            rows = [dict(entry, ok=True) if any(r.get('ok') for r in entry.get('reads', []))
                    else entry for entry in payload['results']]
            if not any(e.get('ok') for e in rows):
                return payload
            mixed = dict(payload, ok=True, results=rows, partial_success=True)
            if _size(mixed) <= MAX_TOOL_RESULT_LEN:
                return mixed
        if size > _MAX_CACHED_CHARS:
            return {'ok': False, 'delivery': 'not_delivered', 'error':
                    'Batch exceeds continuation buffer. Retry fewer subreads or a narrower range; not a no-match.'}
        items = []
        for entry in payload['results']:
            head = {k: v for k, v in entry.items() if k not in ('reads', 'ok', 'error')}
            for read in entry.get('reads') or [{'ok': False, 'error': entry.get('error') or 'No reads'}]:
                status = ('hit' if read.get('ok') else
                          'no_match' if read.get('error') == 'no matches' else 'failed')
                items.append(_Item(head, read, len(items), status))
        if not items:
            return {'ok': False, 'delivery': 'not_delivered', 'error': 'No deliverable subreads'}
        # Send usable evidence first; distinguish failures from pending hits in the overview.
        items.sort(key=lambda item: item.status != 'hit')
        while self.batches and self.cached_chars + size > _MAX_CACHED_CHARS:
            _, (_, evicted) = self.batches.popitem(last=False)
            self.cached_chars -= evicted
        key = secrets.token_hex(12)
        self.batches[key] = (items, size)
        self.cached_chars += size
        return self._page(key, items, 0, 0)

    async def resume(self, db, cursor: str) -> dict:
        from library.repositories.entries import list_live_with_file_by_ids

        parsed = self.resolve(cursor)
        if parsed is None:
            return {'ok': False, 'error': 'Invalid or expired evidence cursor; reopen the source.'}
        key, items, pos, offset, single = parsed
        ids = {i.entry.get('entry_id') for i in items if i.read.get('ok')}
        live = {e.id for e, _ in await list_live_with_file_by_ids(db, list(ids))}
        if live != ids:
            return {'ok': False, 'error': 'Source is no longer live; reopen the current entry.'}
        return self._page(key, items, pos, offset, single)

    def resolve(self, cursor: str) -> tuple[str, list[_Item], int, int, bool] | None:
        try:
            key, pos_s, off_s, mode = cursor.split(':')
            if mode not in ('all', 'one'):
                return None
            items = self.batches[key][0]
            pos, off = int(pos_s), int(off_s)
            if not (0 <= pos < len(items) and 0 <= off <= len(items[pos].read.get('text', ''))):
                return None
            return key, items, pos, off, mode == 'one'
        except (AttributeError, KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _page(key: str, items: list[_Item], pos: int, offset: int, single: bool = False) -> dict:
        packet: dict = {'ok': False, 'results': [], 'count': 0, 'delivery': 'partial',
                        'next_cursor': None}
        stop = pos + 1 if single else len(items)
        if pos == 0 and not offset and not single:
            packet['subreads'] = {s: [i.index for i in items if i.status == s]
                                  for s in ('hit', 'no_match', 'failed')}
            packet['by_read'] = {str(i.index): f'{key}:{j}:0:one' for j, i in enumerate(items) if i.status == 'hit'}
        # Reserve space for the largest possible cursor and envelope changes.
        limit = MAX_TOOL_RESULT_LEN - 128
        while pos < stop:
            item = items[pos]
            read = dict(item.read, read_index=item.index, status=item.status)
            entry = dict(item.entry, ok=read.get('ok', False), reads=[read])
            packet['results'].append(entry)
            body = read.get('text', '')
            if offset:
                read.update(text=body[offset:], char_start=offset, char_end=len(body))
            if _size(packet) > limit:
                if len(packet['results']) > 1:
                    packet['results'].pop()
                    break
                # One subread is too large. Keep the text and its source range,
                # not duplicate hit contexts; explicitly report omitted metadata.
                extras = read.pop('extras', {})
                read['extras'] = {k: v for k, v in extras.items() if k in (
                    'page_start', 'page_end', 'total_pages', 'next_match_offset', 'has_more',
                    'total_matches', 'match_offset', 'next_offset', 'truncated', 'read_compression')
                    and _size(v) <= 1000}
                read['omitted_extra_fields'] = sorted(set(extras) - set(read['extras']))
                read.update(text='', char_start=offset, char_end=len(body))
                if _size(packet) > limit - 64:
                    return {'ok': False, 'delivery': 'not_delivered',
                            'error': 'Subread metadata exceeds budget; request a narrower read.'}
                lo, hi = offset, len(body)
                while lo < hi:
                    mid = (lo + hi + 1) // 2
                    read['text'] = body[offset:mid]
                    if _size(packet) <= limit:
                        lo = mid
                    else:
                        hi = mid - 1
                end = lo
                if end < len(body):
                    if end == offset:
                        return {'ok': False, 'delivery': 'not_delivered', 'error': 'Insufficient space for text'}
                    # Prefer a page/paragraph/line boundary, without losing its newline.
                    for sep in ('\n[Page ', '\n\n', '\n'):
                        i = body.rfind(sep, offset + (end - offset) // 2, end)
                        if i >= 0:
                            end = i + 1
                            break
                read.update(text=body[offset:end], char_end=end)
                if end < len(body):
                    offset = end
                    break
            pos += 1
            offset = 0
        packet['count'] = len(packet['results'])
        # A terminal page of reported misses does not invalidate the hits on
        # earlier pages. For a targeted cursor, report only that subread's status.
        packet['ok'] = any(r['ok'] for r in packet['results']) if single else any(
            i.status == 'hit' for i in items)
        if packet['ok'] and any(i.status != 'hit' for i in items):
            packet['partial_success'] = True
        packet['delivery'] = 'complete' if pos == stop else 'partial'
        if pos < stop:
            packet['next_cursor'] = f"{key}:{pos}:{offset}:{'one' if single else 'all'}"
        return packet
