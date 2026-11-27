"""Offline worker for the actual WeKnora builtin OOXML dispatch."""
import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path

if __name__ == "__main__":
    # -I excludes the script directory; add only our adjacent, trusted modules.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _anydoc_worker import MAX_EXPANDED, MAX_INPUT, MAX_OUTPUT, check_package
    from _weknora_worker import WEKNORA_COMMIT, _BLOBS, _load_modules
else:
    from ._anydoc_worker import MAX_EXPANDED, MAX_INPUT, MAX_OUTPUT, check_package
    from ._weknora_worker import WEKNORA_COMMIT, _BLOBS, _load_modules

_COMMON = {k: _BLOBS[k] for k in ("docreader.config", "docreader.models.document",
                                  "docreader.parser.base_parser", "docreader.parser.concurrency")}
_EXCEL = {
    "docreader.parser.excel_convert": "add5f42600b819c38bd45f7fdd179aa745896647",
    "docreader.parser.xlsx_merge": "25b9b724fd78a9ff32f5e9e79c904eac19c46edf",
    "docreader.parser.xlsx_repair": "039a5b38d6fe229b5a8c2dd06ca5f2d7cd5651d8",
}
_MARKDOWN = {
    "docreader.utils.endecode": "6d18da40d5ea8931fc3157a194261922e6d8f712",
    "docreader.parser.chain_parser": "499c3627e785145f27b8db903d3ef004c5915769",
    "docreader.parser.docx_merge": "ec00062ec73f00cf44a6a40cf845d4f357b31cb5",
    "docreader.parser.markdown_parser": "2df958c8ba6b20394fef8e2feedfae54c6a81c03",
    "docreader.parser.ppt_convert": "22f7516c3c086afabd7e9a8e931d4e17497f249a",
    "docreader.parser.pptx_media": "aac79d9b8e84273456057057361c0bbb8feb3341",
    "docreader.parser.markitdown_parser": "df1694d3f619a34d8c1d1ecee7fcc56552a46aaf",
}
_WORD = {
    "docreader.parser.docx_parser": "463fc4fc538aa671120920b6d969f52c8591533e",
    "docreader.parser.docx2_parser": "872b3ef108227b849a7b25c16630514577c194bd",
}
# Only the private worker receives a repacked DOCX. Allow for the bounded
# uncompressed payload plus ZIP overhead; ingest still admits at most 16 MiB.
MAX_WORD_COPY = MAX_EXPANDED + 8 * 1024**2


def _offline(event, _args):
    if event.startswith("socket.") or event in {"subprocess.Popen", "os.system"}:
        raise PermissionError("Office docreader worker is offline")


def main(checkout, fmt):
    limit = MAX_WORD_COPY if fmt == "docx" else MAX_INPUT
    data = sys.stdin.buffer.read(limit + 1)
    check_package(data, fmt, max_input=limit)
    sys.addaudithook(_offline)
    modules = _COMMON | _EXCEL
    if fmt == "xlsx":
        name, cls = "excel_parser", "ExcelParser"
        modules |= {"docreader.parser.excel_parser": "4fe43e47050fcfab32b1971f1dd1dead794342bc"}
        deps = ("pandas", "openpyxl")
        options = {"xlsx_percent_as_text": True, "xlsx_first_row_as_header": False}
    else:
        modules |= _MARKDOWN
        name, cls = "markitdown_parser", "MarkitdownParser"
        deps = ("markitdown", "python-pptx" if fmt == "pptx" else "python-docx")
        options = {}
        if fmt == "docx":
            modules |= _WORD
            name, cls = "docx2_parser", "Docx2Parser"
    _load_modules(checkout, modules)
    parser = getattr(sys.modules[f"docreader.parser.{name}"], cls)
    out = parser(file_name=f"source.{fmt}", **options).parse(data)
    raw = json.dumps(dict(fmt=fmt, parser=cls, commit=WEKNORA_COMMIT,
                          sha256=hashlib.sha256(data).hexdigest(),
                          versions={n: version(n) for n in deps},
                          content=out.content, source_blocks=out.source_blocks, images=out.images),
                     ensure_ascii=False).encode("utf-8")
    if len(raw) > MAX_OUTPUT:
        raise ValueError("docreader Office result exceeds limit")
    sys.stdout.buffer.write(raw)


if __name__ == "__main__":
    try:
        main(Path(sys.argv[1]), sys.argv[2])
    except Exception as exc:
        # Do not emit contents or filenames from ingestion input.
        raise SystemExit(type(exc).__name__) from None
