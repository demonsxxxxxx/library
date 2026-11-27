"""Local, append-only PDF revision storage and scoped evidence reads."""

import hashlib
import os
import sys
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from uuid import UUID

from .layout import CAPTION_REGION_KINDS, FIGURE_REGION_KINDS, Layout, under_figure
from .models import (
    Coverage, DocumentCoverage, DocumentRef, DocxInput, DocxLocator, EvidenceHit, FigureLink, LiteralResult,
    Manifest, OfficeLocator, PdfInput, PdfLocator, PptxInput, RevisionRef, SourceMapRow, Status, TableData,
)

ASSET_SCHEME = "kb-asset://"


def _archive_path(path: Path) -> Path:
    # A UUID directory plus a content-addressed image name can exceed MAX_PATH
    # under a valid Library Home. Normalize before using the Win32 long-path prefix.
    absolute = os.path.abspath(path)
    if sys.platform == "win32" and not absolute.startswith("\\\\?\\"):
        if absolute.startswith("\\\\"):
            absolute = "\\\\?\\UNC\\" + absolute[2:]
        else:
            absolute = "\\\\?\\" + absolute
    return Path(absolute)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive_status(coverage: Coverage | DocumentCoverage) -> Status:
    if coverage.processed == 0:
        return "failed"
    return "partial" if coverage.failed or coverage.partial else "parsed"


def image_markdown(asset_id: str) -> str:
    return f"![PDF image]({ASSET_SCHEME}{asset_id})"


@dataclass(frozen=True)
class Revision:
    manifest: Manifest
    content: str
    source_map: tuple[SourceMapRow, ...]
    layout: Layout | None = None
    tables: tuple[TableData, ...] = ()

    def figure_link(self, row: SourceMapRow) -> FigureLink | None:
        if row.figure_map_id is None:
            return None
        target = next(item for item in self.source_map if item.source_map_id == row.figure_map_id)
        return FigureLink(source_map_id=target.source_map_id, locator=target.locator, asset_id=target.asset_ids[0])

    def _hit(self, row: SourceMapRow, start: int, end: int) -> EvidenceHit:
        return EvidenceHit(
            ref=self.manifest.ref, source_map_id=row.source_map_id, content_range=(start, end),
            quote=self.content[start:end], locator=row.locator, asset_ids=row.asset_ids, figure=self.figure_link(row),
        )

    def find_literal(self, query: str) -> LiteralResult:
        """Exact matches within a mapped text range; no inference across locators."""
        if not query.strip():
            raise ValueError("literal query must not be blank")
        hits = []
        for row in self.source_map:
            if row.kind == "image":
                continue
            start = row.content_start
            while (at := self.content.find(query, start, row.content_end)) != -1:
                hits.append(self._hit(row, at, at + len(query)))
                start = at + len(query)
        return LiteralResult(hits=tuple(hits), coverage=self.manifest.coverage)


class RevisionArchive:
    """Use an explicitly supplied, private local root; no Library DB or global settings."""

    def __init__(self, root: Path) -> None:
        self._root = _archive_path(root / "document-revisions")

    def read(self, ref: RevisionRef, *, allowed: Collection[RevisionRef]) -> Revision:
        if ref not in allowed:
            raise PermissionError("revision is outside the allowed scope")
        return self._load(self._root / str(ref.revision_id), ref)

    def versions(self, document: DocumentRef, *, delivery_id: str | None = None) -> list[RevisionRef]:
        """List matching manifests; use read() to verify any revision before reuse."""
        if not self._root.exists():
            return []
        versions = []
        for folder in self._root.iterdir():
            try:
                rid = UUID(folder.name)
            except ValueError:  # staging directories are never visible
                continue
            path = folder / "manifest.json"
            try:
                manifest = Manifest.model_validate_json(path.read_bytes())
            except (FileNotFoundError, ValueError):
                # A broken/unpublished sibling must not hide this entry’s healthy revisions.
                continue
            if (manifest.ref.document == document and manifest.revision_id == rid
                    and (delivery_id is None or manifest.delivery_id == delivery_id)):
                versions.append((path.stat().st_mtime_ns, manifest.ref))
        return [ref for _, ref in sorted(versions, key=lambda pair: pair[0], reverse=True)]

    def read_hit(
        self, ref: RevisionRef, hit: EvidenceHit, *, allowed: Collection[RevisionRef],
    ) -> EvidenceHit:
        if hit.ref != ref:
            raise ValueError("hit belongs to a different revision or original")
        revision = self.read(ref, allowed=allowed)
        start, end = hit.content_range
        for row in revision.source_map:
            if (row.source_map_id == hit.source_map_id and row.kind != "image"
                    and row.content_start <= start < end <= row.content_end):
                actual = revision._hit(row, start, end)
                if actual == hit:
                    return actual
                break
        raise ValueError("hit does not match the archived evidence")

    def read_asset(
        self, ref: RevisionRef, asset_id: str, *, allowed: Collection[RevisionRef],
    ) -> bytes:
        revision = self.read(ref, allowed=allowed)
        for asset in revision.manifest.assets:
            if asset.asset_id == asset_id:
                data = (self._root / str(ref.revision_id) / "assets" / asset.filename).read_bytes()
                if sha256(data) != asset_id:
                    raise ValueError("archived asset checksum mismatch")
                return data
        raise KeyError("asset is not in this revision")

    def _publish(
        self, manifest: Manifest, content: bytes, source_map: bytes, assets: Mapping[str, bytes],
        layout: bytes | None = None, tables: Mapping[str, bytes] | None = None,
    ) -> None:
        # Archive directories have only one writer, via this method. A valid
        # existing revision is nonempty and cannot be replaced by a dir rename.
        self._root.mkdir(parents=True, exist_ok=True)
        dest = self._root / str(manifest.revision_id)
        if os.path.lexists(dest):
            raise FileExistsError(dest)
        with TemporaryDirectory(prefix=".stage-", dir=self._root) as scratch:
            stage = Path(scratch) / "revision"
            stage.mkdir()
            (stage / "assets").mkdir()
            tables = tables or {}
            if set(tables) != {table.table_id for table in manifest.tables}:
                raise ValueError("table inventory does not match manifest")
            if tables:
                (stage / "tables").mkdir()
                for table in manifest.tables:
                    (stage / "tables" / f"{table.table_id}.json").write_bytes(tables[table.table_id])
            if layout is not None:
                (stage / "layout.json").write_bytes(layout)
            (stage / "content.md").write_bytes(content)
            (stage / "source-map.jsonl").write_bytes(source_map)
            (stage / "manifest.json").write_text(manifest.model_dump_json() + "\n", encoding="utf-8")
            if set(assets) != {asset.asset_id for asset in manifest.assets}:
                raise ValueError("asset bytes do not match the manifest")
            for asset in manifest.assets:
                data = assets[asset.asset_id]
                if sha256(data) != asset.asset_id:
                    raise ValueError("asset checksum mismatch before publication")
                (stage / "assets" / asset.filename).write_bytes(data)
            self._load(stage, manifest.ref)
            # Windows can briefly hold a child file open after validation. Retry
            # only the rename; do not replace the destination or change permissions.
            for delay in (0.05, 0.15, 0.35, None):
                try:
                    stage.rename(dest)
                    break
                except PermissionError as exc:
                    if delay is None or getattr(exc, "winerror", None) not in (5, 32) or dest.exists():
                        raise
                    time.sleep(delay)

    @staticmethod
    def _load(directory: Path, ref: RevisionRef) -> Revision:
        manifest = Manifest.model_validate_json((directory / "manifest.json").read_bytes())
        if manifest.ref != ref:
            raise ValueError("archive does not belong to the requested revision and original")
        content = (directory / "content.md").read_bytes()
        mapping = (directory / "source-map.jsonl").read_bytes()
        if sha256(content) != manifest.content_sha256 or sha256(mapping) != manifest.source_map_sha256:
            raise ValueError("archive content or source map checksum mismatch")
        rows = tuple(SourceMapRow.model_validate_json(line) for line in mapping.splitlines())
        layout = None
        if manifest.layout_sha256 is not None:
            data = (directory / "layout.json").read_bytes()
            if sha256(data) != manifest.layout_sha256:
                raise ValueError("layout checksum mismatch")
            layout = Layout.model_validate_json(data)
            layout.check_source(ref.document.sha256, manifest.coverage.total)
        tables = []
        for item in manifest.tables:
            data = (directory / "tables" / f"{item.table_id}.json").read_bytes()
            if sha256(data) != item.sha256:
                raise ValueError("archived table checksum mismatch")
            table = TableData.model_validate_json(data)
            if table.table_id != item.table_id:
                raise ValueError("wrong archived table")
            tables.append(table)
        revision = Revision(manifest, content.decode("utf-8"), rows, layout, tuple(tables))
        _validate_projection(revision)
        for asset in manifest.assets:
            path = directory / "assets" / asset.filename
            if not path.is_file() or path.stat().st_size != asset.size_bytes:
                raise ValueError("missing or incomplete archived asset")
            with path.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != asset.asset_id:
                    raise ValueError("archived asset checksum mismatch")
        return revision


def _validate_projection(revision: Revision) -> None:
    m, body, rows = revision.manifest, revision.content, revision.source_map
    c = m.coverage
    asset_ids = {asset.asset_id for asset in m.assets}
    if len(asset_ids) != len(m.assets) or c.image_count != len(asset_ids):
        raise ValueError("invalid asset inventory")
    if isinstance(m.input, PdfInput) == isinstance(c, DocumentCoverage):
        raise ValueError("coverage unit disagrees with source format")
    if isinstance(c, DocumentCoverage):
        _validate_word(revision, c, asset_ids)
        return
    if (len(c.pages) != c.total
            or any(p.page != n for n, p in enumerate(c.pages, 1))
            or c.partial != tuple(p.page for p in c.pages if p.status == "partial")
            or c.failed != tuple(p.page for p in c.pages if p.status == "failed")
            or c.processed != c.total - len(c.failed) or archive_status(c) != m.status
            or any(p.status != "parsed" and not p.warnings for p in c.pages)):
        raise ValueError("invalid page coverage")
    cursor = previous_page = 0
    ids: set[str] = set()
    image_refs: set[tuple[int, str]] = set()
    vision_refs: set[tuple[int, str]] = set()
    engines: dict[int, str] = {}
    image_rows: dict[str, SourceMapRow] = {}
    bound_figures: set[str] = set()
    for row in rows:
        if not isinstance(row.locator, PdfLocator) or row.table_id is not None or row.kind == "table":
            raise ValueError("invalid PDF locator")
        page = row.locator.page
        start, end = row.content_start, row.content_end
        if (row.revision_id != m.revision_id or row.source_map_id in ids
                or not previous_page <= page <= c.total
                or not cursor <= start < end <= len(body) or body[cursor:start].strip()
                or not set(row.asset_ids) <= asset_ids):
            raise ValueError("invalid source-map identity or range")
        ids.add(row.source_map_id)
        cursor, previous_page = end, page
        if row.kind == "image":
            if row.figure_map_id or len(row.asset_ids) != 1 or body[start:end] != image_markdown(row.asset_ids[0]):
                raise ValueError("image markup does not match its asset")
            image_refs.add((page, row.asset_ids[0]))
            image_rows[row.source_map_id] = row
        else:
            engine = {"vision": "vision", "ocr": "paddle_ocr", "native": "text_layer"}[row.kind]
            text = body[start:end]
            if c.pages[page - 1].engine != engine or not text.strip():
                raise ValueError("mapped text disagrees with page coverage")
            if ASSET_SCHEME in text.lower():
                raise ValueError("only mapped images may use the Library asset URI scheme")
            engines[page] = engine
            if engine == "vision":
                if len(row.asset_ids) != 1 or not m.parser.vision_model:
                    raise ValueError("vision text must identify the model and its input image")
                vision_refs.add((page, row.asset_ids[0]))
            elif row.asset_ids:
                raise ValueError("non-vision text cannot claim a vision input")
            if engine == "paddle_ocr" and (not m.parser.ocr_backend or c.pages[page - 1].status != "partial"):
                raise ValueError("OCR text must record its backend and review status")
            if row.figure_map_id:
                target = image_rows.get(row.figure_map_id)
                if (engine != "paddle_ocr" or target is None or row.figure_map_id in bound_figures
                        or target.locator.page != page or target.locator.bbox is None or row.locator.bbox is None
                        or not under_figure(target.locator.bbox, row.locator.bbox) or revision.layout is None):
                    raise ValueError("invalid OCR caption-to-figure link")
                regions = revision.layout.pages[page - 1].regions
                if (sum(r.kind in FIGURE_REGION_KINDS and r.bbox == target.locator.bbox for r in regions) != 1
                        or sum(r.kind in CAPTION_REGION_KINDS and r.bbox == row.locator.bbox
                               and r.text.strip() == text for r in regions) != 1):
                    raise ValueError("caption-to-figure link lacks layout evidence")
                bound_figures.add(target.source_map_id)
            elif row.locator.bbox is not None:
                raise ValueError("unlinked PDF text cannot claim a bounding box")
    if (body[cursor:].strip() or {asset_id for _, asset_id in image_refs} != asset_ids
            or not vision_refs <= image_refs or set(engines) != {p.page for p in c.pages if p.status != "failed"}):
        raise ValueError("archive has unmapped content, assets, or coverage")
    if bound_figures != {mid for mid, row in image_rows.items() if row.locator.bbox is not None}:
        raise ValueError("unlinked PDF figure box")
    if any(not isinstance(t.locator, PdfLocator) or t.locator.page > c.total or t.locator.bbox is not None
           for t in revision.tables):
        raise ValueError("invalid PDF table locator")


def _validate_word(rev: Revision, c: DocumentCoverage, asset_ids: set[str]) -> None:
    tables = {t.table_id: t.locator for t in rev.tables}
    if (len(tables) != len(rev.tables) or rev.layout is not None or c.failed != (() if c.processed else (1,))
            or c.partial not in ((), (1,)) or (c.partial and not c.processed)
            or archive_status(c) != rev.manifest.status or (rev.manifest.status != "parsed" and not c.warnings)):
        raise ValueError("invalid DOCX coverage or inventory")
    cursor = 0
    mapped_assets, mapped_tables, ids = set(), set(), set()
    occurrences = set()
    ocr_sources = set()
    image_rows = {}
    bound_figures = set()
    # Builtin Word grids describe the original physical table, not GFM slots.
    original_grids = {}
    if rev.manifest.parser.name == "weknora-office-builtin" and isinstance(rev.manifest.input, DocxInput):
        for table in rev.tables:
            loc = table.locator
            if (not isinstance(loc, OfficeLocator) or loc.format != "docx" or loc.table is None
                    or loc.row is not None or table.table_id != f"table-{loc.table}" or table.source != "native"):
                raise ValueError("invalid original Word grid reference")
            original_grids[loc.table] = table.grid
    for row in rev.source_map:
        loc = row.locator
        a, b = row.content_start, row.content_end
        if rev.manifest.parser.name in {"docling-office", "anydoc", "weknora-office-builtin"}:
            fmt = "docx" if isinstance(rev.manifest.input, DocxInput) else "pptx" if isinstance(rev.manifest.input, PptxInput) else "xlsx"
            valid = isinstance(loc, OfficeLocator) and loc.format == fmt
        else:
            valid = (isinstance(rev.manifest.input, DocxInput) and isinstance(loc, DocxLocator)
                     and (loc.paragraph is None) != (loc.table is None))
        if (not valid or row.revision_id != rev.manifest.revision_id or row.source_map_id in ids
                or not cursor <= a < b <= len(rev.content) or rev.content[cursor:a].strip()):
            raise ValueError("invalid DOCX source map")
        ids.add(row.source_map_id)
        fragment = rev.content[a:b]
        if isinstance(loc, OfficeLocator) and loc.row is not None and (grid := original_grids.get(loc.table)):
            if loc.row > grid.rows or loc.column > grid.cols:
                raise ValueError("Word cell location outside original grid")
        if row.figure_map_id is not None and row.kind != "ocr":
            raise ValueError("invalid Office figure link")
        if row.kind == "image":
            if (len(row.asset_ids) != 1 or row.table_id is not None
                    or fragment != image_markdown(row.asset_ids[0])):
                raise ValueError("invalid DOCX image reference")
            mapped_assets.update(row.asset_ids)
            occurrences.add((loc, row.asset_ids[0]))
            image_rows[row.source_map_id] = row
        elif row.kind == "ocr":
            if (rev.manifest.parser.name != "weknora-office-builtin" or not rev.manifest.parser.ocr_backend
                    or rev.manifest.status != "partial" or len(row.asset_ids) != 1 or row.table_id is not None
                    or not fragment.strip()):
                raise ValueError("invalid Office OCR source")
            ocr_sources.add((loc, row.asset_ids[0]))
            if row.figure_map_id is not None:
                target = image_rows.get(row.figure_map_id)
                if (not isinstance(rev.manifest.input, DocxInput) or target is None
                        or row.figure_map_id in bound_figures or target.locator != loc
                        or target.asset_ids != row.asset_ids):
                    raise ValueError("invalid Office figure link")
                bound_figures.add(row.figure_map_id)
        elif row.kind == "table" and row.table_id and tables.get(row.table_id) == loc and not row.asset_ids:
            mapped_tables.add(row.table_id)
        elif row.kind != "native" or row.asset_ids or row.table_id is not None:
            raise ValueError("invalid DOCX content reference")
        if row.kind != "image" and ASSET_SCHEME in fragment.lower():
            raise ValueError("reserved asset URI in DOCX source text")
        cursor = b
    if (rev.content[cursor:].strip() or mapped_assets != asset_ids or not ocr_sources <= occurrences
            or mapped_tables != set(tables)
            or bool(rev.source_map) != bool(c.processed)):
        raise ValueError("unmapped DOCX content, table, or asset")
