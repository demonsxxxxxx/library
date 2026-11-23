"""Bind structured numeric claims to PDF pixel evidence delivered this turn."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

MAX_NUMERIC_CLAIMS = 20
SHA256_RE = re.compile(r"[0-9a-f]{64}")
_RESERVED_MARKER = re.compile(r"\[\^pdf-value-\d+\]")
_RESERVED_DEFINITION = re.compile(r"(?m)^ {0,3}\[\^pdf-value-\d+\]:[^\n]*(?:\n|$)")


def is_numeric_marker(marker: str) -> bool:
    return _RESERVED_MARKER.fullmatch(f"[^{marker}]") is not None


def receive_numeric_evidence(wire: str, ledger: dict[str, dict]) -> None:
    """Register complete successful pixel reads from the actual tool envelope."""
    if '"evidence_id"' not in wire:
        return
    try:
        payload = json.loads(wire)
    except ValueError:
        return
    if not isinstance(payload, dict):
        return
    for entry in payload.get("results", []):
        if entry.get("ok") is not True:
            continue
        for read in entry.get("reads", []):
            evidence_id = read.get("evidence_id")
            extra = read.get("extras") or {}
            page = extra.get("page_start")
            sha = extra.get("file_sha256")
            if not (evidence_id and read.get("ok") is True
                    and extra.get("source") == "pdf_page_pixels"
                    and extra.get("numeric_status") == "pixel_agreement"
                    and type(page) is int and page > 0 and page == extra.get("page_end")
                    and isinstance(sha, str) and SHA256_RE.fullmatch(sha)
                    and entry.get("entry_id") and read.get("file_id")):
                continue
            text = read.get("text", "")
            heading, _, expression = text.partition("\n")
            if (heading != f"[Page {page}]" or not expression.strip()
                    or read.get("char_start", 0) != 0 or read.get("char_end", len(text)) != len(text)):
                continue
            ledger[evidence_id] = {
                "entry_id": entry["entry_id"], "file_id": read["file_id"],
                "file_sha256": sha, "page": page, "quote": " ".join(expression.split()),
                "region": extra.get("region"),
            }


async def check_numeric_claims(db, claims: list, ledger: Mapping[str, dict]) -> list[dict]:
    """Accept or reject each claim independently, against live source identity."""
    from library.repositories.entries import list_live_with_file_by_ids

    ids = {ledger[ref]["entry_id"] for c in claims if isinstance(c, Mapping)
           and isinstance(ref := c.get("evidence_id"), str) and ref in ledger}
    live = {e.id: f for e, f in await list_live_with_file_by_ids(db, sorted(ids))} if ids else {}
    results = []
    for index, claim in enumerate(claims, 1):
        reason = "missing_pixel_evidence"
        ref = claim.get("evidence_id") if isinstance(claim, Mapping) else None
        source = ledger.get(ref) if isinstance(ref, str) else None
        if source:
            expr = claim.get("expression")
            current = live.get(source["entry_id"])
            if (claim.get("entry_id") != source["entry_id"] or type(claim.get("page")) is not int
                    or claim["page"] != source["page"]):
                reason = "source_location_mismatch"
            elif not matches_file_version(source, current):
                reason = "source_version_unavailable"
            elif not isinstance(expr, str) or " ".join(expr.split()) != source["quote"]:
                reason = "expression_mismatch"
            else:
                results.append({**source, "index": index, "evidence_id": ref, "status": "verified",
                                "marker": f"pdf-value-{index}"})
                continue
        results.append({"index": index, "status": "unverified", "reason": reason})
    return results


def matches_file_version(source: Mapping[str, Any], current: Any) -> bool:
    return (current is not None and source["file_id"] == getattr(current, "id", None)
            and source["file_sha256"] == getattr(current, "sha256", None))


def attach_numeric_claims(answer: str, claims: Sequence[Mapping[str, Any]]) -> str:
    """Generate values and citations server-side; never publish a rejected value here."""
    if not claims:
        return answer
    from library.agent.citation_manifest import _render_footnote

    answer = _RESERVED_MARKER.sub("", _RESERVED_DEFINITION.sub("", answer)).rstrip()
    chinese = any("\u4e00" <= ch <= "\u9fff" for ch in answer)
    heading = "原页数值核对" if chinese else "Original-page numeric checks"
    unverified = "未核实" if chinese else "Not verified"
    lines, notes = [], []
    for item in claims:
        if item["status"] != "verified":
            lines.append(f"{item['index']}. {unverified}")
            continue
        quote = str(item["quote"])
        # Use an inline code delimiter longer than any literal backtick run.
        fence = "`" * (1 + max((len(m[0]) for m in re.finditer(r"`+", quote)), default=0))
        lines.append(f"{item['index']}. {fence} {quote} {fence}[^{item['marker']}]")
        notes.append(_render_footnote(item))
    return f"{answer}\n\n### {heading}\n\n" + "\n".join(lines) + ("\n\n" + "\n".join(notes) if notes else "")
