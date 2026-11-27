"""Read original OOXML for conservative address verification; never use Markdown ordinals as addresses."""
import io
import re
from collections import Counter, defaultdict
from decimal import Decimal, DecimalException, ROUND_HALF_UP
from zipfile import ZipFile

from docx import Document
from docx.opc.constants import RELATIONSHIP_TYPE as RT
from docx.oxml.ns import qn
from lxml import etree
from openpyxl import load_workbook
from openpyxl.styles.numbers import BUILTIN_FORMATS
from openpyxl.utils import coordinate_to_tuple, get_column_letter
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from .models import OfficeLocator
from .word_tables import MAX_TABLE_SLOTS, is_header, rendered_rows, word_grid


def norm(value):
    return " ".join(value.split())


def signature(cells):
    """Retain gaps. A lone value, even if currently unique, is not a row fingerprint."""
    cells = [(c, norm(v)) for c, v in cells if v.strip()]
    if len({v for _, v in cells}) < 2:
        return None
    first = cells[0][0]
    return tuple((c - first, v) for c, v in cells)


class Original:
    def __init__(self, fmt, data, *, record_sheet_values=False, locate_textboxes=False,
                 word_image_locations=False, word_table_grids=False, word_stories=False):
        self.fmt = fmt
        self.text = {}
        self.rows = {}
        self.controls = []
        self.textboxes = {}
        self.locate_textboxes = locate_textboxes
        self.word_image_locations = word_image_locations
        self.word_table_grids = word_table_grids
        self.word_stories = word_stories
        self.word_list_paragraphs = set()
        self.word_story_fragments = {}
        self.word_footnotes = {}
        self.word_grids = {}
        self.word_rows = {}
        self.images = []
        self.sheet_values = {} if record_sheet_values else None
        self.slide_count = 0
        self.warnings = set()
        self.units = 0
        self.document = self.loc()
        with ZipFile(io.BytesIO(data)) as z:
            if fmt == "docx":
                self.word(data)
            elif fmt == "pptx":
                self.slides(data)
            else:
                self.excel(data, z)

    def loc(self, **values):
        return OfficeLocator(format=self.fmt, **values)

    @staticmethod
    def unique(index, key, value):
        if key in index:
            index[key] = None  # ambiguity is sticky
        else:
            index[key] = value

    def budget(self, n=1):
        self.units += n
        if self.units > 500_000:
            raise ValueError("Office address verification limit exceeded")

    def record_row(self, loc, row, cells):
        cells = [(c, v) for c, v in cells if v.strip()]
        if key := signature(cells):
            self.unique(self.rows, key, (loc, row, cells[0][0]))

    def address(self, base, row, col):
        if self.fmt == "xlsx":
            return self.loc(sheet=base.sheet, cell=f"{get_column_letter(col)}{row}")
        return base.model_copy(update={"row": row, "column": col})

    def word_text(self, node, loc):
        pieces = []
        symbols = []
        for child in node.iter():
            # The final-text view includes insertions, never deletions or move-from runs.
            if child.tag not in {qn("w:t"), qn("w:sym"), qn("w:tab"), qn("w:br")}:
                continue
            if any(a.tag in {qn("w:del"), qn("w:moveFrom")} for a in child.iterancestors()):
                continue
            if self.locate_textboxes and next(child.iterancestors(qn("w:txbxContent")), None) is not None:
                continue
            if child.tag == qn("w:t"):
                pieces.append(child.text or "")
            elif child.tag == qn("w:sym"):
                font = child.get(qn("w:font"), "").lower()
                code = child.get(qn("w:char"), "").upper()
                glyph = "☑" if (font, code) == ("wingdings 2", "F052") else "[符号待核验]"
                pieces.append(glyph)
                symbols.append(glyph)
            else:
                pieces.append(" ")
        value = norm("".join(pieces))
        if symbols:
            self.controls.append((loc, value))
            self.warnings.add("source_symbol_evidence_attached")
        return value

    def word(self, data):
        from library.pipelines.docx import _embedded_image_rids

        doc = Document(io.BytesIO(data))
        if self.locate_textboxes:
            self._word_textboxes(doc.element.body)
        def pictures(el, loc):
            for rid in _embedded_image_rids(el):
                part = doc.part.related_parts.get(rid)
                if part is not None:
                    self.images.append((str(part.partname).lstrip("/"), loc))
        for i, para in enumerate(doc.paragraphs, 1):
            self.budget()
            loc = self.loc(paragraph=i)
            self.unique(self.text, self.word_text(para._p, loc), loc)
            if self.word_stories and (para._p.xpath('./w:pPr/w:numPr/w:numId[@w:val!="0"]')
                    or (not para._p.xpath('./w:pPr/w:numPr/w:numId')
                        and para.style._element.xpath('./w:pPr/w:numPr/w:numId[@w:val!="0"]'))):
                self.word_list_paragraphs.add(loc)
            if self.word_image_locations:
                self._word_pictures(doc.part, para._p, loc)
            else:
                pictures(para._p, loc)
        grid_budget = 500_000
        for i, table in enumerate(doc.tables, 1):
            base = self.loc(table=i)
            if not self.word_image_locations:
                pictures(table._tbl, base)
            trs = table._tbl.tr_lst
            width = len(table._tbl.xpath('./w:tblGrid/w:gridCol')) if self.word_table_grids else 0
            size = len(trs) * width
            build_grid = (self.word_table_grids and 0 < size <= min(grid_budget, MAX_TABLE_SLOTS)
                          and not table._tbl.xpath(
                              './w:sdt | ./w:customXml | ./w:ins | ./w:del'
                              ' | ./w:tr/w:sdt | ./w:tr/w:customXml | ./w:tr/w:ins | ./w:tr/w:del'
                              ' | .//w:tc/w:sdt | .//w:tc/w:customXml | .//w:tbl | .//w:hMerge'
                          ))
            if self.word_table_grids and not build_grid:
                self.warnings.add('word_table_grid_not_verified')
            if build_grid:
                grid_budget -= size
            physical_rows = []
            for r, tr in enumerate(trs, 1):
                col = 1 + int(next(iter(tr.xpath('./w:trPr/w:gridBefore/@w:val')), 0))
                cells = []
                physical = []
                for tc in tr.tc_lst:
                    self.budget()
                    loc = self.address(base, r, col)
                    if self.word_image_locations:
                        self._word_pictures(doc.part, tc, loc)
                    merge, span = tc.vMerge, tc.grid_span
                    if merge != "continue" or build_grid:
                        text = norm(" ".join(self.word_text(p, loc) for p in tc.p_lst))
                        if merge != "continue":
                            cells.append((col, text))
                        if build_grid:
                            physical.append((col - 1, span, merge, text))
                    col += span
                self.record_row(base, r, cells)
                if build_grid:
                    physical_rows.append((physical, is_header(tr)))
            if build_grid:
                try:
                    grid = word_grid(physical_rows, width)
                except ValueError:
                    self.warnings.add('word_table_grid_not_verified')
                else:
                    self.word_grids[i] = grid
                    for r, values, origins in rendered_rows(grid):
                        self.unique(self.word_rows, values, (base, r, origins))
        if doc.element.xpath('.//w:checkBox | .//*[local-name()="checkbox"]'):
            self.warnings.add("form_control_state_requires_original")
        if self.word_stories:
            self._word_stories(doc)

    def _word_stories(self, doc):
        # Follow only internal relationships used by actual sections / live note references.
        refs = {}
        for kind, reltype in (("header", RT.HEADER), ("footer", RT.FOOTER)):
            for node in doc.element.body.xpath(f'./w:sectPr/w:{kind}Reference | ./w:p/w:pPr/w:sectPr/w:{kind}Reference'):
                refs[node.get(qn("r:id"))] = kind, reltype
        note_ids = {int(raw) for node in doc.element.body.iter(qn("w:footnoteReference"))
                    if not any(a.tag in {qn("w:del"), qn("w:moveFrom")} for a in node.iterancestors())
                    and (raw := node.get(qn("w:id"), "")).isascii() and raw.isdecimal()
                    and len(raw) <= 9 and int(raw) > 0}
        notes = [rel for rel in doc.part.rels.values() if rel.reltype == RT.FOOTNOTES]
        if note_ids and len(notes) == 1:
            refs[notes[0].rId] = "footnotes", RT.FOOTNOTES
        for rid, (kind, reltype) in refs.items():
            rel = doc.part.rels.get(rid)
            if rel is None or rel.is_external or rel.reltype != reltype:
                continue
            part = rel.target_part
            name = str(part.partname).lstrip("/")
            pattern = r"word/footnotes\.xml" if kind == "footnotes" else rf"word/{kind}[0-9]*\.xml"
            if not re.fullmatch(pattern, name):
                continue
            root = etree.fromstring(part.blob, etree.XMLParser(resolve_entities=False, no_network=True))
            tag = {"header": "hdr", "footer": "ftr", "footnotes": "footnotes"}[kind]
            if root.tag != qn(f"w:{tag}"):
                continue
            groups = []
            if kind == "footnotes":
                nodes = list(root.iterchildren(qn("w:footnote")))
                ids = Counter(n.get(qn("w:id")) for n in nodes)
                groups = [(int(raw), n) for n in nodes if (raw := n.get(qn("w:id"), "")).isascii()
                          and raw.isdecimal() and len(raw) <= 9 and int(raw) in note_ids and ids[raw] == 1
                          and n.get(qn("w:type")) in (None, "normal")]
            else:
                groups = [(None, root)]
            for note_id, group in groups:
                for i, p in enumerate(group.iter(qn("w:p")), 1):
                    self.budget()
                    if any(a.tag in {qn("w:del"), qn("w:moveFrom"), qn("w:txbxContent")} for a in p.iterancestors()):
                        continue
                    loc = self.loc(word_part=name, story_paragraph=i, footnote_id=note_id)
                    value = self.word_text(p, loc)
                    if not value:
                        continue
                    self.word_story_fragments[loc] = value
                    if note_id is not None:
                        self.unique(self.word_footnotes, value, loc)

    def _word_pictures(self, part, block, loc):
        tags = {qn("a:blip"): qn("r:embed"), "{urn:schemas-microsoft-com:vml}imagedata": qn("r:id")}
        unsupported = {qn("w:del"), qn("w:moveFrom"), qn("w:txbxContent"),
                       "{http://schemas.openxmlformats.org/markup-compatibility/2006}Fallback"}
        for node in block.iter(*tags):
            self.budget()
            if any(parent.tag in unsupported for parent in node.iterancestors()):
                continue
            rel = part.rels.get(node.get(tags[node.tag]))
            if rel is not None and not rel.is_external and rel.reltype == RT.IMAGE:
                self.images.append((str(rel.target_part.partname).lstrip("/"), loc))

    def _word_textboxes(self, body):
        """Index unique final-view paragraph text; count competing body/table paragraphs too."""
        w_box, w_p, w_t = (qn(f"w:{tag}") for tag in ("txbxContent", "p", "t"))
        boxes = {box: i for i, box in enumerate(body.iter(w_box), 1)}
        if not boxes:
            return
        self.budget(len(boxes))
        runs = defaultdict(list)
        deleted = {qn("w:del"), qn("w:moveFrom")}
        separator = {qn("w:tab"), qn("w:br"), qn("w:cr")}
        for node in body.iter(w_t, *separator):
            self.budget()
            if any(parent.tag in deleted for parent in node.iterancestors()):
                continue
            if (p := next(node.iterancestors(w_p), None)) is not None:
                runs[p].append((node.text or "") if node.tag == w_t else " ")
        matches = Counter()
        seen = Counter()
        candidates = {}
        for p in body.iter(w_p):
            self.budget()
            box = next(p.iterancestors(w_box), None)
            if box is not None:
                matches[box] += 1
            if text := norm("".join(runs.pop(p, ()))):
                seen[text] += 1
                if box is not None:
                    candidates[text] = self.loc(textbox=boxes[box], textbox_paragraph=matches[box])
        # Ambiguous source text must not fall back to another paragraph with the same words.
        self.textboxes = {text: loc if seen[text] == 1 else None for text, loc in candidates.items()}

    def slides(self, data):
        prs = Presentation(io.BytesIO(data))
        self.slide_count = len(prs.slides)
        def walk(shapes, n, slide):
            for shape in shapes:
                self.budget()
                base = self.loc(slide=n, shape=shape.shape_id)
                if shape.shape_type == MSO_SHAPE_TYPE.GROUP:
                    walk(shape.shapes, n, slide)
                elif shape.has_table:
                    for r, row in enumerate(shape.table.rows, 1):
                        self.budget(len(row.cells))
                        self.record_row(base, r, [(c, cell.text) for c, cell in enumerate(row.cells, 1)
                                                  if not cell.is_spanned])
                elif shape.has_text_frame:
                    for value in set(norm(p.text) for p in shape.text_frame.paragraphs):
                        self.unique(self.text, value, base)
                if shape.shape_type == MSO_SHAPE_TYPE.PICTURE and shape._element.blip_rId:
                    part = slide.part.related_part(shape._element.blip_rId)
                    self.images.append((str(part.partname).lstrip("/"), base))
        for n, slide in enumerate(prs.slides, 1):
            walk(slide.shapes, n, slide)

    def excel(self, data, z):
        # Stream physical cells only; never expand the declared worksheet dimensions.
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True, keep_links=False)
        x = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
        try:
            for ws in wb.worksheets:
                if ws.sheet_state != "visible":
                    continue
                base = self.loc(sheet=ws.title)
                with z.open(ws._worksheet_path) as stream:
                    for _, row in etree.iterparse(stream, events=("end",), tag=x+"row", resolve_entities=False, no_network=True):
                        cells = []
                        rr = None
                        for c in row.iterchildren(x+"c"):
                            self.budget()
                            address = c.get("r")
                            if not address:
                                continue
                            r, col = coordinate_to_tuple(address)
                            if rr not in (None, r) or not 1 <= r <= 1048576 or not 1 <= col <= 16384:
                                raise ValueError("invalid worksheet address")
                            rr = r
                            typ, raw = c.get("t", "n"), c.findtext(x+"v", "")
                            if typ == "inlineStr":
                                value = "".join(n.text or "" for n in c.iter(x+"t"))
                            elif typ == "s":
                                value = ws._shared_strings[int(raw)]
                            elif typ == "n" and raw:
                                style = wb._cell_styles[int(c.get("s", "0"))].numFmtId
                                code = BUILTIN_FORMATS.get(style, "") if style < 164 else wb._number_formats[style-164]
                                value = _number(raw, code)
                            else:
                                value = raw
                            if self.sheet_values is not None and value:
                                self.sheet_values[ws.title, address] = value
                            cells.append((col, value))
                        if rr:
                            self.record_row(base, rr, sorted(cells))
                        row.clear()
                        while row.getprevious() is not None:
                            del row.getparent()[0]
        finally:
            wb.close()


def _number(value, code):
    if code in {"General", "@", ""}:
        return value
    if re.fullmatch(r"0(?:\.0{1,12})?%?", code):
        try:
            dp = code.count("0")-1
            scale = 100 if code.endswith("%") else 1
            num = (Decimal(value)*scale).quantize(Decimal(1).scaleb(-dp), rounding=ROUND_HALF_UP)
            return format(num, "f") + ("%" if scale == 100 else "")
        except DecimalException:
            pass
    # Nonmatching sentinel prevents an unsupported display format earning a verified address.
    return "\x00" + value
