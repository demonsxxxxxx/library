"""Private PDF-only child process. Load only the pinned upstream builtin modules."""

import hashlib
import json
import sys
from importlib.metadata import version
from pathlib import Path
from types import ModuleType

WEKNORA_COMMIT = "7d064bc3a259f87634840199f565ec4dd644ba1e"
MAX_PDF_BYTES = 16 * 1024 * 1024
MAX_PDF_PAGES = 200
# Git blob IDs at WEKNORA_COMMIT. This workspace can be a source archive, not a clone.
# In order of imports. Do not execute the upstream eager parser package initializer.
_BLOBS = {
    "docreader.config": "2fb6faa75385112d19d52aaa35a14e0682d66b7a",
    "docreader.models.document": "a165e1c77373cce253c5e8cb2bd95fb2eb17ff00",
    "docreader.parser.base_parser": "b868c1e747ca03034db6dc1bfa616c61ff649b63",
    "docreader.parser.concurrency": "fc66ca67a2fd883a601232aab5c252ed28f2d3a9",
    "docreader.parser.source_locator": "d5a1b7ad4cafd2dbf37a371f071f82dc2c5b34db",
    "docreader.parser.stream": "7524d6bd3469e3ef74ef420ed7e8d8c61c52514e",
    "docreader.parser.pdf_parser": "f5be3e5de891aca191e348131d6e4589636dab91",
}


def _load_modules(checkout: Path, blobs):
    for name in ("docreader", "docreader.models", "docreader.parser", "docreader.utils"):
        package = ModuleType(name)
        package.__path__ = []  # Only the verified child modules are importable.
        sys.modules[name] = package
    for name, expected in blobs.items():
        path = checkout.joinpath(*name.split(".")).with_suffix(".py")
        data = path.read_bytes().replace(b"\r\n", b"\n")
        header = f"blob {len(data)}\0".encode("ascii")
        if hashlib.sha1(header + data, usedforsecurity=False).hexdigest() != expected:
            raise ValueError(f"docreader source does not match pinned commit: {name}")
        module = ModuleType(name)
        module.__file__ = str(path)
        module.__package__ = name.rpartition(".")[0]
        sys.modules[name] = module
        exec(compile(data, str(path), "exec"), module.__dict__)


def _load_builtin(checkout: Path):
    _load_modules(checkout, _BLOBS)
    return sys.modules["docreader.parser.pdf_parser"].PDFParser


def _run(checkout: Path) -> None:
    import pypdfium2 as pdfium

    original = sys.stdin.buffer.read(MAX_PDF_BYTES + 1)
    if not original or len(original) > MAX_PDF_BYTES:
        raise ValueError("PDF exceeds local revision slice size limit")
    # Treat any nonblank native layer as reason not to send the whole page. W's
    # image-area heuristic may classify an image-heavy page with text as a scan.
    with pdfium.PdfDocument(original) as pdf:
        if not 1 <= len(pdf) <= MAX_PDF_PAGES:
            raise ValueError("PDF exceeds local revision slice page limit")
        has_native_text = []
        for page in pdf:
            try:
                text_page = page.get_textpage()
                try:
                    has_native_text.append(bool(text_page.get_text_bounded().strip()))
                finally:
                    text_page.close()
            finally:
                page.close()
    parser = _load_builtin(checkout)(file_name="source.pdf", pdf_force_scanned=False)
    document = parser.parse(original)
    sys.stdout.buffer.write(json.dumps({
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "parser_commit": WEKNORA_COMMIT,
        "runtime_versions": {name: version(name) for name in ("pypdfium2", "Pillow", "pydantic")},
        "has_native_text": has_native_text,
        "document": document.model_dump(exclude={"chunks"}),
    }).encode("ascii"))


if __name__ == "__main__":
    _run(Path(sys.argv[1]))
