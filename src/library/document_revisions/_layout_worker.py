"""Local Docling worker. Input and output are data, never user-supplied paths."""
import hashlib
import io
import json
import sys
from importlib.metadata import version

MAX_LAYOUT_PAGES = 32


def main():
    from docling.datamodel.accelerator_options import AcceleratorDevice, AcceleratorOptions
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    versions = {key: version(key) for key in ("docling-slim", "docling-core", "docling-parse")}
    if list(versions.values()) != ["2.129.0", "2.107.1", "7.27.0"]:
        raise RuntimeError("unsupported Docling version")
    pdf = sys.stdin.buffer.read()
    options = PdfPipelineOptions(do_ocr=False, do_table_structure=True, enable_remote_services=False,
        accelerator_options=AcceleratorOptions(device=AcceleratorDevice.CPU, num_threads=4),
        document_timeout=100)
    converter = DocumentConverter(allowed_formats=[InputFormat.PDF],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    result = converter.convert(DocumentStream(name="source.pdf", stream=io.BytesIO(pdf)),
                               max_num_pages=MAX_LAYOUT_PAGES, max_file_size=16 * 1024 * 1024)
    doc = result.document
    pages = {n: [] for n in doc.pages}
    for item, _ in doc.iterate_items(with_groups=False, traverse_pictures=True):
        for prov in getattr(item, "prov", []):
            page = prov.page_no
            size = doc.pages[page].size
            box = prov.bbox.to_top_left_origin(page_height=size.height)
            coords = [box.l / size.width, box.t / size.height, box.r / size.width, box.b / size.height]
            if not all(-0.001 <= n <= 1.001 for n in coords):
                coords = None
            else:
                coords = [max(0., min(1., n)) for n in coords]
                if coords[0] >= coords[2] or coords[1] >= coords[3]:
                    coords = None
            region = dict(kind=item.label.value, bbox=coords, text=getattr(item, "text", ""),
                          caption=item.caption_text(doc) if hasattr(item, "caption_text") else "")
            if item.label.value == "table" and len(item.prov) == 1:
                data = item.data
                region["grid"] = dict(rows=data.num_rows, cols=data.num_cols, cells=[dict(
                    row=c.start_row_offset_idx, col=c.start_col_offset_idx,
                    rowspan=c.row_span, colspan=c.col_span, header=c.column_header, text=c.text,
                ) for c in data.table_cells])
            pages[page].append(region)
    sys.stdout.buffer.write(json.dumps(dict(source_sha256=hashlib.sha256(pdf).hexdigest(),
        versions=versions, complete=result.status.value == "success", pages=[
            dict(page=n, regions=pages[n]) for n in sorted(pages)
        ])).encode("ascii"))


if __name__ == "__main__":
    main()
