"""Keep derived row context searchable without inventing Word source cells."""

import io
from uuid import uuid4

import pytest
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from library.document_revisions import office, weknora_office
from library.document_revisions.archive import RevisionArchive, sha256
from library.document_revisions.models import DocumentRef, OfficeLocator

MARKDOWN = (
    '| Batch | Grouped header |  |\n'
    '| --- | --- | --- |\n'
    '| Batch A | 12 | mg/L |\n'
    '| Batch A | 14 | mg/L |\n'
)


def _table(doc):
    t = doc.add_table(rows=3, cols=3)
    t.rows[0]._tr.get_or_add_trPr().append(OxmlElement('w:tblHeader'))
    t.cell(0, 0).text = 'Batch'
    t.cell(0, 1).merge(t.cell(0, 2)).text = 'Grouped header'
    t.cell(1, 0).merge(t.cell(2, 0)).text = 'Batch A'
    for i, val in enumerate(('12', '14'), 1):
        t.cell(i, 1).text = val
        t.cell(i, 2).text = 'mg/L'
    return t


async def _parse(monkeypatch, doc, markdown):
    buf = io.BytesIO()
    doc.save(buf)
    data = buf.getvalue()
    document = DocumentRef(file_id='fixture', entry_id=str(uuid4()), sha256=sha256(data))
    monkeypatch.setattr(weknora_office, 'run_worker', lambda *_: {
        'fmt': 'docx', 'parser': 'Docx2Parser', 'commit': weknora_office.WEKNORA_COMMIT,
        'sha256': document.sha256, 'content': markdown, 'versions': {}, 'images': {},
    })
    return await weknora_office.parse_weknora_office(
        data, document=document, filename='sample.docx', fmt='docx', python='', checkout='',
        before_send=None, paddle=False,
    )


async def test_verified_grid_and_row_context_survive_publish_and_index(tmp_path, monkeypatch):
    from types import SimpleNamespace

    doc = Document()
    _table(doc)
    parsed = await _parse(monkeypatch, doc, MARKDOWN)
    archive = RevisionArchive(tmp_path)
    ref = parsed.publish(archive)
    rev = archive.read(ref, allowed={ref})
    table, = rev.tables
    assert table.locator == OfficeLocator(format='docx', table=1)
    assert table.table_id == 'table-1'
    cells = {(c.row, c.col): c for c in table.grid.cells}
    assert (cells[0, 1].colspan, cells[0, 1].header) == (2, True)
    assert (cells[1, 0].rowspan, cells[1, 0].text) == (2, 'Batch A')
    assert cells[2, 1].text == '14'
    assert (2, 0) not in cells
    hit, = rev.find_literal('14').hits
    assert archive.read_hit(ref, hit, allowed={ref}).locator == OfficeLocator(
        format='docx', table=1, row=3, column=2,
    )
    assert [x.locator for x in rev.find_literal('Batch A').hits] == [
        OfficeLocator(format='docx', table=1, row=2, column=1),
    ] * 2

    from library import config
    from library.pipelines import _text_indexer
    from library.tasks import ingest_guard

    async def check():
        return None

    async def index(body, ctx, **kw):
        assert '| Batch A | 14 | mg/L |' in body
        assert '| Batch | Grouped header |  |' in body
        assert '<!-- table-1 -->' not in body
        assert kw['coverage']['revision_id'] == str(ref.revision_id)
        return 'indexed'

    monkeypatch.setattr(config, 'get_settings', lambda: SimpleNamespace(library_home=str(tmp_path)))
    monkeypatch.setattr(ingest_guard, 'check_ingest_delivery', check)
    monkeypatch.setattr(_text_indexer, 'index_extracted_text', index)
    assert await office.index_office_revision(ref.document, ref, 'docx') == 'indexed'


async def test_repeated_or_conflicting_rows_are_not_given_exact_locators(monkeypatch):
    doc = Document()
    _table(doc)
    t = doc.add_table(rows=1, cols=3)
    for cell, text in zip(t.rows[0].cells, ('Batch A', '14', 'mg/L'), strict=True):
        cell.text = text
    parsed = await _parse(monkeypatch, doc, MARKDOWN)
    hits = [row for row, text in zip(parsed.rows, parsed.fragments, strict=True) if '14' in text]
    assert len(hits) == 1 and hits[0].locator == OfficeLocator(format='docx')
    assert 'office_address_not_verified' in parsed.coverage.warnings


async def test_invalid_merge_continuation_does_not_publish_a_grid(monkeypatch):
    doc = Document()
    table = _table(doc)
    tc_pr = table.rows[1]._tr.tc_lst[0].tcPr
    tc_pr.remove(tc_pr.find(qn('w:vMerge')))
    parsed = await _parse(monkeypatch, doc, MARKDOWN)
    assert not parsed.tables
    assert 'word_table_grid_not_verified' in parsed.coverage.warnings


async def test_missing_grid_preserves_readable_markdown_without_inventing_geometry(monkeypatch):
    doc = Document()
    table = _table(doc)
    table._tbl.remove(table._tbl.find(qn('w:tblGrid')))
    parsed = await _parse(monkeypatch, doc, MARKDOWN)
    assert not parsed.tables
    assert ''.join(parsed.fragments) == MARKDOWN
    assert 'word_table_grid_not_verified' in parsed.coverage.warnings


async def test_grid_reference_does_not_make_empty_docx_readable(monkeypatch):
    doc = Document()
    doc.add_table(rows=1, cols=2)
    with pytest.raises(ValueError, match='no readable Office text'):
        await _parse(monkeypatch, doc, '|  |  |\n| --- | --- |\n')
