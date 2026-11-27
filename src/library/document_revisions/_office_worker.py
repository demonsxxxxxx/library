"""Offline Docling OOXML worker. Stdin is an original, stdout a bounded neutral payload."""
import hashlib
import io
import json
import sys
from importlib.metadata import version
from zipfile import ZipFile


def _deny_network(event, args):
    if event in {"socket.connect", "socket.getaddrinfo", "socket.sendto"}:
        raise PermissionError("Office conversion is offline")


def main(fmt):
    sys.addaudithook(_deny_network)
    from docling.datamodel.base_models import DocumentStream, InputFormat
    from docling.document_converter import DocumentConverter

    versions = [version(k) for k in ("docling-slim", "docling-core")]
    if versions != ["2.129.0", "2.107.1"]:
        raise ValueError("unexpected Docling Office version")
    itype = {"docx":InputFormat.DOCX,"xlsx":InputFormat.XLSX,"pptx":InputFormat.PPTX}[fmt]
    data = sys.stdin.buffer.read(16 * 1024**2 + 1)
    if not 0 < len(data) <= 16 * 1024**2:
        raise ValueError("Office input limit exceeded")
    with ZipFile(io.BytesIO(data)) as z:
        if sum(i.file_size for i in z.infolist()) > 128 * 1024**2:
            raise ValueError("expanded Office input limit exceeded")
    result = DocumentConverter(allowed_formats=[itype]).convert(
        DocumentStream(name="original."+fmt,stream=io.BytesIO(data)), raises_on_error=False)
    if result.status.value not in {"success", "partial_success"}:
        raise ValueError("Office conversion failed")
    doc = result.document
    sheets = {g.self_ref:g.name for g in doc.groups if g.label.value == "sheet"}
    items = []
    for item, _ in doc.iterate_items():
        prov = item.prov[0] if len(item.prov)==1 else None
        info = {"label":item.label.value,"page":prov.page_no if prov else None,
                "sheet":sheets.get(item.parent.cref) if item.parent else None}
        if item.label.value == "table":
            grid = item.data
            if grid.num_rows * grid.num_cols > 100_000:
                raise ValueError("Office table exceeds grid limit")
            info.update(grid=grid.model_dump(mode="json",include={"num_rows","num_cols","table_cells"}),
                        box=prov.bbox.model_dump(mode="json") if prov else None)
        elif getattr(item,"text",None):
            info["text"] = item.text
        else:
            continue
        items.append(info)
    payload = json.dumps(dict(sha256=hashlib.sha256(data).hexdigest(),fmt=fmt,versions=versions,
        status=result.status.value,items=items),ensure_ascii=False).encode("utf8")
    if len(payload)>32*1024**2:
        raise ValueError("Office projection limit exceeded")
    sys.stdout.buffer.write(payload)


if __name__ == "__main__":
    main(sys.argv[1])
