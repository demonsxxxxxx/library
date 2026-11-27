"""On-demand text and layout views for a live, imported PDF entry."""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
import sys
from pathlib import Path
from threading import Lock
from uuid import UUID

import httpx
from anyio import from_thread, to_thread
from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, UUID4

from library.config import get_settings, has_vision_profile, resolve_profile
from library.db.session import session_scope
from library.document_revisions import _page_image
from library.document_revisions._layout_worker import MAX_LAYOUT_PAGES
from library.document_revisions._weknora_worker import MAX_PDF_BYTES, MAX_PDF_PAGES
from library.document_revisions.archive import RevisionArchive, sha256
from library.document_revisions.layout import Layout, LayoutPage, local_layout
from library.document_revisions.models import DocumentRef, RevisionRef
from library.document_revisions.paddle_ocr import recognize_pages
from library.document_revisions.pdf import VisionReply, build_pdf_revision
from library.document_revisions.weknora import WeKnoraBuiltin, project_pages
from library.llm import ChatMessage, ChatRequest, ImageBlock, TextBlock, get_chat_client
from library.provider_clients import provider_send_check
from library.services.user_files import EntryNotFoundError, EntryRemoteNotHydratedError, open_for_download

router = APIRouter(prefix="/file-entries/{entry_id}/pdf-revisions", tags=["pdf-viewer"])
_build_lock = Lock()  # One foreground model worker per server process.
log = logging.getLogger(__name__)


class BuildOptions(BaseModel):
    allow_vision: bool = False


def _archive():
    settings = get_settings()
    if not settings.pdf_revision_viewer_enabled:
        raise HTTPException(404, "PDF revision viewer disabled")
    return RevisionArchive(Path(settings.library_home))


async def _source(entry_id: str):
    async with session_scope() as session:
        try:
            handle = await open_for_download(session, entry_id=entry_id)
        except EntryNotFoundError:
            raise HTTPException(404, "Entry not available")
        except EntryRemoteNotHydratedError:
            raise HTTPException(409, "Download the original first")
    if not handle.sha256 or not (handle.mime_type == "application/pdf" or handle.display_name.lower().endswith(".pdf")):
        raise HTTPException(415, "Expected a versioned PDF")
    return DocumentRef(entry_id=entry_id, file_id=handle.file_id, sha256=handle.sha256), handle


async def _bytes(handle):
    if handle.size_bytes > MAX_PDF_BYTES:
        raise HTTPException(413, "Viewer supports PDFs up to 16 MiB")
    data = bytearray()
    async for chunk in handle.stream:
        if len(data) + len(chunk) > MAX_PDF_BYTES:
            raise HTTPException(413, "Viewer size limit exceeded")
        data.extend(chunk)
    if sha256(data) != handle.sha256:
        raise HTTPException(409, "Stored original changed; cannot verify this revision")
    return bytes(data)


def _allowed(doc):
    s = get_settings()
    grant = s.pdf_revision_vision_grants.get(doc.sha256)
    if not grant or not (s.pdf_revision_viewer_enabled and s.document_vision_enabled
                         and s.llm_vision_supports_vision and has_vision_profile(s)):
        return False
    profile = resolve_profile(s, "vision")
    # Require an explicit endpoint; do not defer its identity to SDK/environment defaults.
    return bool(grant.base_url and profile.base_url and grant.model == profile.model
                and grant.provider == profile.provider
                and grant.base_url.rstrip("/") == profile.base_url.rstrip("/"))


async def _vision(doc, client, request):
    live, _ = await _source(doc.entry_id)
    if live != doc or not _allowed(doc):
        return VisionReply(status="failed")
    if get_chat_client("vision") is not client or client.model != request.model:
        return VisionReply(status="failed")
    async def authorize_send(outgoing: httpx.Request):
        current, handle = await _source(doc.entry_id)
        if current != doc:
            raise PermissionError("vision original changed")
        await _bytes(handle)
        if not _allowed(doc) or get_chat_client("vision") is not client:
            raise PermissionError("vision authorization changed")
        grant = get_settings().pdf_revision_vision_grants[doc.sha256]
        base = httpx.URL(grant.base_url)
        actual = outgoing.url
        if (actual.scheme, actual.host, actual.port) != (base.scheme, base.host, base.port) or not (
            actual.path.startswith(base.path.rstrip("/") + "/") and grant.model == request.model
        ):
            raise PermissionError("vision destination not authorized")

    message = ChatRequest(
        system='Transcribe the visible page, without interpretation. Return only JSON: '
               '{"status":"complete" or "unreadable", "markdown":"verbatim transcription"}. '
               'Use unreadable if any text cannot be transcribed reliably.',
        messages=[ChatMessage(role="user", content=[TextBlock(text="Transcribe this page."),
            ImageBlock(media_type="image/jpeg", data_b64=base64.b64encode(request.jpeg).decode("ascii"))])],
        max_tokens=4096, temperature=0,
    )
    with provider_send_check(authorize_send):
        response = await asyncio.wait_for(client.complete(message, retry=False), timeout=60)
    if response.stop_reason != "end_turn":
        return VisionReply(status="truncated" if response.stop_reason == "max_tokens" else "failed")
    return VisionReply.model_validate(json.loads(response.text or "{}"))


@router.get("")
async def list_versions(entry_id: str):
    archive = _archive()
    doc, _ = await _source(entry_id)
    refs = await to_thread.run_sync(archive.versions, doc)
    return {"versions": [r.revision_id for r in refs],
            "vision_available": not get_settings().pdf_revision_paddle_ocr_enabled and _allowed(doc)}


@router.post("", status_code=201)
async def recognize(entry_id: str, options: BuildOptions):
    archive = _archive()
    s = get_settings()
    doc, handle = await _source(entry_id)
    if not s.pdf_revision_weknora_python or not s.pdf_revision_weknora_source:
        raise HTTPException(503, "Configure the pinned text recognizer first")
    if not _build_lock.acquire(blocking=False):
        raise HTTPException(429, "Another PDF is being recognized; retry when it finishes")
    try:
        pdf = await _bytes(handle)
        parser = WeKnoraBuiltin(Path(s.pdf_revision_weknora_python), Path(s.pdf_revision_weknora_source))
        allowed = not s.pdf_revision_paddle_ocr_enabled and options.allow_vision and _allowed(doc)
        client = get_chat_client("vision") if allowed else None
        model = client.model if client else None

        async def check_source():
            live, _ = await _source(entry_id)
            if live != doc:
                raise HTTPException(409, "Original changed while recognition was running")

        def generate(native, paddle_pages):
            def layout(data):
                warning = "layout_not_configured"
                if len(native.has_native_text) > MAX_LAYOUT_PAGES:
                    warning = "layout_page_limit"
                elif s.pdf_revision_docling_python and s.pdf_revision_hf_cache:
                    try:
                        projection = local_layout(data, s.pdf_revision_docling_python, s.pdf_revision_hf_cache)
                        projection.check_source(doc.sha256, len(native.has_native_text))
                        return projection
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        warning = "layout_timeout" if isinstance(exc, subprocess.TimeoutExpired) else "layout_failed"
                        log.warning("PDF layout failed (%s)", type(exc).__name__)
                return Layout(source_sha256=doc.sha256, versions={}, complete=False, warning=warning,
                              pages=[LayoutPage(page=i+1, regions=[]) for i in range(len(native.has_native_text))])
            return build_pdf_revision(pdf, document=doc, filename=handle.display_name, archive=archive,
                parser=lambda _: native, enabled=True, recognize_layout=layout,
                vision_model=model or "not-configured", permit_vision=lambda d, m, _: d == doc and m == model and allowed,
                vision=(lambda req: from_thread.run(_vision, doc, client, req)) if client else None,
                paddle_pages=paddle_pages)
        try:
            native = await to_thread.run_sync(parser, pdf)
            paddle_pages = None
            if s.pdf_revision_paddle_ocr_enabled:
                pages = await asyncio.to_thread(project_pages, native, source_sha256=doc.sha256)
                paddle_pages = await recognize_pages(pdf, pages, before_send=check_source)
                await check_source()
            ref = await to_thread.run_sync(generate, native, paddle_pages)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            log.warning("PDF revision build failed (%s)", type(exc).__name__)
            raise HTTPException(422, f"PDF recognition failed; check parser configuration and the {MAX_PDF_PAGES}-page limit")
        await check_source()
        return {"revision_id": ref.revision_id}
    finally:
        _build_lock.release()


async def _read(entry_id: str, rid: UUID):
    archive = _archive()
    doc, handle = await _source(entry_id)
    ref = RevisionRef(document=doc, revision_id=rid)
    try:
        value = await to_thread.run_sync(lambda: archive.read(ref, allowed={ref}))
    except (FileNotFoundError, ValueError):
        raise HTTPException(404, "No verified revision for this entry and original")
    return value, await _bytes(handle), archive


@router.get("/{rid}")
async def view(entry_id: str, rid: UUID4):
    revision, _, _ = await _read(entry_id, rid)
    segments = []
    for row in revision.source_map:
        segment = {"page": row.locator.page, "kind": row.kind, "asset_ids": row.asset_ids,
                   "text": revision.content[row.content_start:row.content_end] if row.kind != "image" else ""}
        if figure := revision.figure_link(row):
            segment["figure"] = figure
        segments.append(segment)
    return {"revision_id": rid, "coverage": revision.manifest.coverage, "layout": revision.layout,
            "segments": segments}


@router.get("/{rid}/assets/{asset_id}")
async def asset(entry_id: str, rid: UUID4, asset_id: str):
    value, _, archive = await _read(entry_id, rid)
    ref = value.manifest.ref
    try:
        data = await to_thread.run_sync(lambda: archive.read_asset(ref, asset_id, allowed={ref}))
    except KeyError:
        raise HTTPException(404, "Unknown revision asset")
    mime = next(a.mime for a in value.manifest.assets if a.asset_id == asset_id)
    return Response(data, media_type=mime, headers={"Cache-Control": "private, no-store"})


@router.get("/{rid}/pages/{page}")
async def original_page(entry_id: str, rid: UUID4, page: int):
    value, pdf, _ = await _read(entry_id, rid)
    if not 1 <= page <= value.manifest.coverage.total:
        raise HTTPException(404, "Invalid physical page")
    def render():
        return subprocess.run([sys.executable, "-I", "-B", _page_image.__file__, str(page)],
                              input=pdf, capture_output=True, timeout=15, check=True).stdout
    return Response(await to_thread.run_sync(render), media_type="image/jpeg",
                    headers={"Cache-Control": "private, no-store"})
