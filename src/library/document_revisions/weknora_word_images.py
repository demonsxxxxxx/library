"""Preserve DOCX image occurrences without guessing their source locations."""

import base64
import io
import re
from collections import Counter, defaultdict, deque
from zipfile import ZipFile

from .anydoc import map_md
from .archive import image_markdown, sha256

_IMAGE = re.compile(
    r'''!\[(?:\\[^\r\n]|[^\\\]\r\n])*\]\((?:<([^<>\r\n]+)>|([^\s()<>]+))'''
    r'''(?:[ \t]+(?:"[^"\r\n]*"|'[^'\r\n]*'|\([^()\r\n]*\)))?[ \t]*\)'''
    r'''|<[iI][mM][gG](?=[\s/>])(?:"[^"\r\n]*"|'[^'\r\n]*'|[^"'<>\r\n])*>'''
)
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})([^\r\n]*)")
_BACKTICKS = re.compile(r"`+")
_APPENDIX = "\n\n<!-- 未定位到正文的图片 -->\n\n"


def _escaped(line, start):
    end = start
    while start and line[start - 1] == "\\":
        start -= 1
    return (end - start) % 2


def _image_links(content):
    """Find DocReader-style links in prose; leave code and escaped syntax alone."""
    at = 0
    fence = None
    for line in content.splitlines(keepends=True):
        marker = _FENCE.match(line)
        if marker and fence and marker[1][0] == fence[0] and len(marker[1]) >= len(fence) and not marker[2].strip():
            fence = None
        elif not fence and marker and (marker[1][0] != "`" or "`" not in marker[2]):
            fence = marker[1]
        elif not fence:
            ticks = [(m.start(), m.end()) for m in _BACKTICKS.finditer(line) if not _escaped(line, m.start())]
            following = {}
            ends = {}
            for start, end in reversed(ticks):
                width = end - start
                following[start] = ends.get(width)
                ends[width] = end
            spans = deque()
            stop = 0
            for start, _ in ticks:
                if start >= stop and following[start] is not None:
                    stop = following[start]
                    spans.append((start, stop))
            for m in _IMAGE.finditer(line):
                while spans and spans[0][1] <= m.start():
                    spans.popleft()
                if not _escaped(line, m.start()) and (not spans or m.start() < spans[0][0]):
                    yield at + m.start(), at + m.end(), (m[1] or m[2])
        at += len(line)


def has_word_text(result):
    # Exclude our appendix label and Markdown syntax for an image-only table.
    text = "".join(part for row, part in zip(result.rows, result.fragments, strict=True)
                   if row.kind == "native" and part != _APPENDIX)
    return any(line.strip(" \t\r\n|:-#>*`+") for line in text.splitlines())


def project_word_images(result, source, data, content, exports):
    originals = {}
    with ZipFile(io.BytesIO(data)) as package:
        for path in package.namelist():
            if path.startswith("word/media/") and not path.endswith("/"):
                raw = package.read(path)
                originals[path] = (sha256(raw), raw)
    by_asset = defaultdict(Counter)
    unlocated = set(originals)
    for path, locator in source.images:
        if path in originals:
            unlocated.discard(path)
            by_asset[originals[path][0]][locator] += 1
    for path in sorted(unlocated):
        by_asset[originals[path][0]][source.document] += 1

    export_ids = {}
    for path, encoded in exports.items():
        try:
            raw = base64.b64decode(encoded, validate=True)
        except (ValueError, TypeError):
            source.warnings.add("docreader_image_invalid")
            continue
        asset_id = result.store_image(raw)
        if asset_id is None:
            source.warnings.add("unsupported_media_kept_in_original")
        else:
            export_ids[path] = asset_id

    # Map text without image markup; offsets in the cleaned view are used solely
    # as insertion points. Rebuild final source-map offsets while emitting.
    plain = []
    events = deque()
    cursor = length = 0
    for start, end, path in _image_links(content):
        plain.append(content[cursor:start])
        length += start - cursor
        cursor = end
        # Raw HTML image tags are never passed through or fetched. Unlike a
        # DOCX parser's Markdown link, their position may be inside an HTML block.
        asset_id = export_ids.get(path) if path is not None else None
        if asset_id is not None:
            events.append((length, asset_id))
        else:
            source.warnings.add("docreader_image_reference_unresolved")
    plain.append(content[cursor:])
    counts = Counter(asset_id for _, asset_id in events)
    original_counts = {asset_id: sum(locators.values()) for asset_id, locators in by_asset.items()}
    placed = defaultdict(Counter)
    used_assets = set()
    pos = 0
    last_context = source.document

    def place(asset_id, locator):
        result.add(image_markdown(asset_id), locator, "image", asset=asset_id)
        placed[asset_id][locator] += 1
        used_assets.add(asset_id)

    def inline(asset_id, context):
        candidates = by_asset.get(asset_id, {})
        verified = None
        if counts[asset_id] <= original_counts.get(asset_id, 0):
            if len(candidates) == 1:
                verified = next(iter(candidates))
            elif context in candidates and placed[asset_id][context] < candidates[context]:
                verified = context
        if verified is None or verified == source.document:
            source.warnings.add("image_occurrence_not_verified")
        place(asset_id, verified or source.document)

    def emit(text, locator):
        nonlocal pos, last_context
        end = pos + len(text)
        index = 0
        while events and events[0][0] < end:
            at, asset_id = events.popleft()
            offset = at - pos
            result.add(text[index:offset], locator)
            context = last_context if offset == 0 and locator == source.document and text.startswith("|") else locator
            inline(asset_id, context)
            index = offset
        result.add(text[index:], locator)
        pos = end
        if text:
            last_context = source.document if text.endswith(("\n", "\r")) else locator

    map_md(result, "".join(plain), source, emit=emit)
    for _, asset_id in events:
        inline(asset_id, last_context)

    # Preserve package assets that the converter did not place. For a digest
    # already used at an unknown position, don't guess which appearances remain.
    appendix = False

    def append(asset_id, locator):
        nonlocal appendix
        result.add("\n\n" if appendix else _APPENDIX, source.document)
        appendix = True
        place(asset_id, locator)

    for asset_id, raw in dict(originals.values()).items():
        expected = by_asset[asset_id]
        if placed[asset_id][source.document]:
            continue
        for locator, remaining in (expected - placed[asset_id]).items():
            if result.store_image(raw) is None:
                source.warnings.add("unsupported_media_kept_in_original")
                break
            source.warnings.add("word_image_not_placed_inline")
            if locator == source.document:
                source.warnings.add("image_occurrence_not_verified")
            for _ in range(remaining):
                append(asset_id, locator)
    for asset_id in sorted(set(export_ids.values()) - used_assets):
        source.warnings.add("image_occurrence_not_verified")
        source.warnings.add("word_image_not_placed_inline")
        append(asset_id, source.document)
    if len(result.rows) > 500_000:
        raise ValueError("Office projection map limit exceeded")
