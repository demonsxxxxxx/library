"""Explicitly enabled local PDF revision build. No ingest, DB, or provider wiring."""

import io
import json
from collections.abc import Callable
from pathlib import PurePath
from typing import Literal
from uuid import uuid4

from ._weknora_worker import MAX_PDF_BYTES, WEKNORA_COMMIT
from .archive import ASSET_SCHEME, RevisionArchive, archive_status, image_markdown, sha256
from .layout import Layout, LayoutPage
from .models import (
    Asset,
    Coverage,
    DocumentRef,
    Identity,
    Manifest,
    NormalizedBox,
    PageCoverage,
    PageNumber,
    ParserStamp,
    PdfInput,
    PdfLocator,
    RevisionRef,
    SourceMapRow,
    SourceVersion,
    TableData,
    TableRef,
    _Record,
)
from .paddle_ocr import BACKEND, OcrPage
from .weknora import PARSER_ENV, WExport, WImage, WPage, project_pages


class VisionReply(_Record):
    """The caller maps the provider's actual completion status, not text alone."""

    status: Literal["complete", "truncated", "unreadable", "failed"]
    markdown: str = ""


class VisionRequest(_Record):
    model: Identity
    page: PageNumber
    jpeg: bytes


VisionClient = Callable[[VisionRequest], VisionReply]
VisionPermission = Callable[[DocumentRef, str, int], bool]


def _transcribe(
    page: WPage,
    scan: WImage,
    document: DocumentRef,
    model: str,
    vision: VisionClient | None,
    permit_vision: VisionPermission | None,
) -> tuple[PageCoverage, str]:
    engine: Literal["none", "vision"] = "none"
    reason = ""
    if page.has_native_text:
        reason = "scan_has_native_layer_needs_review"
    elif scan.jpeg is None:
        reason = "scan_image_missing_or_invalid"
    elif vision is None:
        reason = "vision_client_unconfigured"
    else:
        try:
            allowed = permit_vision is not None and permit_vision(document, model, page.number) is True
        except Exception:
            reason = "vision_policy_error"
        else:
            if not allowed:
                reason = "vision_not_authorized"
            else:
                engine = "vision"
                try:
                    response = vision(VisionRequest(model=model, page=page.number, jpeg=scan.jpeg))
                except TimeoutError:
                    reason = "vision_timeout"
                except Exception:
                    reason = "vision_request_failed"
                else:
                    if not isinstance(response, VisionReply):
                        reason = "vision_invalid_response"
                    elif response.status != "complete":
                        reason = f"vision_{response.status}"
                    elif not (text := response.markdown.strip()):
                        reason = "vision_empty_response"
                    elif ASSET_SCHEME in text.lower():
                        reason = "vision_reserved_asset_uri"
                    else:
                        return PageCoverage(page=page.number, engine=engine, status="parsed"), text
    return PageCoverage(page=page.number, engine=engine, status="failed", warnings=(reason,)), ""


def build_pdf_revision(
    pdf: bytes,
    *,
    document: DocumentRef,
    filename: str,
    archive: RevisionArchive,
    parser: Callable[[bytes], WExport],
    enabled: bool = False,
    delivery_id: str | None = None,
    vision: VisionClient | None = None,
    permit_vision: VisionPermission | None = None,
    recognize_layout: Callable[[bytes], Layout] | None = None,
    vision_model: str = "deepseek-v4-flash",
    paddle_pages: dict[int, OcrPage] | None = None,
) -> RevisionRef:
    """Archive an explicitly selected local PDF; incomplete evidence stays incomplete."""
    if not enabled:
        raise RuntimeError("local PDF revision path is disabled")
    if not pdf or len(pdf) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds local revision slice size limit")
    if sha256(pdf) != document.sha256:
        raise ValueError("PDF bytes do not match the specified original")
    # Store only the display name, never the caller's local path.
    input_file = PdfInput(filename=PurePath(filename.replace("\\", "/")).name)
    if vision is not None and not vision_model.strip():
        raise ValueError("vision model is required when a client is provided")

    parsed = parser(pdf)
    pages = project_pages(parsed, source_sha256=document.sha256)
    ocr = paddle_pages or {}
    if set(ocr) - {p.number for p in pages}:
        raise ValueError("OCR physical page does not belong to the PDF")
    if any(ASSET_SCHEME in part.lower()
           for page in pages for part in page.parts if isinstance(part, str)):
        raise ValueError("docreader text uses the reserved Library asset URI scheme")
    layout = None
    if recognize_layout is not None:
        layout = recognize_layout(pdf)
        layout.check_source(document.sha256, len(pages))
    if ocr:
        existing = {p.page: p for p in layout.pages} if layout else {}
        projected = {n: item.layout for n, item in ocr.items() if item.layout is not None}
        if any(n != page.page for n, page in projected.items()):
            raise ValueError("OCR layout and physical page disagree")
        if projected:
            layout = Layout(source_sha256=document.sha256, versions={**(layout.versions if layout else {}),
                            "ocr_backend": BACKEND}, complete=False, warning="paddle_ocr_selected_pages_only",
                            pages=[projected.get(p.number) or existing.get(p.number)
                                   or LayoutPage(page=p.number, regions=[]) for p in pages])
    layout_bytes = None
    tables = {}
    if layout is not None:
        layout_bytes = layout.model_dump_json().encode("utf-8")
        for pg in layout.pages:
            for region in pg.regions:
                if region.grid is not None:
                    tid = f"table-{len(tables)+1}"
                    table = TableData(table_id=tid, locator=PdfLocator(page=pg.page), source="layout", grid=region.grid)
                    tables[tid] = table.model_dump_json().encode("utf-8")
    revision_id = uuid4()
    fragments: list[str] = []
    rows: list[SourceMapRow] = []
    assets: dict[str, bytes] = {}
    coverage: list[PageCoverage] = []
    offset = 0
    scanned = 0
    fallback_reader = None

    def append(
        text: str, page: int, kind: Literal["native", "image", "vision", "ocr"], asset_id: str | None = None,
        *, bbox: NormalizedBox | None = None, figure_map_id: str | None = None,
    ) -> str:
        nonlocal offset
        if fragments:
            offset += 2  # The separator written by the join below.
        end = offset + len(text)
        rows.append(SourceMapRow(
            source_map_id=f"map-{len(rows) + 1}", revision_id=revision_id, kind=kind,
            content_start=offset, content_end=end, locator=PdfLocator(page=page, bbox=bbox),
            asset_ids=(asset_id,) if asset_id else (),
            figure_map_id=figure_map_id,
        ))
        fragments.append(text)
        offset = end
        return rows[-1].source_map_id

    for page in pages:
        has_w_text = any(isinstance(part, str) for part in page.parts)
        recognized = ocr.get(page.number)
        ocr_text = recognized.text if recognized else ""
        scan: WImage | None = None
        scan_asset_id: str | None = None
        attached: set[str] = set()
        warnings: list[str] = []
        for part in page.parts:
            if isinstance(part, str):
                if not ocr_text:
                    append(part, page.number, "native")
                continue
            if part.scanned:
                scan = part
                scanned += 1
            if part.jpeg is None:
                warnings.append("archive_image_missing_or_invalid")
                continue
            asset_id = sha256(part.jpeg)
            assets.setdefault(asset_id, part.jpeg)
            append(image_markdown(asset_id), page.number, "image", asset_id)
            attached.add(asset_id)
            if part.scanned:
                scan_asset_id = asset_id

        if recognized is not None:
            warnings.extend(recognized.warnings)
            figures = []
            for figure in recognized.figures:
                asset_id = sha256(figure.image)
                assets.setdefault(asset_id, figure.image)
                # Same JPEG bytes can occur at different positions; each gets its own row.
                figure_row = append(image_markdown(asset_id), page.number, "image", asset_id, bbox=figure.bbox)
                figures.append((figure, figure_row))
                attached.add(asset_id)
            for data in recognized.images:
                asset_id = sha256(data)
                if asset_id not in attached:
                    assets.setdefault(asset_id, data)
                    append(image_markdown(asset_id), page.number, "image", asset_id)
                    attached.add(asset_id)
            if ocr_text:
                cursor = 0
                for figure, figure_row in figures:
                    if text := ocr_text[cursor:figure.start].strip():
                        append(text, page.number, "ocr")
                    append(ocr_text[figure.start:figure.end], page.number, "ocr",
                           bbox=figure.caption_bbox, figure_map_id=figure_row)
                    cursor = figure.end
                if text := ocr_text[cursor:].strip():
                    append(text, page.number, "ocr")
                coverage.append(PageCoverage(
                    page=page.number, engine="paddle_ocr", status="partial",
                    warnings=tuple(dict.fromkeys([*warnings, "paddle_ocr_needs_review"])),
                ))
                continue

        if not has_w_text and page.has_native_text:
            # W's image-area classifier can return only a JPEG for a born-digital
            # page. Recover the original text of that physical page, not a caption.
            from pypdf import PdfReader
            from library.pipelines.pdf_text import _extract_page_text

            try:
                if fallback_reader is None:
                    fallback_reader = PdfReader(io.BytesIO(pdf))
                original_text = (_extract_page_text(fallback_reader.pages[page.number - 1]) or "").strip()
            except Exception:
                original_text = ""
            if (any(c.isalnum() for c in original_text) and "\ufffd" not in original_text
                    and "\x00" not in original_text and ASSET_SCHEME not in original_text.lower()):
                append(original_text, page.number, "native")
                warnings.append("native_text_recovered_from_original")
                has_w_text = True

        if has_w_text:
            # W can expose text without proving column order, table structure, or
            # all same-page image content. This slice has no layout certifier.
            warnings.append("native_layout_not_verified")
            if scan is not None:
                warnings.append("mixed_page_region_needs_review")
            coverage.append(PageCoverage(
                page=page.number, engine="text_layer", status="partial", warnings=tuple(warnings),
            ))
        elif recognized is not None:
            coverage.append(PageCoverage(
                page=page.number, engine="none", status="failed",
                warnings=tuple(dict.fromkeys([*warnings, "no_mapped_page_text"])),
            ))
        elif scan is not None:
            page_state, text = _transcribe(page, scan, document, vision_model, vision, permit_vision)
            coverage.append(page_state)
            if text:
                append(text, page.number, "vision", scan_asset_id)
        else:
            coverage.append(PageCoverage(
                page=page.number, engine="none", status="failed",
                warnings=(*warnings, "no_mapped_page_text"),
            ))

    body = "\n\n".join(fragments).encode("utf-8")
    source_map = "".join(row.archive_json() + "\n" for row in rows).encode("utf-8")
    failed = tuple(p.page for p in coverage if p.status == "failed")
    stats = Coverage(
        total=len(pages), processed=len(pages) - len(failed), failed=failed,
        partial=tuple(p.page for p in coverage if p.status == "partial"),
        image_count=len(assets), pages=tuple(coverage),
    )
    config_values = {
        "runtime_versions": parsed.runtime_versions, "worker_env": PARSER_ENV,
        "page_projection": "native-recovery-v1",
        "vision_model": vision_model if vision is not None else None,
    }
    if paddle_pages is not None:
        config_values["ocr_backend"] = BACKEND
        config_values["figure_projection"] = "ocr-crop-caption-v1"
    config = json.dumps(config_values, sort_keys=True).encode("utf-8")
    has_native = any(p.engine == "text_layer" for p in coverage)
    route = "text" if not scanned else "ocr" if scanned == len(pages) and not has_native else "mixed"
    if any(p.engine == "paddle_ocr" for p in coverage):
        route = "mixed" if has_native else "ocr"
    manifest = Manifest(
        revision_id=revision_id, file_id=document.file_id, entry_id=document.entry_id,
        source_version=SourceVersion(sha256=document.sha256, size_bytes=len(pdf)), input=input_file,
        delivery_id=delivery_id,
        parser=ParserStamp(
            route=route,
            version=WEKNORA_COMMIT, config_digest=sha256(config),
            vision_model=vision_model if vision is not None else None,
            ocr_backend=BACKEND if paddle_pages is not None else None,
        ),
        layout_sha256=sha256(layout_bytes) if layout_bytes is not None else None,
        tables=tuple(TableRef(table_id=k, sha256=sha256(v)) for k,v in tables.items()),
        coverage=stats, status=archive_status(stats),
        assets=tuple(Asset(asset_id=key, size_bytes=len(data)) for key, data in assets.items()),
        content_sha256=sha256(body), source_map_sha256=sha256(source_map),
    )
    archive._publish(manifest, body, source_map, assets, layout_bytes, tables)
    return manifest.ref
