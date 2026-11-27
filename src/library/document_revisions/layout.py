"""Independent, offline layout projection. It never supplies transcription text."""
import os
import subprocess
from pathlib import Path
from pydantic import BaseModel

from .models import Digest, NormalizedBox, PageNumber
from .table_data import TableGrid

FIGURE_REGION_KINDS = {"image", "chart"}
CAPTION_REGION_KINDS = {"vision_footnote", "figure_caption", "image_caption"}


def under_figure(figure: NormalizedBox, caption: NormalizedBox) -> bool:
    left, _, right, bottom = figure
    x, top, end, _ = caption
    return 0 <= top - bottom <= 0.025 and left - 0.01 <= x < end <= right + 0.01


class Region(BaseModel):
    kind: str
    bbox: NormalizedBox | None
    text: str
    caption: str
    grid: TableGrid | None = None


class LayoutPage(BaseModel):
    page: PageNumber
    regions: list[Region]


class Layout(BaseModel):
    source_sha256: Digest
    versions: dict[str, str]
    complete: bool  # conversion finished; not a claim about reading order
    pages: list[LayoutPage]
    warning: str | None = None

    def check_source(self, digest: str, total: int):
        if self.source_sha256 != digest or [p.page for p in self.pages] != list(range(1, total + 1)):
            raise ValueError("layout source or physical pages do not match")


def local_layout(pdf: bytes, python: str, cache: str) -> Layout:
    env = {k: v for k, v in os.environ.items() if k.upper() in {
        "SYSTEMROOT", "WINDIR", "PATH", "TMP", "TEMP", "TMPDIR", "HOME", "USERPROFILE", "LOCALAPPDATA",
    }}
    env.update(HF_HOME=str(Path(cache).resolve()), HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
               HF_HUB_DISABLE_TELEMETRY="1", OMP_NUM_THREADS="4")
    result = subprocess.run([python, "-I", "-B", str(Path(__file__).with_name("_layout_worker.py"))],
                            input=pdf, capture_output=True, env=env, timeout=150, check=True)
    return Layout.model_validate_json(result.stdout)
