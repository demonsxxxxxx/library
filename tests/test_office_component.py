"""Synthetic Office upload -> actual handler -> verified revision, no external IO."""
import io
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches

from library.config import get_settings
from library.db.engine import dispose_engine
from sqlalchemy import select

from library.db.models import File, Task
from library.repositories.tasks import mark_running
from library.db.session import session_scope
from library.document_revisions import office, weknora_office
from library.document_revisions.archive import RevisionArchive, sha256
from library.document_revisions.models import DocumentRef
from library.main import app
from library.pipelines import _text_indexer
from library.pipelines.base import PipelineResult
from library.pipelines.docx import DocxPipeline
from library.pipelines.spreadsheet import SpreadsheetPipeline
from library.pipelines.pptx import PptxPipeline
from library.storage import reset_storage_cache
from library.tasks.handlers.ingest_file import handle_ingest_file


def sample(fmt):
    buf = io.BytesIO()
    if fmt == "docx":
        doc = Document()
        doc.add_paragraph("OfficeAnchor")
        doc.add_paragraph("repeat")
        doc.add_paragraph("repeat")
        doc.save(buf)
    elif fmt == "xlsx":
        wb = Workbook()
        ws = wb.active
        ws.title = "Test"
        for addr, value in [("A1","header"),("A2","OfficeAnchor"),("A3","OfficeAnchor")]:
            ws[addr] = value
        wb.save(buf)
    else:
        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(1)).text="OfficeAnchor"
        prs.save(buf)
    return buf.getvalue()


@pytest.mark.asyncio
@pytest.mark.parametrize("fmt", ["docx", "xlsx", "pptx"])
@pytest.mark.parametrize("parser", ["docling-office", "anydoc", "weknora-office-builtin"])
async def test_office_upload_with_pinned_worker(tmp_path, monkeypatch, fmt, parser):
    anydoc = parser == "anydoc"
    weknora = parser == "weknora-office-builtin"
    python = os.environ.get("LIBRARY_TEST_WEKNORA_PYTHON" if weknora else
                            "LIBRARY_TEST_ANYDOC_PYTHON" if anydoc else "LIBRARY_TEST_OFFICE_PYTHON")
    checkout = os.environ.get("LIBRARY_TEST_WEKNORA_SOURCE", "")
    if not python or (weknora and not checkout):
        pytest.skip(f"supply pinned {parser} Python for live Office test")
    for key, value in dict(LIBRARY_HOME=str(tmp_path), DB_BACKEND="sqlite", STORAGE_BACKEND="local",
            WORKER_ENABLED="false", LLM_DEFAULT_API_KEY="fixture",
            DOCUMENT_INGEST_PARSER="weknora" if weknora else "configured",
            OFFICE_INGEST_PARSER="library" if weknora else parser,
            PDF_REVISION_WEKNORA_PYTHON=python, PDF_REVISION_WEKNORA_SOURCE=checkout,
            OFFICE_DOCLING_PYTHON=python, OFFICE_ANYDOC_PYTHON=python).items():
        monkeypatch.setenv(key,value)
    retry = anydoc and fmt == "docx"
    queries = []
    async def index(body, ctx, *, kind, pipeline, coverage):
        queries.append(body)
        if retry and len(queries) == 1:
            raise RuntimeError("summary interrupted")
        assert "OfficeAnchor" in body and coverage["parser"]==parser
        if weknora:
            assert coverage["ocr_backend"] == "paddle-layout-v1"
        return PipelineResult("test", {"coverage":coverage}, kind, None, None, None)
    monkeypatch.setattr(_text_indexer,"index_extracted_text",index)
    for cls in (DocxPipeline, SpreadsheetPipeline, PptxPipeline):
        monkeypatch.setattr(cls,"run", lambda *a,**kw:pytest.fail("legacy parser ran"))
    get_settings.cache_clear()
    reset_storage_cache()
    await dispose_engine()
    try:
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://fixture") as client:
            data = sample(fmt)
            name = "sample."+fmt
            r = await client.post("/v1/upload",params={"remote_path":"/"+name},files={"file":(name,data,"application/octet-stream")})
            assert r.status_code==201, r.text
            ids=r.json()
            archive=RevisionArchive(tmp_path)
            doc=DocumentRef(entry_id=ids["entry_id"],file_id=ids["file_id"],sha256=sha256(data))
            payload={"file_id": doc.file_id}
            if retry:
                from library.document_revisions import anydoc as adapter
                async with session_scope() as db:
                    task, = (await db.scalars(select(Task).where(Task.kind=="ingest_file"))).all()
                    now = datetime.now(timezone.utc)
                    await mark_running(db, ids=[task.id], now=now, lease_until=now+timedelta(minutes=4), worker_id="test")
                    await db.commit()
                payload.update(_task_id=task.id, _task_owner="test")
                with pytest.raises(RuntimeError, match="summary interrupted"):
                    await handle_ingest_file(payload)
                first, = archive.versions(doc)
                monkeypatch.setattr(adapter, "parse_anydoc", lambda *a, **kw: pytest.fail("retry re-extracted original"))
            await handle_ingest_file(payload)
            if retry:
                assert archive.versions(doc) == [first] and queries[0] == queries[1]
            ref,=archive.versions(doc)
            rev=archive.read(ref,allowed={ref})
            if weknora:
                assert rev.manifest.parser.ocr_backend == "paddle-layout-v1"
            hits=rev.find_literal("OfficeAnchor").hits
            if fmt=="xlsx" and anydoc:
                assert len(hits)==2 and all(h.locator.cell is None for h in hits)
            elif fmt=="xlsx":
                assert [(h.locator.sheet,h.locator.cell) for h in hits]==[("Test","A2"),("Test","A3")]
            elif fmt=="docx":
                assert hits[0].locator.paragraph==1
                assert all(h.locator.paragraph is None for h in rev.find_literal("repeat").hits)
            else:
                assert (hits[0].locator.slide,hits[0].locator.shape)==(1,2)
                if weknora:
                    assert not rev.find_literal("Slide number:").hits
            async with session_scope() as db:
                file=await db.get(File,doc.file_id)
                assert file.ingest_status=="done" and file.description["coverage"]["revision_id"]==str(ref.revision_id)
    finally:
        await dispose_engine()
        get_settings.cache_clear()
        reset_storage_cache()


def test_office_rejects_wrong_source_before_worker():
    doc=DocumentRef(file_id="f",entry_id="e",sha256="a"*64)
    with pytest.raises(ValueError,match="mismatched"):
        office.parse_office(b"not original", document=doc,filename="f.xlsx",format="xlsx",python="",worker=lambda *_:pytest.fail("worker ran"))


@pytest.mark.asyncio
@pytest.mark.parametrize("slide_number", [1, 2])
async def test_weknora_office_image_only_slide_markers_are_not_native_text(monkeypatch, slide_number):
    from PIL import Image

    image = io.BytesIO()
    Image.new("RGB", (8, 8)).save(image, format="PNG")
    image.seek(0)
    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    slide.shapes.add_picture(image, Inches(1), Inches(1))
    buf = io.BytesIO()
    prs.save(buf)
    data = buf.getvalue()
    doc = DocumentRef(file_id="f", entry_id="e", sha256=sha256(data))

    def parsed_output(*_):
        return dict(fmt="pptx", parser="MarkitdownParser", commit=weknora_office.WEKNORA_COMMIT,
                    sha256=doc.sha256, content=f"<!-- Slide number: {slide_number} -->\n\n", images={})

    monkeypatch.setattr(weknora_office, "run_worker", parsed_output)
    with pytest.raises(ValueError, match="no readable Office text"):
        await weknora_office.parse_weknora_office(
            data, document=doc, filename="sample.pptx", fmt="pptx", python="", checkout="",
            before_send=lambda: pytest.fail("OCR ran"), paddle=False,
        )
