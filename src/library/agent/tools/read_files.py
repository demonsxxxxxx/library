"""read_files — agent tool, dispatcher over Pipeline.read_segment.

Accepts a list of `requests`, each:

  {
    "entry_id": "...",
    "reads": [
      // any of the args fields a pipeline understands
      { "offset": 0, "max_chars": 8000 },
      { "section_id": "s3" },
      { "heading": "Algorithm" },
      { "line_start": 100, "line_end": 150 },
      { "page_start": 3, "page_end": 5 },          // PDF only
      { "page_label": "54" },                      // PDF printed label
      { "paragraph_start": 4, "paragraph_end": 12 }, // DOCX only
      { "slide_start": 2, "slide_end": 4 },          // PPTX only
      { "pattern": "leader.*election", "context_lines": 3, "max_matches": 10 },
      { "member_path": "papers/raft.pdf", "page_start": 4 }   // container
    ]
  }

Each `reads` entry is an args dict handed to `pipeline.read_segment`.
Pipelines pick the fields they understand and ignore the rest. If a
read item has fields the pipeline doesn't support, it returns
`error="..."` and the caller's `ok` is False for that item.

Same-entry requests share one DB lookup but each `reads` item triggers
a fresh storage round-trip — pipelines are responsible for their own
caching if desired.

`entry_id` may be a full uuid OR an unambiguous short prefix (>= 8 hex
chars). The model often sees 8-char prefixes in the activity bar and
parrots them back; resolve_entry_prefix promotes them to a full uuid
when there's exactly one match, and surfaces an ambiguity error when
multiple entries share the prefix.
"""
from __future__ import annotations

from secrets import token_hex
from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from library.agent.numeric_evidence import SHA256_RE
from library.agent.read_compression import (
    CompressionSettings,
    compress_read_text,
)
from library.agent.tools import ToolContext, tool
from library.config import get_settings
from library.db.models import File, FileEntry
from library.pipelines.base import Pipeline, SegmentResult
from library.pipelines.registry import resolve_pipeline
from library.repositories import entries as entries_repo
from library.storage import get_storage

MAX_EFFECTIVE_READS_PER_ENTRY = 50

_LOCATOR_VALUE_KEYS = {
    "heading",
    "pattern",
    "member_path",
    "section_id",
    "page_label",
    "question",
    "image_id",
}


SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    # Some tool providers disallow top-level oneOf. Enforce exclusivity in the handler.
    "properties": {
        "cursor": {"type": "string", "description": "Resume one issued evidence cursor in this turn; omit requests."},
        "requests": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["entry_id"],
                "properties": {
                    "entry_id": {
                        "type": "string",
                        "description": (
                            "Entry UUID (or short hex prefix, ≥ 8 chars). "
                            "NOT a file name or display_name — get it from "
                            "search_metadata / list_folder first."
                        ),
                    },
                    "reads": {
                        "type": "array",
                        "maxItems": 10,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                # generic chunking
                                "offset": {"type": "integer", "minimum": 0},
                                "max_chars": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 16000,
                                },
                                "compress": {
                                    "type": "boolean",
                                    "description": (
                                        "Set false to reopen an omitted "
                                        "compressed region exactly, without "
                                        "read_files result compression."
                                    ),
                                },
                                # text-shaped
                                "line_start": {"type": "integer", "minimum": 1},
                                "line_end": {"type": "integer", "minimum": 1},
                                "section_id": {"type": "string"},
                                "heading": {"type": "string"},
                                # PDF
                                "search_from_page": {"type":"integer", "minimum":1, "description":"Resume a windowed prose pattern search with the next_search arguments from its prior result."},
                                "pdf_text_view": {
                                    "type": "string", "enum": ["prose"],
                                    "description": "Native reading order. To find prose in a PDF before knowing its page, set prose and pattern, omit page_start/end. Reopen hits with prose and a single physical page_start. For tables omit the view. Not pixel evidence.",
                                },
                                "page_start": {"type": "integer", "minimum": 1},
                                "page_end": {"type": "integer", "minimum": 1},
                                "page_label": {
                                    "type": "string",
                                    "description": (
                                        "PDF only: printed/logical page label "
                                        "from the PDF's page-label metadata. "
                                        "Prefer page_start/page_end from prior "
                                        "read_files results when available."
                                    ),
                                },
                                "numeric_check": {
                                    "type": "object", "additionalProperties": False,
                                    "required": ["locator", "candidate"],
                                    "description": "PDF only. Check an exact candidate against pixels of one physical page_start, even with a readable text layer.",
                                    "properties": {
                                        "locator": {"type": "string", "maxLength": 200,
                                                    "description": "Location or label only, without the proposed answer."},
                                        "candidate": {"type": "string", "maxLength": 300,
                                                      "description": "Exact field including signs, exponents, operands and units as applicable."},
                                        "region": {"type": "array", "items": {"type": "number", "minimum": 0, "maximum": 1},
                                                   "minItems": 4, "maxItems": 4, "description": "Optional [left,top,right,bottom] fractions of displayed page."},
                                    },
                                },
                                # DOCX
                                "paragraph_start": {"type": "integer", "minimum": 1},
                                "paragraph_end": {"type": "integer", "minimum": 1},
                                # PPTX
                                "slide_start": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "PPTX only: first 1-indexed slide to read."
                                    ),
                                },
                                "slide_end": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "PPTX only: last 1-indexed slide to read."
                                    ),
                                },
                                # Embedded images in document-shaped files
                                "image_id": {
                                    "type": "string",
                                    "description": (
                                        "DOCX/PPTX image question mode: exact "
                                        "embedded image id from a prior read, "
                                        "such as docx-img-1 or pptx-img-2."
                                    ),
                                },
                                "image_index": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "description": (
                                        "DOCX/PPTX image question mode: "
                                        "1-indexed embedded image number."
                                    ),
                                },
                                # pattern search
                                "pattern": {"type": "string"},
                                "patterns": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "maxItems": 10,
                                    "description": (
                                        "Multiple patterns to search in the same scope. "
                                        "The tool expands this into one pattern read per "
                                        "term."
                                    ),
                                },
                                "context_lines": {"type": "integer", "minimum": 0},
                                "max_matches": {
                                    "type": "integer",
                                    "minimum": 1,
                                    "maximum": 100,
                                },
                                "match_offset": {
                                    "type": "integer",
                                    "minimum": 0,
                                    "description": (
                                        "Skip first N pattern hits — "
                                        "page through matches with "
                                        "next_match_offset from prior call."
                                    ),
                                },
                                # container
                                "member_path": {"type": "string"},
                                # VLM-on-read: required for image entries.
                                # For documents, source text remains primary;
                                # pixels are inspected only when the requested
                                # range has no readable text.
                                "question": {
                                    "type": "string",
                                    "minLength": 1,
                                    "description": (
                                        "REQUIRED for image entries. For PDF, "
                                        "DOCX, and PPTX, omit it for ordinary "
                                        "text reads; include it only when a "
                                        "range without readable text needs "
                                        "visual inspection. Combine with `page_start`/"
                                        "`page_end`, `slide_start`/`slide_end`, "
                                        "`paragraph_start`/`paragraph_end`, "
                                        "`image_id`, or `image_index` to scope "
                                        "the question."
                                    ),
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


@tool(
    name="read_files",
    description=(
        "Open file contents for one or more entries. Each request lists "
        "one or more `reads`, each addressing a slice via fields the "
        "pipeline understands (offset/max_chars for any file; "
        "page_start/page_end or page_label for PDF; "
        "slide_start/slide_end for PPTX; "
        "line_start/line_end / section_id / "
        "heading for text; pattern or patterns for regex search; member_path for "
        "container members). `pattern` can be combined with a range "
        "(page_start/end, line_start/end, paragraph_start/end, or "
        "spreadsheet heading) to restrict the search to that window — "
        "useful for a long file where the same term appears many times. "
        "PPTX `pattern` can be combined with slide_start/slide_end. "
        "Pattern hits are paginated via `match_offset` (use the "
        "`next_match_offset` from a previous response). Pass `question` "
        "for image entries or when a requested document range has no readable "
        "text and needs visual inspection. Source text is always preferred "
        "and preserved before OCR or vision. For ordinary PDF, DOCX, or PPTX "
        "text, shapes, and tables, omit `question`; for OCR-indexed PDFs, omit "
        "it to read stored OCR text. "
        "For scientific prose in a selected PDF, first use pdf_text_view=prose + pattern WITHOUT page numbers to find relevant passages. Read the matched physical page with the same prose view. Follow next_search for outstanding windows before claiming completeness; disclose unread pages. "
        "For critical numeric fields use numeric_check on one PDF page. Resume remaining evidence using cursor alone. "
        "Use AFTER read_entries_metadata identified relevant sections."
    ),
    schema=SCHEMA,
)
async def read_files(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    if "cursor" in args:
        if "requests" in args:
            return {"ok": False, "error": "Pass cursor or requests, not both"}
        if ctx.read_delivery is None:
            return {"ok": False, "error": "Continuation requires the originating agent turn"}
        return await ctx.read_delivery.resume(db, args["cursor"])
    requests = list(args.get("requests") or [])
    if not requests:
        return {"ok": False, "error": "Pass non-empty requests or a continuation cursor"}

    # Resolve each entry_id to a full uuid. Accept full ids and short
    # hex prefixes (>= 8 chars). Ambiguous or unknown prefixes get a
    # per-entry error result so the agent stops looping on a fabricated
    # / parroted short id.
    resolved: list[tuple[Mapping[str, Any], str]] = []
    early_results: list[dict[str, Any]] = []
    for req in requests:
        raw = (req.get("entry_id") or "").strip() if isinstance(req, dict) else ""
        if not raw:
            early_results.append({
                "ok": False, "entry_id": "", "error": "missing entry_id",
            })
            continue
        full, err = await entries_repo.resolve_entry_id_prefix(db, raw)
        if err:
            early_results.append({"ok": False, "entry_id": raw, "error": err})
            continue
        resolved.append((req, full))

    entry_ids = [eid for _, eid in resolved]
    rows = await entries_repo.list_with_file_by_ids_any(db, entry_ids)
    by_entry: dict[str, tuple[FileEntry, File]] = {
        e.id: (e, f) for e, f in rows
    }

    storage = get_storage()
    settings = get_settings()
    compression_settings = CompressionSettings(
        enabled=settings.compression_enabled,
        min_chars=settings.compression_min_chars,
        target_chars=settings.compression_target_chars,
        context_chars=settings.compression_context_chars,
        max_ratio=settings.compression_max_ratio,
    )
    results: list[dict[str, Any]] = list(early_results)

    for req, eid in resolved:
        pair = by_entry.get(eid)
        if pair is None:
            results.append({
                "ok": False, "entry_id": eid, "error": "entry not found",
            })
            continue
        entry, file_row = pair

        if entry.deleted_at is not None or file_row.deleted_at is not None:
            results.append({"ok": False, "entry_id": eid, "error": "entry no longer live"})
            continue
        if file_row.ingest_status != "done":
            results.append({
                "ok": False, "entry_id": eid,
                "display_name": entry.display_name,
                "error": (
                    f"ingest_status={file_row.ingest_status}; "
                    "file is not yet readable"
                ),
            })
            continue

        pipeline = resolve_pipeline(
            file_row.mime_type, file_row.original_ext,
            filename=entry.display_name,
        )
        if pipeline is None:
            results.append({
                "ok": False, "entry_id": eid,
                "display_name": entry.display_name,
                "error": (
                    f"no pipeline registered for mime={file_row.mime_type} "
                    f"ext={file_row.original_ext}"
                ),
            })
            continue

        reads_args = list(req.get("reads") or [{}])  # default: full chunk-read
        result_obj: dict[str, Any] = {
            "ok": True,
            "entry_id": eid,
            "display_name": entry.display_name,
            "kind": file_row.kind,
            "pipeline": pipeline.name,
            "reads": [],
        }
        any_failed = False
        expanded_reads = _expand_reads(reads_args)
        if len(expanded_reads) > MAX_EFFECTIVE_READS_PER_ENTRY:
            result_obj["ok"] = False
            result_obj["reads"].append({
                "ok": False,
                "error": (
                    f"too many effective reads after pattern expansion "
                    f"({len(expanded_reads)} > {MAX_EFFECTIVE_READS_PER_ENTRY})"
                ),
            })
            results.append(result_obj)
            continue

        for read_args in expanded_reads:
            if 'pdf_text_view' in read_args and pipeline.name != 'pdf':
                result_obj['reads'].append({'ok': False, 'args': read_args, 'error': 'pdf_text_view needs a PDF entry'})
                any_failed = True
                continue
            if "numeric_check" in read_args and pipeline.name != "pdf":
                result_obj["reads"].append({"ok": False, "args": read_args, "error": "numeric_check needs a live PDF entry"})
                any_failed = True
                continue
            seg = await _safe_read_segment(
                pipeline=pipeline, file_row=file_row,
                args=dict(read_args), storage=storage,
            )
            entry_dict: dict[str, Any] = {
                "ok": seg.error is None,
                "args": read_args,
            }
            if seg.error:
                entry_dict["error"] = seg.error
                any_failed = True
            else:
                extras = dict(seg.extras or {})
                if pipeline.name == 'pdf' and '/C0' in seg.text:
                    extras['numeric_warning'] = ('Ambiguous PDF glyphs. Do not infer a sign or exponent. '
                                                 'Use numeric_check on the pertinent physical page.')
                compression = compress_read_text(
                    seg.text,
                    entry_id=eid,
                    args=dict(read_args),
                    extras=extras,
                    pipeline=pipeline.name,
                    kind=file_row.kind,
                    query=ctx.user_message,
                    source_name=str(read_args.get("member_path") or entry.display_name or ""),
                    source_ext=str(file_row.original_ext or ""),
                    settings=compression_settings,
                )
                entry_dict["text"] = compression.text
                sha = extras.get("file_sha256")
                if (ctx.read_delivery is not None and extras.get("source") == "pdf_page_pixels"
                        and extras.get("numeric_status") == "pixel_agreement"
                        and isinstance(sha, str) and SHA256_RE.fullmatch(sha)):
                    entry_dict.update(evidence_id=token_hex(16), file_id=file_row.id, text=seg.text)
                elif compression.compressed:
                    extras["read_compression"] = compression.metadata()
                if extras:
                    entry_dict["extras"] = extras
            if seg.extras and "extras" not in entry_dict:
                entry_dict["extras"] = seg.extras
            result_obj["reads"].append(entry_dict)
        if any_failed:
            result_obj["ok"] = False
        results.append(result_obj)

    overall_ok = all(r.get("ok") for r in results)
    payload = {"ok": overall_ok, "results": results, "count": len(results)}
    return ctx.read_delivery.start(payload) if ctx.read_delivery is not None else payload


def _expand_reads(reads_args: list[Any]) -> list[dict[str, Any]]:
    expanded: list[dict[str, Any]] = []
    for raw in reads_args:
        read_args = dict(raw) if isinstance(raw, Mapping) else {}
        raw_patterns = read_args.get("patterns")
        patterns: list[str] = []
        if read_args.get("pattern"):
            patterns.append(str(read_args["pattern"]))
        if isinstance(raw_patterns, list):
            patterns.extend(str(item) for item in raw_patterns if str(item))

        if not raw_patterns:
            read_args.pop("patterns", None)
            expanded.append(read_args)
            continue

        seen: set[str] = set()
        for pattern in patterns:
            if pattern in seen:
                continue
            seen.add(pattern)
            item = dict(read_args)
            item.pop("patterns", None)
            item["pattern"] = pattern
            expanded.append(item)
    return expanded


def _locator_diagnostic(args: Mapping[str, Any]) -> dict[str, Any]:
    """Return read locator shape without logging user/file content values."""
    diag: dict[str, Any] = {}
    numeric_keys = (
        "offset",
        "max_chars",
        "line_start",
        "line_end",
        "page_start",
        "page_end",
        "paragraph_start",
        "paragraph_end",
        "slide_start",
        "slide_end",
        "image_index",
        "context_lines",
        "max_matches",
        "match_offset",
    )
    for key in numeric_keys:
        value = args.get(key)
        if isinstance(value, int):
            diag[key] = value
    for key in _LOCATOR_VALUE_KEYS:
        if args.get(key):
            diag[f"has_{key}"] = True
    patterns = args.get("patterns")
    if isinstance(patterns, list):
        diag["patterns_count"] = len(patterns)
    return diag


def _segment_diagnostic(extras: Mapping[str, Any]) -> dict[str, Any]:
    """Return segment metadata shape without copying content-bearing values."""
    diag: dict[str, Any] = {}
    scalar_keys = (
        "offset",
        "char_count",
        "total_chars",
        "line_start",
        "line_end",
        "page_start",
        "page_end",
        "total_pages",
        "paragraph_start",
        "paragraph_end",
        "total_paragraphs",
        "slide_start",
        "slide_end",
        "image_index",
        "total_slides",
        "match_count",
        "total_matches",
        "match_offset",
        "next_match_offset",
        "next_offset",
    )
    for key in scalar_keys:
        if key not in extras:
            continue
        value = extras.get(key)
        if isinstance(value, (bool, int, float, str)) or value is None:
            diag[key] = value
    for key in ("truncated", "has_more", "read_truncated", "source_truncated"):
        value = extras.get(key)
        if isinstance(value, bool):
            diag[key] = value
    count_keys = {
        "available_sheets": "available_sheets_count",
        "available": "available_members_count",
    }
    for key, out_key in count_keys.items():
        value = extras.get(key)
        if isinstance(value, list):
            diag[out_key] = len(value)
    return diag


def _error_diagnostic(error: str) -> str:
    lowered = error.casefold()
    if "not found" in lowered:
        return "not found"
    if "invalid regex" in lowered:
        return "invalid regex"
    if "integer" in lowered or "must be" in lowered:
        return "invalid locator"
    if "unsupported" in lowered or "does not support" in lowered:
        return "unsupported"
    if "empty result" in lowered or "no matches" in lowered:
        return "empty"
    return "error"


async def _safe_read_segment(
    *,
    pipeline: Pipeline,
    file_row: File,
    args: dict[str, Any],
    storage,
) -> SegmentResult:
    try:
        return await pipeline.read_segment(
            file_row=file_row, args=args, storage=storage,
        )
    except Exception as exc:  # noqa: BLE001
        return SegmentResult(
            error=f"{pipeline.name} read_segment crashed: {exc!r}",
        )
