"""DOCX image references retain their original Markdown positions and source provenance."""

import base64
import io
from uuid import uuid4

import pytest
from docx import Document
from PIL import Image

from library.document_revisions import weknora_office as office
from library.document_revisions.archive import RevisionArchive, image_markdown, sha256
from library.document_revisions.models import DocumentRef, OfficeLocator
from library.document_revisions.paddle_ocr import OcrPage


def _png(color):
    buffer = io.BytesIO()
    Image.new('RGB', (24, 24), color).save(buffer, format='PNG')
    return buffer.getvalue()


def _save(doc):
    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue()


async def _parse(doc, markdown, images, monkeypatch, *, paddle=False):
    ref = DocumentRef(file_id='file', entry_id=str(uuid4()), sha256=sha256(doc))
    monkeypatch.setattr(office, 'run_worker', lambda *_: {
        'fmt': 'docx', 'parser': 'Docx2Parser', 'commit': office.WEKNORA_COMMIT,
        'sha256': ref.sha256, 'content': markdown, 'versions': {},
        'images': {name: base64.b64encode(blob).decode() for name, blob in images.items()},
    })
    return await office.parse_weknora_office(doc, document=ref, filename='fixture.docx', fmt='docx',
                                            python='', checkout='', before_send=None, paddle=paddle)


async def test_skip_header_footer_before_conversion_and_ocr(tmp_path, monkeypatch):
    from zipfile import ZipFile

    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.opc.packuri import PackURI
    from docx.opc.part import Part
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    from library.document_revisions.office import index_office_revision
    from library.pipelines import _text_indexer

    shared = _png('green')
    header_only = _png('red')
    footer_only = _png('blue')
    doc = Document()
    doc.add_paragraph('Body figure ').add_run().add_picture(io.BytesIO(shared))
    # A deleted body image can leave an unused relationship in document.xml.rels.
    stale = doc.add_paragraph()
    stale.add_run().add_picture(io.BytesIO(header_only))
    stale._p.getparent().remove(stale._p)
    section = doc.sections[0]
    section.header.paragraphs[0].text = 'Header version H-071'
    for image in (shared, header_only):
        section.header.paragraphs[0].add_run().add_picture(io.BytesIO(image))
    section.first_page_footer.paragraphs[0].text = 'Footer version F-031'
    section.first_page_footer.paragraphs[0].add_run().add_picture(io.BytesIO(footer_only))
    note_ref = OxmlElement('w:footnoteReference')
    note_ref.set(qn('w:id'), '7')
    doc.paragraphs[0].add_run()._r.append(note_ref)
    note = Part(PackURI('/word/footnotes.xml'),
                'application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml',
                b'<w:footnotes xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                b'<w:footnote w:id="7"><w:p><w:r><w:t>Footnote retained</w:t></w:r></w:p></w:footnote>'
                b'</w:footnotes>', doc.part.package)
    doc.part.relate_to(note, RT.FOOTNOTES)
    original = _save(doc)
    ref = DocumentRef(file_id='file', entry_id=str(uuid4()), sha256=sha256(original))

    def worker(data, fmt, *_):
        with ZipFile(io.BytesIO(data)) as archive:
            assert not any(n.startswith(('word/header', 'word/footer')) for n in archive.namelist())
            assert archive.read('word/footnotes.xml') == note.blob
            media = [archive.read(n) for n in archive.namelist() if n.startswith('word/media/')]
            assert media == [shared]
        copy = Document(io.BytesIO(data))
        assert all(n.tag not in {qn('w:headerReference'), qn('w:footerReference')}
                   for s in copy.element.body.iter(qn('w:sectPr')) for n in s)
        assert [p.text for p in copy.paragraphs] == ['Body figure ']
        return dict(fmt=fmt, parser='Docx2Parser', commit=office.WEKNORA_COMMIT, sha256=sha256(data), versions={},
                    content='Body figure ![](images/a.png)',
                    images={'images/a.png': base64.b64encode(shared).decode()})

    async def recognize(assets, *, before_send):
        assert assets == {sha256(shared): shared}
        return {sha256(shared): OcrPage(text='Body figure OCR')}

    monkeypatch.setattr(office, 'run_worker', worker)
    monkeypatch.setattr(office, 'recognize_images', recognize)
    parsed = await office.parse_weknora_office(original, document=ref, filename='fixture.docx', fmt='docx',
                                               python='', checkout='', before_send=None)
    archive = RevisionArchive(tmp_path)
    revision_ref = parsed.publish(archive)
    revision = archive.read(revision_ref, allowed={revision_ref})
    assert revision.manifest.source_version.sha256 == ref.sha256
    assert revision.manifest.source_version.size_bytes == len(original)
    assert revision.manifest.coverage.image_count == 1
    assert {r.locator.word_part for r in revision.source_map if r.locator.word_part} == {'word/footnotes.xml'}
    assert 'H-071' not in revision.content and 'F-031' not in revision.content
    hit, = revision.find_literal('Body figure OCR').hits
    assert hit.figure.locator.paragraph == 1
    note_hit, = revision.find_literal('Footnote retained').hits
    assert note_hit.locator.footnote_id == 7
    assert Document(io.BytesIO(original)).sections[0].header.paragraphs[0].text == 'Header version H-071'

    async def index(body, *args, **kwargs):
        assert 'Footnote retained' in body
        assert 'H-071' not in body and 'F-031' not in body
        return 'indexed'

    monkeypatch.setenv('LIBRARY_HOME', str(tmp_path))
    monkeypatch.setattr(_text_indexer, 'index_extracted_text', index)
    from library.config import get_settings
    get_settings.cache_clear()
    try:
        assert await index_office_revision(ref, revision_ref, 'docx') == 'indexed'
    finally:
        get_settings.cache_clear()


def test_repacked_word_has_a_separate_worker_budget(monkeypatch):
    from types import SimpleNamespace

    from library.document_revisions import _weknora_office_worker as worker
    from library.document_revisions._anydoc_worker import check_package

    raw = _save(Document())
    fake_bytes = io.BytesIO(raw)
    stdout = io.BytesIO()

    class Parser:
        def __init__(self, **kwargs):
            pass

        def parse(self, data):
            assert data == raw
            return SimpleNamespace(content='body', source_blocks=[], images={})

    def load(*args):
        monkeypatch.setitem(worker.sys.modules, 'docreader.parser.docx2_parser', SimpleNamespace(Docx2Parser=Parser))

    monkeypatch.setattr(worker, 'MAX_INPUT', len(raw) - 1)
    monkeypatch.setattr(worker, '_load_modules', load)
    monkeypatch.setattr(worker, 'version', lambda name: 'test')
    monkeypatch.setattr(worker, 'sys', SimpleNamespace(
        stdin=SimpleNamespace(buffer=fake_bytes), stdout=SimpleNamespace(buffer=stdout),
        addaudithook=lambda fn: None, modules={},
    ))
    with pytest.raises(ValueError, match='invalid Office input'):
        check_package(raw, 'docx', max_input=worker.MAX_INPUT)
    worker.main(None, 'docx')
    assert b'"content": "body"' in stdout.getvalue()


async def test_inline_images_keep_markdown_order_instead_of_media_order(tmp_path, monkeypatch):
    img_a, img_b = _png('red'), _png('blue')
    doc = Document()
    # The order in which relationships/media are created differs from display order.
    b = doc.add_paragraph('Second figure ')
    b.add_run().add_picture(io.BytesIO(img_b))
    a = b.insert_paragraph_before('First figure ')
    a.add_run().add_picture(io.BytesIO(img_a))
    doc.add_paragraph('Following text')
    markdown = 'First figure ![](images/a.png)\n\nSecond figure ![](images/b.png)\n\nFollowing text'
    parsed = await _parse(_save(doc), markdown, {'images/a.png': img_a, 'images/b.png': img_b}, monkeypatch)
    archive = RevisionArchive(tmp_path)
    ref = parsed.publish(archive)
    revision = archive.read(ref, allowed={ref})
    assert revision.content == (f'First figure {image_markdown(sha256(img_a))}\n\n'
                                f'Second figure {image_markdown(sha256(img_b))}\n\nFollowing text')
    assert [row.locator for row in revision.source_map if row.kind == 'image'] == [
        OfficeLocator(format='docx', paragraph=1), OfficeLocator(format='docx', paragraph=2),
    ]


async def test_word_image_in_table_keeps_structure_and_adjacent_cell_mappings(tmp_path, monkeypatch):
    image = _png('green')
    doc = Document()
    left, right = doc.add_table(rows=1, cols=2).rows[0].cells
    left.text = 'Left cell '
    left.paragraphs[0].add_run().add_picture(io.BytesIO(image))
    right.text = 'Right cell'
    text = '| Left cell ![Figure](<images/a.png> "title") | Right cell |\n| --- | --- |\n'
    parsed = await _parse(_save(doc), text, {'images/a.png': image}, monkeypatch)
    archive = RevisionArchive(tmp_path)
    ref = parsed.publish(archive)
    revision = archive.read(ref, allowed={ref})
    assert revision.content == (f'| Left cell {image_markdown(sha256(image))} | Right cell |\n'
                                '| --- | --- |\n\n\n<!-- table-1 -->')
    figure, = [row for row in revision.source_map if row.kind == 'image']
    assert figure.locator == OfficeLocator(format='docx', table=1, row=1, column=1)
    assert revision.find_literal('Left cell').hits[0].locator == figure.locator
    assert revision.find_literal('Right cell').hits[0].locator.column == 2


async def test_repeated_inline_asset_keeps_two_occurrences_and_ocr_points_at_each(tmp_path, monkeypatch):
    image = _png('green')
    asset_id = sha256(image)
    doc = Document()
    for text in ('First figure ', 'Second figure '):
        doc.add_paragraph(text).add_run().add_picture(io.BytesIO(image))

    async def recognize(assets, *, before_send):
        assert assets == {asset_id: image}
        return {asset_id: OcrPage(text='Text from both copies')}

    monkeypatch.setattr(office, 'recognize_images', recognize)
    text = 'First figure ![](images/a.png)\n\nSecond figure ![](images/b.png)'
    parsed = await _parse(_save(doc), text, {'images/a.png': image, 'images/b.png': image},
                          monkeypatch, paddle=True)
    archive = RevisionArchive(tmp_path)
    ref = parsed.publish(archive)
    revision = archive.read(ref, allowed={ref})
    assert len(revision.manifest.assets) == 1
    figures = [row for row in revision.source_map if row.kind == 'image']
    assert [figure.locator.paragraph for figure in figures] == [1, 2]
    hits = revision.find_literal('Text from both copies').hits
    assert len(hits) == 2
    assert [hit.figure.source_map_id for hit in hits] == [figure.source_map_id for figure in figures]
    assert revision.content.index('Text from both copies') > figures[-1].content_end


async def test_output_only_duplicate_images_do_not_claim_original_position(monkeypatch):
    image = _png('green')
    doc = Document()
    doc.add_paragraph('Only original ').add_run().add_picture(io.BytesIO(image))
    text = 'Only original ![](images/a.png)\n\nCopied ![](images/a.png)'
    parsed = await _parse(_save(doc), text, {'images/a.png': image}, monkeypatch)
    figures = [row for row in parsed.rows if row.kind == 'image']
    assert len(figures) == 2
    assert all(row.locator == OfficeLocator(format='docx') for row in figures)
    assert 'image_occurrence_not_verified' in parsed.coverage.warnings


async def test_reference_missing_or_inside_code_does_not_activate(tmp_path, monkeypatch):
    image = _png('green')
    doc = Document()
    doc.add_paragraph('Origin ').add_run().add_picture(io.BytesIO(image))
    text = ('Origin\n\n`![](images/a.png)`\n\n```md\n![](images/a.png)\n```\n\n'
            '![](https://example.test/missing.png)\n\n'
            '<IMG src="https://example.test/missing.png">\n\nAfter text')
    parsed = await _parse(_save(doc), text, {'images/a.png': image}, monkeypatch)
    archive = RevisionArchive(tmp_path)
    ref = parsed.publish(archive)
    revision = archive.read(ref, allowed={ref})
    assert '`![](images/a.png)`' in revision.content
    assert '```md\n![](images/a.png)\n```' in revision.content
    assert 'https://example.test/missing.png' not in revision.content
    assert revision.content.endswith(image_markdown(sha256(image)))
    assert '<!-- 未定位到正文的图片 -->' in revision.content
    assert 'docreader_image_reference_unresolved' in revision.manifest.coverage.warnings


@pytest.mark.parametrize('text, export', [
    ('![](images/a.png)', True),
    ('| ![](images/a.png) | |\n| --- | --- |', True),
    ('', False),
])
async def test_image_only_docx_does_not_treat_markdown_or_appendix_as_native(text, export, monkeypatch):
    image = _png('green')
    doc = Document()
    doc.add_picture(io.BytesIO(image))
    exports = {'images/a.png': image} if export else {}
    with pytest.raises(ValueError, match='no readable Office text'):
        await _parse(_save(doc), text, exports, monkeypatch)
