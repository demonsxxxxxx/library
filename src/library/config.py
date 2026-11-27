from __future__ import annotations

import logging
import os
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

log = logging.getLogger(__name__)


LlmProvider = Literal["openai", "openai-compatible", "anthropic"]
# "openai"            -> OpenAI proper (supports strict json_schema)
# "openai-compatible" -> DeepSeek / Together / Groq / vllm / ollama. Same wire
#                        protocol as OpenAI, but only the basic
#                        response_format={"type":"json_object"} is supported,
#                        so the adapter injects the schema as text instead.
# "anthropic"         -> Anthropic Messages API.


class VisionDestination(BaseModel):
    provider: LlmProvider
    model: str
    base_url: str | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "dev"
    build_sha: str = "unknown"
    build_id: str = "local"
    library_api_token: str | None = None
    library_api_host: str = "127.0.0.1"
    library_api_port: int = 8000
    # Opt-in ingest archival of native PDF/DOCX, independent of the PDF viewer and LLM.
    document_revision_ingest_enabled: bool = False
    # Selecting an external parser replaces PDF ingest extraction, independently of the viewer.
    document_ingest_parser: Literal["configured", "weknora"] = "configured"
    pdf_ingest_parser: Literal["library", "weknora-builtin"] = "library"
    # Server-side opt-in: sends selected pages only to the configured OCR endpoint.
    pdf_revision_paddle_ocr_enabled: bool = False
    pdf_revision_paddle_ocr_endpoint: str = ""
    office_ingest_parser: Literal["library", "docling-office", "anydoc", "weknora-builtin"] = "library"
    office_docling_formats: set[Literal["docx", "xlsx", "pptx"]] = Field(default_factory=lambda: {"docx", "xlsx", "pptx"})
    office_docling_python: str = ""
    office_anydoc_python: str = ""
    pdf_revision_viewer_enabled: bool = False
    pdf_revision_weknora_python: str = ""
    pdf_revision_weknora_source: str = ""
    pdf_revision_docling_python: str = ""
    pdf_revision_hf_cache: str = ""
    # Server-owned allowlist; a GUI checkbox alone does not authorize sending bytes.
    pdf_revision_vision_grants: dict[str, VisionDestination] = Field(default_factory=dict)
    readiness_timeout_seconds: float = Field(default=2.0, ge=0.1, le=30.0)

    @model_validator(mode="after")
    def apply_document_parser(self) -> Settings:
        if self.document_ingest_parser == "weknora":
            self.pdf_ingest_parser = self.office_ingest_parser = "weknora-builtin"
            self.pdf_revision_paddle_ocr_enabled = True
        return self

    # Single root for all on-disk state (db, library, caches). Default
    # is ~/LibraryData. Per-component overrides below take precedence
    # when set; otherwise everything sits under library_home/.
    library_home: str = ""  # resolved to ~/LibraryData at runtime

    db_backend: Literal["sqlite", "postgres"] = "sqlite"
    # sqlite db file always lives at `<library_home>/library.db`. Not an
    # env override — relocate the whole footprint via LIBRARY_HOME instead.
    postgres_dsn: str = "postgresql+asyncpg://library:library@localhost:5432/library"
    postgres_pool_size: int = Field(default=10, ge=1, le=200)
    postgres_max_overflow: int = Field(default=20, ge=0, le=500)
    postgres_pool_timeout_seconds: float = Field(default=30.0, ge=0.1, le=300.0)
    # Set to zero for transaction-pooled PostgreSQL proxies. asyncpg still
    # prepares statements in that mode, so the engine assigns globally unique
    # names to avoid collisions when a transaction lands on another server
    # connection.
    postgres_prepared_statement_cache_size: int = Field(
        default=100, ge=0, le=10_000,
    )
    # Local installs keep the zero-setup bootstrap path. Managed
    # deployments can disable startup DDL after applying Alembic migrations,
    # preventing API and worker replicas from racing over schema changes.
    runtime_schema_bootstrap_enabled: bool = True

    # mirror = folder-tree on disk matching the user's intent; default.
    # local  = UUID-flat object pool; faster, dedup-on, less human-friendly.
    # s3     = remote object storage for multi-host deployments.
    # mirror/local always live under <library_home>/{library,objects}.
    # Relocate the whole footprint via LIBRARY_HOME, or symlink a single
    # subdir if you want db on SSD and library on a big disk.
    storage_backend: Literal["mirror", "local", "s3"] = "mirror"
    s3_endpoint_url: str | None = None
    s3_bucket: str = "library"
    s3_access_key: str | None = None
    s3_secret_key: str | None = None
    s3_region: str = "us-east-1"

    # WebDAV snapshot publishing. This is intentionally not a storage
    # backend: Library keeps its local database/indexes, then publishes
    # content-addressed knowledge-pack snapshots to WebDAV on demand.
    webdav_url: str | None = None
    webdav_username: str | None = None
    webdav_password: str | None = None
    webdav_remote_path: str = "/library"
    webdav_auto_sync_enabled: bool = False
    webdav_auto_sync_interval_minutes: int = 60

    worker_enabled: bool = True
    worker_poll_interval_seconds: float = 2.0
    worker_batch_size: int = 4
    bulk_reprocess_page_size: int = Field(default=500, ge=10, le=5_000)
    worker_lease_seconds: int = 60
    worker_heartbeat_seconds: int = 20
    worker_retry_base_seconds: float = Field(default=60.0, ge=0.1, le=86_400.0)
    worker_retry_max_seconds: float = Field(default=3_600.0, ge=0.1, le=604_800.0)
    worker_scheduler_enabled: bool = True

    # Automatic active -> demoted -> archived transitions are opt-in.
    # Personal knowledge bases often prefer manual lifecycle control, while
    # team/shared deployments may want background cost management.
    auto_lifecycle_enabled: bool = False

    # Rolling 24-hour token cap for background maintenance LLM work.
    # 0 disables the cap. Foreground ingest/chat reflection are not limited.
    maintenance_daily_token_budget: int = 0
    relation_background_vetting_enabled: bool = False
    audit_retention_days: int = Field(default=90, ge=0, le=3650)
    task_retention_days: int = Field(default=30, ge=0, le=3650)
    task_outcome_retention_days: int = Field(default=30, ge=0, le=3650)
    agent_event_retention_days: int = Field(default=30, ge=0, le=3650)
    prune_batch_size: int = Field(default=1_000, ge=1, le=100_000)
    prune_max_batches: int = Field(default=10, ge=1, le=1_000)
    # Bound relation mining at every expensive stage. These are intentionally
    # independent from the small number of relations written per run: limiting
    # only output would still allow unbounded reads and in-memory pair sets.
    relation_mining_entry_page_size: int = Field(
        default=20_000, ge=100, le=100_000,
    )
    relation_mining_activity_limit: int = Field(
        default=50_000, ge=100, le=500_000,
    )
    relation_mining_eligible_tag_limit: int = Field(
        default=20_000, ge=100, le=100_000,
    )
    relation_mining_candidate_limit: int = Field(
        default=100_000, ge=1_000, le=1_000_000,
    )

    # Optional global capacity gates. Zero preserves the historical unlimited
    # behavior. They are useful when a shared deployment must fail fast before
    # an upload or background backlog consumes all available resources.
    library_document_limit: int = Field(default=0, ge=0)
    library_storage_bytes_limit: int = Field(default=0, ge=0)
    ingest_backlog_limit: int = Field(default=0, ge=0)
    chat_concurrency_limit: int = Field(default=0, ge=0, le=10_000)

    # Default policy when an upload / rename / move would collide with an
    # existing display_name in the same folder. `rename` suffixes ` (1)`,
    # `error` raises 409, `skip` returns the existing entry. Per-call
    # overrides on `/v1/upload` and the file-entry endpoints win when set.
    default_on_conflict: Literal["rename", "error", "skip"] = "rename"

    # Max accepted size in bytes for a single POST /v1/upload body.
    # 0 (default) = unlimited, preserving historical behavior; set
    # LIBRARY_UPLOAD_MAX_BYTES to cap per-request disk consumption.
    upload_max_bytes: int = Field(
        default=0, ge=0, validation_alias="LIBRARY_UPLOAD_MAX_BYTES",
    )

    # --- Multimodal chat (images pasted/dropped into the composer) ----------
    # Per-turn caps for images carried on POST /v1/chat. base64 in the JSON
    # body is decoded eagerly and validated in post_chat BEFORE the SSE
    # stream starts, so an over-cap request gets a real HTTP 413 instead of
    # an in-stream error frame. Images belong to the current turn only —
    # they are never persisted as bytes into conversation history.
    chat_image_max_count: int = Field(
        default=4, ge=0, validation_alias="LIBRARY_CHAT_IMAGE_MAX_COUNT",
    )
    chat_image_max_bytes: int = Field(
        default=10 * 1024 * 1024, ge=0,
        validation_alias="LIBRARY_CHAT_IMAGE_MAX_BYTES",
    )
    # How pasted chat images reach the model when the `chat` profile might be
    # text-only. There is no reliable capability API across OpenAI-compatible
    # providers, so the default `auto` PROBES once per (provider, model): it
    # sends a 1x1 image and, if the provider rejects image input, remembers the
    # model is text-only and routes images through the `vision` profile as a
    # text description thereafter (no-op unless a `vision` profile exists).
    #   auto → probe + cache (zero config, self-correcting)
    #   on   → always send images directly (model is known vision-capable)
    #   off  → always describe via the `vision` profile (model is text-only)
    chat_vision: Literal["auto", "on", "off"] = Field(
        default="auto", validation_alias="LIBRARY_CHAT_VISION",
    )

    # --- LLM defaults (used when a profile leaves a field blank) ------------
    llm_default_provider: LlmProvider = "openai"
    llm_default_api_key: str | None = None
    llm_default_base_url: str | None = None
    llm_default_model: str = "gpt-4o-mini"
    llm_default_tps: int = Field(default=10, ge=1, le=10_000)
    # Operator-declared model capabilities. The request dialect is inferred
    # only from the provider family when omitted, never from a gateway URL.
    llm_default_dialect: str | None = None
    llm_default_context_window: int = Field(default=128_000, ge=1_024)
    llm_default_tokenizer: str = "o200k_base"
    llm_default_supports_vision: bool = True
    llm_default_supports_tools: bool = True
    llm_default_supports_temperature: bool = True
    llm_default_token_limit_param: Literal[
        "max_tokens", "max_completion_tokens"
    ] = "max_tokens"

    # Optional backup model for the default profile: when the primary
    # exhausts its transient-error retries (rate limit / timeout / 5xx /
    # overload), the chat client fails over to this model once. A backup
    # only needs the four fields that reach a different endpoint; its
    # capabilities inherit from the primary (see resolve_backup).
    llm_default_backup_provider: LlmProvider | None = None
    llm_default_backup_api_key: str | None = None
    llm_default_backup_base_url: str | None = None
    llm_default_backup_model: str | None = None

    # --- Per-profile overrides (chat / reflect / ingest / vision / audio) ---
    # Any field left blank inherits the corresponding `llm_default_*` value.
    # `audio` is text-transcription only (Whisper et al.) — provider must be
    # OpenAI-compatible since Anthropic has no transcription API.
    llm_chat_provider: LlmProvider | None = None
    llm_chat_api_key: str | None = None
    llm_chat_base_url: str | None = None
    llm_chat_model: str | None = None
    llm_chat_tps: int = Field(default=10, ge=1, le=10_000)
    llm_chat_dialect: str | None = None
    llm_chat_context_window: int | None = Field(default=None, ge=1_024)
    llm_chat_tokenizer: str | None = None
    llm_chat_supports_vision: bool | None = None
    llm_chat_supports_tools: bool | None = None
    llm_chat_supports_temperature: bool | None = None
    llm_chat_token_limit_param: Literal[
        "max_tokens", "max_completion_tokens"
    ] | None = None

    llm_chat_backup_provider: LlmProvider | None = None
    llm_chat_backup_api_key: str | None = None
    llm_chat_backup_base_url: str | None = None
    llm_chat_backup_model: str | None = None

    llm_reflect_provider: LlmProvider | None = None
    llm_reflect_api_key: str | None = None
    llm_reflect_base_url: str | None = None
    llm_reflect_model: str | None = None
    llm_reflect_tps: int = Field(default=10, ge=1, le=10_000)
    llm_reflect_dialect: str | None = None
    llm_reflect_context_window: int | None = Field(default=None, ge=1_024)
    llm_reflect_tokenizer: str | None = None
    llm_reflect_supports_vision: bool | None = None
    llm_reflect_supports_tools: bool | None = None
    llm_reflect_supports_temperature: bool | None = None
    llm_reflect_token_limit_param: Literal[
        "max_tokens", "max_completion_tokens"
    ] | None = None

    llm_reflect_backup_provider: LlmProvider | None = None
    llm_reflect_backup_api_key: str | None = None
    llm_reflect_backup_base_url: str | None = None
    llm_reflect_backup_model: str | None = None

    llm_ingest_provider: LlmProvider | None = None
    llm_ingest_api_key: str | None = None
    llm_ingest_base_url: str | None = None
    llm_ingest_model: str | None = None
    llm_ingest_tps: int = Field(default=10, ge=1, le=10_000)
    llm_ingest_dialect: str | None = None
    llm_ingest_context_window: int | None = Field(default=None, ge=1_024)
    llm_ingest_tokenizer: str | None = None
    llm_ingest_supports_vision: bool | None = None
    llm_ingest_supports_tools: bool | None = None
    llm_ingest_supports_temperature: bool | None = None
    llm_ingest_token_limit_param: Literal[
        "max_tokens", "max_completion_tokens"
    ] | None = None

    llm_ingest_backup_provider: LlmProvider | None = None
    llm_ingest_backup_api_key: str | None = None
    llm_ingest_backup_base_url: str | None = None
    llm_ingest_backup_model: str | None = None

    llm_vision_provider: LlmProvider | None = None
    llm_vision_api_key: str | None = None
    llm_vision_base_url: str | None = None
    llm_vision_model: str | None = None
    llm_vision_tps: int = Field(default=10, ge=1, le=10_000)
    llm_vision_dialect: str | None = None
    llm_vision_context_window: int | None = Field(default=None, ge=1_024)
    llm_vision_tokenizer: str | None = None
    llm_vision_supports_vision: bool = True
    llm_vision_supports_tools: bool | None = None
    llm_vision_supports_temperature: bool | None = None
    llm_vision_token_limit_param: Literal[
        "max_tokens", "max_completion_tokens"
    ] | None = None

    llm_vision_backup_provider: LlmProvider | None = None
    llm_vision_backup_api_key: str | None = None
    llm_vision_backup_base_url: str | None = None
    llm_vision_backup_model: str | None = None

    llm_audio_provider: LlmProvider | None = None  # only "openai" makes sense
    llm_audio_api_key: str | None = None
    llm_audio_base_url: str | None = None
    llm_audio_model: str | None = None
    llm_audio_tps: int = Field(default=10, ge=1, le=10_000)

    # --- Agent token budgets ------------------------------------------------
    # Per-call max_tokens for the planner / executor. Sized for *reasoning*
    # models (DeepSeek/Qwen "thinking" variants), which spend most of their
    # output budget on hidden reasoning before any visible text — too small a
    # cap gets consumed by reasoning and truncates the plan/answer.
    # If the executor hits `stop_reason=max_tokens` during the final answer,
    # runtime.py can continue instead of returning a half-finished answer.
    agent_plan_max_tokens: int = 2048
    agent_execute_max_tokens: int = 4096
    agent_execute_max_turns: int = 15
    agent_max_parallel_tool_calls: int = Field(default=8, ge=1, le=32)
    agent_final_answer_continue_turns: int = 3
    agent_final_answer_max_chars: int = 120_000
    # Hard wall-clock cap for one foreground chat turn. 0 disables the cap.
    # This is intentionally backend-owned so web and CLI clients
    # get the same stuck-turn recovery behavior.
    agent_turn_timeout_seconds: float = 1800.0
    agent_cache_slo_min_hit_ratio: float = Field(default=0.95, ge=0.0, le=1.0)
    agent_cache_slo_min_eligible_requests: int = Field(
        default=2, ge=1, le=1_000_000,
    )
    # Conversation history compaction is token-aware and independent from
    # evidence compression. It only changes the provider request view; stored
    # turns and tool results remain lossless.
    conversation_compaction_enabled: bool = True
    conversation_compaction_reserve_tokens: int = Field(default=8_192, ge=1_024)
    # Unified compression switch. Only COMPRESSION_ENABLED controls all
    # built-in ingest, query, and read_files compression paths.
    compression_enabled: bool = Field(True, validation_alias="COMPRESSION_ENABLED")
    compression_min_chars: int = Field(12_000, validation_alias="COMPRESSION_MIN_CHARS")
    compression_target_chars: int = Field(8_000, validation_alias="COMPRESSION_TARGET_CHARS")
    compression_context_chars: int = Field(220, validation_alias="COMPRESSION_CONTEXT_CHARS")
    compression_max_ratio: float = Field(0.85, validation_alias="COMPRESSION_MAX_RATIO")
    # Bounded fan-out for ingest-time LLM work: long text/PDF chunk
    # indexing and scanned-PDF OCR page calls. Keep this conservative;
    # provider rate limits and local network bandwidth are the real cap.
    # Zero omits the output-token field for OpenAI-shaped providers and lets
    # the provider apply its own limit. Anthropic requires a positive value
    # and is rejected by validate_llm_config below.
    llm_ingest_max_tokens: int = Field(default=1200, ge=0, le=16_384)
    llm_ingest_concurrency: int = 4

    # --- Embeddings / semantic recall --------------------------------------
    # Uses Alibaba Cloud Model Studio (DashScope/Bailian) by default through
    # its OpenAI-compatible /v1/embeddings endpoint. Keep this separate from
    # LLM_* profiles so vision/chat credentials do not implicitly bleed into
    # semantic recall.
    embedding_provider: Literal["dashscope", "openai-compatible"] = "openai-compatible"
    embedding_api_key: str | None = None
    embedding_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"
    embedding_dimensions: int = 1024
    embedding_tps: int = Field(default=10, ge=1, le=10_000)
    embedding_batch_size: int = Field(default=10, ge=1, le=10)
    semantic_index_backend: Literal["auto", "file", "sqlite-vec"] = "auto"
    semantic_recall_enabled: bool = False
    semantic_recall_limit: int = 100
    semantic_rebuild_page_size: int = Field(default=100, ge=1, le=1_000)
    section_backfill_min_score: float = Field(default=0.45, ge=0.0, le=1.0)
    section_embedding_max_sections: int = Field(default=200, ge=0, le=200)

    # --- Optional rerank ----------------------------------------------------
    # Rerank is a second-stage retrieval refinement over already-recalled
    # candidates. It has its own key so retrieval experiments do not silently
    # consume chat/vision credentials.
    rerank_enabled: bool = False
    rerank_api_key: str | None = None
    rerank_base_url: str = "https://dashscope.aliyuncs.com/compatible-api/v1"
    rerank_model: str = "qwen3-rerank"
    rerank_tps: int = Field(default=10, ge=1, le=10_000)
    rerank_batch_size: int = Field(default=100, ge=1, le=10_000)
    rerank_top_n: int = 80
    rerank_max_doc_chars: int = 1800
    rerank_concurrency: int = 10
    evidence_selection: Literal["quota", "rerank"] = "quota"

    # --- Embedded document image vision -----------------------------------
    document_vision_enabled: bool = True
    document_vision_max_images: int = Field(default=20, ge=0, le=500)
    document_vision_question_max_images: int = Field(default=5, ge=1, le=50)
    document_vision_min_image_bytes: int = Field(default=2_048, ge=0, le=100_000_000)
    document_vision_min_image_dimension: int = Field(default=32, ge=0, le=100_000)
    document_vision_min_image_area: int = Field(default=4_096, ge=0, le=100_000_000)

    # --- Scanned-PDF OCR ---------------------------------------------------
    # Per-document page cap for the VLM OCR fallback. Scanned PDFs issue one
    # vision LLM call per page, so an uncapped multi-thousand-page document
    # would fan out an unbounded number of calls in a single ingest task.
    # When the cap trips, ingest records an "ocr_page_cap" partial-coverage
    # reason (mirroring the text-layer PDF_TEXT_MAX_INDEX_PAGES cap) and the
    # agent can still OCR deeper pages on demand via read_segment. 0/negative
    # means "no cap" (OCR every page).
    ocr_max_pages: int = Field(default=300, validation_alias="OCR_MAX_PAGES")

    @property
    def database_url(self) -> str:
        if self.db_backend == "sqlite":
            from pathlib import Path
            home = Path(self.library_home).expanduser()
            return f"sqlite+aiosqlite:///{home / 'library.db'}"
        return self.postgres_dsn

    @property
    def mirror_vault_root(self) -> str:
        from pathlib import Path
        return str(Path(self.library_home).expanduser() / "library")

    @property
    def local_storage_root(self) -> str:
        from pathlib import Path
        return str(Path(self.library_home).expanduser() / "objects")


@dataclass(slots=True, frozen=True)
class ModelCapabilities:
    """Explicit request and context limits for one resolved model."""

    dialect: str = "openai-compatible"
    context_window: int = 128_000
    tokenizer: str = "o200k_base"
    supports_vision: bool = True
    supports_tools: bool = True
    supports_temperature: bool = True
    token_limit_param: Literal["max_tokens", "max_completion_tokens"] = "max_tokens"


@dataclass(slots=True, frozen=True)
class LlmProfile:
    name: str
    provider: LlmProvider
    api_key: str | None
    base_url: str | None
    model: str
    tps: int = 10
    capabilities: ModelCapabilities = ModelCapabilities()


LLM_PROFILES: tuple[str, ...] = ("chat", "reflect", "ingest", "vision", "audio")
# Profiles users actually rely on out of the box. `vision` and `audio` are
# opt-in: vision adds figure descriptions and OCR for scanned PDFs; audio
# is only used by transcription pipelines. Pipelines that need them call
# `has_vision_profile` / equivalent and degrade gracefully when absent.

# Subset surfaced in the Settings GUI / `/v1/settings/llm` payload.
# `audio` is intentionally hidden until a transcription pipeline actually
# consumes it — exposing the form today would let users fill in keys that
# nothing reads, then file bugs about "I configured it and nothing
# happens". Keep the underlying Settings fields + `has_audio_profile`
# helper so flipping audio back on is a one-line change here.
LLM_PROFILES_VISIBLE: tuple[str, ...] = ("chat", "reflect", "ingest", "vision")
_REQUIRED_PROFILES: tuple[str, ...] = ("chat", "reflect", "ingest")


def _profile_field(settings: Settings, profile: str, field: str) -> object:
    """Read a per-profile override, falling back to the matching default."""
    override = getattr(settings, f"llm_{profile}_{field}")
    return override if override is not None else getattr(settings, f"llm_default_{field}")


def resolve_profile(settings: Settings, profile: str) -> LlmProfile:
    """Resolve `profile` (one of LLM_PROFILES, or "default" for the
    `LLM_DEFAULT_*` fields themselves) against `LLM_<PROFILE>_*` overrides,
    falling back to `LLM_DEFAULT_*` per-field."""
    if profile not in LLM_PROFILES and profile != "default":
        raise ValueError(f"unknown LLM profile: {profile!r}")
    provider = _profile_field(settings, profile, "provider")  # type: ignore[assignment]
    base_url = _profile_field(settings, profile, "base_url")  # type: ignore[assignment]
    model = _profile_field(settings, profile, "model")  # type: ignore[assignment]
    capabilities = _resolve_model_capabilities(settings, profile, str(provider or ""))
    return LlmProfile(
        name=profile,
        provider=provider,  # type: ignore[arg-type]
        api_key=_profile_field(settings, profile, "api_key"),  # type: ignore[arg-type]
        base_url=base_url,  # type: ignore[arg-type]
        model=model,  # type: ignore[arg-type]
        tps=_effective_llm_tps(
            settings,
            provider=str(provider or ""),
            base_url=base_url if isinstance(base_url, str) else None,
            model=str(model or ""),
        ),
        capabilities=capabilities,
    )


def _provider_family(provider: str) -> str:
    """Normalize a provider to its request-dialect family. Dialects drive
    request construction (max_tokens vs max_completion_tokens, thinking
    controls), so they must match the endpoint actually being called."""
    if provider == "anthropic":
        return "anthropic"
    if provider == "openai":
        return "openai"
    return "openai-compatible"


def resolve_backup(settings: Settings, profile: str) -> LlmProfile | None:
    """Resolve the optional failover target for `profile` (a member of
    LLM_PROFILES, or "default" for the `LLM_DEFAULT_BACKUP_*` fields).

    Returns ``None`` when no backup model is configured (neither on the
    profile itself nor inherited from the default's backup). The backup
    only carries the four fields that reach a different endpoint
    (provider / api_key / base_url / model); its capabilities inherit the
    primary's resolved capabilities, with the dialect kept when the
    provider family matches the primary's and re-derived otherwise, and
    ``token_limit_param`` pinned to ``max_tokens`` for an Anthropic
    backup (its SDK always uses ``max_tokens``)."""
    model = _profile_field(settings, profile, "backup_model")
    if not model:
        return None
    provider = str(_profile_field(settings, profile, "backup_provider") or "").strip()
    provider = provider or "openai-compatible"
    base_url = _profile_field(settings, profile, "backup_base_url")
    api_key = _profile_field(settings, profile, "backup_api_key")
    primary = resolve_profile(settings, profile)
    cap = primary.capabilities
    dialect = (
        cap.dialect
        if _provider_family(provider) == _provider_family(primary.provider)
        else _provider_family(provider)
    )
    token_limit_param = (
        "max_tokens" if _provider_family(provider) == "anthropic" else cap.token_limit_param
    )
    return LlmProfile(
        name=f"{profile}.backup",
        provider=provider,  # type: ignore[arg-type]
        api_key=api_key,  # type: ignore[arg-type]
        base_url=base_url if isinstance(base_url, str) else None,  # type: ignore[arg-type]
        model=str(model),
        tps=_effective_llm_tps(
            settings,
            provider=provider,
            base_url=base_url if isinstance(base_url, str) else None,
            model=str(model),
        ),
        capabilities=replace(
            cap, dialect=dialect, token_limit_param=token_limit_param
        ),
    )


def _resolve_model_capabilities(
    settings: Settings,
    profile: str,
    provider: str,
) -> ModelCapabilities:
    # Audio uses a different API and does not consume chat capability fields.
    # Give it provider-shaped defaults without adding dead audio settings.
    if profile == "audio":
        dialect = "anthropic" if provider == "anthropic" else (
            "openai" if provider == "openai" else "openai-compatible"
        )
        return ModelCapabilities(dialect=dialect)

    dialect = _profile_field(settings, profile, "dialect")
    if not dialect:
        dialect = "anthropic" if provider == "anthropic" else (
            "openai" if provider == "openai" else "openai-compatible"
        )
    tokenizer = str(_profile_field(settings, profile, "tokenizer") or "").strip()
    return ModelCapabilities(
        dialect=str(dialect).strip().lower(),
        context_window=int(_profile_field(settings, profile, "context_window")),
        tokenizer=tokenizer or "utf8_upper_bound",
        supports_vision=bool(_profile_field(settings, profile, "supports_vision")),
        supports_tools=bool(_profile_field(settings, profile, "supports_tools")),
        supports_temperature=bool(
            _profile_field(settings, profile, "supports_temperature")
        ),
        token_limit_param=_profile_field(  # type: ignore[arg-type]
            settings, profile, "token_limit_param"
        ),
    )


def _effective_llm_tps(
    settings: Settings,
    *,
    provider: str,
    base_url: str | None,
    model: str,
) -> int:
    rates: list[int] = []
    if _same_model(
        provider,
        base_url,
        model,
        settings.llm_default_provider,
        settings.llm_default_base_url,
        settings.llm_default_model,
    ):
        rates.append(settings.llm_default_tps)
    for profile in LLM_PROFILES:
        profile_provider = _profile_field(settings, profile, "provider")
        profile_base_url = _profile_field(settings, profile, "base_url")
        profile_model = _profile_field(settings, profile, "model")
        if _same_model(
            provider,
            base_url,
            model,
            str(profile_provider or ""),
            profile_base_url if isinstance(profile_base_url, str) else None,
            str(profile_model or ""),
        ):
            rates.append(int(getattr(settings, f"llm_{profile}_tps") or 1))
    return max(1, min(rates)) if rates else 1


def _same_model(
    left_provider: str,
    left_base_url: str | None,
    left_model: str,
    right_provider: str,
    right_base_url: str | None,
    right_model: str,
) -> bool:
    return (
        str(left_provider or "").strip().lower()
        == str(right_provider or "").strip().lower()
        and str(left_base_url or "").strip().rstrip("/").lower()
        == str(right_base_url or "").strip().rstrip("/").lower()
        and str(left_model or "").strip() == str(right_model or "").strip()
    )


def has_vision_profile(settings: Settings | None = None) -> bool:
    """Whether the optional `vision` profile is *explicitly* configured.

    True only when the user set at least one `LLM_VISION_*` override
    (api_key / base_url / model). Inheriting the default api_key alone
    is NOT enough: the default model is often text-only (DeepSeek-V3,
    qwen-text), and silently routing vision calls to it produces 400
    errors per page from the provider rather than a useful failure.

    Pipelines that *augment* their output with VLM calls (PDF figure
    captions, scanned-PDF OCR, image indexing) check this so they can
    skip the VLM path entirely on installations that didn't configure
    one — instead of crashing or filling logs with provider errors.
    """
    return _has_optional_profile(settings, "vision")


def has_audio_profile(settings: Settings | None = None) -> bool:
    """Symmetric to `has_vision_profile` for transcription.

    The default profile is usually a chat model with no transcription
    endpoint, so falling back silently produces 404s. Audio pipelines
    check this and skip / surface a useful error when unset."""
    return _has_optional_profile(settings, "audio")


def _has_optional_profile(settings: Settings | None, profile: str) -> bool:
    s = settings if settings is not None else get_settings()
    return any(
        getattr(s, f"llm_{profile}_{field}") not in (None, "")
        for field in ("api_key", "base_url", "model")
    )


class LlmConfigError(RuntimeError):
    """Startup-time LLM configuration is incomplete or inconsistent."""


def validate_llm_config(settings: Settings) -> None:
    """Fail fast at startup if required LLM credentials are missing.

    Without this, a freshly-installed Library accepts `/upload` and `/chat`
    requests but every task quietly errors when it first tries to call the
    provider — the failure shows up in `task_outcomes`, not in the foreground.

    Rule: each required profile must resolve to a non-empty api_key. A profile
    can satisfy this either via its own `LLM_<PROFILE>_API_KEY` or by inheriting
    `LLM_DEFAULT_API_KEY`.
    """
    missing = [p for p in _REQUIRED_PROFILES if not _profile_field(settings, p, "api_key")]
    if missing:
        raise LlmConfigError(
            "LLM api_key is not configured for required profile(s): "
            f"{', '.join(missing)}. Set LLM_DEFAULT_API_KEY in .env, or set "
            "the per-profile override LLM_<PROFILE>_API_KEY for each."
        )
    ingest_profile = resolve_profile(settings, "ingest")
    if settings.llm_ingest_max_tokens == 0 and ingest_profile.provider == "anthropic":
        raise LlmConfigError(
            "LLM_INGEST_MAX_TOKENS=0 omits the provider output-token limit, "
            "but Anthropic requires max_tokens. Configure a positive ingest "
            "limit for Anthropic profiles."
        )
    if (
        settings.document_vision_enabled
        and has_vision_profile(settings)
        and not settings.llm_vision_supports_vision
    ):
        raise LlmConfigError(
            "Document vision is enabled, but the configured vision profile is "
            "declared without image support. Set LLM_VISION_SUPPORTS_VISION=true "
            "or disable DOCUMENT_VISION_ENABLED."
        )


def _default_home() -> str:
    """`~/LibraryData` cross-platform. Used when LIBRARY_HOME is unset."""
    from pathlib import Path
    return str(Path.home() / "LibraryData")


def _settings_env_file() -> str:
    """Prefer project-local `.env`, then the home `.env` under LIBRARY_HOME.

    Editable installs historically read `.env` from the caller's working
    directory. CLI wrappers can be launched from any shell directory, so
    they need the starter `.env` under LIBRARY_HOME.
    """
    from pathlib import Path

    cwd_env = Path(".env")
    if cwd_env.is_file():
        return str(cwd_env)

    home = os.environ.get("LIBRARY_HOME") or _default_home()
    home_env = Path(home).expanduser() / ".env"
    if home_env.is_file():
        return str(home_env)

    return ".env"


def _resolve_paths(settings: "Settings", env_file: str | None = None) -> None:
    """In-place: resolve `library_home` to an absolute path and ensure
    it exists.

    A relative home (e.g. `LIBRARY_HOME=./data` written by older
    `library init`) is anchored to the directory containing the .env
    that was loaded, falling back to cwd — otherwise CLI / server / worker
    processes started from different working directories silently resolve
    different homes and operate on different databases.

    We only anchor to the .env directory when the relative value actually
    came from that .env file. A value coming from the *process* environment
    is relative to the process cwd; anchoring it to a .env that happens to
    live under the home (the starter `.env` is written to LIBRARY_HOME)
    would double-nest it to `<cwd>/<home>/<home>`.

    Without the mkdir, an unset / fresh-install LIBRARY_HOME blows up at
    the first sqlite connect with `unable to open database file` because
    aiosqlite refuses to mkdir for you. Storage backends (mirror/local)
    handle their own subdir creation lazily, so the home dir itself is the
    only thing we guarantee here.
    """
    from pathlib import Path
    home = settings.library_home or _default_home()
    home_path = Path(home).expanduser()
    if not home_path.is_absolute():
        anchor = Path.cwd()
        # A process-env LIBRARY_HOME is relative to the process cwd; only a
        # value that came from the .env file is anchored to that file's dir.
        from_process_env = os.environ.get("LIBRARY_HOME") is not None
        if not from_process_env and env_file and Path(env_file).is_file():
            anchor = Path(env_file).resolve().parent
        home_path = (anchor / home_path).resolve()
    settings.library_home = str(home_path)
    home_path.mkdir(parents=True, exist_ok=True)
    log.info("library home resolved to %s", settings.library_home)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    env_file = _settings_env_file()
    s = Settings(_env_file=env_file)
    _resolve_paths(s, env_file=env_file)
    # Merge the GUI-writable overlay (config_overlay.json under
    # LIBRARY_HOME) so its values take precedence over .env. Imported
    # lazily to avoid an import cycle (services -> config -> services).
    from library.services.config_overlay import (
        merge_overlay_into_settings, read_overlay,
    )
    merge_overlay_into_settings(s, read_overlay(s.library_home))
    return s
