"""Built-in compression for read_files results.

The public function remains ``compress_read_text`` so the read_files tool keeps
one stable integration point. The implementation delegates compression to
built-in transforms and fails open to the original text whenever compression
cannot shrink the content or does not beat the configured savings threshold.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from library.agent.compression_adapter import maybe_compress_read_view

CODE_AUTO_MIN_CHARS = 64_000
_CODE_EXTS = {
    ".py", ".pyw", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".c", ".cc", ".cpp", ".h", ".hpp",
    ".cs", ".rb", ".php", ".swift", ".kt", ".kts", ".scala",
    ".sh", ".bash", ".zsh", ".ps1", ".bat", ".cmd", ".sql",
    ".lua", ".r", ".jl", ".ex", ".exs", ".erl", ".hrl",
}


@dataclass(slots=True)
class CompressionSettings:
    enabled: bool = True
    min_chars: int = 12_000
    target_chars: int = 8_000
    context_chars: int = 220
    max_ratio: float = 0.85


@dataclass(slots=True)
class ReadCompressionResult:
    text: str
    compressed: bool
    strategy: str | None = None
    original_chars: int = 0
    compressed_chars: int = 0
    omitted: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def metadata(self) -> dict[str, Any]:
        return {
            "compressed": self.compressed,
            "strategy": self.strategy,
            "original_chars": self.original_chars,
            "compressed_chars": self.compressed_chars,
            "tokens_saved_estimate": max(0, self.original_chars - self.compressed_chars) // 4,
            "omitted": self.omitted,
            "lossy": bool(self.extra.get("lossy", self.compressed)),
            "quote_safe": (
                "Cite only exact text still visible in `text`; reopen the original "
                "read_files args with compress=false before quoting omitted material."
            ),
            "note": self.note,
            **self.extra,
        }


def compress_read_text(
    text: str,
    *,
    entry_id: str,
    args: dict[str, Any],
    extras: dict[str, Any] | None = None,
    pipeline: str = "",
    kind: str = "",
    query: str = "",
    source_name: str = "",
    source_ext: str = "",
    settings: CompressionSettings | None = None,
) -> ReadCompressionResult:
    """Compress a read_files text result when it is worthwhile."""
    cfg = settings or CompressionSettings()
    original_len = len(text or "")
    extras = dict(extras or {})
    if not cfg.enabled or not text or original_len < cfg.min_chars:
        return ReadCompressionResult(text=text, compressed=False, original_chars=original_len)
    if args.get("compress") is False:
        return ReadCompressionResult(text=text, compressed=False, original_chars=original_len)
    if _is_precision_read(args, extras):
        return ReadCompressionResult(text=text, compressed=False, original_chars=original_len)

    explicit_compress = args.get("compress") is True
    allow_code = explicit_compress or original_len >= CODE_AUTO_MIN_CHARS
    if _is_code_read(
        args=args,
        pipeline=pipeline,
        kind=kind,
        source_name=source_name,
        source_ext=source_ext,
    ) and not allow_code:
        return ReadCompressionResult(text=text, compressed=False, original_chars=original_len)

    compressed = maybe_compress_read_view(
        text,
        pipeline=pipeline,
        kind=kind,
        context=query,
        target_ratio=_target_ratio(cfg, original_len),
        source_name=source_name,
        source_ext=source_ext,
        member_path=str(args.get("member_path") or ""),
        allow_code=allow_code,
    )
    if compressed is None or not compressed.text.strip():
        return ReadCompressionResult(text=text, compressed=False, original_chars=original_len)
    if not _beats_threshold(
        original_chars=original_len,
        compressed_chars=len(compressed.text),
        max_ratio=cfg.max_ratio,
    ):
        return ReadCompressionResult(text=text, compressed=False, original_chars=original_len)

    omitted = _omitted_entries(
        entry_id=entry_id,
        args=args,
        extras=extras,
        original_chars=original_len,
        pipeline=pipeline,
    )
    return ReadCompressionResult(
        text=compressed.text,
        compressed=True,
        strategy=compressed.strategy,
        original_chars=original_len,
        compressed_chars=len(compressed.text),
        omitted=omitted,
        note="Read result compressed; reopen omitted args for exact text.",
        extra=compressed.metadata(),
    )


def _is_precision_read(args: dict[str, Any], extras: dict[str, Any]) -> bool:
    if args.get("pdf_text_view") == "prose" or args.get("question") or extras.get("vlm_used"):
        return True
    if args.get("pattern") or args.get("patterns") or extras.get("hits"):
        return True
    if args.get("line_start") or args.get("line_end"):
        return True
    if args.get("paragraph_start") or args.get("paragraph_end"):
        return True
    return False


def _is_code_read(
    *,
    args: dict[str, Any],
    pipeline: str,
    kind: str,
    source_name: str,
    source_ext: str,
) -> bool:
    if (kind or "").lower() == "code":
        return True
    ext = _source_suffix(
        str(args.get("member_path") or source_name or ""),
        source_ext,
    )
    return (pipeline or "").lower() == "text" and ext in _CODE_EXTS


def _source_suffix(source_name: str, source_ext: str) -> str:
    for candidate in (source_name, source_ext):
        raw = (candidate or "").strip().lower()
        if not raw:
            continue
        if raw.startswith(".") and "/" not in raw and "\\" not in raw:
            return raw
        if "/" not in raw and "\\" not in raw and "." not in raw and len(raw) <= 8:
            return f".{raw}"
        name = raw.replace("\\", "/").rsplit("/", 1)[-1]
        if "." in name:
            return "." + name.rsplit(".", 1)[-1]
    return ""


def _target_ratio(cfg: CompressionSettings, original_len: int) -> float:
    if original_len <= 0:
        return 0.5
    try:
        ratio = int(cfg.target_chars) / original_len
    except (TypeError, ValueError, ZeroDivisionError):
        ratio = 0.5
    return min(0.8, max(0.1, ratio))


def _beats_threshold(*, original_chars: int, compressed_chars: int, max_ratio: float) -> bool:
    if original_chars <= 0:
        return False
    return compressed_chars < int(original_chars * max_ratio)


def _omitted_entries(
    *,
    entry_id: str,
    args: dict[str, Any],
    extras: dict[str, Any],
    original_chars: int,
    pipeline: str,
) -> list[dict[str, Any]]:
    omitted: list[dict[str, Any]] = []
    _append_omitted(
        omitted,
        kind="original_read",
        entry_id=entry_id,
        read_args=_reopen_args(args),
        original_chars=original_chars,
    )

    scope = _scope_args(args)
    if extras.get("page_start") or extras.get("page_end"):
        read_args = dict(scope)
        _copy_present(read_args, extras, ("page_start", "page_end"))
        _append_omitted(
            omitted,
            kind="page_window",
            entry_id=entry_id,
            read_args=read_args,
            original_chars=original_chars,
        )

    if extras.get("line_start") or extras.get("line_end"):
        read_args = dict(scope)
        _copy_present(read_args, extras, ("line_start", "line_end"))
        _append_omitted(
            omitted,
            kind="line_window",
            entry_id=entry_id,
            read_args=read_args,
            original_chars=original_chars,
        )

    if extras.get("paragraph_start") or extras.get("paragraph_end"):
        read_args = dict(scope)
        _copy_present(read_args, extras, ("paragraph_start", "paragraph_end"))
        _append_omitted(
            omitted,
            kind="paragraph_window",
            entry_id=entry_id,
            read_args=read_args,
            original_chars=original_chars,
        )

    section_id = args.get("section_id") or extras.get("section_id")
    if section_id:
        read_args = dict(scope)
        read_args["section_id"] = section_id
        _append_omitted(
            omitted,
            kind="section_window",
            entry_id=entry_id,
            read_args=read_args,
            original_chars=original_chars,
        )

    heading = args.get("heading") or extras.get("heading") or extras.get("scope_heading")
    if heading:
        read_args = dict(scope)
        read_args["heading"] = heading
        _append_omitted(
            omitted,
            kind="sheet_window" if (pipeline or "").lower() == "spreadsheet" else "heading_window",
            entry_id=entry_id,
            read_args=read_args,
            original_chars=original_chars,
        )

    return omitted


def _append_omitted(
    omitted: list[dict[str, Any]],
    *,
    kind: str,
    entry_id: str,
    read_args: dict[str, Any],
    original_chars: int,
) -> None:
    cleaned = {
        key: value for key, value in read_args.items()
        if value is not None and value != ""
    }
    cleaned["compress"] = False
    if any(item.get("read_files_args") == cleaned for item in omitted):
        return
    omitted.append({
        "kind": kind,
        "entry_id": entry_id,
        "read_files_args": cleaned,
        "original_chars": original_chars,
    })


def _scope_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if args.get("member_path"):
        out["member_path"] = args["member_path"]
    return out


def _copy_present(out: dict[str, Any], src: dict[str, Any], keys: tuple[str, ...]) -> None:
    for key in keys:
        if src.get(key) is not None:
            out[key] = src[key]


def _reopen_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in (
        "member_path",
        "offset",
        "max_chars",
        "page_start",
        "page_end",
        "page_label",
        "line_start",
        "line_end",
        "section_id",
        "heading",
        "paragraph_start",
        "paragraph_end",
    ):
        if key in args:
            out[key] = args[key]
    out["compress"] = False
    return out
