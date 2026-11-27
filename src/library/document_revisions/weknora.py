"""Pinned W boundary: a local parser process, then PDF page projection."""

import os
import re
import subprocess
from base64 import b64decode
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel, ConfigDict, Json

from ._weknora_worker import MAX_PDF_PAGES, WEKNORA_COMMIT
from .models import Digest

PARSER_ENV = {"DOCREADER_PDF_RENDER_PARALLELISM": "1"}
_IMAGE = re.compile(r"!\[[^\]\n]*\]\((images/source_[A-Za-z0-9_]+\.jpg)\)")


class _NativeRecord(BaseModel):
    model_config = ConfigDict(strict=True)


class _Locator(_NativeRecord):
    type: Literal["pdf"]
    page: int


class _Block(_NativeRecord):
    start: int
    end: int
    locator: _Locator


class _Metadata(_NativeRecord):
    page_count: int
    scanned_page_count: int
    text_page_count: int
    image_source_types: Json[dict[str, Literal["scanned_pdf", "pdf_text_layer"]]]


class _NativeDocument(_NativeRecord):
    content: str
    images: dict[str, str]
    source_blocks: list[_Block]
    metadata: _Metadata


class WExport(_NativeRecord):
    source_sha256: Digest
    parser_commit: str
    runtime_versions: dict[str, str]
    has_native_text: list[bool]
    document: _NativeDocument


@dataclass(frozen=True)
class WeKnoraBuiltin:
    """Use a separately prepared interpreter and the local pinned source tree."""

    python: Path
    checkout: Path

    def __call__(self, pdf: bytes) -> WExport:
        # Do not pass application configuration, provider credentials, or W
        # environment overrides into the parser. Keep only OS/temp plumbing.
        env = {k: v for k, v in os.environ.items() if k.upper() in {
            "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR",
        }}
        worker = Path(__file__).with_name("_weknora_worker.py")
        proc = subprocess.run(
            [os.path.abspath(self.python), "-I", "-B", str(worker), str(self.checkout.resolve())],
            input=pdf, env=env | PARSER_ENV, capture_output=True, timeout=30, check=False,
        )
        if proc.returncode:
            diagnostic = proc.stderr.decode("utf-8", errors="replace").splitlines()
            detail = diagnostic[-1] if diagnostic else f"exit {proc.returncode}"
            raise ValueError(f"docreader builtin could not parse PDF: {detail}")
        return WExport.model_validate_json(proc.stdout)


@dataclass(frozen=True)
class WImage:
    jpeg: bytes | None
    scanned: bool


@dataclass(frozen=True)
class WPage:
    number: int
    has_native_text: bool
    parts: tuple[str | WImage, ...]


def _jpeg(encoded: str | None) -> bytes | None:
    if encoded is None:
        return None
    try:
        data = b64decode(encoded, validate=True)
        with Image.open(BytesIO(data), formats=["JPEG"]) as image:
            if max(image.size) > 2000:
                return None
            image.load()
    except (ValueError, OSError, Image.DecompressionBombError):
        return None
    return data


def project_pages(result: WExport, *, source_sha256: str) -> tuple[WPage, ...]:
    """Use W physical page anchors, not phrase matching or the image label alone."""
    if result.source_sha256 != source_sha256 or result.parser_commit != WEKNORA_COMMIT:
        raise ValueError("docreader output belongs to a different source or parser revision")
    doc = result.document
    meta = doc.metadata
    total = len(result.has_native_text)
    if not 1 <= total <= MAX_PDF_PAGES or total != meta.page_count:
        raise ValueError("docreader page count disagrees with the PDF")
    tags = meta.image_source_types
    if doc.images.keys() - tags.keys():
        raise ValueError("docreader returned an untagged image")

    bounds: dict[int, tuple[int, int]] = {}
    cursor = last_page = 0
    for block in doc.source_blocks:
        page = block.locator.page
        if not (1 <= page <= total and page >= last_page
                and cursor <= block.start < block.end <= len(doc.content)):
            raise ValueError("invalid or out-of-order docreader source locator")
        if doc.content[cursor:block.start].strip():
            raise ValueError("docreader text is not covered by a source locator")
        left = bounds[page][0] if page in bounds else block.start
        bounds[page] = (left, block.end)
        cursor, last_page = block.end, page
    if doc.content[cursor:].strip():
        raise ValueError("docreader text is not covered by a source locator")

    pages = []
    referenced: set[str] = set()
    scan_count = 0
    for page, has_text in enumerate(result.has_native_text, 1):
        start, end = bounds.get(page, (0, 0))
        markdown = doc.content[start:end]
        parts: list[str | WImage] = []
        cursor = 0
        for link in _IMAGE.finditer(markdown):
            uri = link[1]
            if uri not in tags:  # A literal from the original PDF, not an emitted asset.
                continue
            kind = tags[uri]
            scanned = kind == "scanned_pdf"
            if scanned and uri != f"images/source_page_{page}.jpg":
                raise ValueError("scan image does not match its docreader page locator")
            if uri in referenced:
                raise ValueError("duplicate docreader image anchor")
            referenced.add(uri)
            scan_count += scanned
            if text := markdown[cursor:link.start()].strip():
                parts.append(text)
            parts.append(WImage(_jpeg(doc.images.get(uri)), scanned))
            cursor = link.end()
        if text := markdown[cursor:].strip():
            parts.append(text)
        pages.append(WPage(page, has_text, tuple(parts)))
    if (referenced != tags.keys() or scan_count != meta.scanned_page_count
            or meta.text_page_count != total - scan_count):
        raise ValueError("docreader image anchors disagree with PDF coverage metadata")
    return tuple(pages)
