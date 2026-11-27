"""Local Docling Office adapter. Keep verified addresses distinct from coarse locations."""
import io
import json
import os
import subprocess
from pathlib import Path
from zipfile import ZipFile

from openpyxl.utils import get_column_letter

from .archive import sha256
from .models import DocumentCoverage, OfficeLocator, ParserStamp, TableData
from .native import ParsedDocument
from .office_source import SourceIndex
from .table_data import Cell, TableGrid


def run_worker(data, fmt, python):
    if not python:
        raise ValueError("set OFFICE_DOCLING_PYTHON to the local pinned interpreter")
    env = {k:v for k,v in os.environ.items() if k.upper() in {
        "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "TMPDIR", "USERPROFILE", "LOCALAPPDATA"}}
    env.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", OMP_NUM_THREADS="1")
    proc = subprocess.run([str(Path(python).resolve()), "-I", "-B", str(Path(__file__).with_name("_office_worker.py")), fmt],
                          input=data, env=env, capture_output=True, timeout=120)
    if proc.returncode:
        raise ValueError("offline Office parser failed; verify dependencies and source")
    return json.loads(proc.stdout)


def parse_office(data, *, document, filename, format, python, worker=run_worker):
    if format not in {"docx","xlsx","pptx"} or not 0 < len(data) <= 16*1024**2 or sha256(data) != document.sha256:
        raise ValueError("unsupported or mismatched Office original")
    with ZipFile(io.BytesIO(data)) as z:
        if sum(i.file_size for i in z.infolist()) > 128*1024**2:
            raise ValueError("expanded Office input limit exceeded")
    out = worker(data, format, python)
    if (out["sha256"] != document.sha256 or out["fmt"] != format or out["versions"] != ["2.129.0","2.107.1"]
            or out["status"] not in {"success","partial_success"}):
        raise ValueError("invalid Docling Office export identity")
    source = SourceIndex(format,data)
    result = ParsedDocument(document,filename,len(data),format)
    result.parser_stamp = ParserStamp(name="docling-office", route="text",version="2.129.0/core-2.107.1",
                                      config_digest=sha256(b"office-simple-v1/offline/no-ocr"))
    warnings = {"document_completeness_not_certified"}
    if out["status"] != "success":
        warnings.add("component_partial")
    for item in out["items"]:
        slide = item["page"] if format=="pptx" and item["page"] in source.slides else None
        sheet = item["sheet"] if format=="xlsx" and item["sheet"] in source.sheets else None
        coarse = OfficeLocator(format=format, slide=slide, sheet=sheet)
        if "text" in item:
            text = item["text"]
            loc = source.text_locator(text,slide,coarse)
            if loc == coarse:
                warnings.add("text_address_not_verified")
            prefix = {"title":"# ","section_header":"## ","list_item":"- "}.get(item["label"], "")
            result.add(prefix+text,loc)
            continue
        g = item["grid"]
        cells = [Cell(row=v["start_row_offset_idx"],col=v["start_col_offset_idx"],rowspan=v["row_span"],
                      colspan=v["col_span"],text=v["text"],header=v["column_header"] or v["row_header"])
                 for v in g["table_cells"]]
        loc = source.table_locator(cells,slide,coarse) if format!="xlsx" else coarse
        try:
            grid = TableGrid(rows=g["num_rows"],cols=g["num_cols"],cells=cells)
        except ValueError:
            warnings.add("grid_not_verified")
        else:
            tid = f"table-{len(result.tables)+1}"
            result.tables.append(TableData(table_id=tid,locator=loc,source="native",grid=grid))
            result.add(f"### {sheet+' / ' if sheet else ''}{tid}",loc,"table",table=tid)
        for cell in cells:
            if not cell.text.strip():
                continue
            position = loc
            r,c = cell.row+1,cell.col+1
            box = item["box"]
            if sheet and box and box["coord_origin"]=="TOPLEFT" and all(
                    float(box[k]).is_integer() and box[k]>=0 for k in ("l","t")):
                col = int(box["l"])+c
                if 1 <= col <= 16384:
                    addr = f"{get_column_letter(col)}{int(box['t'])+r}"
                    if source.cells.get((sheet,addr)) == cell.text.strip():
                        position = loc.model_copy(update={"cell":addr})
            elif loc.table or loc.shape:
                position = loc.model_copy(update={"row":r,"column":c})
            if position == loc:
                warnings.add("cell_address_not_verified")
            address = position.cell or f"R{r}C{c}"
            result.add(f"{address}: {cell.text}",position)
    located = set()
    for name, loc in source.images:
        if name in source.media and result.image(source.media[name],loc):
            located.add(name)
    for name,blob in source.media.items():
        if name not in located:
            if not result.image(blob,source.loc()):
                warnings.add("unsupported_media_kept_in_original")
            else:
                warnings.add("image_occurrence_not_verified")
    if source.media:
        warnings.add("images_not_transcribed")
    has_data = int(bool(result.rows))
    result.coverage = DocumentCoverage(processed=has_data,partial=(1,) if has_data else (),
        failed=() if has_data else (1,),image_count=len(result.assets),warnings=tuple(sorted(warnings)))
    return result


async def index_office_revision(ctx, ref, fmt):
    import asyncio
    from library.config import get_settings
    from library.pipelines._text_indexer import index_extracted_text
    from library.tasks.ingest_guard import check_ingest_delivery
    from .archive import RevisionArchive

    if ref.document.file_id != ctx.file_id or ref.document.sha256 != ctx.sha256:
        raise ValueError("Office ingest source changed")
    archive = RevisionArchive(Path(get_settings().library_home))
    revision = await asyncio.to_thread(archive.read, ref, allowed={ref})
    parser = revision.manifest.parser.name
    if parser not in {"anydoc", "docling-office", "weknora-office-builtin"}:
        raise ValueError("expected an Office revision")
    text_rows = [r for r in revision.source_map if r.kind in {"native", "ocr"}
                 and revision.content[r.content_start:r.content_end].strip()]
    if not text_rows and (parser != "anydoc" or not revision.manifest.assets):
        raise ValueError("no readable Office text to index")
    # Bound downstream indexing, not the persisted Markdown. Do not treat media links as OCR.
    if parser in {"anydoc", "weknora-office-builtin"}:
        # The builtin Word table rows here are archive references, not prose.
        excluded = {"image", "table"} if parser == "weknora-office-builtin" and fmt == "docx" else {"image"}
        body = "".join(revision.content[r.content_start:r.content_end] for r in revision.source_map
                       if r.kind not in excluded)
    else:
        body = "\n\n".join(revision.content[r.content_start:r.content_end] for r in text_rows)
    cap = 80000
    warnings = list(revision.manifest.coverage.warnings)
    if len(body)>cap:
        warnings.append("index_character_limit")
    coverage = dict(revision_id=str(ref.revision_id), parser=parser, indexed_partial=True,
                    indexed_chars=min(len(body),cap), total_chars=len(body), partial_reasons=warnings)
    if revision.manifest.parser.ocr_backend:
        coverage["ocr_backend"] = revision.manifest.parser.ocr_backend
    await check_ingest_delivery()
    return await index_extracted_text(body[:cap], ctx, kind="table" if fmt=="xlsx" else "text",
                                      pipeline=fmt, coverage=coverage)
