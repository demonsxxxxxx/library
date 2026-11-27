"""Build Word grids from physical OOXML cells, not from rendered pipe tables."""

from dataclasses import dataclass

from docx.oxml.ns import qn

from .table_data import Cell, TableGrid

MAX_TABLE_SLOTS = 100_000


@dataclass
class _Origin:
    row: int
    col: int
    width: int
    text: str
    header: bool
    height: int = 1


def word_grid(rows, width):
    """Read physical cells in row/column order; a covered slot has no new content."""
    if not width or not rows or len(rows) * width > MAX_TABLE_SLOTS:
        raise ValueError("invalid Word table size")
    origins = []
    active = {}
    for r, (row, header) in enumerate(rows):
        cursor = 0
        following = {}
        for col, span, merge, text in row:
            if col != cursor or span < 1 or col + span > width:
                raise ValueError("incomplete or invalid Word table row")
            key = col, span
            if merge == "continue":
                origin = active.get(key)
                if origin is None or text:
                    raise ValueError("invalid Word vertical merge continuation")
                origin.height += 1
                following[key] = origin
            elif merge in (None, "restart"):
                origin = _Origin(r, col, span, text, header)
                origins.append(origin)
                if merge == "restart":
                    following[key] = origin
            else:
                raise ValueError("unknown Word vertical merge state")
            cursor += span
        if cursor != width:
            raise ValueError("incomplete Word table row")
        active = following
    return TableGrid(rows=len(rows), cols=width, cells=[
        Cell(row=x.row, col=x.col, rowspan=x.height, colspan=x.width, header=x.header, text=x.text)
        for x in origins
    ])


def is_header(row):
    headers = row.xpath('./w:trPr/w:tblHeader')
    return bool(headers and headers[0].get(qn('w:val'), 'true') in {'true', '1', 'on'})


def rendered_rows(grid):
    """Yield complete GFM row candidates and the true origin behind each value.

    WeKnora/MarkItDown copies vertical values and pads horizontal spans. Its
    python-docx fallback can repeat horizontal values. A covered slot is never
    represented as a physical cell, even when its value appears in Markdown.
    """
    slots = [[None] * grid.cols for _ in range(grid.rows)]
    for cell in grid.cells:
        if cell.text:
            for r in range(cell.row, cell.row + cell.rowspan):
                slots[r][cell.col:cell.col + cell.colspan] = [cell] * cell.colspan
    for r, row in enumerate(slots):
        variants = {}
        for v, h in ((True, False), (False, False), (True, True)):
            cells = tuple(x if x and (v or x.row == r) and (h or x.col == c) else None
                          for c, x in enumerate(row))
            variant = tuple(x.text if x else '' for x in cells)
            origins = {(x.row, x.col, x.text) for x in cells if x}
            if len({x[2] for x in origins}) >= 2:
                variants[variant] = tuple((x.row + 1, x.col + 1) if x else None for x in cells)
        for variant, locations in variants.items():
            yield r + 1, variant, locations
