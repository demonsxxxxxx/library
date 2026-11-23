"""Internal signal that closes evidence gathering before final composition."""
from __future__ import annotations

from typing import Any, Mapping

from sqlalchemy.ext.asyncio import AsyncSession

from library.agent.numeric_evidence import MAX_NUMERIC_CLAIMS, check_numeric_claims
from library.agent.tools import ToolContext, ToolPolicy, tool


DESCRIPTION = (
    "Finish the evidence-gathering phase for the current request. Call this only "
    "after enough verified evidence has been collected, or targeted searches show "
    "that the requested evidence is unavailable. When evidence is sufficient and "
    "read_files returned source text, declare the exact citations that support the "
    "answer. The runtime validates them, assigns footnote markers, and persists a "
    "citation manifest. Submit original-page numeric claims separately as numeric_claims; "
    "the runtime renders those values, including unverified fields, after the answer. "
    "After this tool succeeds, write the final answer using the assigned prose citation markers."
)

SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "numeric_claims": {
            "type": "array",
            "maxItems": MAX_NUMERIC_CLAIMS,
            "description": (
                "One critical expression per item. Use its current-turn pixel evidence ID, "
                "entry, physical page, and exact transcription. Omit evidence_id for an "
                "unverified field instead of guessing. List items in answer order."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "evidence_id": {"type": "string", "maxLength": 32},
                    "entry_id": {"type": "string", "maxLength": 36},
                    "page": {"type": "integer", "minimum": 1},
                    "expression": {"type": "string", "maxLength": 300},
                },
            },
        },
        "evidence_status": {
            "type": "string",
            "enum": ["sufficient", "insufficient"],
            "description": (
                "Whether the collected evidence is sufficient for a supported "
                "answer. Use insufficient only after targeted searches or reads."
            ),
        },
        "reason": {
            "type": "string",
            "maxLength": 500,
            "description": "Brief internal reason for ending evidence gathering.",
        },
        "citations": {
            "type": "array",
            "maxItems": 20,
            "description": (
                "Exact source citations selected from successful read_files text. "
                "Required for prose claims when evidence_status is sufficient and "
                "source text was read. A numeric-only answer may use numeric_claims instead. "
                "Omit or pass [] for insufficient prose evidence."
            ),
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "entry_id": {
                        "type": "string",
                        "minLength": 7,
                        "maxLength": 36,
                        "pattern": "^[0-9a-fA-F][0-9a-fA-F-]{6,35}$",
                        "description": "Entry ID returned by read_files.",
                    },
                    "quote": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                        "description": (
                            "Verbatim visible source text from read_files; do not "
                            "quote omitted or metadata-only content."
                        ),
                    },
                    "page": {
                        "type": "integer",
                        "minimum": 1,
                        "description": (
                            "Verified physical PDF page or PPTX slide number. "
                            "Omit when the read result did not establish one."
                        ),
                    },
                    "reason": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 500,
                        "description": "How this source supports the answer.",
                    },
                },
                "required": ["entry_id", "quote", "reason"],
            },
        },
    },
    "required": ["evidence_status"],
}


@tool(
    name="finish_research",
    description=DESCRIPTION,
    schema=SCHEMA,
    policy=ToolPolicy(
        access="read",
        replay="safe",
        confirmation="never",
        timeout_seconds=30.0,
        concurrency="session_serial",
    ),
)
async def finish_research(
    db: AsyncSession,
    ctx: ToolContext,
    args: Mapping[str, Any],
) -> dict[str, Any]:
    claims = args.get("numeric_claims", [])
    if not isinstance(claims, list) or len(claims) > MAX_NUMERIC_CLAIMS:
        return {"ok": False, "error": "numeric_claims must be a bounded list"}
    numeric_results = await check_numeric_claims(db, claims, ctx.numeric_evidence)
    evidence_status = str(args.get("evidence_status") or "")
    if numeric_results and any(item["status"] == "unverified" for item in numeric_results):
        has_support = args.get("citations") or any(item["status"] == "verified" for item in numeric_results)
        evidence_status = "partial" if has_support else "insufficient"
    reason = str(args.get("reason") or "").strip()
    return {
        "ok": True,
        "evidence_status": evidence_status,
        **({"reason": reason} if reason else {}),
        **({"numeric_results": numeric_results} if numeric_results else {}),
        "next": "Write the final answer now without calling more tools.",
    }
