"""Bounded native PDF prose search with offsets relative to the single-page prose text."""

from __future__ import annotations
import regex
from library.pipelines.base import SegmentResult

WINDOW = 8
PAGE_CHAR_LIMIT = 80_000


def search(body, args, open_pdf):
    pattern = args.get("pattern")
    if (
        args.get("pdf_text_view") != "prose"
        or not isinstance(pattern, str)
        or not pattern.strip()
        or len(pattern) > 384
        or any(
            k in args for k in ("page_start", "page_end", "page_label", "numeric_check", "question")
        )
    ):
        return SegmentResult(
            error="Document prose search needs a non-empty pattern (up to 384 characters); omit physical page range and numeric/vision args."
        )
    start = args.get("search_from_page", 1)
    skip = args.get("match_offset", 0)
    limit = args.get("max_matches", 12)
    if (
        type(start) is not int
        or start < 1
        or type(skip) is not int
        or skip < 0
        or type(limit) is not int
        or limit < 1
        or limit > 100
    ):
        return SegmentResult(error="Invalid prose search window/offset/limit")
    try:
        rx = regex.compile(pattern, regex.IGNORECASE)
    except regex.error:
        return SegmentResult(error="Invalid prose search pattern")
    try:
        hits = []
        misses = []
        evidence = []
        count = 0
        limit = min(18, limit)
        with open_pdf(body) as doc:
            total = len(doc)
            end = min(total, start + WINDOW - 1)
            if start > total:
                return SegmentResult(error="Search start outside PDF")
            for pg in range(start, end + 1):
                page = doc[pg - 1]
                reason = None
                found = []
                try:
                    tp = page.get_textpage()
                    try:
                        if tp.count_chars() > PAGE_CHAR_LIMIT:
                            reason = "page_char_budget"
                            raw = ""
                        else:
                            raw = tp.get_text_bounded().replace("\r\n", "\n").strip()
                    finally:
                        tp.close()
                except Exception:
                    raw = ""
                    reason = "native_read_failed"
                finally:
                    page.close()
                if not raw:
                    misses.append({"page": pg, "reason": reason or "empty_native_text"})
                    continue
                # Collapse whitespace only, separately per physical page; map every match back to the exact raw string.
                chars = []
                offsets = []
                for i, c in enumerate(raw):
                    if c.isspace():
                        if chars and chars[-1] == " ":
                            continue
                        c = " "
                    offsets.append(i)
                    chars.append(c)
                offsets.append(len(raw))
                normalized = "".join(chars)
                local_count = 0
                try:
                    for m in rx.finditer(normalized, timeout=0.10):
                        if m.end() <= m.start():
                            continue
                        if skip <= count + local_count < skip + limit:
                            a, b = offsets[m.start()], offsets[m.end()]
                            left = max(0, a - 140)
                            right = min(len(raw), b + 140, left + 800)
                            found.append(
                                (
                                    {
                                        "page": pg,
                                        "raw_start": a,
                                        "raw_end": b,
                                        "excerpt_start": left,
                                        "excerpt_end": right,
                                        "match_truncated": b > right,
                                    },
                                    raw[left:right],
                                )
                            )
                        local_count += 1
                except TimeoutError:
                    misses.append({"page": pg, "reason": "regex_timeout"})
                    continue
                for h, excerpt in found:
                    hits.append(h)
                    evidence.append(f"[Page {pg}]\n{excerpt}")
                count += local_count
        more = skip + len(hits) < count
        extras = {
            "pdf_text_view": "prose",
            "search_mode": "window",
            "window_start": start,
            "window_end": end,
            "total_pages": total,
            "unread_pages": misses,
            "hits": hits,
            "window_match_count": count,
            "match_offset": skip,
            "coverage": "partial"
            if start != 1 or end != total or misses
            else "all_native_pages_in_this_call",
        }
        if more:
            extras["next_search"] = {
                "pdf_text_view": "prose",
                "pattern": pattern,
                "search_from_page": start,
                "match_offset": skip + len(hits),
                "max_matches": limit,
            }
        elif end < total:
            extras["next_search"] = {
                "pdf_text_view": "prose",
                "pattern": pattern,
                "search_from_page": end + 1,
                "max_matches": limit,
            }
        if not hits:
            error = (
                "No further hits at this offset"
                if skip and count
                else "No matches in inspected text of this window"
            )
            return SegmentResult(error=error + "; not proof of absence from paper.", extras=extras)
        return SegmentResult(text="\n\n".join(evidence), extras=extras)
    except Exception as exc:
        return SegmentResult(
            error=f"Could not search PDF ({type(exc).__name__}); coverage unknown."
        )
