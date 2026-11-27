"""Ingest-side publication: external extraction has no catalogue or archive write access."""
import asyncio
from pathlib import Path

from library.config import get_settings
from library.db.session import session_scope
from library.pipelines.base import PipelineContext
from library.repositories import entries, tasks
from library.storage.base import StorageBackend
from library.tasks.ingest_guard import IngestCancelled, check_ingest_delivery

from ._weknora_worker import MAX_PDF_BYTES
from .archive import RevisionArchive, sha256
from .models import DocumentRef, RevisionRef
from .native import parse_native
from .paddle_ocr import BACKEND, recognize_pages
from .pdf import build_pdf_revision
from .weknora import WeKnoraBuiltin, project_pages
from .weknora_office import PARSER_NAME as W_OFFICE, parse_weknora_office


async def archive_delivery(ctx: PipelineContext, entry_id: str, format: str, storage: StorageBackend,
                           delivery_id: str | None, *, use_weknora: bool = False, use_office: bool = False) -> RevisionRef:
    doc = DocumentRef(entry_id=entry_id, file_id=ctx.file_id, sha256=ctx.sha256)
    settings = get_settings()
    archive = RevisionArchive(Path(settings.library_home))
    if ctx.size_bytes > MAX_PDF_BYTES:
        raise ValueError("revision exceeds 16 MiB input limit")
    buffer = bytearray()
    async for chunk in storage.get(ctx.storage_key):
        if len(buffer) + len(chunk) > MAX_PDF_BYTES:
            raise ValueError("revision exceeds 16 MiB input limit")
        buffer.extend(chunk)
    data = bytes(buffer)
    filename = ctx.display_name or format
    weknora_office = use_office and settings.office_ingest_parser == "weknora-builtin"
    paddle_enabled = (use_weknora or weknora_office) and settings.pdf_revision_paddle_ocr_enabled
    parser_name = (W_OFFICE if weknora_office else settings.office_ingest_parser if use_office else
                   "weknora-docreader-builtin" if use_weknora else "library-native")

    def existing(document: DocumentRef) -> RevisionRef | None:
        if delivery_id:
            for ref in archive.versions(document, delivery_id=delivery_id):
                manifest = archive.read(ref, allowed={ref}).manifest
                if manifest.parser.name != parser_name:
                    raise ValueError("ingest parser changed for this delivery; start a new task")
                if (use_weknora or weknora_office) and manifest.parser.ocr_backend != (BACKEND if paddle_enabled else None):
                    raise ValueError("OCR configuration changed for this delivery; start a new task")
                return ref
        return None

    parsed = None
    paddle_pages = None
    if weknora_office:
        if not await asyncio.to_thread(existing, doc):
            parsed = await parse_weknora_office(
                data, document=doc, filename=filename, fmt=format,
                python=settings.pdf_revision_weknora_python, checkout=settings.pdf_revision_weknora_source,
                before_send=check_ingest_delivery, paddle=paddle_enabled,
            )
    elif use_office:
        if settings.office_ingest_parser == "anydoc":
            from .anydoc import parse_anydoc as parser
            python = settings.office_anydoc_python
        else:
            from .office import parse_office as parser
            python = settings.office_docling_python
        if sha256(data) != doc.sha256:
            raise ValueError("Office original checksum mismatch")
        if not await asyncio.to_thread(existing, doc):
            parsed = await asyncio.to_thread(parser, data, document=doc, filename=filename,
                                             format=format, python=python)
    elif use_weknora:
        if format != "pdf" or sha256(data) != doc.sha256:
            raise ValueError("expected original PDF for WeKnora ingest")
        # A summary retry reuses its verified revision without running docreader again.
        previous = await asyncio.to_thread(existing, doc)
        export = None
        if previous is None:
            if not settings.pdf_revision_weknora_python or not settings.pdf_revision_weknora_source:
                raise ValueError("WeKnora ingest requires PDF_REVISION_WEKNORA_PYTHON and PDF_REVISION_WEKNORA_SOURCE")
            parser = WeKnoraBuiltin(Path(settings.pdf_revision_weknora_python), Path(settings.pdf_revision_weknora_source))
            export = await asyncio.to_thread(parser, data)
            if paddle_enabled:
                pages = await asyncio.to_thread(project_pages, export, source_sha256=doc.sha256)
                paddle_pages = await recognize_pages(data, pages, before_send=check_ingest_delivery)
    else:
        parsed = await asyncio.to_thread(parse_native, data, document=doc, filename=filename, format=format)
    # Only this fenced caller may publish; the worker cannot publish after cancellation.
    async with session_scope() as db:
        await tasks.lock_ingest_file(db, ctx.file_id)
        await check_ingest_delivery(db)
        pair = await entries.get_live_with_file(db, entry_id)
        if pair is None:
            survivor = await entries.find_first_live_for_file(db, ctx.file_id)
            if survivor is not None:
                pair = await entries.get_live_with_file(db, survivor.id)
                doc = doc.model_copy(update={"entry_id": survivor.id})
                filename = survivor.display_name
        if pair is None or pair[1].id != ctx.file_id or pair[1].sha256 != ctx.sha256:
            raise IngestCancelled("archive source no longer live or no longer matches")
        if ref := existing(doc):
            return ref
        if use_weknora:
            if export is None:
                # The cached source entry disappeared between the read and publication fence.
                raise ValueError("revision source changed during retry; retry with the live entry")
            return build_pdf_revision(data, document=doc, filename=filename, archive=archive,
                                      parser=lambda _: export, enabled=True, delivery_id=delivery_id,
                                      paddle_pages=paddle_pages)
        if parsed is None:
            raise ValueError("Office revision source changed during retry")
        parsed.document = doc
        parsed.filename = filename
        return parsed.publish(archive, delivery_id=delivery_id)
