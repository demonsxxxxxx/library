"""PDF pipeline (DESIGN.md §11.3).

Handles application/pdf and `.pdf`. Strategy: pypdf extracts the text
layer page by page; significant images are concurrently described by
the vision LLM and inlined as `[Figure N.M] ...` lines next to their
pages; the assembled body then goes through the same tagged-response
indexing prompt as the text pipeline, but with page anchors in
`<sections>`.

PDFs without a text layer (scanned images) run the VLM OCR fallback.
The extracted OCR text is indexed like a PDF and stored as page/block
text so read_segment can serve pattern/page/section reads without
another vision call.

read_segment supports page_start / page_end ranges, regex pattern
search across pages, and the generic offset/max_chars chunking.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import json
import logging
import math
import re
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Any

from library.agent.compression_adapter import (
    maybe_compress_ingest_aggregate_view,
    maybe_compress_ingest_view,
)
from library.config import get_settings, has_vision_profile
from library.llm import (
    ChatMessage,
    ChatRequest,
    ImageBlock,
    TextBlock,
    cacheable_prompt_messages,
    get_chat_client,
)
from library.llm.model_controls import DISABLE_THINKING_EXTRA_BODY
from library.llm.tagged_response import (
    render_format_hint,
    render_sections_hint,
)
from library.pipelines.base import (
    Pipeline,
    PipelineContext,
    PipelineResult,
    SegmentResult,
)
from library.pipelines._long_index import (
    build_retrieval_extra,
    fallback_section,
    ingest_output_tokens,
    llm_ingest_concurrency,
    parse_index_response,
    render_sections_digest,
    renumber_sections,
)
from library.pipelines.image import downscale_for_vlm
from library.pipelines.document_vision import prefer_source_text_for_question
from library.pipelines.pdf_text import (
    extract_pdf_page_labels,
    extract_pdf_text_range,
    pdf_page_count,
    resolve_page_label,
)
from library.pipelines.registry import register_pipeline
from library.storage.base import StorageBackend
from library.tasks.usage import measure_stage

log = logging.getLogger(__name__)

MAX_PAGES = 60                    # legacy single-prompt page cap
MAX_TOTAL_TEXT_BYTES = 80_000     # ≈ 25-30k tokens cap
PDF_CHUNK_PAGES = 40              # long-doc page window for per-chunk indexing
PDF_TEXT_MAX_INDEX_PAGES = 400    # hard text-layer ingest budget
PDF_SECTION_DIGEST_BYTES = 60_000 # cap the aggregate summary prompt
MIN_TEXT_PER_PAGE_FOR_TEXT_LAYER = 50  # if every page yields fewer chars,
                                       # the doc is probably scanned
# Per-document OCR page cap for scanned PDFs. Seeded from the configurable
# `ocr_max_pages` setting (env OCR_MAX_PAGES) so the default is bounded rather
# than fanning out one VLM call per page for arbitrarily long documents.
# None/<=0 means OCR every page at ingest. Kept as a module-level constant
# (rather than reading settings inside _ocr_configured_page_cap) so tests and
# callers can override it directly.
OCR_MAX_PAGES: int | None = get_settings().ocr_max_pages
PDF_READ_MAX_PAGES_PER_CALL = 50
PDF_PATTERN_UNSCOPED_MAX_PAGES = 200
PDF_DEFAULT_READ_PAGES = 20
OCR_BLOCK_MAX_CHARS = 8_000
OCR_RENDER_BATCH_PAGES = 20
OCR_RENDER_DPI = 200              # JPEG render DPI before VLM (sweet spot)
OCR_VLM_MAX_LONG_EDGE = 2048      # OCR is glyph-sensitive — keep more
                                  # detail than the caption path's 1568
PDF_VISION_MAX_DPI = 150.0
PDF_VISION_MAX_PIXELS = 20_000_000
PDF_VISION_JPEG_QUALITIES = (80, 75, 70, 65, 60)
PDF_VISION_MAX_REQUEST_CHARS = 1_000_000
PDF_VISION_REQUEST_OVERHEAD_CHARS = 100_000
PDF_VISION_MAX_QUESTION_CHARS = 40_000
PDF_VISION_DATA_URL_PREFIX = "data:image/jpeg;base64,"
PDF_VISION_MAX_DATA_URL_CHARS = (
    PDF_VISION_MAX_REQUEST_CHARS - PDF_VISION_REQUEST_OVERHEAD_CHARS
)
PDF_VISION_MAX_BASE64_CHARS = (
    (PDF_VISION_MAX_DATA_URL_CHARS - len(PDF_VISION_DATA_URL_PREFIX)) // 4
) * 4
PDF_VISION_MAX_JPEG_BYTES = PDF_VISION_MAX_BASE64_CHARS // 4 * 3
PDF_VISION_MAX_PAGES_PER_BATCH = 3
PDF_VISION_QUESTION_MAX_PAGES = 5
PDF_VISION_SCAN_COVERAGE_THRESHOLD = 0.80
PDF_VISION_SCAN_MIN_PIXELS = 500_000


PDF_OCR_PROMPT = """You are an OCR assistant. Extract all body text from the provided document image and output pure Markdown in the document's own language.

Rules:
1. Ignore page headers, footers, and page numbers.
2. Preserve paragraph and heading hierarchy where visible.
3. Use Markdown table syntax for tables.
4. Use LaTeX for math (wrapped with $ or $$).
5. Output ONLY the extracted text. No HTML, no preamble, no commentary.
6. Do not describe your process. Do not include analysis, checklists, labels like "Transcription:", or <think> blocks.
7. If the page has no recognisable text content, reply only with: No text content."""


PDF_PIPELINE_SYSTEM = """You are Library's PDF document indexer.

You receive the indexed text of a PDF, page-by-page. It may be only the
first `indexed_pages` of a longer PDF; use only the pages provided and do
not infer content from missing pages. Produce a structured index that lets a
downstream agent decide whether to retrieve the document and find the
relevant page.

`summary` is one or two sentences (<=60 Chinese characters / <=30 English words) in the
document's own language — the spine of what the document is and why a
reader would open it. Keep it tight; depth belongs in `description`.
`description` is a free-text walk-through of the document's structure and
key points. `sections` lists every meaningful section/heading; each line
takes the form `id | <pages X-Y> | title | one-or-two-sentence summary |
term1, term2, term3`. Pages are 1-indexed and inclusive. `extra` carries
cross-cutting machine-readable insights as `key: value` lines (one per
line; leave the block empty if nothing notable). `entry_extra` is the
same shape but for position-aware insights. `entry_catalog_path` is a
best-guess classification path. `tags` are 3-10 facet:name pairs; valid
facets are topic | form | time | source | language | extra.

""" + render_format_hint() + "\n" + render_sections_hint(
    anchor_unit="pages", anchor_example="pages 4-7",
)


PDF_CHUNK_SYSTEM = """You are Library's PDF section indexer.

You receive one page range from a larger PDF. Produce a local index for this
range only. Use the original page numbers shown in the `### Page N` markers.

`summary` briefly states what this range covers. `description` can add a
short walk-through. `sections` is required and should cover every meaningful
heading or logical chunk in the provided range. Keep key terms useful for
later retrieval.

""" + render_format_hint() + "\n" + render_sections_hint(
    anchor_unit="pages", anchor_example="pages 401-425",
)


PDF_AGGREGATE_SYSTEM = """You are Library's PDF aggregate indexer.

You receive a precomputed section map for the indexed portion of a PDF. Do NOT
read or invent outside that map. If `coverage.indexed_partial` is true, make
the limited coverage clear and do not imply that later pages were reviewed.
Produce only file-level fields: summary, description, extra, entry_extra,
catalog_path, and tags. Do not output a sections block; the caller will
preserve the section map separately in `description.sections`.

Make `extra` retrieval-friendly: include important alternate names, recurring
technical terms, and high-value page ranges from the section map.

""" + render_format_hint()


# Schema kept for legacy callers but no longer fed to the LLM.
PDF_PIPELINE_SCHEMA: dict[str, Any] = {}


class PdfNeedsOcrError(Exception):
    """Raised when the OCR fallback itself failed (e.g. VLM unavailable
    or returned only empty pages). Kept for the dispatcher to mark the
    file as 'failed' with reason 'needs_ocr' so the user can retry once
    the VLM is back up. The text-layer-missing case no longer raises —
    it triggers the OCR path automatically."""

    def __init__(self, *, total_pages: int, total_chars: int) -> None:
        super().__init__(
            f"PDF has no usable text layer "
            f"(pages={total_pages}, chars={total_chars}); needs OCR."
        )
        self.total_pages = total_pages
        self.total_chars = total_chars


_NO_TEXT_LAYER_ERROR = (
    "PDF has no usable text layer; pages may be scanned images. "
    "Pass the `question` parameter to read it with the vision model."
)


@register_pipeline(
    mimes=("application/pdf",),
    exts=(".pdf",),
)
class PdfPipeline(Pipeline):
    name = "pdf"

    async def run(
        self,
        *,
        ctx: PipelineContext,
        storage: StorageBackend,
    ) -> PipelineResult:
        with measure_stage("extraction"):
            body = await self._read_bytes(storage, ctx.storage_key)
            # Keep CPU-heavy page parsing off the event loop.
            total_pages = await asyncio.to_thread(self._page_count, body)
            text_index_pages = min(total_pages, PDF_TEXT_MAX_INDEX_PAGES)
            text_per_page = await asyncio.to_thread(
                self._extract_text, body, max_pages=text_index_pages,
            )

        vlm_available = has_vision_profile()

        total_chars = sum(len(t) for t in text_per_page)
        ocr_used = False
        ocr_pages_done = 0
        ocr_document_type: str | None = None
        ocr_pages_for_storage: list[str] | None = None
        indexed_pages = len(text_per_page)
        partial_reasons: list[str] = []
        if indexed_pages < total_pages:
            partial_reasons.append("text_page_cap")
        avg_chars = total_chars / max(indexed_pages, 1)
        if total_pages > 0 and avg_chars < MIN_TEXT_PER_PAGE_FOR_TEXT_LAYER:
            if not vlm_available:
                # No VLM profile configured — can't OCR. Mark file as needing
                # OCR so the user can retry once a vision model is wired up.
                raise PdfNeedsOcrError(
                    total_pages=total_pages, total_chars=total_chars,
                )
            log.info(
                "pdf %s appears scanned (pages=%d, avg_chars=%.1f); "
                "running VLM OCR fallback",
                ctx.storage_key, total_pages,
                avg_chars,
            )
            ocr_used = True
            with measure_stage("vision"):
                ocr_text_per_page = await _ocr_pdf_pages(body, total_pages)
            ocr_pages_done = sum(1 for t in ocr_text_per_page if t.strip())
            if ocr_pages_done == 0:
                raise PdfNeedsOcrError(
                    total_pages=total_pages, total_chars=total_chars,
                )
            text_per_page = ocr_text_per_page
            total_chars = sum(len(t) for t in text_per_page)
            indexed_pages = _ocr_pages_to_process(total_pages)
            ocr_pages_for_storage = text_per_page[:indexed_pages]
            ocr_document_type = _classify_ocr_document(
                ocr_pages_for_storage, total_pages=total_pages,
            )
            partial_reasons = []
            if indexed_pages < total_pages:
                partial_reasons.append("ocr_page_cap")

        # Extract embedded figures and describe them via vision profile.
        # Single-image failures degrade to placeholder text; the ingest
        # call below still gets useful context.
        # Skip figure extraction in OCR mode — the page render IS the figure,
        # and we already have its OCR text.
        # Skip entirely when no vision profile is configured: the figures
        # would just produce "(figure description unavailable)" rows.
        if ocr_used or not vlm_available:
            described = []
        else:
            with measure_stage("vision"):
                images = await asyncio.to_thread(
                    extract_images, body, max_pages=indexed_pages,
                )
                described = await describe_images(images) if images else []

        with measure_stage("intelligence"):
            if self._needs_chunked_index(text_per_page[:indexed_pages], described):
                return await self._run_chunked_index(
                    ctx=ctx,
                    text_per_page=text_per_page[:indexed_pages],
                    described=described,
                    total_pages=total_pages,
                    indexed_pages=indexed_pages,
                    ocr_used=ocr_used,
                    ocr_pages_done=ocr_pages_done,
                    partial_reasons=partial_reasons,
                    ocr_pages=ocr_pages_for_storage,
                    ocr_document_type=ocr_document_type,
                )

            return await self._run_single_index(
                ctx=ctx,
                text_per_page=text_per_page[:indexed_pages],
                described=described,
                total_pages=total_pages,
                indexed_pages=indexed_pages,
                ocr_used=ocr_used,
                ocr_pages_done=ocr_pages_done,
                partial_reasons=partial_reasons,
                ocr_pages=ocr_pages_for_storage,
                ocr_document_type=ocr_document_type,
            )

    async def index_revision_pages(self, ctx: PipelineContext, pages: list[str], coverage: dict[str, Any]) -> PipelineResult:
        """Index verified component text. No storage, extraction, OCR or figure calls here."""
        index = self._run_chunked_index if self._needs_chunked_index(pages, []) else self._run_single_index
        with measure_stage("intelligence"):
            return await index(ctx=ctx, text_per_page=pages, described=[], total_pages=len(pages),
                               indexed_pages=coverage["indexed_pages"], partial_reasons=coverage["partial_reasons"],
                               ocr_used=False, ocr_pages_done=0, revision_coverage=coverage)

    @staticmethod
    def _revision_sections(pages: list[str], coverage: dict[str, Any]) -> list[dict[str, Any]]:
        # A missing middle page cannot be represented by a 1..processed prefix.
        failed = set(coverage["failed_pages"])
        return [fallback_section(title=f"Page {n}", anchor_unit="pages", anchor_value=str(n), summary=text[:160])
                for n, text in enumerate(pages, 1) if n not in failed]

    @staticmethod
    def _merge_revision_coverage(indexed: dict[str, Any], parsed: dict[str, Any]) -> None:
        reasons = list(dict.fromkeys([*indexed["partial_reasons"], *parsed["partial_reasons"]]))
        partial = indexed["indexed_partial"] or parsed["indexed_partial"]
        indexed.update(parsed, partial_reasons=reasons, indexed_partial=partial)

    @staticmethod
    async def _read_bytes(
        storage: StorageBackend, key: str,
    ) -> bytes:
        buf = bytearray()
        async for chunk in storage.get(key):
            buf.extend(chunk)
        return bytes(buf)

    @staticmethod
    def _page_count(pdf_bytes: bytes) -> int:
        return pdf_page_count(pdf_bytes)

    def _needs_chunked_index(
        self,
        text_per_page: list[str],
        described: list["DescribedImage"],
    ) -> bool:
        if len(text_per_page) > MAX_PAGES:
            return True
        rendered = render_pages_with_figures(text_per_page, described)
        return len(rendered) > MAX_TOTAL_TEXT_BYTES

    async def _run_single_index(
        self,
        *,
        ctx: PipelineContext,
        text_per_page: list[str],
        described: list["DescribedImage"],
        total_pages: int,
        indexed_pages: int,
        ocr_used: bool,
        ocr_pages_done: int,
        partial_reasons: list[str],
        ocr_pages: list[str] | None = None,
        ocr_document_type: str | None = None,
        revision_coverage: dict[str, Any] | None = None,
    ) -> PipelineResult:
        body_text_raw = render_pages_with_figures(text_per_page, described)
        body_text = self._truncate(body_text_raw)
        text_truncated = len(body_text_raw) > MAX_TOTAL_TEXT_BYTES
        coverage = self._coverage(
            total_pages=total_pages,
            indexed_pages=indexed_pages,
            chunk_count=1,
            text_truncated=text_truncated,
            ocr_used=ocr_used,
            ocr_pages_done=ocr_pages_done,
            partial_reasons=partial_reasons,
            max_index_pages=(
                _ocr_configured_page_cap()
                if ocr_used else PDF_TEXT_MAX_INDEX_PAGES
            ),
        )
        if revision_coverage:
            self._merge_revision_coverage(coverage, revision_coverage)
        body_for_index, compression_meta = maybe_compress_ingest_view(
            body_text,
            kind="pdf",
            context=ctx.display_name or "",
        )
        if compression_meta is not None:
            coverage["compression"] = compression_meta

        user_payload = {
            **({"revision_coverage": revision_coverage} if revision_coverage else {}),
            "folder_path": ctx.folder_path,
            "sibling_names": ctx.sibling_names,
            "catalog_sketch": ctx.catalog_sketch,
            "tag_vocabulary": ctx.tag_vocabulary,
            "page_count": total_pages,
            "indexed_pages": coverage["indexed_pages"],
            "figure_count": len(described),
            "ocr_used": ocr_used,
            "ocr_pages_done": ocr_pages_done if ocr_used else 0,
            "ocr_document_type": ocr_document_type if ocr_used else None,
        }
        stable_prefix = (
            "Index the PDF pages below. Hints are advisory; the provided "
            "text and figure captions take precedence. If indexed_pages is "
            "less than page_count, cover only the provided pages and do not "
            "infer missing pages.\n\n"
            + render_format_hint() + "\n"
            + render_sections_hint(anchor_unit="pages", anchor_example="pages 4-7")
        )
        file_content = (
            f"<context>\n{json.dumps(user_payload, ensure_ascii=False)}\n</context>\n\n"
            f"<document>\n{body_for_index}\n</document>"
        )

        client = get_chat_client("ingest")
        max_out = ingest_output_tokens(len(body_for_index))
        resp = await client.complete(ChatRequest(
            system=PDF_PIPELINE_SYSTEM,
            messages=cacheable_prompt_messages(stable_prefix, file_content),
            max_tokens=max_out,
            temperature=0.2,
            cache_breakpoints=[0],
        ))
        fields = parse_index_response(resp, anchor_unit="pages")
        if not fields.summary:
            log.warning(
                "pdf pipeline: no <summary> in response. text=%r",
                (resp.text or "")[:300],
            )
            raise ValueError("pdf pipeline produced empty summary")
        if revision_coverage:
            sections = self._revision_sections(text_per_page, revision_coverage)
        else:
            sections = fields.sections
        sections = sections or [
            fallback_section(
                title=f"Pages 1-{max(indexed_pages, 1)}",
                anchor_unit="pages",
                anchor_value=f"1-{max(indexed_pages, 1)}",
                summary=fields.summary,
            )
        ]
        return self._result_from_fields(
            fields=fields,
            sections=renumber_sections(sections),
            coverage=coverage,
            ocr_used=ocr_used,
            ocr_pages_done=ocr_pages_done,
            described=described,
            ocr_pages=ocr_pages,
            ocr_document_type=ocr_document_type,
        )

    async def _run_chunked_index(
        self,
        *,
        ctx: PipelineContext,
        text_per_page: list[str],
        described: list["DescribedImage"],
        total_pages: int,
        indexed_pages: int,
        ocr_used: bool,
        ocr_pages_done: int,
        partial_reasons: list[str],
        ocr_pages: list[str] | None = None,
        ocr_document_type: str | None = None,
        revision_coverage: dict[str, Any] | None = None,
    ) -> PipelineResult:
        client = get_chat_client("ingest")
        all_sections: list[dict[str, Any]] = []
        chunk_summaries: list[dict[str, Any]] = []
        truncated_chunks = 0

        chunks = list(enumerate(
            self._iter_prompt_chunks(text_per_page, described),
            start=1,
        ))
        sem = asyncio.Semaphore(llm_ingest_concurrency())

        async def _index_chunk(
            chunk_no: int,
            start: int,
            end: int,
            rendered: str,
            text_truncated: bool,
        ) -> dict[str, Any]:
            async with sem:
                user_payload = {
                    "folder_path": ctx.folder_path,
                    "sibling_names": ctx.sibling_names,
                    "catalog_sketch": ctx.catalog_sketch,
                    "tag_vocabulary": ctx.tag_vocabulary,
                    **({"revision_coverage": revision_coverage} if revision_coverage else {}),
                    "page_count": total_pages,
                    "page_start": start,
                    "page_end": end,
                    "chunk_no": chunk_no,
                    "ocr_used": ocr_used,
                    "ocr_document_type": ocr_document_type if ocr_used else None,
                }
                stable_prefix = (
                    "Index this page range from a larger PDF. Use original page "
                    "numbers from the page markers.\n\n"
                    + render_format_hint() + "\n"
                    + render_sections_hint(
                        anchor_unit="pages",
                        anchor_example=f"pages {start}-{end}",
                    )
                )
                file_content = (
                    f"<context>\n{json.dumps(user_payload, ensure_ascii=False)}\n</context>\n\n"
                    f"<document>\n{rendered}\n</document>"
                )
                resp = await client.complete(ChatRequest(
                    system=PDF_CHUNK_SYSTEM,
                    messages=cacheable_prompt_messages(stable_prefix, file_content),
                    max_tokens=ingest_output_tokens(len(rendered)),
                    temperature=0.2,
                    cache_breakpoints=[0],
                ))
            fields = parse_index_response(resp, anchor_unit="pages")
            summary = fields.summary or fields.description_text or f"Pages {start}-{end}"
            sections = fields.sections or [
                fallback_section(
                    title=f"Pages {start}-{end}",
                    anchor_unit="pages",
                    anchor_value=f"{start}-{end}",
                    summary=summary,
                )
            ]
            return {
                "sections": sections,
                "text_truncated": text_truncated,
                "summary": {
                    "page_start": start,
                    "page_end": end,
                    "summary": summary,
                    "description": fields.description_text or "",
                },
            }

        chunk_results = await asyncio.gather(*(
            _index_chunk(chunk_no, start, end, rendered, text_truncated)
            for chunk_no, (start, end, rendered, text_truncated) in chunks
        ))
        for result in chunk_results:
            if result["text_truncated"]:
                truncated_chunks += 1
            all_sections.extend(result["sections"])
            chunk_summaries.append(result["summary"])

        if revision_coverage:
            all_sections = self._revision_sections(text_per_page, revision_coverage)
        sections = renumber_sections(all_sections)
        coverage = self._coverage(
            total_pages=total_pages,
            indexed_pages=indexed_pages,
            chunk_count=len(chunk_summaries),
            text_truncated=truncated_chunks > 0,
            ocr_used=ocr_used,
            ocr_pages_done=ocr_pages_done,
            partial_reasons=partial_reasons,
            max_index_pages=(
                _ocr_configured_page_cap()
                if ocr_used else PDF_TEXT_MAX_INDEX_PAGES
            ),
        )
        if revision_coverage:
            self._merge_revision_coverage(coverage, revision_coverage)
        if truncated_chunks:
            coverage["truncated_chunks"] = truncated_chunks

        digest = render_sections_digest(
            sections, max_chars=PDF_SECTION_DIGEST_BYTES,
        )
        aggregate_payload = {
            "folder_path": ctx.folder_path,
            "sibling_names": ctx.sibling_names,
            "catalog_sketch": ctx.catalog_sketch,
            "tag_vocabulary": ctx.tag_vocabulary,
            "coverage": coverage,
            "chunk_summaries": chunk_summaries,
            "ocr_document_type": ocr_document_type if ocr_used else None,
        }
        aggregate_content = (
            f"<context>\n{json.dumps(aggregate_payload, ensure_ascii=False)}\n</context>\n\n"
            f"<section_map>\n{digest}\n</section_map>"
        )
        aggregate_content, aggregate_meta = maybe_compress_ingest_aggregate_view(
            aggregate_content,
            kind="pdf_aggregate",
            context=ctx.display_name or "",
        )
        if aggregate_meta is not None:
            coverage["aggregate_compression"] = aggregate_meta
        resp = await client.complete(ChatRequest(
            system=PDF_AGGREGATE_SYSTEM,
            messages=cacheable_prompt_messages(
                (
                    "Summarize the indexed PDF coverage from this section map. "
                    "The caller already has `description.sections`; "
                    "produce file-level recall fields only."
                ),
                aggregate_content,
            ),
            max_tokens=ingest_output_tokens(len(aggregate_content)),
            temperature=0.2,
            cache_breakpoints=[0],
        ))
        fields = parse_index_response(resp, anchor_unit="pages")
        if not fields.summary:
            first = chunk_summaries[0]["summary"] if chunk_summaries else "PDF"
            fields.summary = (
                f"Long PDF indexed into {len(chunk_summaries)} page ranges. "
                f"First range: {first}"
            )
        return self._result_from_fields(
            fields=fields,
            sections=sections,
            coverage=coverage,
            ocr_used=ocr_used,
            ocr_pages_done=ocr_pages_done,
            described=described,
            ocr_pages=ocr_pages,
            ocr_document_type=ocr_document_type,
        )

    def _iter_prompt_chunks(
        self,
        text_per_page: list[str],
        described: list["DescribedImage"],
    ):
        start = 0
        n_pages = len(text_per_page)
        while start < n_pages:
            end = min(start + PDF_CHUNK_PAGES, n_pages)
            rendered = render_pages_with_figures(
                text_per_page[start:end],
                described,
                start_page=start + 1,
            )
            while len(rendered) > MAX_TOTAL_TEXT_BYTES and end - start > 1:
                end = start + max(1, (end - start) // 2)
                rendered = render_pages_with_figures(
                    text_per_page[start:end],
                    described,
                    start_page=start + 1,
                )
            text_truncated = False
            if len(rendered) > MAX_TOTAL_TEXT_BYTES:
                rendered = self._truncate(rendered)
                text_truncated = True
            yield start + 1, end, rendered, text_truncated
            start = end

    def _result_from_fields(
        self,
        *,
        fields,
        sections: list[dict[str, Any]],
        coverage: dict[str, Any],
        ocr_used: bool,
        ocr_pages_done: int,
        described: list["DescribedImage"],
        ocr_pages: list[str] | None = None,
        ocr_document_type: str | None = None,
    ) -> PipelineResult:
        description: dict[str, Any] = {
            "sections": sections,
            "coverage": coverage,
        }
        if fields.description_text:
            description["text"] = fields.description_text
        figures = _pdf_figures_payload(described)
        if figures:
            description["figures"] = figures
        if ocr_used:
            stored_pages, block_count = _build_ocr_pages_payload(ocr_pages or [])
            description["ocr"] = {
                "engine": "vlm",
                "pages_total": coverage.get("total_pages"),
                "pages_processed": ocr_pages_done,
                "document_type": ocr_document_type or "document",
                "stored_pages": len(stored_pages),
                "block_count": block_count,
            }
            description["ocr_pages"] = stored_pages
        base_extra = fields.extra
        if ocr_used:
            base_extra = _ocr_retrieval_extra(
                base_extra=fields.extra,
                ocr_pages=ocr_pages or [],
                document_type=ocr_document_type or "document",
            )
        return PipelineResult(
            summary=fields.summary,
            description=description,
            kind="text",
            extra=build_retrieval_extra(
                sections=sections,
                coverage=coverage,
                base_extra=base_extra,
            ),
            entry_extra=fields.entry_extra,
            entry_catalog_path=fields.catalog_path,
            entry_tags=fields.tags,
        )

    @staticmethod
    def _coverage(
        *,
        total_pages: int,
        indexed_pages: int,
        chunk_count: int,
        text_truncated: bool,
        ocr_used: bool,
        ocr_pages_done: int,
        partial_reasons: list[str],
        max_index_pages: int | None,
    ) -> dict[str, Any]:
        reasons = list(dict.fromkeys(partial_reasons))
        if text_truncated and "prompt_text_cap" not in reasons:
            reasons.append("prompt_text_cap")
        indexed_partial = indexed_pages < total_pages or text_truncated
        coverage: dict[str, Any] = {
            "unit": "pages",
            "total_pages": total_pages,
            "indexed_pages": indexed_pages,
            "indexed_partial": indexed_partial,
            "partial_reasons": reasons if indexed_partial else [],
            "chunked": chunk_count > 1,
            "chunk_count": chunk_count,
            "text_truncated": text_truncated,
        }
        if max_index_pages is not None:
            coverage["max_index_pages"] = max_index_pages
        if ocr_used:
            coverage["ocr_used"] = True
            coverage["ocr_pages_done"] = ocr_pages_done
        return coverage

    # ---- read_segment -----------------------------------------------------

    READ_DEFAULT_MAX_CHARS = 8000

    async def read_segment(
        self,
        *,
        file_row: Any,
        args: dict[str, Any],
        storage: StorageBackend,
    ) -> SegmentResult:
        """Read source or stored OCR text before using bounded page vision."""
        if 'pdf_text_view' in args:
            if _file_was_ocr_indexed(file_row):
                return SegmentResult(error='Use the default view for stored PDF OCR text.')
            body = await self._read_bytes(storage, file_row.storage_key)
            return await asyncio.to_thread(self._slice_prose, body, args)
        if 'numeric_check' in args:
            return await self._check_numeric_pixels(file_row, args, storage)
        is_ocr_pdf = _file_was_ocr_indexed(file_row)
        question = (args.get("question") or "").strip() if isinstance(args, dict) else ""
        pdf_bytes: bytes | None = None
        if is_ocr_pdf:
            source_result = self._slice_ocr_text(file_row, args)
        else:
            pdf_bytes = await self._read_bytes(storage, file_row.storage_key)
            source_result = self._slice(pdf_bytes, args, file_row=file_row)
        if not question:
            return source_result
        if _has_meaningful_pdf_text(source_result.text):
            source_answer = prefer_source_text_for_question(
                source_result,
                mode="pdf_ocr_question" if is_ocr_pdf else "pdf_text_question",
                question=question,
                answered_by="persisted_pdf_ocr" if is_ocr_pdf else "pdf_text_layer",
            )
            if source_answer is not None:
                return source_answer
        return await self._answer_with_vlm(
            file_row=file_row,
            question=question,
            args=args,
            storage=storage,
            pdf_bytes=pdf_bytes,
        )

    async def _check_numeric_pixels(self, file_row, args, storage) -> SegmentResult:
        """Read one original page independently; never feed the candidate to the visual reader."""
        page = args.get('page_start')
        check = args.get('numeric_check')
        meta = {'source': 'pdf_page_pixels', 'numeric_status': 'unverified',
                'file_sha256': getattr(file_row, 'sha256', None)}

        def reject(reason):
            return SegmentResult(error=f'Numeric field unverified: {reason}', extras=meta)

        if (type(page) is not int or page < 1 or args.get('page_end', page) != page
                or set(args) - {'page_start', 'page_end', 'numeric_check', 'compress'}
                or not isinstance(check, dict)):
            return reject('select exactly one physical page and numeric_check')
        candidate, locator = check.get('candidate'), check.get('locator')
        if not (isinstance(locator, str) and 0 < len(locator.strip()) <= 200 and
                isinstance(candidate, str) and 0 < len(candidate.strip()) <= 300):
            return reject('a short locator and exact candidate are required')
        region = check.get('region', [0, 0, 1, 1])
        if (not isinstance(region, list) or len(region) != 4 or
                not all(type(v) in (float, int) and 0 <= v <= 1 for v in region) or
                region[0] >= region[2] or region[1] >= region[3]):
            return reject('invalid normalized page region')
        meta.update(page_start=page, page_end=page, region=region)
        if not has_vision_profile():
            return reject('vision profile not configured')
        body = await self._read_bytes(storage, file_row.storage_key)
        if meta["file_sha256"] and hashlib.sha256(body).hexdigest() != meta["file_sha256"]:
            return reject("PDF bytes do not match the recorded file version")
        try:
            jpeg = await asyncio.to_thread(_numeric_region_jpeg, body, page, region)
            client = get_chat_client('vision')
            reply = await client.complete(ChatRequest(
                system=('Transcribe only from the original PDF pixels. The locator is a label, not an answer. '
                        'Preserve characters, case, superscripts, signs, operand order, units and cell association. '
                        'If the requested field is ambiguous, incomplete or illegible, return status unclear. '
                        'Output one JSON object: {"status":"clear" or "unclear", "text":"exact field"}.'),
                messages=[ChatMessage(role='user', content=[TextBlock(text=locator), ImageBlock(
                    media_type='image/jpeg', data_b64=base64.b64encode(jpeg).decode('ascii'))])],
                max_tokens=512, temperature=0.0,
            ))
            if reply.stop_reason == 'max_tokens':
                return reject('incomplete visual transcription')
            observed = json.loads(reply.text or '')
        except Exception as exc:
            log.warning('Numeric PDF pixel check failed on page %d (%s)', page, type(exc).__name__)
            return reject('page rendering or visual transcription unavailable')
        if not isinstance(observed, dict) or observed.get('status') != 'clear':
            return reject('field not clear on selected page/region')
        value = observed.get('text')
        if not isinstance(value, str) or not 0 < len(value.strip()) <= 300:
            return reject('empty or invalid transcription')
        # No NFKC, punctuation removal or fuzzy matching: e.g. 10² is not 102.
        if ' '.join(value.split()) != ' '.join(candidate.split()):
            meta.update(numeric_status='disagreement', pixel_transcription=value)
            return reject('candidate disagrees with the page; inspect or narrow the region')
        meta.update(numeric_status='pixel_agreement', vlm_used=True)
        return SegmentResult(text=f'[Page {page}]\n{value.strip()}', extras=meta)

    async def _answer_with_vlm(
        self,
        *,
        file_row: Any,
        question: str,
        args: dict[str, Any],
        storage: StorageBackend,
        pdf_bytes: bytes | None = None,
    ) -> SegmentResult:
        """Render the requested page range to JPEGs and ask the VLM."""
        ocr_indexed = _file_was_ocr_indexed(file_row)
        base_extras: dict[str, Any] = {"kind": "pdf"}
        if ocr_indexed:
            base_extras["ocr_indexed"] = True
        if not has_vision_profile():
            return SegmentResult(error=(
                "PDF has no readable text in the requested range and visual "
                "inspection requires the `vision` LLM profile"
            ), extras=base_extras)
        if pdf_bytes is None:
            try:
                pdf_bytes = await self._read_bytes(storage, file_row.storage_key)
            except Exception as exc:  # noqa: BLE001
                return SegmentResult(error=f"PDF read failed: {exc}",
                                     extras=base_extras)

        # Page selection: explicit page_start/page_end if given, else the
        # first PDF_READ_MAX_PAGES_PER_CALL pages. This is an ad-hoc VLM read,
        # not ingest; keep each direct vision call bounded.
        try:
            from pypdf import PdfReader
            total_pages = await asyncio.to_thread(
                lambda: len(PdfReader(io.BytesIO(pdf_bytes)).pages)
            )
        except Exception:  # noqa: BLE001
            total_pages = 0
        ps_arg = args.get("page_start")
        pe_arg = args.get("page_end")
        if ps_arg:
            try:
                ps = max(1, int(ps_arg))
                pe = int(pe_arg) if pe_arg else ps
                pe = max(ps, pe)
            except (TypeError, ValueError):
                return SegmentResult(error="page_start/page_end must be integers")
        else:
            ps, pe = 1, min(
                total_pages or PDF_VISION_QUESTION_MAX_PAGES,
                PDF_VISION_QUESTION_MAX_PAGES,
            )
        requested_pe = pe
        pe = min(pe, ps + PDF_VISION_QUESTION_MAX_PAGES - 1)

        # Render only the selected range. A late-page question must not render
        # every earlier page merely to discard it afterwards.
        try:
            jpegs = await asyncio.to_thread(
                _render_pdf_pages_to_jpeg,
                pdf_bytes,
                pe - ps + 1,
                start_page=ps - 1,
                dpi=PDF_VISION_MAX_DPI,
            )
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(error=f"PDF render failed: {exc}",
                                 extras=base_extras)
        if not jpegs:
            return SegmentResult(error="no pages rendered",
                                 extras=base_extras)

        bounded_question = _bounded_pdf_vision_question(question)
        prepared_pages: list[tuple[int, bytes]] = []
        for offset, jpeg in enumerate(jpegs):
            prepared = downscale_for_vlm(
                jpeg, max_long_edge=OCR_VLM_MAX_LONG_EDGE,
            )
            if prepared is None:
                continue
            scaled, _media_type = prepared
            try:
                bounded_jpeg = _fit_pdf_vision_jpeg_budget(scaled)
            except Exception as exc:  # noqa: BLE001 - malformed page image
                log.warning("PDF page %d could not fit vision budget: %s", ps + offset, exc)
                continue
            prepared_pages.append((ps + offset, bounded_jpeg))

        if not prepared_pages:
            return SegmentResult(
                error="no PDF pages could be prepared for the vision model",
                extras=base_extras,
            )

        client = get_chat_client("vision")
        try:
            answers, batch_count, used_single_page_fallback = (
                await _answer_pdf_question_pages(
                    client=client,
                    pages=prepared_pages,
                    question=bounded_question,
                )
            )
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(error=f"VLM call failed: {exc}",
                                 extras=base_extras)
        text = "\n\n".join(answers).strip()
        extras: dict[str, Any] = {
            **base_extras,
            "vlm_used": True,
            "question": bounded_question,
            "page_start": prepared_pages[0][0],
            "page_end": prepared_pages[-1][0],
            "pages_sent": len(prepared_pages),
            "vision_batches": batch_count,
        }
        if used_single_page_fallback:
            extras["multi_image_fallback"] = True
        if bounded_question != question.strip():
            extras["question_truncated"] = True
            extras["question_serialized_char_limit"] = PDF_VISION_MAX_QUESTION_CHARS
        if pe < requested_pe:
            extras["window_truncated"] = True
            extras["requested_page_end"] = requested_pe
            extras["warning"] = (
                "PDF vision question reads are capped at "
                f"{PDF_VISION_QUESTION_MAX_PAGES} pages per call"
            )
        if not text:
            return SegmentResult(
                error="vision model returned no PDF answer",
                extras=extras,
            )
        return SegmentResult(text=text, extras=extras)

    def _slice_ocr_text(
        self,
        file_row: Any,
        args: dict[str, Any],
    ) -> SegmentResult:
        pages, meta = _ocr_pages_from_file(file_row)
        if not pages:
            return SegmentResult(error=(
                "this PDF was OCR-indexed before stored OCR text was available; "
                "pass `question` to query rendered pages via the vision model, "
                "or reprocess the file to build OCR blocks"
            ), extras={"kind": "pdf", "ocr_indexed": True})

        total_indexed_pages = len(pages)
        total_pages = _ocr_total_pages(meta, fallback=total_indexed_pages)
        labels = [str(i) for i in range(1, total_indexed_pages + 1)]
        offset = _int_arg(args.get("offset"), default=0, minimum=0)
        max_chars = _int_arg(
            args.get("max_chars"), default=self.READ_DEFAULT_MAX_CHARS, minimum=1,
        )

        pattern = (args.get("pattern") or "").strip()
        has_page_scope = _has_pdf_page_scope(args)
        if pattern:
            if has_page_scope:
                resolved = _resolve_pdf_page_window(
                    args,
                    total_pages=total_indexed_pages,
                    labels=labels,
                    default_all=True,
                    max_pages=PDF_READ_MAX_PAGES_PER_CALL,
                )
                if isinstance(resolved, SegmentResult):
                    _add_ocr_extras(resolved.extras, meta)
                    return resolved
                scoped_pages = pages[resolved.page_start - 1: resolved.page_end]
                result = _pdf_pattern_search(
                    pages=scoped_pages,
                    pattern=pattern,
                    context_lines=int(args.get("context_lines") or 2),
                    max_matches=int(args.get("max_matches") or 20),
                    match_offset=max(0, int(args.get("match_offset") or 0)),
                    page_offset=resolved.page_start - 1,
                    total_pages_full=total_pages,
                    page_labels=labels[resolved.page_start - 1: resolved.page_end],
                )
                _add_ocr_window_extras(result.extras, resolved, meta)
                return result
            result = _pdf_pattern_search(
                pages=pages,
                pattern=pattern,
                context_lines=int(args.get("context_lines") or 2),
                max_matches=int(args.get("max_matches") or 20),
                match_offset=max(0, int(args.get("match_offset") or 0)),
                total_pages_full=total_pages,
                page_labels=labels,
            )
            _add_ocr_extras(result.extras, meta)
            return result

        section_id = (args.get("section_id") or "").strip()
        heading = (args.get("heading") or "").strip()
        if section_id or heading:
            section = _find_pdf_section(file_row, section_id=section_id, heading=heading)
            if section is None:
                miss = section_id or f"heading={heading!r}"
                return SegmentResult(
                    error=f"section not found: {miss}",
                    extras=_ocr_base_extras(meta),
                )
            window = _page_window_from_section(section, total_pages=total_indexed_pages)
            if window is None:
                summary = str(section.get("summary") or "").strip()
                extras = _ocr_base_extras(meta)
                extras.update({
                    "section_id": section.get("id"),
                    "title": section.get("title"),
                    "summary": summary,
                    "note": "section anchor did not resolve to OCR pages",
                })
                return SegmentResult(text=summary, extras=extras)
            body = _render_ocr_pages(
                pages[window.page_start - 1: window.page_end],
                start_page=window.page_start,
            )
            result = _clamp_pdf(
                body,
                offset,
                max_chars,
                extras={
                    "section_id": section.get("id"),
                    "title": section.get("title"),
                },
            )
            _add_ocr_window_extras(result.extras, window, meta)
            return result

        if has_page_scope:
            resolved = _resolve_pdf_page_window(
                args,
                total_pages=total_indexed_pages,
                labels=labels,
                default_all=False,
                max_pages=PDF_READ_MAX_PAGES_PER_CALL,
            )
            if isinstance(resolved, SegmentResult):
                _add_ocr_extras(resolved.extras, meta)
                return resolved
            body = _render_ocr_pages(
                pages[resolved.page_start - 1: resolved.page_end],
                start_page=resolved.page_start,
            )
            result = _clamp_pdf(body, offset, max_chars)
            _add_ocr_window_extras(result.extras, resolved, meta)
            return result

        body = _render_ocr_pages(pages, start_page=1)
        ps, pe = _page_range_from_offset(body, offset, max_chars, total_indexed_pages)
        result = _clamp_pdf(
            body,
            offset,
            max_chars,
            extras={"page_start": ps, "page_end": pe},
        )
        _add_ocr_extras(result.extras, meta)
        return result

    async def read_segment_from_bytes(
        self,
        body: bytes,
        args: dict[str, Any],
        *,
        filename: str | None = None,
    ) -> SegmentResult:
        """Bytes-first variant — used by ArchivePipeline for member peeks."""
        return self._slice(body, args)

    def _slice(
        self, pdf_bytes: bytes, args: dict[str, Any], *, file_row: Any | None = None,
    ) -> SegmentResult:
        """Resolve args against a PDF's text body.

        Field priority:
          1. pattern             → regex search across all pages
          2. page_start/page_end → return text for that page range
          3. (default)           → return offset..offset+max_chars of the
                                    full concatenated body

        offset/max_chars further clamp the result of (2).

        When pypdf extracts no text from any page (scanned/image PDF),
        returns an actionable error suggesting `question` for VLM-based
        reading instead of the opaque "empty result".
        """
        if 'pdf_text_view' in args:
            return self._slice_prose(pdf_bytes, args)
        return self._slice_text_layer(pdf_bytes, args, file_row=file_row)

    def _slice_prose(self, body: bytes, args: dict[str, Any]) -> SegmentResult:
        """Bounded alternate text-layer view, never a table or pixel transcription."""
        page_no = args.get('page_start')
        if page_no is None and ('pattern' in args or 'search_from_page' in args):
            from .prose_window_trial import search
            return search(body, args, _pdfium_document)
        if 'search_from_page' in args:
            return SegmentResult(error='Use search_from_page only with document prose search.')
        if (args.get('pdf_text_view') != 'prose' or type(page_no) is not int or page_no < 1
                or args.get('page_end', page_no) != page_no or 'page_label' in args
                or 'question' in args or 'numeric_check' in args):
            return SegmentResult(error='pdf_text_view=prose requires one physical page_start; '
                                 'omit page_label, question and numeric_check.')
        extras = {'pdf_text_view': 'prose', 'page_start': page_no, 'page_end': page_no}
        try:
            with _pdfium_document(body) as doc:
                total = len(doc)
                if page_no > total:
                    return SegmentResult(error='Requested prose page outside PDF', extras=extras)
                page = doc[page_no - 1]
                try:
                    textpage = page.get_textpage()
                    try:
                        text = textpage.get_text_bounded().replace('\r\n', '\n').strip()
                    finally:
                        textpage.close()
                finally:
                    page.close()
        except Exception as exc:
            return SegmentResult(error=f'PDF prose read failed ({type(exc).__name__}).', extras=extras)
        extras['total_pages'] = total
        if not text:
            return SegmentResult(error='No readable native prose on this page.', extras=extras)
        pattern = (args.get('pattern') or '').strip()
        if pattern:
            result = _pdf_pattern_search(
                pages=[text], pattern=pattern, context_lines=_int_arg(args.get('context_lines'), default=2, minimum=0),
                max_matches=_int_arg(args.get('max_matches'), default=20, minimum=1),
                match_offset=_int_arg(args.get('match_offset'), default=0, minimum=0),
                page_offset=page_no - 1, total_pages_full=total,
            )
            result.extras.update(extras)
            return result
        return _clamp_pdf(f'[Page {page_no}]\n{text}', _int_arg(args.get('offset'), default=0, minimum=0),
                          _int_arg(args.get('max_chars'), default=self.READ_DEFAULT_MAX_CHARS, minimum=1),
                          extras=extras)

    def _slice_text_layer(
        self,
        pdf_bytes: bytes,
        args: dict[str, Any],
        *,
        file_row: Any | None = None,
    ) -> SegmentResult:
        try:
            labels = extract_pdf_page_labels(pdf_bytes)
            total_pages = len(labels)
        except Exception as exc:  # noqa: BLE001
            return SegmentResult(error=f"PDF parse failed: {exc}")
        if total_pages == 0:
            return SegmentResult(error="PDF has no pages")
        figures = _pdf_figures_from_file(file_row)

        offset = _int_arg(args.get("offset"), default=0, minimum=0)
        max_chars = _int_arg(
            args.get("max_chars"), default=self.READ_DEFAULT_MAX_CHARS, minimum=1,
        )

        pattern = (args.get("pattern") or "").strip()
        has_page_scope = _has_pdf_page_scope(args)
        if pattern:
            if has_page_scope:
                resolved = _resolve_pdf_page_window(
                    args,
                    total_pages=total_pages,
                    labels=labels,
                    default_all=True,
                    max_pages=PDF_READ_MAX_PAGES_PER_CALL,
                )
            else:
                end = min(total_pages, PDF_PATTERN_UNSCOPED_MAX_PAGES)
                resolved = _PdfPageWindow(
                    page_start=1,
                    page_end=end,
                    requested_page_end=total_pages,
                    truncated=end < total_pages,
                )
            if isinstance(resolved, SegmentResult):
                return resolved
            doc = extract_pdf_text_range(
                pdf_bytes,
                page_start=resolved.page_start,
                page_end=resolved.page_end,
            )
            pages = _inline_pdf_figures(
                doc.pages,
                start_page=doc.page_start,
                figures=figures,
            )
            if all(not page.strip() for page in pages):
                return SegmentResult(
                    error=_NO_TEXT_LAYER_ERROR,
                    extras={
                        "pattern": pattern,
                        "total_pages": total_pages,
                        "page_start": resolved.page_start,
                        "page_end": resolved.page_end,
                    },
                )
            result = _pdf_pattern_search(
                pages=pages,
                pattern=pattern,
                context_lines=int(args.get("context_lines") or 2),
                max_matches=int(args.get("max_matches") or 20),
                match_offset=max(0, int(args.get("match_offset") or 0)),
                page_offset=doc.page_start - 1,
                total_pages_full=total_pages,
                page_labels=doc.page_labels,
            )
            _add_pdf_window_extras(result.extras, resolved, doc)
            if resolved.truncated:
                result.extras["search_truncated"] = True
                result.extras["hint"] = (
                    "PDF search was capped; use read_entries_metadata sections, "
                    "then pass page_start/page_end."
                )
            return result

        if has_page_scope:
            resolved = _resolve_pdf_page_window(
                args,
                total_pages=total_pages,
                labels=labels,
                default_all=False,
                max_pages=PDF_READ_MAX_PAGES_PER_CALL,
            )
            if isinstance(resolved, SegmentResult):
                return resolved
            doc = extract_pdf_text_range(
                pdf_bytes,
                page_start=resolved.page_start,
                page_end=resolved.page_end,
            )
            pages = _inline_pdf_figures(
                doc.pages,
                start_page=doc.page_start,
                figures=figures,
            )
            if all(not page.strip() for page in pages):
                return SegmentResult(
                    error=_NO_TEXT_LAYER_ERROR,
                    extras={
                        "page_start": doc.page_start,
                        "page_end": doc.page_start + len(doc.pages) - 1,
                        "total_pages": total_pages,
                        "empty_pages_in_range": len(doc.pages),
                    },
                )
            result = _clamp_pdf(
                _render_pdf_text_pages(doc, pages),
                offset,
                max_chars,
                extras={"total_pages": total_pages},
            )
            _add_pdf_window_extras(result.extras, resolved, doc)
            return result

        end = min(total_pages, PDF_DEFAULT_READ_PAGES)
        doc = extract_pdf_text_range(pdf_bytes, page_start=1, page_end=end)
        pages = _inline_pdf_figures(
            doc.pages,
            start_page=doc.page_start,
            figures=figures,
        )
        if all(not page.strip() for page in pages):
            return SegmentResult(
                error=_NO_TEXT_LAYER_ERROR,
                extras={"total_pages": total_pages, "page_end": end},
            )
        body = _render_pdf_text_pages(doc, pages)
        if offset >= len(body) and end < total_pages:
            return SegmentResult(
                error=(
                    "offset is beyond the default PDF read window; use "
                    "page_start/page_end from metadata sections instead"
                ),
                extras={
                    "total_pages": total_pages,
                    "page_start": 1,
                    "page_end": end,
                    "read_truncated": True,
                    "next_page_start": end + 1,
                },
            )
        ps, pe = _page_range_from_offset(body, offset, max_chars, total_pages)
        result = _clamp_pdf(
            body,
            offset,
            max_chars,
            extras={"total_pages": total_pages, "page_start": ps, "page_end": pe},
        )
        if end < total_pages:
            result.extras.update({
                "read_truncated": True,
                "read_page_end": end,
                "next_page_start": end + 1,
                "hint": (
                    "Only the first PDF page window was extracted; use "
                    "read_entries_metadata sections, then read a targeted "
                    "page_start/page_end window."
                ),
            })
        return result

    @staticmethod
    def _extract_text(
        pdf_bytes: bytes, *, max_pages: int | None = MAX_PAGES,
    ) -> list[str]:
        """Return text per page.

        `max_pages` is only for prompt construction. Readback passes
        `None` so `read_files(page_start=900)` can access late pages.
        """
        doc = extract_pdf_text_range(
            pdf_bytes,
            page_start=1,
            page_end=max_pages,
        )
        return doc.pages

    @staticmethod
    def _truncate(rendered: str) -> str:
        if len(rendered) <= MAX_TOTAL_TEXT_BYTES:
            return rendered
        return rendered[:MAX_TOTAL_TEXT_BYTES] + "\n[...truncated...]"

    @staticmethod
    def _render_for_prompt(text_per_page: list[str]) -> str:
        """Backwards-compatible legacy renderer (no figures). Kept for
        contexts that explicitly want text-only output."""
        chunks: list[str] = []
        size = 0
        for i, t in enumerate(text_per_page, start=1):
            head = f"### Page {i}\n"
            chunk = head + (t.strip() or "(no text on this page)")
            if size + len(chunk) > MAX_TOTAL_TEXT_BYTES:
                truncated = chunk[: MAX_TOTAL_TEXT_BYTES - size]
                chunks.append(truncated + "\n[...truncated...]")
                break
            chunks.append(chunk)
            size += len(chunk)
        return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# Image extraction + VLM description
#
# Two responsibilities:
#   (1) Walk the PDF and emit a small list of significant images,
#       filtering icons / decorations.
#   (2) Concurrently describe each image via the `vision` profile.
#
# Failure semantics differ from the main ingest path: a single image
# failing here (VLM timeout, oversize, decode error) degrades to a
# placeholder rather than blocking the surrounding PDF transaction.
# ---------------------------------------------------------------------------

MIN_IMAGE_BYTES = 512
# Pixel-dimension test (>= MIN_IMAGE_PX in both axes) is the primary
# significance filter. The byte test is a backstop catching truly
# trivial extracts (single-color icons compressed to a few hundred bytes
# even at large pixel dims).
MIN_IMAGE_PX = 100
MAX_IMAGES_PER_PAGE = 5
MAX_IMAGES_PER_DOC = 30
VLM_BATCH_SIZE = 5
VLM_TIMEOUT_SECONDS = 30
MAX_IMAGE_BYTES_PER_VLM = 4 * 1024 * 1024  # 4 MB cap per image to VLM

_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "gif": "image/gif",
    "webp": "image/webp",
}


FIGURE_DESCRIBE_SYSTEM = (
    "You are Library's figure describer. Given one image extracted from "
    "a PDF, output ONE short paragraph (1-3 sentences) describing what the "
    "image shows. Focus on: figure type (chart/diagram/photo/equation/"
    "table-as-image), the key entities or numbers, and the takeaway. "
    "Do NOT speculate beyond what is visible. Do NOT prefix with 'This "
    "image shows' — just describe directly. Output plain text only."
)


# ---- scanned-PDF OCR via VLM ---------------------------------------------

def _build_ocr_pages_payload(pages: list[str]) -> tuple[list[dict[str, Any]], int]:
    stored: list[dict[str, Any]] = []
    block_count = 0
    for page_no, text in enumerate(pages, start=1):
        clean = (text or "").strip()
        if not clean:
            continue
        blocks = _split_ocr_blocks(clean, page_no=page_no)
        block_count += len(blocks)
        stored.append({
            "page": page_no,
            "text": clean,
            "char_count": len(clean),
            "blocks": blocks,
        })
    return stored, block_count


def _split_ocr_blocks(text: str, *, page_no: int) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    current: list[str] = []
    current_type = "paragraph"

    def flush() -> None:
        nonlocal current
        body = "\n".join(current).strip()
        current = []
        if not body:
            return
        idx = len(blocks) + 1
        if len(body) > OCR_BLOCK_MAX_CHARS:
            body = body[:OCR_BLOCK_MAX_CHARS].rstrip() + "\n[block truncated]"
        blocks.append({
            "id": f"p{page_no}b{idx}",
            "type": current_type,
            "label": _ocr_block_label(body, current_type),
            "text": body,
        })

    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush()
            current_type = "paragraph"
            continue
        line_type = _ocr_line_type(stripped)
        if line_type == "heading":
            flush()
            current_type = "heading"
            current = [stripped]
            flush()
            current_type = "paragraph"
            continue
        if line_type != current_type and current:
            flush()
        current_type = line_type
        current.append(line)
    flush()
    return blocks


def _ocr_line_type(line: str) -> str:
    if line.startswith("#"):
        return "heading"
    if line.count("|") >= 2:
        return "table"
    if re.match(r"^\s*(chapter|section|part|appendix)\b", line, re.IGNORECASE):
        return "heading"
    if re.match(r"^\s*\d+(\.\d+){0,3}\s+\S+", line) and len(line) <= 120:
        return "heading"
    return "paragraph"


def _ocr_block_label(text: str, block_type: str) -> str:
    first = next((ln.strip("# ").strip() for ln in text.splitlines() if ln.strip()), "")
    if not first:
        return block_type
    return first[:80]


def _classify_ocr_document(pages: list[str], *, total_pages: int) -> str:
    text = "\n".join(pages)
    lower = text.lower()
    colon_lines = sum(1 for ln in text.splitlines() if ":" in ln or "：" in ln)
    table_lines = sum(1 for ln in text.splitlines() if ln.count("|") >= 2)
    heading_hits = len(re.findall(
        r"(^|\n)\s*#{1,4}\s+|(^|\n)\s*(chapter|part|appendix)\b",
        lower,
        flags=re.IGNORECASE,
    ))
    if "invoice" in lower or "receipt" in lower or "发票" in text or "收据" in text:
        return "receipt"
    if table_lines >= 3:
        return "table"
    if colon_lines >= 6 and re.search(
        r"\b(name|date|address|signature|applicant)\b|姓名|日期|地址|签名|申请人",
        text,
        flags=re.IGNORECASE,
    ):
        return "form"
    if total_pages >= 10 and heading_hits >= 2:
        return "book"
    if total_pages >= 20:
        return "long_document"
    return "document"


def _ocr_retrieval_extra(
    *,
    base_extra: str | None,
    ocr_pages: list[str],
    document_type: str,
) -> str:
    lines: list[str] = []
    if base_extra and base_extra.strip():
        lines.append(base_extra.strip())
    lines.append(f"ocr_document_type: {document_type}")
    pages_with_text = sum(1 for text in ocr_pages if (text or "").strip())
    lines.append(f"ocr_pages_with_text: {pages_with_text}")
    return "\n".join(lines)


def _clean_ocr_response_text(text: str | None) -> str:
    clean = (text or "").strip()
    if not clean:
        return ""
    clean = re.sub(r"(?is)<think>.*?</think>", "", clean).strip()
    marker = "</think>"
    idx = clean.casefold().rfind(marker)
    if idx >= 0:
        clean = clean[idx + len(marker):].strip()
    for prefix in ("Transcription:", "OCR text:", "Extracted text:"):
        if clean.casefold().startswith(prefix.casefold()):
            clean = clean[len(prefix):].lstrip()
            break
    return clean


def _has_meaningful_pdf_text(text: str) -> bool:
    for raw_line in str(text or "").splitlines():
        line = raw_line.strip()
        if not line or line == "(no OCR text on this page)":
            continue
        if re.fullmatch(r"\[Page \d+\]", line):
            continue
        if re.fullmatch(r"\[Page label: .+\]", line):
            continue
        if line.startswith("[page extraction failed:") and line.endswith("]"):
            continue
        return True
    return False


def _file_was_ocr_indexed(file_row: Any) -> bool:
    """True iff the ingest pipeline marked this PDF as OCR-only.

    Set by `PdfPipeline.run` when the text-layer extraction came back
    nearly empty and the VLM was used to reconstruct page text. Stored
    as `description.ocr` (a dict carrying engine + page counts).
    """
    desc = getattr(file_row, "description", None)
    return isinstance(desc, dict) and isinstance(desc.get("ocr"), dict)


def _ocr_pages_from_file(file_row: Any) -> tuple[list[str], dict[str, Any]]:
    desc = getattr(file_row, "description", None)
    if not isinstance(desc, dict):
        return [], {}
    meta = desc.get("ocr") if isinstance(desc.get("ocr"), dict) else {}
    raw_pages = desc.get("ocr_pages")
    if not isinstance(raw_pages, list):
        return [], dict(meta)
    max_page = 0
    page_text: dict[int, str] = {}
    for item in raw_pages:
        if not isinstance(item, dict):
            continue
        try:
            page_no = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if page_no <= 0:
            continue
        max_page = max(max_page, page_no)
        page_text[page_no] = str(item.get("text") or "")
    pages = [page_text.get(i, "") for i in range(1, max_page + 1)]
    return pages, dict(meta)


def _render_ocr_pages(pages: list[str], *, start_page: int) -> str:
    chunks: list[str] = []
    for offset, text in enumerate(pages):
        page_no = start_page + offset
        body = (text or "").strip() or "(no OCR text on this page)"
        chunks.append(f"[Page {page_no}]\n{body}")
    return "\n\n".join(chunks)


def _find_pdf_section(
    file_row: Any, *, section_id: str = "", heading: str = "",
) -> dict[str, Any] | None:
    desc = getattr(file_row, "description", None)
    if not isinstance(desc, dict):
        return None
    sections = desc.get("sections")
    if not isinstance(sections, list):
        return None
    for sec in sections:
        if not isinstance(sec, dict):
            continue
        if section_id and str(sec.get("id") or "") == section_id:
            return sec
    if heading:
        needle = heading.strip().casefold()
        for sec in sections:
            if not isinstance(sec, dict):
                continue
            if str(sec.get("title") or "").strip().casefold() == needle:
                return sec
    return None


def _page_window_from_section(
    section: dict[str, Any], *, total_pages: int,
) -> _PdfPageWindow | None:
    anchor = section.get("anchor") or {}
    if isinstance(anchor, dict):
        value = str(anchor.get("value") or anchor.get("path") or "")
    else:
        value = str(anchor)
    nums = [int(n) for n in re.findall(r"\d+", value)]
    if not nums:
        return None
    start = max(1, min(nums[0], total_pages))
    end = max(start, min(nums[-1], total_pages))
    return _PdfPageWindow(
        page_start=start,
        page_end=end,
        requested_page_end=end,
        truncated=False,
    )


def _ocr_base_extras(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "kind": "pdf",
        "ocr_indexed": True,
        "ocr_document_type": meta.get("document_type"),
        "ocr_pages_total": meta.get("pages_total"),
        "ocr_pages_processed": meta.get("pages_processed"),
        "ocr_stored_pages": meta.get("stored_pages"),
    }


def _add_ocr_extras(extras: dict[str, Any], meta: dict[str, Any]) -> None:
    extras.update(_ocr_base_extras(meta))


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _ocr_total_pages(meta: dict[str, Any], *, fallback: int) -> int:
    return _positive_int(meta.get("pages_total")) or fallback


def _ocr_configured_page_cap() -> int | None:
    return _positive_int(OCR_MAX_PAGES)


def _ocr_pages_to_process(total_pages: int) -> int:
    cap = _ocr_configured_page_cap()
    return min(total_pages, cap) if cap is not None else total_pages


def _add_ocr_window_extras(
    extras: dict[str, Any],
    window: _PdfPageWindow,
    meta: dict[str, Any],
) -> None:
    _add_ocr_extras(extras, meta)
    fallback_total = _positive_int(meta.get("stored_pages")) or window.page_end
    extras.update({
        "page_start": window.page_start,
        "page_end": window.page_end,
        "total_pages": _ocr_total_pages(meta, fallback=fallback_total),
    })
    if window.page_label is not None:
        extras["page_label"] = window.page_label
        extras["resolved_page"] = window.resolved_page
    if window.truncated:
        extras["window_truncated"] = True
        extras["requested_page_end"] = window.requested_page_end


async def _answer_pdf_question_pages(
    *,
    client: Any,
    pages: list[tuple[int, bytes]],
    question: str,
) -> tuple[list[str], int, bool]:
    """Query bounded page batches, falling back only for multi-image incompatibility."""
    answers: list[str] = []
    request_count = 0
    multi_image_supported = True
    used_single_page_fallback = False

    async def complete(batch: list[tuple[int, bytes]]) -> None:
        nonlocal request_count
        request_count += 1
        response = await _complete_pdf_question_batch(
            client=client,
            batch=batch,
            question=question,
        )
        text = (response.text or "").strip()
        if not text:
            return
        if len(batch) == 1 and f"[Page {batch[0][0]}]" not in text:
            text = f"[Page {batch[0][0]}]\n{text}"
        answers.append(text)

    for batch in _pdf_vision_page_batches(pages):
        if len(batch) == 1 or not multi_image_supported:
            for page in batch:
                await complete([page])
            continue
        try:
            await complete(batch)
        except Exception as exc:  # noqa: BLE001 - SDK/provider types vary
            if not _pdf_vision_multi_image_incompatible(exc):
                raise
            multi_image_supported = False
            used_single_page_fallback = True
            log.warning(
                "multi-page PDF vision query was rejected for pages %s; "
                "falling back to single-page calls: %s",
                [page_no for page_no, _jpeg in batch],
                exc,
            )
            for page in batch:
                await complete([page])

    return answers, request_count, used_single_page_fallback


async def _complete_pdf_question_batch(
    *,
    client: Any,
    batch: list[tuple[int, bytes]],
    question: str,
) -> Any:
    content: list[Any] = [TextBlock(text=(
        f"Question: {question}\n\n"
        "The following scanned PDF page images are labeled in order. Answer "
        "the question for every page using only visible evidence. Use one "
        "[Page N] heading per page and quote visible text when relevant. If "
        "the answer is absent, say so plainly."
    ))]
    for page_no, jpeg in batch:
        content.append(TextBlock(text=f"PDF page {page_no}:"))
        content.append(ImageBlock(
            media_type="image/jpeg",
            data_b64=base64.b64encode(jpeg).decode("ascii"),
        ))
    return await client.complete(ChatRequest(
        system=(
            "Answer questions about scanned document pages using only visible "
            "evidence. Keep evidence from different pages separated."
        ),
        messages=[ChatMessage(role="user", content=content)],
        max_tokens=min(8_192, 2_048 * len(batch)),
        temperature=0.2,
    ))


def _pdf_vision_multi_image_incompatible(exc: BaseException) -> bool:
    status_code = getattr(exc, "status_code", None)
    if status_code in {400, 413, 415, 422}:
        return True
    name = type(exc).__name__.lower()
    if "badrequest" in name or "unsupportedmedia" in name:
        return True
    message = str(exc).lower()
    return any(marker in message for marker in (
        "does not accept multiple images",
        "doesn't accept multiple images",
        "multiple images are not supported",
        "multiple image inputs are not supported",
        "multi-image is not supported",
        "multi image is not supported",
        "too many images",
        "image count",
    ))


def _bounded_pdf_vision_question(question: str) -> str:
    text = str(question or "").strip()
    if _json_string_chars(text) <= PDF_VISION_MAX_QUESTION_CHARS:
        return text
    lower = 0
    upper = len(text)
    while lower < upper:
        midpoint = (lower + upper + 1) // 2
        if _json_string_chars(text[:midpoint]) <= PDF_VISION_MAX_QUESTION_CHARS:
            lower = midpoint
        else:
            upper = midpoint - 1
    log.warning(
        "PDF vision question exceeded %d serialized characters and was truncated",
        PDF_VISION_MAX_QUESTION_CHARS,
    )
    return text[:lower]


def _json_string_chars(text: str) -> int:
    return max(0, len(json.dumps(text, ensure_ascii=True)) - 2)


def _pdf_vision_page_batches(
    pages: list[tuple[int, bytes]],
) -> list[list[tuple[int, bytes]]]:
    batches: list[list[tuple[int, bytes]]] = []
    current: list[tuple[int, bytes]] = []
    current_chars = 0
    for page in pages:
        page_chars = _pdf_vision_data_url_chars(page[1])
        if page_chars > PDF_VISION_MAX_DATA_URL_CHARS:
            raise ValueError("PDF vision page exceeds request image budget")
        if current and (
            len(current) >= PDF_VISION_MAX_PAGES_PER_BATCH
            or current_chars + page_chars > PDF_VISION_MAX_DATA_URL_CHARS
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append(page)
        current_chars += page_chars
    if current:
        batches.append(current)
    return batches


async def _ocr_pdf_pages(pdf_bytes: bytes, total_pages: int) -> list[str]:
    """Render OCR pages to JPEG via pypdfium2,
    down-scale each via downscale_for_vlm, and ask the vision profile
    to extract text in markdown. Returns one entry per rendered page;
    if an explicit OCR_MAX_PAGES cap is configured, pages beyond the cap
    are returned as empty strings.

    Empty / "No text content" responses are normalised to '' so the
    caller can detect the all-empty-page case and raise PdfNeedsOcrError.
    """
    pages_to_ocr = _ocr_pages_to_process(total_pages)
    client = get_chat_client("vision")
    out: list[str] = [""] * pages_to_ocr
    sem = asyncio.Semaphore(llm_ingest_concurrency())

    async def _ocr_one(i: int, jpeg_bytes: bytes) -> None:
        try:
            async with sem:
                # OCR is more sensitive to fine glyph detail than image
                # caption, so use a higher long-edge cap than the caption
                # path. 200-DPI A4 renders to ~2200px and only loses ~7%
                # at 2048; 8pt footnotes in dense layouts stay readable.
                scaled, media_type = downscale_for_vlm(
                    jpeg_bytes, max_long_edge=OCR_VLM_MAX_LONG_EDGE,
                )
                b64 = base64.b64encode(scaled).decode("ascii")
                extra_body = (
                    DISABLE_THINKING_EXTRA_BODY
                    if getattr(client, "provider", None) == "openai-compatible"
                    else None
                )
                resp = await client.complete(ChatRequest(
                    system=PDF_OCR_PROMPT,
                    messages=[ChatMessage(role="user", content=[
                        TextBlock(text=f"Page {i + 1} of {pages_to_ocr}."),
                        ImageBlock(media_type=media_type, data_b64=b64),
                    ])],
                    max_tokens=4096,
                    temperature=0.0,
                    extra_body=extra_body,
                ))
        except Exception as exc:  # noqa: BLE001
            log.warning("OCR call failed for page %d: %s", i + 1, exc)
            return
        text = _clean_ocr_response_text(resp.text)
        if text.lower() in ("no text content", "no text content."):
            text = ""
        out[i] = text

    for start in range(0, pages_to_ocr, OCR_RENDER_BATCH_PAGES):
        batch_count = min(OCR_RENDER_BATCH_PAGES, pages_to_ocr - start)
        page_jpegs = await asyncio.to_thread(
            _render_pdf_pages_to_jpeg,
            pdf_bytes,
            batch_count,
            start_page=start,
        )
        await asyncio.gather(*(
            _ocr_one(start + i, jpeg_bytes)
            for i, jpeg_bytes in enumerate(page_jpegs)
        ))
    # Pad with empties for pages we skipped past the cap, so caller's
    # page indexing stays aligned with total_pages.
    while len(out) < total_pages:
        out.append("")
    return out


_PDFIUM_LOCK = Lock()  # PDFium is not thread safe, even for independent documents.


@contextmanager
def _pdfium_document(body: bytes):
    import pypdfium2 as pdfium

    with _PDFIUM_LOCK, pdfium.PdfDocument(body) as doc:
        yield doc


def _render_pdf_pages_to_jpeg(
    pdf_bytes: bytes,
    page_count: int,
    *,
    start_page: int = 0,
    dpi: float = OCR_RENDER_DPI,
) -> list[bytes]:
    """Render `page_count` pages to JPEG bytes. Sync, intended to run
    inside asyncio.to_thread. Mirrors WeKnora's PDFScannedParser shape:
    pypdfium2 → PIL.Image → JPEG via Pillow."""
    out: list[bytes] = []
    with _pdfium_document(pdf_bytes) as pdf:
        start = max(0, start_page)
        end = min(start + page_count, len(pdf))
        for i in range(start, end):
            page = pdf[i]
            try:
                out.append(_render_pdf_page_to_vision_jpeg(page, max_dpi=dpi))
            finally:
                close = getattr(page, "close", None)
                if close:
                    close()
    return out


def _numeric_region_jpeg(body: bytes, page_no: int, region: list[float]) -> bytes:
    with _pdfium_document(body) as doc:
        if not 1 <= page_no <= len(doc):
            raise ValueError('physical page outside document')
        page = doc[page_no - 1]
        try:
            width, height = page.get_size()
            left, top, right, bottom = region
            crop = (width * left, height * (1 - bottom), width * (1 - right), height * top)
            return _render_pdf_page_to_vision_jpeg(page, crop=crop)
        finally:
            page.close()


def _render_pdf_page_to_vision_jpeg(
    page: Any,
    *,
    max_dpi: float = PDF_VISION_MAX_DPI,
    crop: tuple[float, float, float, float] | None = None,
) -> bytes:
    scale = _pdf_vision_render_scale(page, max_dpi=max_dpi)
    bitmap = None
    image = None
    try:
        bitmap = page.render(scale=scale, **({'crop': crop} if crop else {}))
        image = bitmap.to_pil()
        image.load()
        return _encode_pdf_vision_jpeg(image, effective_dpi=scale * 72.0)
    finally:
        if image is not None:
            close = getattr(image, "close", None)
            if close:
                close()
        if bitmap is not None:
            close = getattr(bitmap, "close", None)
            if close:
                close()


def _pdf_vision_render_scale(
    page: Any,
    *,
    max_dpi: float = PDF_VISION_MAX_DPI,
) -> float:
    page_width, page_height = page.get_size()
    if page_width <= 0 or page_height <= 0:
        raise ValueError("PDF page has invalid dimensions")
    scale = max(1.0, float(max_dpi)) / 72.0
    page_area = page_width * page_height
    if page_area * scale * scale > PDF_VISION_MAX_PIXELS:
        scale = min(scale, math.sqrt(PDF_VISION_MAX_PIXELS / page_area))
    scan_cap = _dominant_pdf_scan_scale_cap(
        page,
        page_width=page_width,
        page_height=page_height,
    )
    if scan_cap is not None:
        scale = min(scale, scan_cap)
    return max(scale, 1e-6)


def _dominant_pdf_scan_scale_cap(
    page: Any,
    *,
    page_width: float,
    page_height: float,
) -> float | None:
    try:
        from pypdfium2 import raw as pdfium_c
    except ImportError:  # pragma: no cover - packaged dependency
        return None
    try:
        left, bottom, right, top = page.get_bbox()
        page_area = abs((right - left) * (top - bottom))
    except Exception as exc:  # noqa: BLE001 - malformed page boxes degrade
        log.debug("could not inspect PDF page bounding box: %r", exc)
        return None
    if page_area <= 0:
        return None
    caps: list[float] = []
    try:
        images = page.get_objects(
            filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE],
            max_depth=15,
        )
        for image in images:
            try:
                matrix = _full_pdf_object_matrix(image)
                placed_x = math.hypot(matrix.a, matrix.b)
                placed_y = math.hypot(matrix.c, matrix.d)
                placed_area = abs(matrix.a * matrix.d - matrix.b * matrix.c)
                if placed_x <= 0 or placed_y <= 0 or placed_area <= 0:
                    continue
                pixel_width, pixel_height = image.get_px_size()
                if (
                    placed_area / page_area < PDF_VISION_SCAN_COVERAGE_THRESHOLD
                    or pixel_width <= 0
                    or pixel_height <= 0
                    or pixel_width * pixel_height < PDF_VISION_SCAN_MIN_PIXELS
                ):
                    continue
                page_short, page_long = sorted((page_width, page_height))
                pixel_short, pixel_long = sorted((pixel_width, pixel_height))
                caps.append(min(
                    max(0.5, pixel_width - 0.5) / placed_x,
                    max(0.5, pixel_height - 0.5) / placed_y,
                    max(0.5, pixel_short - 0.5) / page_short,
                    max(0.5, pixel_long - 0.5) / page_long,
                ))
            except Exception as exc:  # noqa: BLE001 - malformed images degrade
                log.debug("could not inspect PDF image scale cap: %r", exc)
    except Exception as exc:  # noqa: BLE001 - vector pages may expose no objects
        log.debug("could not enumerate PDF image objects: %r", exc)
    return min(caps) if caps else None


def _full_pdf_object_matrix(obj: Any) -> Any:
    matrix = obj.get_matrix()
    parent = getattr(obj, "container", None)
    while parent is not None:
        matrix = matrix.multiply(parent.get_matrix())
        parent = getattr(parent, "container", None)
    return matrix


def _fit_pdf_vision_jpeg_budget(jpeg: bytes) -> bytes:
    if _pdf_vision_data_url_chars(jpeg) <= PDF_VISION_MAX_DATA_URL_CHARS:
        return jpeg
    from PIL import Image

    with Image.open(io.BytesIO(jpeg)) as image:
        image.load()
        return _encode_pdf_vision_jpeg(image, effective_dpi=0.0)


def _encode_pdf_vision_jpeg(image: Any, *, effective_dpi: float) -> bytes:
    from PIL import Image

    current = _pdf_vision_rgb_image(image)
    current_dpi = max(0.0, float(effective_dpi or 0.0))
    try:
        while True:
            smallest: bytes | None = None
            for quality in PDF_VISION_JPEG_QUALITIES:
                encoded = _encode_pdf_vision_jpeg_once(
                    current,
                    quality=quality,
                    effective_dpi=current_dpi,
                )
                if _pdf_vision_data_url_chars(encoded) <= PDF_VISION_MAX_DATA_URL_CHARS:
                    return encoded
                smallest = encoded
            width, height = current.size
            if width <= 1 and height <= 1:
                raise RuntimeError("could not fit PDF vision JPEG within request budget")
            assert smallest is not None
            estimated_ratio = math.sqrt(PDF_VISION_MAX_JPEG_BYTES / len(smallest)) * 0.95
            resize_ratio = min(0.90, max(0.10, estimated_ratio))
            new_size = (
                max(1, int(width * resize_ratio)),
                max(1, int(height * resize_ratio)),
            )
            if new_size == current.size:
                new_size = (max(1, width - 1), max(1, height - 1))
            resized = current.resize(new_size, Image.Resampling.LANCZOS)
            linear_ratio = min(
                resized.width / max(1, width),
                resized.height / max(1, height),
            )
            current.close()
            current = resized
            current_dpi *= linear_ratio
    finally:
        current.close()


def _pdf_vision_rgb_image(image: Any) -> Any:
    from PIL import Image

    if image.mode == "RGB":
        return image.copy()
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        try:
            background.paste(rgba, mask=rgba.getchannel("A"))
        finally:
            rgba.close()
        return background
    return image.convert("RGB")


def _encode_pdf_vision_jpeg_once(
    image: Any,
    *,
    quality: int,
    effective_dpi: float,
) -> bytes:
    buf = io.BytesIO()
    kwargs: dict[str, Any] = {
        "format": "JPEG",
        "quality": quality,
        "optimize": True,
    }
    if effective_dpi > 0:
        kwargs["dpi"] = (effective_dpi, effective_dpi)
    image.save(buf, **kwargs)
    return buf.getvalue()


def _pdf_vision_data_url_chars(jpeg: bytes) -> int:
    return len(PDF_VISION_DATA_URL_PREFIX) + 4 * math.ceil(len(jpeg) / 3)


@dataclass(slots=True)
class ExtractedImage:
    page_num: int       # 1-indexed
    fig_index: int      # 1-indexed within the page
    media_type: str
    data: bytes
    width: int
    height: int


@dataclass(slots=True)
class DescribedImage:
    page_num: int
    fig_index: int
    description: str
    error: str | None = None


def extract_images(
    pdf_bytes: bytes, *, max_pages: int | None = None,
) -> list[ExtractedImage]:
    """Walk the PDF and return significant images (icons filtered)."""
    from pypdf import PdfReader  # imported lazily

    out: list[ExtractedImage] = []
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception:
        log.exception("pypdf failed to open PDF for image extraction")
        return out

    total = 0
    pages = reader.pages if max_pages is None else reader.pages[:max_pages]
    for page_num, page in enumerate(pages, start=1):
        try:
            page_images = list(page.images)[:MAX_IMAGES_PER_PAGE]
        except Exception as exc:
            # Common when Pillow isn't installed: pypdf can't decode the
            # image stream and raises. Once per-page is too noisy at WARNING.
            log.debug("pypdf failed listing images on page %d: %s",
                      page_num, exc)
            continue

        page_kept = 0
        for _fig_idx, img in enumerate(page_images, start=1):
            data = img.data or b""
            if len(data) < MIN_IMAGE_BYTES:
                continue

            width = height = 0
            try:
                pil = img.image
                if pil is not None:
                    width, height = pil.size
            except Exception:
                pass
            if width and height:
                if width < MIN_IMAGE_PX or height < MIN_IMAGE_PX:
                    continue

            ext = (img.name or "").rsplit(".", 1)[-1].lower()
            media_type = _MIME_BY_EXT.get(ext, "image/png")

            out.append(ExtractedImage(
                page_num=page_num,
                fig_index=page_kept + 1,
                media_type=media_type,
                data=data[:MAX_IMAGE_BYTES_PER_VLM],
                width=width, height=height,
            ))
            page_kept += 1
            total += 1
            if total >= MAX_IMAGES_PER_DOC:
                return out
    return out


async def describe_images(
    images: list[ExtractedImage],
) -> list[DescribedImage]:
    """Send each image through the vision profile concurrently."""
    if not images:
        return []
    client = get_chat_client("vision")
    out: list[DescribedImage] = []

    for batch_start in range(0, len(images), VLM_BATCH_SIZE):
        batch = images[batch_start : batch_start + VLM_BATCH_SIZE]
        results = await asyncio.gather(
            *(_describe_one(client, img) for img in batch),
            return_exceptions=True,
        )
        for img, res in zip(batch, results):
            if isinstance(res, BaseException):
                log.warning("VLM describe failed for fig %d.%d: %r",
                            img.page_num, img.fig_index, res)
                out.append(DescribedImage(
                    page_num=img.page_num, fig_index=img.fig_index,
                    description="(figure description unavailable)",
                    error=repr(res),
                ))
            else:
                out.append(res)
    return out


async def _describe_one(client, img: ExtractedImage) -> DescribedImage:
    scaled, media_type = downscale_for_vlm(img.data)
    b64 = base64.b64encode(scaled).decode("ascii")
    user_text = (
        f"Figure on page {img.page_num} (fig {img.fig_index}) of a PDF. "
        f"Describe in 1-3 sentences."
    )
    request = ChatRequest(
        system=FIGURE_DESCRIBE_SYSTEM,
        messages=[ChatMessage(role="user", content=[
            TextBlock(text=user_text),
            ImageBlock(media_type=media_type, data_b64=b64),
        ])],
        max_tokens=300,
        temperature=0.2,
    )
    try:
        resp = await asyncio.wait_for(
            client.complete(request), timeout=VLM_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        return DescribedImage(
            page_num=img.page_num, fig_index=img.fig_index,
            description="(figure description timed out)",
            error="timeout",
        )
    text = (resp.text or "").strip() or "(empty VLM response)"
    return DescribedImage(
        page_num=img.page_num, fig_index=img.fig_index,
        description=text,
    )


def render_pages_with_figures(
    text_per_page: list[str],
    described: list[DescribedImage],
    *,
    start_page: int = 1,
) -> str:
    """Build the prompt body, with `[Figure X.Y] ...` lines appended to
    each page's text block."""
    by_page: dict[int, list[DescribedImage]] = {}
    for d in described:
        by_page.setdefault(d.page_num, []).append(d)

    chunks: list[str] = []
    for i, t in enumerate(text_per_page, start=start_page):
        body = (t or "").strip() or "(no text on this page)"
        figs = by_page.get(i, [])
        if figs:
            fig_lines = [
                f"[Figure {f.page_num}.{f.fig_index}] {f.description}"
                for f in figs
            ]
            body = body + "\n\n" + "\n".join(fig_lines)
        chunks.append(f"### Page {i}\n{body}")
    return "\n\n".join(chunks)


def _pdf_figures_payload(described: list[DescribedImage]) -> list[dict[str, Any]]:
    figures: list[dict[str, Any]] = []
    for item in described:
        text = (item.description or "").strip()
        if not text:
            continue
        figure: dict[str, Any] = {
            "page": item.page_num,
            "figure": item.fig_index,
            "label": f"Figure {item.page_num}.{item.fig_index}",
            "text": text,
        }
        if item.error:
            figure["error"] = item.error
        figures.append(figure)
    return figures


def _pdf_figures_from_file(file_row: Any | None) -> list[dict[str, Any]]:
    description = getattr(file_row, "description", None) if file_row is not None else None
    if not isinstance(description, dict):
        return []
    raw = description.get("figures")
    if not isinstance(raw, list):
        return []
    figures: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            page = int(item.get("page") or 0)
            figure = int(item.get("figure") or 0)
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or item.get("description") or "").strip()
        if page <= 0 or figure <= 0 or not text:
            continue
        figures.append({
            "page": page,
            "figure": figure,
            "label": str(item.get("label") or f"Figure {page}.{figure}"),
            "text": text,
        })
    return figures


def _inline_pdf_figures(
    pages: list[str],
    *,
    start_page: int,
    figures: list[dict[str, Any]],
) -> list[str]:
    if not figures:
        return pages
    out = list(pages)
    end_page = start_page + len(out) - 1
    by_page: dict[int, list[str]] = {}
    for item in figures:
        try:
            page = int(item.get("page") or 0)
        except (TypeError, ValueError):
            continue
        if page < start_page or page > end_page:
            continue
        label = str(item.get("label") or f"Figure {page}.{item.get('figure') or 1}")
        text = str(item.get("text") or "").strip()
        if not text:
            continue
        clean_label = label.strip().removeprefix("[").removesuffix("]")
        by_page.setdefault(page, []).append(f"[{clean_label}] {text}")
    for page, lines in by_page.items():
        idx = page - start_page
        body = (out[idx] or "").rstrip()
        figure_text = "\n".join(lines)
        out[idx] = f"{body}\n\n{figure_text}" if body else figure_text
    return out


def _render_pdf_text_pages(doc: Any, pages: list[str]) -> str:
    chunks: list[str] = []
    for offset, txt in enumerate(pages):
        page = doc.page_start + offset
        label = doc.page_labels[offset] if offset < len(doc.page_labels) else str(page)
        label_line = "" if label == str(page) else f"\n[Page label: {label}]"
        chunks.append(f"[Page {page}]{label_line}\n{txt}")
    return "\n\n".join(chunks)


# ---------------------------------------------------------------------------
# read_segment helpers
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class _PdfPageWindow:
    page_start: int
    page_end: int
    requested_page_end: int
    truncated: bool = False
    page_label: str | None = None
    resolved_page: int | None = None


def _int_arg(value: Any, *, default: int, minimum: int | None = None) -> int:
    if value in (None, ""):
        parsed = default
    else:
        parsed = int(value)
    if minimum is not None:
        parsed = max(minimum, parsed)
    return parsed


def _has_pdf_page_scope(args: dict[str, Any]) -> bool:
    return any(
        args.get(key) not in (None, "")
        for key in ("page_start", "page_end", "page_label")
    )


def _resolve_pdf_page_window(
    args: dict[str, Any],
    *,
    total_pages: int,
    labels: list[str],
    default_all: bool,
    max_pages: int,
) -> _PdfPageWindow | SegmentResult:
    try:
        page_label_raw = args.get("page_label")
        if page_label_raw not in (None, ""):
            resolved = resolve_page_label(labels, page_label_raw)
            if resolved is None:
                return SegmentResult(
                    error="page_label was not found in PDF page labels",
                    extras={
                        "page_label": str(page_label_raw),
                        "total_pages": total_pages,
                    },
                )
            start = resolved
            end = _int_arg(args.get("page_end"), default=start, minimum=start)
            requested_end = min(end, total_pages)
            end = min(requested_end, start + max_pages - 1)
            return _PdfPageWindow(
                page_start=start,
                page_end=end,
                requested_page_end=requested_end,
                truncated=end < requested_end,
                page_label=str(page_label_raw),
                resolved_page=resolved,
            )

        start = _int_arg(args.get("page_start"), default=1, minimum=1)
        default_end = total_pages if default_all else start
        end = _int_arg(args.get("page_end"), default=default_end, minimum=start)
    except (TypeError, ValueError):
        return SegmentResult(
            error="page_start/page_end/page_label must identify PDF pages",
            extras={"total_pages": total_pages},
        )

    start = max(1, min(start, total_pages))
    requested_end = max(start, min(end, total_pages))
    capped_end = min(requested_end, start + max_pages - 1)
    return _PdfPageWindow(
        page_start=start,
        page_end=capped_end,
        requested_page_end=requested_end,
        truncated=capped_end < requested_end,
    )


def _add_pdf_window_extras(
    extras: dict[str, Any],
    window: _PdfPageWindow,
    doc: Any,
) -> None:
    page_end = doc.page_start + len(doc.pages) - 1 if doc.pages else doc.page_start
    extras.update({
        "page_start": doc.page_start,
        "page_end": page_end,
        "total_pages": doc.total_pages,
    })
    if doc.page_labels:
        extras["page_label_start"] = doc.page_labels[0]
        extras["page_label_end"] = doc.page_labels[-1]
    if window.page_label is not None:
        extras["page_label"] = window.page_label
        extras["resolved_page"] = window.resolved_page
    if window.truncated:
        extras["window_truncated"] = True
        extras["requested_page_end"] = window.requested_page_end


_PAGE_MARKER_RE = re.compile(r"\[Page (\d+)\]")


def _page_range_from_offset(
    body: str, offset: int, max_chars: int, total_pages: int,
) -> tuple[int, int]:
    """Given a char offset in the concatenated PDF body (with [Page N]
    markers), find the page_start and page_end for the chunk that would
    be read at that offset."""
    # Find all [Page N] marker positions.
    markers = [(m.start(), int(m.group(1))) for m in _PAGE_MARKER_RE.finditer(body)]
    if not markers:
        return 1, total_pages
    # page_start: the last marker whose position <= offset.
    ps = 1
    for pos, pn in markers:
        if pos <= offset:
            ps = pn
        else:
            break
    # page_end: find the last marker whose position < offset + max_chars.
    end = offset + max_chars
    pe = ps
    for pos, pn in markers:
        if pos < end:
            pe = pn
        else:
            break
    return ps, pe


def _clamp_pdf(
    text: str, offset: int, max_chars: int,
    *, extras: dict[str, Any] | None = None,
) -> SegmentResult:
    extras = dict(extras or {})
    total = len(text)
    chunk = text[offset: offset + max_chars]
    truncated = (offset + len(chunk)) < total
    extras.update({
        "offset": offset,
        "char_count": len(chunk),
        "total_chars": total,
        "truncated": truncated,
    })
    if truncated:
        extras["next_offset"] = offset + len(chunk)
    if not chunk:
        return SegmentResult(text="", error="empty result", extras=extras)
    return SegmentResult(text=chunk, extras=extras)


def _pdf_pattern_search(
    *, pages: list[str], pattern: str,
    context_lines: int, max_matches: int,
    match_offset: int = 0, page_offset: int = 0,
    total_pages_full: int | None = None,
    page_labels: list[str] | None = None,
) -> SegmentResult:
    try:
        rx = re.compile(pattern, re.IGNORECASE | re.MULTILINE)
    except re.error as exc:
        return SegmentResult(error=f"invalid regex: {exc}")

    full_total_pages = total_pages_full if total_pages_full is not None else len(pages)

    all_hits: list[dict[str, Any]] = []
    for idx, page_text in enumerate(pages):
        if not page_text:
            continue
        page_no = idx + 1 + page_offset
        label = page_labels[idx] if page_labels and idx < len(page_labels) else None
        page_lines = page_text.splitlines()
        for m in rx.finditer(page_text):
            line_no = page_text.count("\n", 0, m.start()) + 1
            s = max(0, line_no - 1 - context_lines)
            e = min(len(page_lines), line_no + context_lines)
            hit = {
                "page": page_no,
                "line": line_no,
                "match": m.group(0)[:200],
                "context": "\n".join(page_lines[s:e]),
            }
            if label is not None:
                hit["page_label"] = label
            all_hits.append(hit)

    total = len(all_hits)
    hits = all_hits[match_offset: match_offset + max_matches]
    has_more = (match_offset + len(hits)) < total

    extras: dict[str, Any] = {
        "pattern": pattern,
        "match_count": len(hits),
        "total_matches": total,
        "match_offset": match_offset,
        "has_more": has_more,
        "hits": hits,
        "total_pages": full_total_pages,
    }
    if page_offset:
        extras["scope_page_start"] = page_offset + 1
        extras["scope_page_end"] = page_offset + len(pages)
    if has_more:
        extras["next_match_offset"] = match_offset + len(hits)

    if not hits:
        if match_offset and total:
            err = f"match_offset {match_offset} exceeds total_matches {total}"
        else:
            err = "no matches"
        return SegmentResult(text="", error=err, extras=extras)

    rendered_lines: list[str] = []
    for h in hits:
        label = h.get("page_label")
        label_text = f" label {label}" if label and label != str(h["page"]) else ""
        rendered_lines.append(
            f"[Page {h['page']}{label_text} L{h['line']}] "
            f"{h['match']}\n  > {h['context']}"
        )
    return SegmentResult(text="\n\n".join(rendered_lines), extras=extras)
