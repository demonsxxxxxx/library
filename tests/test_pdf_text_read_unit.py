from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

from library.pipelines.pdf import PdfPipeline
from library.pipelines.pdf_text import (
    extract_pdf_page_labels,
    extract_pdf_text_range,
    resolve_page_label,
)
from library.pipelines.text import TextPipeline


class _FakeStorage:
    def __init__(self, payload: bytes, *, chunk_size: int | None = None):
        self.payload = payload
        self.chunk_size = chunk_size or len(payload)

    async def get(self, key: str):  # noqa: ARG002
        for start in range(0, len(self.payload), self.chunk_size):
            yield self.payload[start:start + self.chunk_size]


def _build_text_pdf(page_count: int) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    for i in range(1, page_count + 1):
        page = writer.add_blank_page(width=400, height=300)
        font = DictionaryObject({
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        })
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
        })
        stream = DecodedStreamObject()
        stream.set_data(
            (
                f"BT /F1 12 Tf 1 0 0 1 40 240 Tm (Physical page {i}) Tj ET\n"
                f"BT /F1 12 Tf 1 0 0 1 40 220 Tm (Unique token p{i:03d}) Tj ET"
            ).encode("ascii")
        )
        page.replace_contents(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _with_labels(pdf_bytes: bytes) -> bytes:
    from pypdf import PdfReader, PdfWriter
    from pypdf.constants import PageLabelStyle

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.set_page_label(0, 1, style=PageLabelStyle.LOWERCASE_ROMAN, start=1)
    writer.set_page_label(2, len(reader.pages) - 1, style=PageLabelStyle.DECIMAL, start=1)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _build_stream_order_table_pdf() -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=300)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })

    ops: list[str] = []

    def text_at(x: int, y: int, text: str) -> None:
        safe = text.replace("\\", "\\\\").replace("(", r"\(").replace(")", r"\)")
        ops.append(f"BT /F1 11 Tf 1 0 0 1 {x} {y} Tm ({safe}) Tj ET")

    # Cells are deliberately written before row headers. Plain extraction
    # follows this stream order; layout extraction should restore visual rows.
    for y, label, body in [
        (230, "Message", "Communication timeout."),
        (216, "Cause", "Timeout talking to servo."),
        (202, "Action", "Power off and restart."),
        (188, "Message", "Driver not connected."),
        (174, "Cause", "Servo driver cannot be found."),
        (160, "Action", "Check cable and driver power."),
        (146, "Message", "Driver count abnormal."),
    ]:
        text_at(90, y, label)
        text_at(165, y, body)
    text_at(30, 230, "H8820")
    text_at(30, 188, "H8830")

    stream = DecodedStreamObject()
    stream.set_data("\n".join(ops).encode("ascii"))
    page.replace_contents(stream)
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def test_pdf_page_labels_map_printed_page_to_physical_page() -> None:
    pdf_bytes = _with_labels(_build_text_pdf(8))

    labels = extract_pdf_page_labels(pdf_bytes)
    assert labels[:6] == ["i", "ii", "1", "2", "3", "4"]
    assert resolve_page_label(labels, "4") == 6

    seg = PdfPipeline()._slice(pdf_bytes, {"page_label": "4"})
    assert seg.error is None
    assert "Physical page 6" in seg.text
    assert "Physical page 4" not in seg.text
    assert seg.extras["resolved_page"] == 6
    assert seg.extras["page_label"] == "4"


def test_pdf_page_window_extracts_only_requested_pages() -> None:
    pdf_bytes = _build_text_pdf(30)
    doc = extract_pdf_text_range(pdf_bytes, page_start=25, page_end=25)
    assert doc.total_pages == 30
    assert doc.page_start == 25
    assert len(doc.pages) == 1
    assert "Unique token p025" in doc.pages[0]

    seg = PdfPipeline()._slice(pdf_bytes, {"page_start": 25, "page_end": 25})
    assert seg.error is None
    assert "Unique token p025" in seg.text
    assert "Unique token p001" not in seg.text


def test_pdf_read_inlines_persisted_figure_descriptions() -> None:
    pdf_bytes = _build_text_pdf(3)
    file_row = SimpleNamespace(
        description={
            "figures": [
                {
                    "page": 2,
                    "figure": 1,
                    "label": "Figure 2.1",
                    "text": "Architecture diagram with ingest and recall stages.",
                }
            ]
        }
    )

    seg = PdfPipeline()._slice(
        pdf_bytes,
        {"page_start": 2, "page_end": 2},
        file_row=file_row,
    )

    assert seg.error is None
    assert "[Figure 2.1] Architecture diagram" in seg.text
    assert "Unique token p002" in seg.text


def test_pdf_layout_extraction_keeps_table_row_headers_with_cells() -> None:
    pdf_bytes = _build_stream_order_table_pdf()
    doc = extract_pdf_text_range(pdf_bytes, page_start=1, page_end=1)
    text = doc.pages[0]

    assert "H8830 | Message | Driver not connected." in text
    assert "Message | Driver count abnormal.\nH8830" not in text

    seg = PdfPipeline()._slice(
        pdf_bytes,
        {
            "page_start": 1,
            "page_end": 1,
            "pattern": "H8830|Driver not connected",
            "context_lines": 1,
            "max_matches": 2,
        },
    )
    assert seg.error is None
    assert "H8830 | Message | Driver not connected." in seg.text


def test_prose_read_and_search_use_the_same_bounded_text_view() -> None:
    body = _build_text_pdf(3)
    args = {'pdf_text_view': 'prose', 'page_start': 2}
    pipeline = PdfPipeline()
    read = pipeline._slice(body, args)
    assert read.error is None and read.extras['pdf_text_view'] == 'prose'
    assert 'Unique token p002' in read.text and 'p001' not in read.text
    search = pipeline._slice(body, {**args, 'pattern': 'Unique token p002'})
    assert search.error is None and search.extras['pdf_text_view'] == 'prose'
    assert 'Unique token p002' in search.text and search.extras['hits'][0]['page'] == 2
    head = pipeline._slice(body, {**args, 'max_chars': 12})
    tail = pipeline._slice(body, {**args, 'offset': head.extras['next_offset']})
    assert head.text + tail.text == read.text
    assert 'numeric_status' not in read.extras and 'evidence_id' not in read.extras
    for bad in ({}, {'page_start': 4}, {'page_start': 1, 'page_end': 2}, {'page_start': 0},
                {'page_start': 2, 'numeric_check': {}}, {'page_start': 2, 'question': 'Inspect'}):
        assert pipeline._slice(body, {'pdf_text_view': 'prose', **bad}).error


def test_document_prose_search_reports_partial_miss_and_resumes() -> None:
    body = _build_text_pdf(12)
    pipeline = PdfPipeline()
    miss = pipeline._slice(body, {"pdf_text_view": "prose", "pattern": "Unique token p012"})
    assert miss.error and "not proof of absence" in miss.error
    assert miss.extras["coverage"] == "partial"
    assert miss.extras["window_end"] == 8

    hit = pipeline._slice(body, miss.extras["next_search"])
    assert hit.error is None and hit.extras["window_start"] == 9
    assert "next_search" not in hit.extras
    [match] = hit.extras["hits"]
    assert match["page"] == 12
    page = pipeline._slice(body, {"pdf_text_view": "prose", "page_start": match["page"]})
    raw = page.text.split("\n", 1)[1]
    assert raw[match["raw_start"]:match["raw_end"]] == "Unique token p012"
    assert raw[match["excerpt_start"]:match["excerpt_end"]] in hit.text


def test_document_prose_search_preserves_original_wrap_and_paginates_matches() -> None:
    body = _build_text_pdf(3)
    pipeline = PdfPipeline()
    args = {"pdf_text_view": "prose", "pattern": r"page \d+ Unique token", "max_matches": 1}
    for expected in range(1, 4):
        result = pipeline._slice(body, args)
        assert result.error is None
        assert [hit["page"] for hit in result.extras["hits"]] == [expected]
        assert f"page {expected}\nUnique token" in result.text
        args = result.extras.get("next_search")
    assert args is None


def test_pdf_default_read_is_windowed_for_long_documents() -> None:
    pdf_bytes = _build_text_pdf(30)
    seg = PdfPipeline()._slice(pdf_bytes, {})
    assert seg.error is None
    assert "Unique token p001" in seg.text
    assert "Unique token p025" not in seg.text
    assert seg.extras["read_truncated"] is True
    assert seg.extras["next_page_start"] == 21


def test_text_default_read_cap_tracks_requested_window() -> None:
    body = ("alpha\n" * 200_000).encode("utf-8")
    file_row = SimpleNamespace(storage_key="long.txt", size_bytes=len(body))
    seg = asyncio.run(TextPipeline().read_segment(
        file_row=file_row,
        args={"max_chars": 200},
        storage=_FakeStorage(body, chunk_size=1024),
    ))
    assert seg.error is None
    assert len(seg.text) == 200
    assert seg.extras["source_truncated"] is True
    assert seg.extras["source_bytes_read"] < 20_000
