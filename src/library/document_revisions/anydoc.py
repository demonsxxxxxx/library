"""Retain the actual AnyDoc Markdown; attach independently verified ranges, never inferred ordinals."""
import io
import json
import os
import re
import subprocess
from collections import Counter, defaultdict
from pathlib import Path
from zipfile import ZipFile

from ._anydoc_worker import ANYDOC_VERSION, MAX_OUTPUT, check_package
from .anydoc_source import Original, norm, signature
from .archive import sha256
from .models import DocumentCoverage, ParserStamp, TableData
from .native import ParsedDocument
from .table_data import TableGrid


def run_worker(data, fmt, python):
    if not python or not Path(python).is_absolute():
        raise ValueError("OFFICE_ANYDOC_PYTHON must name an absolute, pinned Python interpreter")
    env = {k: v for k, v in os.environ.items() if k.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP"}}
    proc = subprocess.run([python, "-I", "-B", str(Path(__file__).with_name("_anydoc_worker.py")), fmt],
                          input=data, capture_output=True, env=env, timeout=120)
    if proc.returncode:
        code = proc.stderr.decode("ascii", "ignore")
        raise ValueError("AnyDoc Office worker failed: " + (code if re.fullmatch(r"[A-Za-z]{1,50}", code) else "conversion_error"))
    if len(proc.stdout) > MAX_OUTPUT:
        raise ValueError("AnyDoc output exceeds limit")
    return json.loads(proc.stdout)


def plain(s):
    # Only presentation whitespace and delimiters: never strip signs, units, or digits.
    s = s.replace(r"\|", "|").replace("<br>", " ")
    return norm(re.sub(r"\*\*(.+?)\*\*", r"\1", s))


def fields(line):
    if not line.startswith("| ") or not line.rstrip().endswith("|"):
        return []
    bars = [m.start() for m in re.finditer(r"(?<!\\)\|", line)]
    return [(a+1, b, plain(line[a+1:b])) for a, b in zip(bars, bars[1:])]


def _paragraph_value(line):
    return plain(re.sub(r"^#{1,6} +", "", line))


def map_md(result, md, source, *, emit=None):
    emit = result.add if emit is None else emit
    lines = md.splitlines(keepends=True)
    if len(lines) > 500_000:
        raise ValueError("Office Markdown line limit exceeded")
    seen = Counter()
    word_seen = Counter()
    word_rows = source.word_rows
    boxes = source.textboxes
    box_counts = Counter()
    note_pattern = re.compile(r"^\s*\d+\.\s+(.+?)\s+\[↑\]\(#footnote-ref-\d+\)\s*$")
    note_counts = Counter(plain(m[1]) for line in lines if source.word_footnotes and (m := note_pattern.fullmatch(line)))
    for line in lines:
        cols = fields(line)
        key = signature([(c, v) for c, (_, _, v) in enumerate(cols)])
        if key:
            seen[key] += 1
        if word_rows and cols:
            word_seen[tuple(v for _, _, v in cols)] += 1
        if boxes:
            box_counts.update(v for v in ([v for _, _, v in cols] if cols else [_paragraph_value(line)]) if v in boxes)
    for line in lines:
        cols = fields(line)
        values = [(c, v) for c, (_, _, v) in enumerate(cols) if v]
        key = signature(values)
        match = source.rows.get(key) if seen.get(key) == 1 else None
        word_key = tuple(v for _, _, v in cols) if word_rows and cols else ()
        word_match = word_rows.get(word_key) if word_seen.get(word_key) == 1 else None
        # An unmodeled source table can still conflict with a complete grid match.
        if word_match and (key not in source.rows or (match and match[:2] == word_match[:2])):
            base, _, origins = word_match
            cursor = 0
            for (a, b, value), origin in zip(cols, origins, strict=True):
                if value and origin is not None:
                    emit(line[cursor:a], source.document)
                    emit(line[a:b], source.address(base, *origin))
                    cursor = b
            emit(line[cursor:], source.document)
        elif match and word_key not in word_rows and match[0].table not in source.word_grids:
            base, r, first = match
            cursor = 0
            for c, (a, b, value) in enumerate(cols):
                if value:
                    emit(line[cursor:a], source.document)
                    emit(line[a:b], source.address(base, r, first+c-values[0][0]))
                    cursor = b
            emit(line[cursor:], source.document)
        elif cols and any(v in boxes and boxes[v] is not None and box_counts[v] == 1 for _, _, v in cols):
            cursor = 0
            for a, b, value in cols:
                loc = boxes.get(value) if box_counts[value] == 1 else None
                if loc is None:
                    if value:
                        source.warnings.add("office_address_not_verified")
                    continue
                emit(line[cursor:a], source.document)
                emit(line[a:b], loc)
                cursor = b
            emit(line[cursor:], source.document)
        else:
            value = _paragraph_value(line)
            loc = None
            if not cols:
                if value in boxes:
                    loc = boxes[value] if box_counts[value] == 1 else None
                else:
                    loc = source.text.get(value)
                    if loc is None and source.word_stories and value not in source.text:
                        # Only match presentation markers against verified original ranges.
                        if (note := note_pattern.fullmatch(line)) and note_counts[plain(note[1])] == 1:
                            loc = source.word_footnotes.get(plain(note[1]))
                        else:
                            stripped = re.sub(r"\[\[\d+\]\]\(#footnote-\d+\)", "", value).strip()
                            candidate = source.text.get(stripped)
                            if candidate is None and stripped not in source.text:
                                item = re.sub(r"^(?:\d+[.)]|[-+*])\s+", "", stripped, count=1)
                                candidate = source.text.get(item)
                                if candidate not in source.word_list_paragraphs:
                                    candidate = None
                            loc = candidate
            if line.strip() and loc is None:
                source.warnings.add("office_address_not_verified")
            emit(line, loc or source.document)
        if len(result.rows) > 500_000:
            raise ValueError("Office projection map limit exceeded")


def parse_anydoc(data, *, document, filename, format, python, worker=run_worker):
    if sha256(data) != document.sha256:
        raise ValueError("mismatched Office original")
    check_package(data, format)
    out = worker(data, format, python)
    if (out.get("version"), out.get("fmt"), out.get("sha256")) != (ANYDOC_VERSION, format, document.sha256):
        raise ValueError("invalid AnyDoc export identity")
    source = Original(format, data)
    result = ParsedDocument(document, filename, len(data), format, separator="")
    result.parser_stamp = ParserStamp(name="anydoc", route="text", version=ANYDOC_VERSION,
                                      config_digest=sha256(b"anydoc-office-v1/source-rows/offline/ocr-reject"))
    source.warnings.add("document_completeness_not_certified")
    md = out["markdown"]
    if md.strip():
        map_md(result, md, source)
    for exported in out["grids"]:
        grid = TableGrid.model_validate(exported)
        tid = f"table-{len(result.tables)+1}"
        # These are component grids, not verified source table ordinals.
        result.tables.append(TableData(table_id=tid, locator=source.document, source="native", grid=grid))
        result.add(f"\n\n<!-- {tid} -->", source.document, "table", table=tid)
    for loc, value in source.controls:
        result.add("\n\n[原件符号核对] " + value, loc)
    prefix = {"docx": "word", "xlsx": "xl", "pptx": "ppt"}[format] + "/media/"
    occurrences = defaultdict(list)
    for name, loc in source.images:
        occurrences[name].append(loc)
    with ZipFile(io.BytesIO(data)) as z:
        for name in z.namelist():
            if not name.startswith(prefix) or name.endswith("/"):
                continue
            blob = z.read(name)
            source.warnings.add("images_not_transcribed")
            if not occurrences[name]:
                source.warnings.add("image_occurrence_not_verified")
            for loc in occurrences[name] or [source.document]:
                if not result.image(blob, loc):
                    source.warnings.add("unsupported_media_kept_in_original")
    n = int(bool(result.rows))
    result.coverage = DocumentCoverage(processed=n, partial=(1,) if n else (), failed=() if n else (1,),
                                       image_count=len(result.assets), warnings=tuple(sorted(source.warnings)))
    return result
