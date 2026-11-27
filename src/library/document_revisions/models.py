"""Persistent PDF/DOCX revision and evidence contracts."""

from typing import Annotated, Literal

from pydantic import UUID4, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from .table_data import TableGrid

Digest = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
Identity = Annotated[str, StringConstraints(min_length=1)]
Status = Literal["parsed", "partial", "failed"]
PageNumber = Annotated[int, Field(ge=1)]
MapId = Annotated[str, StringConstraints(pattern=r"^map-[1-9][0-9]*$")]
Unit = Annotated[float, Field(ge=0, le=1)]
NormalizedBox = tuple[Unit, Unit, Unit, Unit]


class _Record(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class DocumentRef(_Record):
    file_id: Identity
    entry_id: Identity
    sha256: Digest


class RevisionRef(_Record):
    document: DocumentRef
    revision_id: UUID4


class SourceVersion(_Record):
    sha256: Digest
    size_bytes: Annotated[int, Field(gt=0)]


class PdfInput(_Record):
    filename: Identity
    mime: Literal["application/pdf"] = "application/pdf"


class DocxInput(_Record):
    filename: Identity
    mime: Literal["application/vnd.openxmlformats-officedocument.wordprocessingml.document"] = (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )


class XlsxInput(_Record):
    filename: Identity
    mime: Literal["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"] = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


class PptxInput(_Record):
    filename: Identity
    mime: Literal["application/vnd.openxmlformats-officedocument.presentationml.presentation"] = "application/vnd.openxmlformats-officedocument.presentationml.presentation"


class OfficeLocator(_Record):
    type: Literal["office"] = "office"
    format: Literal["docx", "xlsx", "pptx"]
    paragraph: PageNumber | None = None
    table: PageNumber | None = None
    row: PageNumber | None = None
    column: PageNumber | None = None
    sheet: str | None = None
    sheet_row: PageNumber | None = None
    cell: Annotated[str, StringConstraints(pattern=r"^[A-Z]{1,3}[1-9][0-9]*$")] | None = None
    slide: PageNumber | None = None
    shape: PageNumber | None = None
    # One-based ordinals in word/document.xml, not positions in Markdown or Word page numbers.
    textbox: PageNumber | None = None
    textbox_paragraph: PageNumber | None = None
    # Internal OOXML story address. These are not rendered Word page numbers.
    word_part: Annotated[str, StringConstraints(pattern=r"^word/(?:header[0-9]*|footer[0-9]*|footnotes)\.xml$")] | None = None
    story_paragraph: PageNumber | None = None
    footnote_id: PageNumber | None = None

    @model_validator(mode="after")
    def check_address(self):
        if self.word_part is not None:
            if (self.format != "docx" or self.story_paragraph is None
                    or (self.word_part == "word/footnotes.xml") != (self.footnote_id is not None)
                    or any((self.paragraph, self.table, self.row, self.column, self.sheet,
                            self.sheet_row, self.cell, self.slide, self.shape, self.textbox, self.textbox_paragraph))):
                raise ValueError("invalid Word story address")
        elif self.story_paragraph is not None or self.footnote_id is not None:
            raise ValueError("Word story address requires a part")
        if (self.textbox is None) != (self.textbox_paragraph is None):
            raise ValueError("incomplete Word textbox address")
        if self.textbox is not None and (self.format != "docx" or self.paragraph or self.table):
            raise ValueError("Word textbox address cannot be mixed with another locator")
        if (self.row is None) != (self.column is None):
            raise ValueError("incomplete table address")
        if self.sheet_row is not None and (self.format != "xlsx" or not self.sheet or self.cell):
            raise ValueError("sheet row requires a worksheet and no cell address")
        if self.format == "xlsx":
            valid = not any((self.paragraph, self.table, self.slide, self.shape, self.row)) and (not self.cell or self.sheet)
        elif self.format == "docx":
            valid = (not any((self.sheet, self.cell, self.slide, self.shape)) and not (self.paragraph and self.table)
                     and (not self.row or self.table))
        else:
            valid = (not any((self.paragraph, self.table, self.sheet, self.cell)) and (not self.shape or self.slide)
                     and (not self.row or self.shape))
        if not valid:
            raise ValueError("incompatible Office address")
        return self

    def archive_exclude(self) -> set[str]:
        # Earlier readers reject new keys, even when null. Preserve old archive bytes
        # for addresses that don't use the newer optional locator fields.
        absent = {
            key for key in ("word_part", "story_paragraph", "footnote_id") if getattr(self, key) is None
        }
        if self.textbox is None:
            absent.update(("textbox", "textbox_paragraph"))
        return absent


class ParserStamp(_Record):
    route: Literal["text", "ocr", "mixed"]
    name: Literal["weknora-docreader-builtin", "weknora-office-builtin", "library-native", "docling-office", "anydoc"] = "weknora-docreader-builtin"
    version: Identity
    config_digest: Digest
    vision_model: Identity | None = None
    ocr_backend: Literal["paddle-layout-v1"] | None = None


class PageCoverage(_Record):
    page: PageNumber
    engine: Literal["text_layer", "vision", "paddle_ocr", "none"]
    status: Status
    warnings: tuple[str, ...] = ()


class Coverage(_Record):
    units: Literal["page"] = "page"
    total: PageNumber
    processed: Annotated[int, Field(ge=0)]
    failed: tuple[PageNumber, ...]
    partial: tuple[PageNumber, ...]
    image_count: Annotated[int, Field(ge=0)]
    pages: tuple[PageCoverage, ...]


class DocumentCoverage(_Record):
    units: Literal["document"] = "document"
    total: Literal[1] = 1
    processed: Literal[0, 1]
    failed: tuple[Literal[1], ...] = ()
    partial: tuple[Literal[1], ...] = ()
    warnings: tuple[str, ...] = ()
    image_count: Annotated[int, Field(ge=0)]


class DocxLocator(_Record):
    type: Literal["docx"] = "docx"
    # One-based body paragraph or table ordinal (independent sequences, including blanks).
    paragraph: PageNumber | None = None
    table: PageNumber | None = None


TableId = Annotated[str, StringConstraints(pattern=r"^table-[1-9][0-9]*$")]


class TableRef(_Record):
    table_id: TableId
    sha256: Digest


class Asset(_Record):
    asset_id: Digest  # SHA-256 of the archived bytes; provenance is in the source map.
    mime: Literal["image/jpeg", "image/png", "image/gif", "image/tiff", "image/webp", "image/bmp"] = "image/jpeg"
    size_bytes: Annotated[int, Field(gt=0)]

    @property
    def filename(self) -> str:
        ext = self.mime.removeprefix("image/")
        return f"{self.asset_id}.{'jpg' if ext == 'jpeg' else ext}"


class Manifest(_Record):
    schema_version: Literal[1] = 1
    offset_unit: Literal["unicode_code_point_half_open"] = "unicode_code_point_half_open"
    revision_id: UUID4
    file_id: Identity
    entry_id: Identity
    source_version: SourceVersion
    input: PdfInput | DocxInput | XlsxInput | PptxInput
    parser: ParserStamp
    coverage: Coverage | DocumentCoverage
    tables: tuple[TableRef, ...] = ()
    delivery_id: str | None = None
    status: Status
    assets: tuple[Asset, ...]
    content_sha256: Digest
    source_map_sha256: Digest
    layout_sha256: Digest | None = None  # older, text-only revisions remain readable

    @property
    def ref(self) -> RevisionRef:
        return RevisionRef(
            document=DocumentRef(
                file_id=self.file_id, entry_id=self.entry_id, sha256=self.source_version.sha256,
            ),
            revision_id=self.revision_id,
        )


class PdfLocator(_Record):
    type: Literal["pdf"] = "pdf"
    page: PageNumber
    bbox: NormalizedBox | None = None  # Top-left origin in the OCR-rendered physical page.

    @model_validator(mode="after")
    def check_box(self):
        if self.bbox is not None:
            left, top, right, bottom = self.bbox
            if not (left < right and top < bottom):
                raise ValueError("invalid PDF locator box")
        return self


class TableData(_Record):
    table_id: TableId
    locator: PdfLocator | DocxLocator | OfficeLocator
    source: Literal["native", "layout"]
    grid: TableGrid

    def archive_json(self) -> str:
        # Keep earlier table archive readers compatible with ordinary grid data.
        exclude = {"locator": {"word_part", "story_paragraph", "footnote_id"}} if isinstance(
            self.locator, OfficeLocator) and self.locator.word_part is None else None
        return self.model_dump_json(exclude=exclude)


class SourceMapRow(_Record):
    source_map_id: MapId
    revision_id: UUID4
    kind: Literal["native", "vision", "ocr", "image", "table"]
    content_start: Annotated[int, Field(ge=0)]
    content_end: Annotated[int, Field(gt=0)]
    locator: PdfLocator | DocxLocator | OfficeLocator
    table_id: TableId | None = None
    asset_ids: tuple[Digest, ...] = ()
    figure_map_id: MapId | None = None  # OCR caption -> image occurrence in this revision.

    def archive_json(self) -> str:
        # Keep rows without links compatible with the earlier archive reader.
        exclude = {} if self.figure_map_id is not None else {"figure_map_id": True}
        if isinstance(self.locator, OfficeLocator):
            exclude["locator"] = self.locator.archive_exclude()
        return self.model_dump_json(exclude=exclude)


class FigureLink(_Record):
    source_map_id: MapId
    locator: PdfLocator | OfficeLocator
    asset_id: Digest


class EvidenceHit(_Record):
    ref: RevisionRef
    source_map_id: str
    content_range: tuple[int, int]
    quote: str
    locator: PdfLocator | DocxLocator | OfficeLocator
    asset_ids: tuple[Digest, ...]
    figure: FigureLink | None = None


class LiteralResult(_Record):
    hits: tuple[EvidenceHit, ...]
    coverage: Coverage | DocumentCoverage
