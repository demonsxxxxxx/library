"""Selected-page adapter for the internal PaddleOCR layout API."""
from __future__ import annotations

import asyncio
import base64
import io
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser

import httpx
from PIL import Image
from pypdf import PdfReader, PdfWriter

from library.config import get_settings

from ._weknora_worker import MAX_PDF_BYTES
from .archive import ASSET_SCHEME
from .layout import CAPTION_REGION_KINDS, FIGURE_REGION_KINDS, LayoutPage, Region, under_figure
from .models import NormalizedBox
from .table_data import Cell, TableGrid
from .weknora import WImage, WPage

BACKEND = "paddle-layout-v1"
_BATCH_PAGES = 4
_MAX_REPLY_BYTES = 32 * 1024 * 1024
_MAX_IMAGE_BYTES = 4 * 1024 * 1024
_MAX_IMAGE_PIXELS = 8_000_000
_MAX_IMAGES = 24
_MAX_MARKDOWN = 256_000
_MAX_BLOCKS = 2000
_IMAGE_LINKS = re.compile(r"<img\b[^>]*>|!\[[^\]\r\n]*\]\([^\r\n]*?\)", re.I)
_IMAGE_TARGET = re.compile(r'''<img\b[^>]*?\s+src\s*=\s*["']([^"']+)["'][^>]*>|!\[[^\]\r\n]*\]\(([^\s)]+)\)''', re.I)
_EMPTY_IMAGE_DIV = re.compile(r'(?m)^[ \t]*<div style="text-align: center;">[ \t]*</div>[ \t]*\r?\n?')
_CROP_KEY = re.compile(r"imgs/img_in_(image|chart)_box_(\d{1,8})_(\d{1,8})_(\d{1,8})_(\d{1,8})\.jpg")


@dataclass(frozen=True)
class OcrFigure:
    image: bytes
    bbox: NormalizedBox
    caption_bbox: NormalizedBox
    start: int
    end: int


@dataclass(frozen=True)
class OcrPage:
    text: str = ""
    layout: LayoutPage | None = None
    images: tuple[bytes, ...] = ()
    warnings: tuple[str, ...] = ()
    figures: tuple[OcrFigure, ...] = ()


def candidate_pages(pages: Sequence[WPage]) -> tuple[int, ...]:
    # Only clearly flagged pages, not all native pages with unverified layout.
    return tuple(p.number for p in pages if not any(isinstance(x, str) for x in p.parts)
                 or any(isinstance(x, WImage) and x.scanned for x in p.parts))


def _slice(data: bytes, pages: Sequence[int], source_total: int) -> bytes:
    source = PdfReader(io.BytesIO(data))
    if len(source.pages) != source_total:
        raise ValueError("OCR source page count mismatch")
    writer = PdfWriter()
    for physical in pages:
        # Keep page content, not document links, attachments, or article chains.
        writer.add_page(source.pages[physical - 1], excluded_keys=("/Annots", "/AA", "/B"))
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


class _GridParser(HTMLParser):
    """Read one Paddle table; reject ambiguous or incomplete grids."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows: list[list[tuple[int, int, bool, str]]] = []
        self.current: tuple[int, int, bool] | None = None
        self.parts: list[str] = []
        self.tables = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables += 1
            if self.tables != 1:
                raise ValueError("nested or multiple tables")
        elif tag == "tr":
            if self.current is not None or len(self.rows) >= 1000:
                raise ValueError("invalid table row")
            self.rows.append([])
        elif tag in ("td", "th"):
            if not self.rows or self.current is not None or len(self.rows[-1]) >= 1000:
                raise ValueError("invalid table cell")
            attrs = dict(attrs)
            h, w = int(attrs.get("rowspan", "1")), int(attrs.get("colspan", "1"))
            if not 1 <= h <= 1000 or not 1 <= w <= 1000:
                raise ValueError("invalid table span")
            self.current = (h, w, tag == "th")
            self.parts = []
        elif tag == "br" and self.current is not None:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.current is not None:
            self.parts.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th"):
            if self.current is None:
                raise ValueError("unmatched table cell")
            self.rows[-1].append((*self.current, "".join(self.parts).strip()))
            self.current = None

    def grid(self) -> TableGrid:
        if self.tables != 1 or self.current is not None or not self.rows:
            raise ValueError("incomplete table")
        used: set[tuple[int, int]] = set()
        cells = []
        for r, row in enumerate(self.rows):
            c = 0
            for h, w, header, text in row:
                while (r, c) in used:
                    c += 1
                if r + h > len(self.rows) or (c + w) * len(self.rows) > 100_000:
                    raise ValueError("table spans exceed bounds")
                span = {(i, j) for i in range(r, r + h) for j in range(c, c + w)}
                if used & span:
                    raise ValueError("overlapping OCR table spans")
                used.update(span)
                cells.append(Cell(row=r, col=c, rowspan=h, colspan=w, header=header, text=text))
                c += w
        return TableGrid(rows=len(self.rows), cols=max((j + 1 for _, j in used), default=0), cells=cells)


def _box(value, width, height):
    if not (isinstance(value, list) and len(value) == 4 and all(type(n) in (int, float) for n in value)):
        return None
    x, y, right, bottom = value
    if not (0 <= x < right <= width and 0 <= y < bottom <= height):
        return None
    return x / width, y / height, right / width, bottom / height


def _image(value: str) -> bytes:
    # No secondary network fetches; accept only inline bytes from the OCR response.
    if value.startswith("data:"):
        head, _, value = value.partition(",")
        if head not in ("data:image/jpeg;base64", "data:image/jpg;base64", "data:image/png;base64"):
            raise ValueError("unsupported OCR image encoding")
    if len(value) > (_MAX_IMAGE_BYTES * 4 // 3 + 4):
        raise ValueError("OCR image exceeds cap")
    raw = base64.b64decode(value, validate=True)
    with Image.open(io.BytesIO(raw), formats=["JPEG", "PNG"]) as image:
        if image.width * image.height > _MAX_IMAGE_PIXELS:
            raise ValueError("OCR image exceeds pixel cap")
        if image.format == "JPEG":
            image.load()
            return raw
        converted = io.BytesIO()
        image.convert("RGB").save(converted, "JPEG", quality=92)
        return converted.getvalue()


def _figures(raw, text, regions, images, width, height) -> tuple[OcrFigure, ...]:
    """Cross-check crop keys, exact transcript spans and unambiguous layout pairs."""
    links = {}
    for link in _IMAGE_TARGET.finditer(raw):
        links.setdefault(link[1] or link[2], []).append(link.end())
    figure_regions = [r for r in regions if r.kind in FIGURE_REGION_KINDS and r.bbox is not None]
    captions = [r for r in regions if r.kind in CAPTION_REGION_KINDS and r.bbox is not None and r.text.strip()]
    lines = {}
    offset = 0
    for line in text.splitlines(keepends=True):
        label = line.strip()
        if label:
            start = offset + len(line) - len(line.lstrip())
            lines.setdefault(label, []).append((start, start + len(label)))
        offset += len(line)
    caption_counts = Counter(c.text.strip() for c in captions)
    result = []
    for key, image in images.items():
        match = _CROP_KEY.fullmatch(key) if isinstance(key, str) else None
        if not match or len(positions := links.get(key, [])) != 1:
            continue
        rect = _box([int(v) for v in match.groups()[1:]], width, height)
        if rect is None or sum(r.kind == match[1] and r.bbox == rect for r in figure_regions) != 1:
            continue
        candidates = [c for c in captions if under_figure(rect, c.bbox)]
        if len(candidates) != 1:
            continue
        cap = candidates[0]
        label = cap.text.strip()
        if (sum(under_figure(r.bbox, cap.bbox) for r in figure_regions) != 1
                or caption_counts[label] != 1 or len(spans := lines.get(label, [])) != 1):
            continue
        start, end = spans[0]
        # Require the Markdown to name the exact crop next to this caption too.
        after = raw[positions[0]:]
        after = re.sub(r"^\s*</div>", "", after, count=1).lstrip()
        if after.split("\n", 1)[0].strip() != label:
            continue
        result.append(OcrFigure(image, rect, cap.bbox, start, end))
    starts = Counter(f.start for f in result)
    return tuple(sorted((f for f in result if starts[f.start] == 1), key=lambda f: f.start))


def _page(result: dict, physical: int) -> OcrPage:
    md = result["markdown"]
    raw = md["text"]
    if (not isinstance(raw, str) or len(raw) > _MAX_MARKDOWN or "\x00" in raw
            or ASSET_SCHEME in raw.lower()):
        raise ValueError("invalid OCR Markdown")
    text = _EMPTY_IMAGE_DIV.sub("", _IMAGE_LINKS.sub("", raw)).strip()
    warnings = []
    layout = result.get("prunedResult") or {}
    width, height = layout.get("width"), layout.get("height")
    blocks = layout.get("parsing_res_list")
    regions = []
    if (not isinstance(blocks, list) or len(blocks) > _MAX_BLOCKS
            or type(width) is not int or type(height) is not int or width <= 0 or height <= 0):
        warnings.append("paddle_ocr_layout_missing_or_invalid")
    else:
        for block in blocks:
            kind, content = block["block_label"], block.get("block_content", "")
            if not isinstance(kind, str) or not isinstance(content, str) or len(content) > _MAX_MARKDOWN:
                raise ValueError("invalid OCR block")
            grid = None
            if kind == "table":
                try:
                    parser = _GridParser()
                    parser.feed(content)
                    parser.close()
                    grid = parser.grid()
                except ValueError:
                    warnings.append("paddle_ocr_table_grid_needs_review")
            box = _box(block.get("block_bbox"), width, height)
            regions.append(Region(kind=kind, bbox=box, text=content, caption="", grid=grid))
    images = {}
    incoming = md.get("images") or {}
    if not isinstance(incoming, dict) or len(incoming) > _MAX_IMAGES:
        warnings.append("paddle_ocr_image_unavailable")
    else:
        for key, value in incoming.items():
            try:
                if not isinstance(value, str):
                    raise ValueError("invalid OCR image")
                images[key] = _image(value)
            except (ValueError, OSError, Image.DecompressionBombError):
                warnings.append("paddle_ocr_image_unavailable")
    if not any(c.isalnum() for c in re.sub(r"<[^>]*>", "", text)):
        warnings.append("paddle_ocr_no_text")
        text = ""
    figures = _figures(raw, text, regions, images, width, height) if regions and images and text else ()
    return OcrPage(text, LayoutPage(page=physical, regions=regions), tuple(images.values()),
                   tuple(dict.fromkeys(warnings)), figures)


async def _request(client: httpx.AsyncClient, data: bytes, pages: tuple[int, ...]) -> dict[int, OcrPage]:
    endpoint = get_settings().pdf_revision_paddle_ocr_endpoint.strip()
    if not endpoint:
        return {n: OcrPage(warnings=("paddle_ocr_endpoint_not_configured",)) for n in pages}
    if len(data) > MAX_PDF_BYTES:
        return {n: OcrPage(warnings=("paddle_ocr_slice_too_large",)) for n in pages}
    payload = {"file": base64.b64encode(data).decode("ascii"), "fileType": 0,
               "visualize": False, "restructurePages": False}
    try:
        async with asyncio.timeout(100), client.stream("POST", endpoint, json=payload) as response:
            response.raise_for_status()
            body = bytearray()
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > _MAX_REPLY_BYTES:
                    raise ValueError("OCR response exceeds cap")
                body.extend(chunk)
        reply = json.loads(body)
        if type(reply["errorCode"]) is not int or reply["errorCode"] != 0:
            return {n: OcrPage(warnings=("paddle_ocr_service_rejected",)) for n in pages}
        result = reply["result"]
        outputs = result["layoutParsingResults"]
        total = result["dataInfo"]["numPages"]
        if type(total) is not int or total != len(pages) or not isinstance(outputs, list) or len(outputs) != total:
            return {n: OcrPage(warnings=("paddle_ocr_page_count_mismatch",)) for n in pages}
        reports = {}
        for n, output in zip(pages, outputs, strict=True):
            try:
                reports[n] = _page(output, n)
            except (AttributeError, ValueError, TypeError, KeyError):
                reports[n] = OcrPage(warnings=("paddle_ocr_invalid_response",))
        return reports
    except (TimeoutError, httpx.TimeoutException):
        reason = "paddle_ocr_timeout"
    except httpx.HTTPError:
        reason = "paddle_ocr_unavailable"
    except (AttributeError, ValueError, TypeError, KeyError):
        reason = "paddle_ocr_invalid_response"
    return {n: OcrPage(warnings=(reason,)) for n in pages}


async def recognize_pages(
    pdf: bytes, pages: Sequence[WPage], *, before_send: Callable[[], Awaitable[None]],
) -> dict[int, OcrPage]:
    selected = candidate_pages(pages)
    if not selected:
        return {}
    results = {}
    # Server-configured destination; never use inherited proxies or redirects.
    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=5.0),
                                 trust_env=False, follow_redirects=False) as client:
        for i in range(0, len(selected), _BATCH_PAGES):
            batch = selected[i:i + _BATCH_PAGES]
            data = await asyncio.to_thread(_slice, pdf, batch, len(pages))
            await before_send()
            results.update(await _request(client, data, batch))
    return results


def _pack_images(blobs: Sequence[bytes]) -> bytes:
    # Use the same proven PDF endpoint for Office images; each image is one page.
    jpegs = [io.BytesIO(_image(base64.b64encode(b).decode("ascii"))) for b in blobs]
    images = []
    try:
        images = [Image.open(b) for b in jpegs]
        rgb = [im.convert("RGB") for im in images]
        try:
            out = io.BytesIO()
            rgb[0].save(out, "PDF", save_all=True, append_images=rgb[1:], resolution=144)
            return out.getvalue()
        finally:
            for image in rgb:
                image.close()
    finally:
        for image in images:
            image.close()


async def recognize_images(
    assets: dict[str, bytes], *, before_send: Callable[[], Awaitable[None]],
) -> dict[str, OcrPage]:
    results = {}
    # A modest per-document budget; extra assets stay available as originals.
    ids = list(assets)

    async def submit(client, batch):
        try:
            data = await asyncio.to_thread(_pack_images, [assets[key] for key in batch])
        except (ValueError, OSError, Image.DecompressionBombError):
            if len(batch) == 1:
                results[batch[0]] = OcrPage(warnings=("paddle_ocr_image_unavailable",))
                return
        else:
            if len(data) <= MAX_PDF_BYTES or len(batch) == 1:
                await before_send()
                response = await _request(client, data, tuple(range(1, len(batch) + 1)))
                results.update({key: response[i] for i, key in enumerate(batch, 1)})
                return
        # Isolate an invalid asset, or keep a large group below the slice cap.
        for key in batch:
            await submit(client, [key])

    async with httpx.AsyncClient(timeout=httpx.Timeout(90.0, connect=5.0),
                                 trust_env=False, follow_redirects=False) as client:
        requested = ids[:_MAX_IMAGES]
        for i in range(0, len(requested), _BATCH_PAGES):
            await submit(client, requested[i:i + _BATCH_PAGES])
        for asset_id in ids[_MAX_IMAGES:]:
            results[asset_id] = OcrPage(warnings=("paddle_ocr_office_image_budget",))
    return results
