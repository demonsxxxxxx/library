"""Project docreader Office output onto independently checked source locations."""
from __future__ import annotations

import base64
import io
import json
import os
import re
import subprocess
from collections import defaultdict
from pathlib import Path
from zipfile import ZipFile

from ._anydoc_worker import MAX_OUTPUT, check_package
from ._weknora_office_worker import MAX_WORD_COPY
from ._weknora_worker import WEKNORA_COMMIT
from .anydoc_source import Original, norm
from .archive import ASSET_SCHEME, sha256
from .models import DocumentCoverage, ParserStamp, TableData
from .native import ParsedDocument
from .paddle_ocr import BACKEND, recognize_images
from .weknora_word_images import has_word_text, project_word_images

PARSER_NAME = "weknora-office-builtin"
PARSERS = {"docx": "Docx2Parser", "xlsx": "ExcelParser", "pptx": "MarkitdownParser"}
_SLIDE = re.compile(r"^<!-- Slide number: (\d+) -->[ \t]*$", re.M)
_IMG = re.compile(r"!\[[^\]\r\n]*\]\(([^\s)]+)(?:[ \t]+[^\r\n)]*)?\)")
_CELL = re.compile(r"(?:^|,)([A-Z]{1,3}): ")


def _word_without_headers_and_footers(data):
    """Build an in-memory parser copy; keep the immutable original for citations."""
    from docx import Document
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml.ns import qn
    from lxml import etree

    doc = Document(io.BytesIO(data))
    relationship_ids = [rid for rid, rel in doc.part.rels.items() if rel.reltype in (RT.HEADER, RT.FOOTER)]
    tags = {qn("w:headerReference"), qn("w:footerReference")}
    references = [node for section in doc.element.iter(qn("w:sectPr")) for node in section if node.tag in tags]
    if not relationship_ids and not references:
        return data
    side_images = {
        edge.target_part.partname
        for rid in relationship_ids if not doc.part.rels[rid].is_external
        for edge in doc.part.rels[rid].target_part.rels.values()
        if not edge.is_external and edge.reltype == RT.IMAGE
    }
    for node in references:
        node.getparent().remove(node)
    for rid in relationship_ids:
        del doc.part.rels[rid]
    if side_images:
        relationship_namespace = qn("r:id").split("}", 1)[0] + "}"
        for part in list(doc.part.package.iter_parts()):
            edges = [rid for rid, rel in part.rels.items() if not rel.is_external and rel.reltype == RT.IMAGE
                     and rel.target_part.partname in side_images]
            if not edges or part.partname.ext != "xml":
                continue
            xml = etree.fromstring(part.blob, etree.XMLParser(resolve_entities=False, no_network=True))
            referenced = {value for node in xml.iter() for key, value in node.attrib.items()
                          if key.startswith(relationship_namespace)
                          or key == "{urn:schemas-microsoft-com:office:office}relid"}
            for rid in edges:
                if rid not in referenced:
                    del part.rels[rid]
    # The writer follows remaining relationships, so header/footer-only assets
    # disappear while assets later needed by body/table/notes stay reachable.
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


def run_worker(data, fmt, python, checkout):
    if not python or not checkout:
        raise ValueError("WeKnora ingest requires PDF_REVISION_WEKNORA_PYTHON and PDF_REVISION_WEKNORA_SOURCE")
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR"}}
    worker = Path(__file__).with_name("_weknora_office_worker.py")
    # Keep the virtualenv's interpreter symlink so Python uses its pyvenv.cfg.
    result = subprocess.run([os.path.abspath(python), "-I", "-B", str(worker), str(Path(checkout).resolve()), fmt],
                            input=data, capture_output=True, timeout=120, env=env)
    if result.returncode or len(result.stdout) > MAX_OUTPUT:
        reason = result.stderr.decode("ascii", "ignore").strip()
        raise ValueError("docreader Office failed: " + (reason if re.fullmatch(r"[A-Za-z]{1,48}", reason) else "worker_error"))
    return json.loads(result.stdout)


def _excel(result, source, text, blocks):
    end = 0
    for block in blocks:
        a, b, loc = block["start"], block["end"], block["locator"]
        row, sheet = loc.get("row_start"), loc.get("sheet")
        if (not 0 <= end <= a < b <= len(text) or text[end:a].strip()
                or loc.get("type") != "sheet" or type(row) is not int or not 1 <= row <= 1048576
                or row != loc.get("row_end") or not isinstance(sheet, str)):
            raise ValueError("invalid docreader worksheet range")
        fragment = text[a:b]
        fields = list(_CELL.finditer(fragment))
        # Do not infer hidden sheets or force a cell assignment for a display mismatch.
        verified = []
        for i, match in enumerate(fields):
            stop = fields[i + 1].start() if i + 1 < len(fields) else len(fragment.rstrip("\r\n"))
            addr = f"{match[1]}{row}"
            if norm(fragment[match.end():stop]) == norm(source.sheet_values.get((sheet, addr), "\x00")):
                verified.append((match.end(), stop, source.loc(sheet=sheet, cell=addr)))
        coarse = source.loc(sheet=sheet, sheet_row=row) if verified else source.document
        if len(verified) != len(fields) or not verified:
            source.warnings.add("office_cell_address_not_verified")
        result.add(text[end:a], source.document)
        cursor = 0
        for start, stop, position in verified:
            result.add(fragment[cursor:start], coarse)
            result.add(fragment[start:stop], position)
            cursor = stop
        result.add(fragment[cursor:], coarse)
        end = b
    if text[end:].strip():
        source.warnings.add("office_cell_address_not_verified")
    result.add(text[end:], source.document)


def _slides(result, source, text):
    markers = list(_SLIDE.finditer(text))
    # Ambiguous numbering (for example literal HTML comments in a slide) earns no slide guess.
    if [int(m[1]) for m in markers] != list(range(1, source.slide_count + 1)):
        source.warnings.add("office_slide_address_not_verified")
        result.add(_SLIDE.sub("", text), source.document)
        return
    result.add(text[:markers[0].start()] if markers else text, source.document)
    for i, marker in enumerate(markers):
        end = markers[i + 1].start() if i + 1 < len(markers) else len(text)
        slide = source.loc(slide=int(marker[1]))
        # The comment identifies the original slide; it is not searchable slide text.
        for line in text[marker.end():end].splitlines(keepends=True):
            loc = source.text.get(norm(re.sub(r"^#{1,6}\s+", "", line)))
            result.add(line, loc if loc is not None and loc.slide == slide.slide else slide)


def _project(data, document, filename, fmt, out, *, original_size, input_sha):
    if ((out.get("fmt"), out.get("parser"), out.get("commit"), out.get("sha256"))
            != (fmt, PARSERS[fmt], WEKNORA_COMMIT, input_sha)):
        raise ValueError("invalid docreader Office output identity")
    source = Original(fmt, data, record_sheet_values=fmt == "xlsx", locate_textboxes=fmt == "docx",
                      word_image_locations=fmt == "docx", word_table_grids=fmt == "docx", word_stories=fmt == "docx")
    source.warnings.add("document_completeness_not_certified")
    result = ParsedDocument(document, filename, original_size, fmt, separator="")
    content = out["content"]
    if ASSET_SCHEME in content.lower() or "\x00" in content:
        raise ValueError("invalid docreader Office content")
    if fmt == "docx":
        project_word_images(result, source, data, content, out["images"])
        mapped = {row.locator for row in result.rows if row.kind == "native"}
        for loc, value in source.word_story_fragments.items():
            if loc not in mapped:
                result.add("\n\n", loc)
                result.add(value, loc)
        for number, grid in source.word_grids.items():
            locator = source.loc(table=number)
            table_id = f"table-{number}"
            result.tables.append(TableData(table_id=table_id, locator=locator, source="native", grid=grid))
            # Keep the parser body and image offsets unchanged. The existing
            # archive contract requires one mapped reference per table.
            result.add(f"\n\n<!-- {table_id} -->", locator, "table", table=table_id)
        return result, source.warnings
    # Preserve Excel offsets. Other Office formats have no authoritative byte ranges.
    if fmt != "xlsx":
        content = _IMG.sub("", content)
    if fmt == "xlsx":
        _excel(result, source, content, out["source_blocks"])
    else:
        _slides(result, source, content)

    prefix = {"docx": "word", "xlsx": "xl", "pptx": "ppt"}[fmt] + "/media/"
    locations = defaultdict(set)
    for name, loc in source.images:
        locations[name].add(loc)
    seen = set()

    def add_image(blob, loc):
        key = sha256(blob), loc
        if key not in seen:
            result.add("\n\n", loc)
            if result.image(blob, loc):
                seen.add(key)
            else:
                source.warnings.add("unsupported_media_kept_in_original")

    with ZipFile(io.BytesIO(data)) as package:
        for path in package.namelist():
            if not path.startswith(prefix) or path.endswith("/"):
                continue
            if not locations[path]:
                source.warnings.add("image_occurrence_not_verified")
            for loc in sorted(locations[path] or {source.document}, key=lambda item: item.model_dump_json()):
                add_image(package.read(path), loc)
    original_ids = {key[0] for key in seen}
    for blob in out["images"].values():
        try:
            raw = base64.b64decode(blob, validate=True)
        except (TypeError, ValueError):
            source.warnings.add("docreader_image_invalid")
            continue
        if sha256(raw) not in original_ids:
            source.warnings.add("image_occurrence_not_verified")
            add_image(raw, source.document)
    return result, source.warnings


async def parse_weknora_office(data, *, document, filename, fmt, python, checkout, before_send, paddle=True):
    import asyncio

    if sha256(data) != document.sha256:
        raise ValueError("mismatched Office original")
    check_package(data, fmt)
    parser_input = await asyncio.to_thread(_word_without_headers_and_footers, data) if fmt == "docx" else data
    if len(parser_input) > MAX_WORD_COPY:
        raise ValueError("repacked Word input exceeds worker limit")
    input_sha = document.sha256 if parser_input is data else sha256(parser_input)
    out = await asyncio.to_thread(run_worker, parser_input, fmt, python, checkout)
    result, warnings = await asyncio.to_thread(
        _project, parser_input, document, filename, fmt, out, original_size=len(data), input_sha=input_sha,
    )
    if fmt == "docx":
        native = has_word_text(result)
    else:
        native = any(r.kind == "native" and part.strip() for r, part in zip(result.rows, result.fragments, strict=True))
    if paddle and result.assets:
        reports = await recognize_images(result.assets, before_send=before_send)
        for row in list(result.rows):
            if row.kind != "image":
                continue
            report = reports[row.asset_ids[0]]
            warnings.update(report.warnings)
            if report.text:
                warnings.add("paddle_ocr_needs_review")
                result.add("\n\n", row.locator)
                result.add(report.text, row.locator, "ocr", asset=row.asset_ids[0],
                           figure_map_id=row.source_map_id if fmt == "docx" else None)
    elif result.assets:
        warnings.add("images_not_transcribed")
    has_ocr = any(r.kind == "ocr" for r in result.rows)
    if not (native or has_ocr):
        raise ValueError("docreader produced no readable Office text")
    backend = BACKEND if paddle else None
    config = [fmt, out["versions"], backend]
    if fmt == "docx":
        config.extend(("inline-images-v1", "original-table-grids-v1", "word-footnotes-no-headers-footers-v1"))
    result.parser_stamp = ParserStamp(name=PARSER_NAME, version=WEKNORA_COMMIT, ocr_backend=backend,
        route="mixed" if native and has_ocr else "ocr" if has_ocr else "text",
        config_digest=sha256(json.dumps(config, sort_keys=True).encode()))
    result.coverage = DocumentCoverage(processed=1, partial=(1,), image_count=len(result.assets),
                                       warnings=tuple(sorted(warnings)))
    return result
