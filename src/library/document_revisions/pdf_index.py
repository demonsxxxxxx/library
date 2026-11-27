"""Feed Library's existing PDF summary pipeline from one specific, verified revision."""
import asyncio
from pathlib import Path

from library.config import get_settings
from library.pipelines.base import PipelineContext, PipelineResult
from library.pipelines.pdf import PdfPipeline
from library.tasks.ingest_guard import check_ingest_delivery

from ._weknora_worker import MAX_PDF_PAGES
from .archive import RevisionArchive
from .models import Coverage, RevisionRef


async def index_pdf_revision(ctx: PipelineContext, ref: RevisionRef, pipeline: PdfPipeline) -> PipelineResult:
    if ref.document.file_id != ctx.file_id or ref.document.sha256 != ctx.sha256:
        raise ValueError("parser revision does not match ingest source")
    archive = RevisionArchive(Path(get_settings().library_home))
    revision = await asyncio.to_thread(archive.read, ref, allowed={ref})
    manifest = revision.manifest
    c = manifest.coverage
    if not isinstance(c, Coverage) or manifest.parser.name != "weknora-docreader-builtin":
        raise ValueError("unexpected parser revision for PDF ingest")
    if not c.processed:
        raise ValueError("no mapped page text in PDF revision; scan requires authorized recognition")
    parts: list[list[str]] = [[] for _ in c.pages]
    for row in revision.source_map:
        if row.kind != "image":  # An image reference is not transcribed evidence.
            parts[row.locator.page - 1].append(revision.content[row.content_start:row.content_end])
    pages = ["\n\n".join(text) if text else "[No verified transcription for this page]" for text in parts]
    coverage = {
        "revision_id": str(ref.revision_id), "parser": manifest.parser.name,
        "parse_status": manifest.status, "failed_pages": list(c.failed), "partial_pages": list(c.partial),
        "indexed_pages": c.processed, "max_index_pages": MAX_PDF_PAGES, "indexed_partial": manifest.status != "parsed",
        "partial_reasons": sorted({reason for pg in c.pages for reason in pg.warnings}),
    }
    if manifest.parser.ocr_backend:
        coverage["ocr_backend"] = manifest.parser.ocr_backend
        coverage["ocr_pages"] = [pg.page for pg in c.pages if pg.engine == "paddle_ocr"]
    await check_ingest_delivery()
    return await pipeline.index_revision_pages(ctx, pages, coverage)
