"""Library-native PDF and ordinary DOCX projections. No model, index or database calls."""
from __future__ import annotations

import io
import json
from dataclasses import dataclass, field
from importlib.metadata import version
from pathlib import PurePath
from typing import Protocol
from uuid import UUID, uuid4
from zipfile import ZipFile

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from PIL import Image
from pypdf import PdfReader

from library.pipelines.docx import _embedded_image_rids, _iter_block_items, _render_block
from library.pipelines.pdf_text import _extract_page_text

from .archive import RevisionArchive, archive_status, image_markdown, sha256
from .models import (
    Asset, Coverage, DocumentCoverage, DocumentRef, DocxInput, DocxLocator, Manifest, PageCoverage,
    ParserStamp, PdfInput, PdfLocator, PptxInput, RevisionRef, SourceMapRow, SourceVersion, TableData, TableRef, XlsxInput,
)
from .table_data import Cell, TableGrid


@dataclass
class ParsedDocument:
    """One source snapshot; offsets refer to the final UTF-8 Markdown in code points."""
    document: DocumentRef
    filename: str
    size: int
    format: str
    revision_id: UUID = field(default_factory=uuid4)
    fragments: list[str] = field(default_factory=list)
    rows: list[SourceMapRow] = field(default_factory=list)
    assets: dict[str, bytes] = field(default_factory=dict)
    inventory: dict[str, Asset] = field(default_factory=dict)
    tables: list[TableData] = field(default_factory=list)
    end: int = 0
    coverage: Coverage | DocumentCoverage | None = None
    parser_stamp: ParserStamp | None = None

    separator: str = "\n\n"

    def add(self, text, locator, kind="native", *, asset=None, table=None, figure_map_id=None):
        if not text:
            return
        start = self.end + (len(self.separator) if self.fragments else 0)
        self.end = start + len(text)
        self.fragments.append(text)
        self.rows.append(SourceMapRow(revision_id=self.revision_id, source_map_id=f"map-{len(self.rows)+1}",
            kind=kind, content_start=start, content_end=self.end, locator=locator,
            asset_ids=(asset,) if asset else (), table_id=table, figure_map_id=figure_map_id))

    def store_image(self, blob):
        # Preserve original raster bytes, not a transcoded thumbnail.
        asset_id = sha256(blob)
        if asset_id in self.assets:
            return asset_id
        try:
            with Image.open(io.BytesIO(blob)) as image:
                mime = Image.MIME.get(image.format)
                image.verify()
            asset = Asset(asset_id=asset_id, mime=mime, size_bytes=len(blob))
        except (ValueError, OSError, SyntaxError, Image.DecompressionBombError):
            return None
        self.assets[asset_id] = blob
        self.inventory[asset_id] = asset
        return asset_id

    def image(self, blob, locator):
        asset_id = self.store_image(blob)
        if asset_id is None:
            return False
        self.add(image_markdown(asset_id), locator, "image", asset=asset_id)
        return True

    def publish(self, archive: RevisionArchive, delivery_id=None) -> RevisionRef:
        if self.coverage is None:
            raise ValueError("missing native coverage")
        body = self.separator.join(self.fragments).encode()
        mapping = "".join(r.archive_json()+"\n" for r in self.rows).encode()
        tables = {t.table_id: t.archive_json().encode() for t in self.tables}
        pdf = self.format == "pdf"
        stamp = self.parser_stamp or ParserStamp(name="library-native", route="text", version=version("pypdf" if pdf else "python-docx"),
            config_digest=sha256(json.dumps([self.format, "native-archive-v1" if pdf else "native-archive-v2", "no-ocr"]).encode()))
        m = Manifest(revision_id=self.revision_id, entry_id=self.document.entry_id, file_id=self.document.file_id,
            input={"pdf": PdfInput, "docx": DocxInput, "xlsx": XlsxInput, "pptx": PptxInput}[self.format](filename=self.filename),
            source_version=SourceVersion(sha256=self.document.sha256, size_bytes=self.size), parser=stamp,
            coverage=self.coverage, status=archive_status(self.coverage), assets=tuple(self.inventory.values()),
            tables=tuple(TableRef(table_id=k, sha256=sha256(v)) for k,v in tables.items()),
            content_sha256=sha256(body), source_map_sha256=sha256(mapping), delivery_id=delivery_id)
        archive._publish(m, body, mapping, self.assets, tables=tables)
        return m.ref


def _word_grid(table: Table) -> TableGrid:
    rows, cols = len(table.rows), len(table.columns)
    if rows * cols > 100_000:
        raise ValueError("table exceeds archive grid limit")
    by_cell = {}
    for r, row in enumerate(table.rows):
        before = sum(int(n.get(qn("w:val"), "0")) for n in row._tr.xpath("./w:trPr/w:gridBefore"))
        for c, cell in enumerate(row.cells, before):
            # python-docx resolves both horizontal and vertical merged cells to the same XML tc.
            if cell._tc not in by_cell:
                by_cell[cell._tc] = [r,c,r,c,cell.text]
            else:
                by_cell[cell._tc][2:4] = [r,c]
    return TableGrid(rows=rows, cols=cols, cells=[Cell(row=r,col=c,rowspan=r2-r+1,colspan=c2-c+1,
        text=text,header=False) for r,c,r2,c2,text in by_cell.values()])


class ParserAdapter(Protocol):
    """Populate a projection for authenticated source bytes without publishing it."""

    def __call__(self, parsed: ParsedDocument, data: bytes, /) -> None: ...


def _unwrap_simple_fields(body, warnings: set[str]) -> None:
    """Expose cached display runs to python-docx; never evaluate the field instructions.

    Only the in-memory document is changed. Unwrapping before block traversal keeps
    the same run order for both paragraph Markdown and table cell text.
    """
    run, link, para, simple = map(qn, ("w:r", "w:hyperlink", "w:p", "w:fldSimple"))
    for node in reversed(body.xpath(".//w:fldSimple")):
        parent = node.getparent()
        displayed = False
        for child in list(node):
            if child.tag == qn("w:fldData"):
                continue  # field metadata, not visible text
            if parent.tag in (para, link, simple) and child.tag == run:
                displayed |= bool(child.text)
            elif parent.tag in (para, simple) and child.tag == link:
                displayed |= any(r.text for r in child.findall(run))
            else:
                warnings.add("unsupported_inline_or_nested_content")
            node.addprevious(child)
        if not displayed:
            warnings.add("simple_field_display_unavailable")
        parent.remove(node)


def _word(parsed: ParsedDocument, data: bytes) -> None:
    with ZipFile(io.BytesIO(data)) as z:
        if sum(i.file_size for i in z.infolist()) > 128 * 1024**2:
            raise ValueError("DOCX exceeds expanded size limit")
        side_parts = any(n.startswith(("word/header", "word/footer", "word/footnotes", "word/endnotes", "word/comments")) for n in z.namelist())
    doc = Document(io.BytesIO(data))
    warnings = set()
    if side_parts:
        warnings.add("non_body_parts_not_transcribed")
    body = doc.element.body
    _unwrap_simple_fields(body, warnings)
    if any(body.xpath(expr) for expr in (".//w:sdt", ".//w:ins", ".//w:del", ".//w:txbxContent", ".//w:pict",
            ".//w:object", ".//w:altChunk", ".//w:tbl//w:tbl", ".//a:blip[@r:link]")):
        warnings.add("unsupported_inline_or_nested_content")
    para_no = table_no = 0
    for block in _iter_block_items(doc):
        if isinstance(block, Table):
            table_no += 1
            loc = DocxLocator(table=table_no)
            try:
                grid = _word_grid(block)
            except (IndexError, ValueError):
                warnings.add(f"table_{table_no}_grid_not_verified")
                # Retain readable cell text, but never invent a grid.
                parsed.add(_render_block(block), loc)
            else:
                tid = f"table-{table_no}"
                parsed.tables.append(TableData(table_id=tid, locator=loc, source="native", grid=grid))
                # Readable Markdown; spans and exact (unescaped) cell strings live in the sidecar.
                lines = [[""] * grid.cols for _ in range(grid.rows)]
                for cell in grid.cells:
                    lines[cell.row][cell.col] = cell.text.replace("|", "\\|").replace("\n", "<br>")
                rows = ["| " + " | ".join(row) + " |" for row in lines]
                rows.insert(1, "| " + " | ".join(["---"] * grid.cols) + " |")
                parsed.add("\n".join(rows), loc, "table", table=tid)
        else:
            para_no += 1
            loc = DocxLocator(paragraph=para_no)
            parsed.add(_render_block(block), loc)
        for rid in _embedded_image_rids(block._element):
            part = doc.part.related_parts.get(rid)
            if part is None or not parsed.image(part.blob, loc):
                warnings.add("image_unavailable_or_unsupported")
            else:
                warnings.add("image_preserved_not_transcribed")
    processed = int(bool(parsed.rows))
    if not processed:
        warnings.add("no_mapped_body_content")
    parsed.coverage = DocumentCoverage(processed=processed, failed=() if processed else (1,),
        partial=(1,) if processed and warnings else (), warnings=tuple(sorted(warnings)), image_count=len(parsed.assets))


def _pdf(parsed: ParsedDocument, data: bytes) -> None:
    reader = PdfReader(io.BytesIO(data))
    pages = []
    for n, page in enumerate(reader.pages, 1):
        loc = PdfLocator(page=n)
        warnings = []
        try:
            text = _extract_page_text(page).strip()
        except Exception:
            text = ""
        parsed.add(text, loc)
        if not text:
            warnings.append("no_reliable_native_text")
        if text:
            warnings.append("native_layout_not_verified")
        try:
            for img in page.images:
                if not parsed.image(img.data, loc):
                    warnings.append("image_unsupported")
                else:
                    warnings.append("image_preserved_not_transcribed")
        except Exception:
            warnings.append("image_extraction_incomplete")
        pages.append(PageCoverage(page=n, engine="text_layer" if text else "none",
            status="partial" if text else "failed", warnings=tuple(dict.fromkeys(warnings))))
    failed = tuple(p.page for p in pages if p.status == "failed")
    parsed.coverage = Coverage(total=len(pages), pages=tuple(pages), processed=len(pages)-len(failed), failed=failed,
        partial=tuple(p.page for p in pages if p.status == "partial"), image_count=len(parsed.assets))


def parse_native(data: bytes, *, document: DocumentRef, filename: str, format: str) -> ParsedDocument:
    if sha256(data) != document.sha256:
        raise ValueError("native parser original checksum mismatch")
    adapters: dict[str, ParserAdapter] = {"pdf": _pdf, "docx": _word}
    if format not in adapters:
        raise ValueError("unsupported native revision format")
    result = ParsedDocument(document, PurePath(filename.replace("\\", "/")).name, len(data), format)
    adapters[format](result, data)
    return result
