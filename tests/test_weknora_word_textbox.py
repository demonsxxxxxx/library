"""Word textbox evidence must come from the same document XML, not Markdown order."""

import io
from uuid import uuid4

import pytest
from docx import Document
from docx.oxml import parse_xml
from pydantic import ValidationError

from library.document_revisions import weknora_office as office
from library.document_revisions.archive import RevisionArchive, sha256
from library.document_revisions.models import DocumentRef, OfficeLocator, SourceMapRow


def _box(*texts):
    body = ''.join(f'<w:p><w:r><w:t>{text}</w:t></w:r></w:p>' for text in texts)
    return parse_xml(f'<w:r xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
                     f'xmlns:v="urn:schemas-microsoft-com:vml"><w:pict><v:shape><v:textbox>'
                     f'<w:txbxContent>{body}</w:txbxContent></v:textbox></v:shape></w:pict></w:r>')


def _sample(*, duplicate=False):
    doc = Document()
    doc.add_paragraph('Regular paragraph')
    anchor = doc.add_paragraph()
    anchor._p.append(_box('First textbox line', 'Second textbox line'))
    anchor._p.append(_box('Another textbox'))
    if duplicate:
        doc.add_table(rows=1, cols=1).cell(0, 0).text = 'Another textbox'
        doc.add_paragraph()._p.append(_box('Second textbox line'))
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


async def _parse(doc, markdown, monkeypatch):
    ref = DocumentRef(file_id='file', entry_id=str(uuid4()), sha256=sha256(doc))
    monkeypatch.setattr(office, 'run_worker', lambda *_: {
        'fmt': 'docx', 'parser': 'Docx2Parser', 'commit': office.WEKNORA_COMMIT,
        'sha256': ref.sha256, 'content': markdown, 'images': {}, 'versions': {},
    })
    return await office.parse_weknora_office(doc, document=ref, filename='fixture.docx', fmt='docx',
                                            python='', checkout='', before_send=None, paddle=False)


@pytest.mark.asyncio
async def test_textbox_address_uses_original_xml_order_and_survives_archive(tmp_path, monkeypatch):
    parsed = await _parse(_sample(), 'Another textbox\n\nSecond textbox line\n\nRegular paragraph\n\nFirst textbox line\n', monkeypatch)
    archive = RevisionArchive(tmp_path)
    ref = parsed.publish(archive)
    revision = archive.read(ref, allowed={ref})
    for text, address in {'Another textbox': (2, 1), 'Second textbox line': (1, 2), 'First textbox line': (1, 1)}.items():
        hit, = revision.find_literal(text).hits
        assert (hit.locator.textbox, hit.locator.textbox_paragraph) == address
        assert hit.locator.paragraph is None
    assert revision.find_literal('Regular paragraph').hits[0].locator.paragraph == 1


@pytest.mark.asyncio
async def test_textbox_in_markdown_cell_keeps_its_source_range(tmp_path, monkeypatch):
    parsed = await _parse(_sample(), '| Second textbox line | not verified |\n', monkeypatch)
    archive = RevisionArchive(tmp_path)
    ref = parsed.publish(archive)
    rev = archive.read(ref, allowed={ref})
    hit, = rev.find_literal('Second textbox line').hits
    assert (hit.locator.textbox, hit.locator.textbox_paragraph) == (1, 2)
    assert rev.content[slice(*hit.content_range)] == 'Second textbox line'

@pytest.mark.asyncio
async def test_textbox_duplicate_in_source_or_markdown_does_not_guess(monkeypatch):
    parsed = await _parse(_sample(duplicate=True),
                          'Another textbox\nSecond textbox line\nFirst textbox line\nFirst textbox line\n', monkeypatch)
    for span, row in zip(parsed.fragments, parsed.rows, strict=True):
        if row.kind == 'native' and span.strip():
            assert row.locator == OfficeLocator(format='docx')


def test_textbox_locator_only_for_docx_and_omitted_from_legacy_rows():
    for fields in ({'format': 'pptx', 'textbox': 1, 'textbox_paragraph': 1},
                   {'format': 'docx', 'textbox': 1},
                   {'format': 'docx', 'textbox': 1, 'textbox_paragraph': 1, 'paragraph': 2}):
        with pytest.raises(ValidationError):
            OfficeLocator(**fields)
    row = SourceMapRow(source_map_id='map-1', revision_id=uuid4(), kind='native', content_start=0,
                       content_end=1, locator=OfficeLocator(format='docx', paragraph=1))
    assert 'textbox' not in row.archive_json()
