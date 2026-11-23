"""Validated citation checkpoints for explicit research finalization."""
from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

from library.citations import quote_matches_source_text
from library.llm import ToolCall


MISSING_CITATIONS_ERROR = (
    "finish_research(sufficient) requires citations selected from successful "
    "read_files source evidence"
)
_CITABLE_ENTRY_ID_RE = re.compile(r"^[0-9a-fA-F][0-9a-fA-F-]{6,35}$")


def prepare_finish_citation_manifest(
    tool_call: ToolCall,
    prior_tool_calls: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Validate declared citations against source text visible to the model."""
    if tool_call.name != "finish_research":
        return [], None
    status = str(tool_call.arguments.get("evidence_status") or "")
    declared = tool_call.arguments.get("citations")
    raw_citations = declared if isinstance(declared, list) else []
    evidence = _read_file_evidence(prior_tool_calls)
    if status != "sufficient":
        if raw_citations:
            return [], _finish_error(
                "finish_research(insufficient) must not declare citations",
                guard="unexpected_citations",
            )
        return [], None
    if evidence and not raw_citations:
        return [], _finish_error(
            MISSING_CITATIONS_ERROR,
            guard="missing_citations",
        )
    if raw_citations and not evidence:
        return [], _finish_error(
            "finish_research citations require successful read_files source evidence",
            guard="citations_without_source_read",
        )

    manifest: list[dict[str, Any]] = []
    seen: set[tuple[str, str, int | None]] = set()
    for index, raw in enumerate(raw_citations):
        if not isinstance(raw, Mapping):
            return [], _citation_error(index, "citation must be an object")
        entry_id = str(raw.get("entry_id") or "").strip()
        quote = _single_line(raw.get("quote"))
        reason = _single_line(raw.get("reason"))
        page = _positive_int(raw.get("page"))
        if not entry_id:
            return [], _citation_error(index, "entry_id is required")
        if _CITABLE_ENTRY_ID_RE.fullmatch(entry_id) is None:
            return [], _citation_error(index, "entry_id is not a citable entry ID")
        if not quote:
            return [], _citation_error(index, "quote is required")
        if not reason:
            return [], _citation_error(index, "reason is required")
        if raw.get("page") is not None and page is None:
            return [], _citation_error(index, "page must be a positive integer")

        matching_reads = [
            item
            for item in evidence.get(entry_id, [])
            if _matches_read(item, quote)
        ]
        if not matching_reads:
            return [], _citation_error(
                index,
                "quote was not visible in a successful read_files result for entry_id",
                guard="citation_quote_not_found",
            )
        supported_pages = {
            supported
            for item in matching_reads
            for supported in _supported_pages(item, quote)
        }
        if page is not None and (not supported_pages or page not in supported_pages):
            return [], _citation_error(
                index,
                "page was not supported by the read_files range containing the quote",
                guard="citation_page_not_verified",
            )
        resolved_page = page
        if resolved_page is None and len(supported_pages) == 1:
            resolved_page = next(iter(supported_pages))
        key = (entry_id, quote, resolved_page)
        if key in seen:
            continue
        seen.add(key)
        item: dict[str, Any] = {
            "marker": _citation_marker(len(manifest)),
            "entry_id": entry_id,
            "quote": quote,
            "reason": reason,
            "source": "read_files",
        }
        if resolved_page is not None:
            item["page"] = resolved_page
        manifest.append(item)
    return manifest, None


def attach_citation_manifest(
    answer: str,
    manifest: Sequence[Mapping[str, Any]],
) -> str:
    """Attach canonical definitions and fallback markers to a final answer."""
    items = [dict(item) for item in manifest if item.get("marker") and item.get("entry_id")]
    if not answer.strip() or not items:
        return answer
    text = answer.rstrip()
    for item in items:
        marker = re.escape(str(item["marker"]))
        text = re.sub(
            rf"(?m)^\[\^{marker}\]:[^\n]*(?:\n|$)",
            "",
            text,
        ).rstrip()
    missing = [
        str(item["marker"])
        for item in items
        if not re.search(rf"\[\^{re.escape(str(item['marker']))}\]", text)
    ]
    if missing:
        label = "来源" if _contains_cjk(text) else "Sources"
        text += f"\n\n{label}：" if label == "来源" else f"\n\n{label}: "
        text += " ".join(f"[^{marker}]" for marker in missing)
    definitions = "\n".join(_render_footnote(item) for item in items)
    return f"{text}\n\n{definitions}"


def _read_file_evidence(
    prior_tool_calls: Sequence[Mapping[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    evidence: dict[str, list[dict[str, Any]]] = {}
    for call in prior_tool_calls:
        if str(call.get("name") or "") != "read_files" or _tool_call_failed(call):
            continue
        output = _tool_call_output(call)
        if not isinstance(output, Mapping):
            continue
        results = output.get("results")
        if not isinstance(results, list):
            continue
        for result in results:
            if not isinstance(result, Mapping) or result.get("ok") is False:
                continue
            entry_id = str(result.get("entry_id") or "").strip()
            reads = result.get("reads")
            if not entry_id or not isinstance(reads, list):
                continue
            for read in reads:
                if not isinstance(read, Mapping) or read.get("ok") is False:
                    continue
                # New pixel evidence is cited through structured numeric_claims.
                # Do not let a prose citation bypass its version and page checks.
                if read.get("evidence_id"):
                    continue
                text = str(read.get("text") or "").strip()
                if text:
                    evidence.setdefault(entry_id, []).append(dict(read))
    return evidence


def _matches_read(read: Mapping[str, Any], quote: str) -> bool:
    extras = read.get("extras") or {}
    if extras.get("source") == "pdf_page_pixels":
        # A numeric transcription must not pass prose normalization, which
        # folds superscripts, punctuation and case. Ignore the page header.
        _, _, field = str(read.get("text") or "").partition("\n")
        return (extras.get("numeric_status") == "pixel_agreement"
                and _single_line(field) == quote)
    return quote_matches_source_text(str(read.get("text") or ""), quote)


def _supported_pages(read: Mapping[str, Any], quote: str) -> set[int]:
    text = str(read.get("text") or "")
    marked = _marked_pages_containing_quote(text, quote)
    if marked:
        return marked
    args = read.get("args") if isinstance(read.get("args"), Mapping) else {}
    extras = read.get("extras") if isinstance(read.get("extras"), Mapping) else {}
    for start_key, end_key in (
        ("page_start", "page_end"),
        ("slide_start", "slide_end"),
    ):
        start = (
            _positive_int(read.get(start_key))
            or _positive_int(args.get(start_key))
            or _positive_int(extras.get(start_key))
        )
        end = (
            _positive_int(read.get(end_key))
            or _positive_int(args.get(end_key))
            or _positive_int(extras.get(end_key))
        )
        if start is not None and end is None:
            end = start
        if start is not None and end is not None and end >= start:
            return {start} if start == end else set()
    return set()


_PAGE_OR_SLIDE_RE = re.compile(
    r"(?m)^(?:\[(?:OCR )?Page (?P<page>\d+)\]"
    r"|#{1,3} (?:OCR )?Page (?P<heading_page>\d+)"
    r"|# Slide (?P<slide>\d+)(?::[^\n]*)?)\s*$"
)


def _marked_pages_containing_quote(text: str, quote: str) -> set[int]:
    matches = list(_PAGE_OR_SLIDE_RE.finditer(text))
    supported: set[int] = set()
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        if not quote_matches_source_text(text[match.end() : end], quote):
            continue
        value = (
            match.group("page")
            or match.group("heading_page")
            or match.group("slide")
        )
        if value:
            supported.add(int(value))
    return supported


def _tool_call_output(call: Mapping[str, Any]) -> Any:
    return call.get("output") if "output" in call else call.get("result")


def _tool_call_failed(call: Mapping[str, Any]) -> bool:
    if call.get("is_error") is True or call.get("error"):
        return True
    output = _tool_call_output(call)
    return isinstance(output, Mapping) and (
        output.get("ok") is False or bool(output.get("error"))
    )


def _citation_marker(index: int) -> str:
    value = index + 1
    marker = ""
    while value:
        value, remainder = divmod(value - 1, 26)
        marker = chr(ord("a") + remainder) + marker
    return marker


def _render_footnote(item: Mapping[str, Any]) -> str:
    marker = str(item["marker"])
    entry_id = str(item["entry_id"])
    quote = _escape_quote(str(item.get("quote") or ""))
    reason = _single_line(item.get("reason"))
    page = _positive_int(item.get("page"))
    fields = f'entry_id={entry_id}, quote="{quote}"'
    if page is not None:
        fields += f", page={page}"
    return f"[^{marker}]: {fields} - {reason}"


def _escape_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _single_line(value: object) -> str:
    return " ".join(str(value or "").split())


def _positive_int(value: object) -> int | None:
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _contains_cjk(text: str) -> bool:
    return any("\u4e00" <= char <= "\u9fff" for char in text)


def _finish_error(message: str, *, guard: str) -> dict[str, Any]:
    return {"error": message, "retryable": True, "guard": guard}


def _citation_error(
    index: int,
    message: str,
    *,
    guard: str = "invalid_citation",
) -> dict[str, Any]:
    return _finish_error(f"citation[{index}] {message}", guard=guard)


__all__ = [
    "MISSING_CITATIONS_ERROR",
    "attach_citation_manifest",
    "prepare_finish_citation_manifest",
]
