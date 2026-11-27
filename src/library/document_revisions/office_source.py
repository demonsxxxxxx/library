"""Read original Office addresses for verification, not as replacement parser output."""
import io
from collections import defaultdict
from zipfile import ZipFile

from .models import OfficeLocator


class SourceIndex:
    def __init__(self, fmt, data):
        self.fmt = fmt
        self.text = defaultdict(set)
        self.tables = defaultdict(list)
        self.cells = {}
        self.images = []
        self.slides = set()
        self.sheets = set()
        if fmt == "docx":
            self._word(data)
        elif fmt == "xlsx":
            self._excel(data)
        else:
            self._slides(data)
        with ZipFile(io.BytesIO(data)) as z:
            self.media = {n: z.read(n) for n in z.namelist() if n.startswith(
                {"docx":"word/media/", "xlsx":"xl/media/", "pptx":"ppt/media/"}[fmt]) and not n.endswith("/")}

    def loc(self, **kw):
        return OfficeLocator(format=self.fmt, **kw)

    def _record(self, scope, value, loc):
        if value.strip():
            self.text[(scope, value.strip())].add(loc)

    def _word(self, data):
        from docx import Document
        from library.pipelines.docx import _embedded_image_rids
        doc = Document(io.BytesIO(data))
        def pictures(element, loc):
            for rid in _embedded_image_rids(element):
                part = doc.part.related_parts.get(rid)
                if part is not None:
                    self.images.append((str(part.partname).lstrip("/"), loc))
        for i, para in enumerate(doc.paragraphs, 1):
            loc = self.loc(paragraph=i)
            self._record(None, para.text, loc)
            pictures(para._p, loc)
        for i, table in enumerate(doc.tables, 1):
            vals = {}
            for r, row in enumerate(table.rows, 1):
                for c, cell in enumerate(row.cells, 1+getattr(row,"grid_cols_before",0)):
                    vals[(r,c)] = cell.text.strip()
            self.tables[None].append((self.loc(table=i), vals))
            pictures(table._tbl, self.loc(table=i))

    def _excel(self, data):
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True, keep_links=False)
        try:
            for ws in wb.worksheets:
                self.sheets.add(ws.title)
                # Some producers declare A1 even though the worksheet XML has many cells.
                # Stream actual rows without trusting the cached dimension or materialising a rectangle.
                ws.reset_dimensions()
                examined = 0
                for row in ws.rows:
                    examined += max(1, len(row))
                    if examined > 500_000:
                        break
                    for cell in row:
                        if cell.value is not None:
                            self.cells[ws.title,cell.coordinate] = str(cell.value).strip()
        finally:
            wb.close()

    def _slides(self, data):
        from pptx import Presentation
        from pptx.enum.shapes import MSO_SHAPE_TYPE
        prs = Presentation(io.BytesIO(data))
        def visit(shapes, n, slide):
            for shape in shapes:
                loc = self.loc(slide=n,shape=shape.shape_id)
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    visit(shape.shapes, n, slide)
                elif shape.has_table:
                    tab = shape.table
                    vals = {(r+1,c+1):cell.text.strip() for r,row in enumerate(tab.rows) for c,cell in enumerate(row.cells)}
                    self.tables[n].append((loc,vals))
                elif shape.has_text_frame:
                    self._record(n,shape.text,loc)
                    for para in shape.text_frame.paragraphs:
                        self._record(n,para.text,loc)
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                    rid = shape._element.blip_rId
                    if rid:
                        part = slide.part.related_part(rid)
                        self.images.append((str(part.partname).lstrip("/"),loc))
        for n, slide in enumerate(prs.slides,1):
            self.slides.add(n)
            visit(slide.shapes,n,slide)

    def text_locator(self, text, scope, fallback):
        hits = self.text.get((scope,text.strip()), set())
        return next(iter(hits)) if len(hits)==1 else fallback

    def table_locator(self, cells, scope, fallback):
        values = {(c.row+1,c.col+1):c.text.strip() for c in cells if c.text.strip()}
        matches = [loc for loc,data in self.tables.get(scope,()) if values and
                   all(data.get(k)==v for k,v in values.items())]
        return matches[0] if len(matches)==1 else fallback
