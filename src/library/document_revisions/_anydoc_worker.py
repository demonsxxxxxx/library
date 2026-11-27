"""Pinned local Office converter; no Library imports, database, or hosted OCR."""
import hashlib
import io
import json
import sys
from importlib.metadata import version
from zipfile import ZipFile

ANYDOC_VERSION = "0.2.5"
MAX_INPUT = 16 * 1024**2
MAX_EXPANDED = 128 * 1024**2
MAX_OUTPUT = 32 * 1024**2


def check_package(data, fmt, *, max_input=MAX_INPUT):
    if fmt not in {"docx", "pptx", "xlsx"} or not 0 < len(data) <= max_input:
        raise ValueError("invalid Office input")
    with ZipFile(io.BytesIO(data)) as z:
        files = z.infolist()
        if len(files) > 12000 or sum(i.file_size for i in files) > MAX_EXPANDED:
            raise ValueError("Office package limit exceeded")
        if len({i.filename for i in files}) != len(files):
            raise ValueError("ambiguous Office package")
        if any(i.flag_bits & 1 for i in files):
            raise ValueError("encrypted Office package")
        content_types = z.read("[Content_Types].xml").lower()
        if b"macroenabled" in content_types or b"vbaproject" in content_types:
            raise ValueError("macro-enabled Office package not supported")


def offline(event, _args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system"}:
        raise PermissionError("AnyDoc Office conversion is local only")


def inline_text(i):
    if i.kind == "checkbox":
        return "☑" if i.checked else "☐"
    if i.kind == "line_break":
        return "\n"
    if i.content:
        return "".join(inline_text(child) for child in i.content)
    return i.text or i.alt or ""


def block_text(block):
    if block.content is not None:
        return "".join(inline_text(i) for i in block.content)
    if block.list is not None:
        return "\n".join(block_text(b) for item in block.list.items for b in item.blocks)
    if block.blocks:
        return "\n".join(block_text(b) for b in block.blocks)
    return block.text or ""


def grids(doc):
    # The canonical model retains spans which GFM tables cannot express.
    out = []
    total = 0
    for block in doc.blocks:
        if block.kind != "table":
            continue
        table = block.table
        grid = table.grid
        width = max((len(row) for row in grid), default=0)
        if not width:
            continue
        total += len(grid) * width
        if total > 500_000 or len(grid) * width > 100_000:
            raise ValueError("Office table projection limit exceeded")
        cells = []
        for r, row in enumerate(grid):
            for c, slot in enumerate(row):
                if slot.kind != "origin":
                    continue
                cell = slot.cell
                cells.append(dict(row=r, col=c, rowspan=cell.row_span, colspan=cell.col_span,
                                  header=r < table.header_rows,
                                  text="\n".join(block_text(b) for b in cell.blocks)))
        out.append(dict(rows=len(grid), cols=width, cells=cells))
    return out


def main(fmt):
    sys.addaudithook(offline)
    import anydoc

    installed = version("firecrawl-anydoc")
    if installed != ANYDOC_VERSION:
        raise ValueError(f"expected firecrawl-anydoc {ANYDOC_VERSION}")
    data = sys.stdin.buffer.read(MAX_INPUT + 1)
    check_package(data, fmt)
    if anydoc.format_from_bytes(data) != fmt:
        raise ValueError("Office signature disagrees with requested format")
    markdown = anydoc.to_markdown_bytes(data, fmt, ocr="reject")
    payload = json.dumps({"fmt": fmt, "version": installed, "sha256": hashlib.sha256(data).hexdigest(),
                          "markdown": markdown, "grids": grids(anydoc.to_document(data, fmt))}, ensure_ascii=False).encode()
    if len(payload) > MAX_OUTPUT:
        raise ValueError("Office projection limit exceeded")
    sys.stdout.buffer.write(payload)


if __name__ == "__main__":
    try:
        main(sys.argv[1])
    except Exception as exc:
        # Only report a class, never private filenames or excerpts from a parser exception.
        sys.stderr.write(type(exc).__name__)
        sys.exit(1)
