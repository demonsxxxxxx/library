from __future__ import annotations

import ipaddress
import logging
import os
import time
import uuid
import asyncio
from contextlib import asynccontextmanager
from hmac import compare_digest
from typing import Any

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

import library.tasks.handlers  # noqa: F401  (registers task handlers)
from library import __version__
from library.api.routes_agent import router as sessions_router
from library.api.routes_chat import router as chat_router
from library.api.routes_exports import router as exports_router
from library.api.routes_file_entries import router as file_entries_router
from library.api.routes_files import router as files_router
from library.api.routes_folders import router as folders_router
from library.api.routes_mcp import router as mcp_router
from library.api.routes_pdf_revisions import router as pdf_revisions_router
from library.api.routes_semantic_index import router as semantic_index_router
from library.api.routes_settings import router as settings_router
from library.api.routes_stats import router as stats_router
from library.api.routes_tasks import router as tasks_router
from library.api.routes_tend import router as tend_router
from library.api.routes_upload import router as upload_router
from library.api.routes_user_files import router as user_files_router
from library.api.routes_webdav_sync import router as webdav_sync_router
from library.config import get_settings, validate_llm_config
from library.db.bootstrap import bootstrap_schema
from library.db.engine import dispose_engine
from library.db.engine import get_engine
from library.db.session import session_scope
from library.provider_clients import close_provider_clients
from library.repositories import tasks as tasks_repo
from library.server_discovery import clear_server_state, write_server_state
from library.services import worker_lifecycle
from library.upload_limits import UploadSizeLimitMiddleware
from library.storage import get_storage

log = logging.getLogger(__name__)
SLOW_REQUEST_LOG_MS = 10_000
PUBLIC_PROBE_PATHS = frozenset({"/health", "/live", "/ready"})


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    state_written = False
    # The in-process runner is owned by worker_lifecycle so the Settings
    # page can start/stop it live; lifespan only decides whether to boot it.
    log.info(
        "backend startup: home=%s storage_backend=%s worker_enabled=%s",
        settings.library_home,
        settings.storage_backend,
        settings.worker_enabled,
    )
    _warn_if_unauthenticated_bind(settings)
    try:
        validate_llm_config(settings)
        await bootstrap_schema()
        if settings.runtime_schema_bootstrap_enabled:
            log.info("database schema bootstrap complete")
        else:
            log.info("runtime schema bootstrap disabled; using migrated schema")
        await _check_storage_consistency(settings)
        log.info("storage consistency check passed")
        if not settings.worker_enabled:
            await _warn_if_worker_disabled_with_pending()
        if os.environ.get("LIBRARY_HTTP_SERVER") == "1":
            host = os.environ.get("LIBRARY_API_HOST") or settings.library_api_host
            raw_port = os.environ.get("LIBRARY_API_PORT") or str(settings.library_api_port)
            try:
                port = int(raw_port)
            except ValueError:
                port = settings.library_api_port
                log.warning("invalid LIBRARY_API_PORT=%r; using %s", raw_port, port)
            state = write_server_state(
                settings.library_home,
                host=host,
                port=port,
                pid=os.getpid(),
            )
            state_written = True
            log.info("server discovery state written: %s", state["base_url"])
        if settings.worker_enabled:
            # Keep the in-process runner on live settings so GUI changes to
            # WORKER_BATCH_SIZE affect new task claims without a restart.
            await worker_lifecycle.start()
            log.info("task runner started in-process")
    except Exception:
        log.exception("backend startup failed")
        raise
    try:
        yield
    finally:
        log.info("backend shutdown starting")
        await worker_lifecycle.stop()
        if state_written:
            clear_server_state(settings.library_home, pid=os.getpid())
        await close_provider_clients()
        await dispose_engine()
        log.info("backend shutdown complete")


def _warn_if_unauthenticated_bind(settings) -> None:
    """Loud startup warning when the configured bind host is non-loopback
    and no LIBRARY_API_TOKEN is set: every endpoint is then reachable
    unauthenticated by any host on the network. Intentionally a warning,
    not a hard failure — existing tokenless loopback deployments and
    reverse-proxy setups keep working."""
    if settings.library_api_token:
        return
    host = os.environ.get("LIBRARY_API_HOST") or settings.library_api_host
    try:
        is_loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        is_loopback = host.lower() == "localhost"
    if is_loopback:
        return
    log.warning(
        "\n"
        "================================================================\n"
        "  SECURITY WARNING: LIBRARY_API_TOKEN is not set while the\n"
        "  API is configured to bind non-loopback host %r.\n"
        "\n"
        "  EVERY endpoint is UNAUTHENTICATED: any host that can reach\n"
        "  this port can download all ingested documents, rewrite LLM\n"
        "  settings (redirecting prompts and stored API keys to an\n"
        "  attacker endpoint), upload files, and delete data.\n"
        "\n"
        "  Set LIBRARY_API_TOKEN before exposing the API beyond\n"
        "  127.0.0.1.\n"
        "================================================================",
        host,
    )


async def _warn_if_worker_disabled_with_pending() -> None:
    """Warn when the worker is disabled but queued tasks will go unprocessed.

    The single-process trap: `library serve` with the worker off silently
    stops processing queued tasks, so pending work looks stuck with no
    hint. Only warns when there actually is work to do, and points at the
    two escape hatches (Settings-page toggle / standalone daemon).
    """
    try:
        async with session_scope() as session:
            counts = await tasks_repo.count_running_and_pending(session)
        pending = int(counts.get("pending", 0))
        if pending > 0:
            log.warning(
                "worker disabled (WORKER_ENABLED=false) but %d task(s) are "
                "pending and will not be processed; enable the worker in "
                "Settings, or run `library-worker` as a separate daemon",
                pending,
            )
    except Exception:
        log.exception("failed to check pending tasks at startup")


async def _check_storage_consistency(settings) -> None:
    """Detect when STORAGE_BACKEND was switched without migrating
    existing files. UUID-shaped storage_keys imply local; relative
    paths with slashes imply mirror — mixing them silently breaks.

    Raises StorageBackendMismatchError on conflict; the error message
    points the user at `library storage migrate`.
    """
    from library.db.engine import get_session_factory
    from library.repositories import files as files_repo

    factory = get_session_factory()
    async with factory() as s:
        sample = await files_repo.sample_live_storage_keys(s, limit=5)
    if not sample:
        return  # empty db, nothing to check

    def _looks_uuid_flat(k: str) -> bool:
        """Local backend storage_keys are 'xx/yy/<uuid>' or short test
        fixtures like '00/aa/x'. The defining property: the leading two
        segments are hex prefix dirs. Anything that starts with a real
        word segment ('research/llm/paper.pdf') is a mirror key."""
        parts = k.split("/")
        if len(parts) < 2:
            return False
        # Hex prefix dirs are short and hex-only.
        for seg in parts[:2]:
            if not (1 <= len(seg) <= 4):
                return False
            if not all(c in "0123456789abcdef" for c in seg):
                return False
        return True

    backend = settings.storage_backend
    for k in sample:
        is_uuid = _looks_uuid_flat(k)
        if backend == "mirror" and is_uuid:
            raise StorageBackendMismatchError(
                f"STORAGE_BACKEND=mirror but existing files reference "
                f"UUID storage keys (e.g. {k!r}). Either revert "
                f"STORAGE_BACKEND=local, or run:\n"
                f"  library storage migrate --from local --to mirror"
            )
        if backend == "local" and not is_uuid and "/" in k:
            raise StorageBackendMismatchError(
                f"STORAGE_BACKEND=local but existing files reference "
                f"path-shaped storage keys (e.g. {k!r}). Either revert "
                f"STORAGE_BACKEND=mirror, or run:\n"
                f"  library storage migrate --from mirror --to local"
            )


class StorageBackendMismatchError(RuntimeError):
    pass


app = FastAPI(title="Library", lifespan=lifespan)

# Browser-based GUI (frontend/) runs on the Vite dev server (5173), which
# differs in origin from the API host and needs CORS to read responses.
# The CLI client (httpx ASGITransport or remote-mode httpx) bypasses the
# browser stack and is unaffected.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=[
        "X-File-Id", "X-Size-Bytes", "X-Conversation-Id",
        "X-Citation-Count", "X-Missing-Count", "X-Folder-Id",
        "X-Member-Count",
    ],
)


app.add_middleware(UploadSizeLimitMiddleware)


@app.middleware("http")
async def optional_bearer_auth(request: Request, call_next):
    token = get_settings().library_api_token
    if (
        not token
        or request.method == "OPTIONS"
        or request.url.path in PUBLIC_PROBE_PATHS
    ):
        return await call_next(request)
    auth = request.headers.get("authorization", "")
    prefix = "Bearer "
    if not auth.startswith(prefix) or not compare_digest(auth[len(prefix):], token):
        return JSONResponse(
            {"detail": "missing or invalid bearer token"},
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return await call_next(request)


@app.middleware("http")
async def request_diagnostics(request: Request, call_next):
    request_id = uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        duration_ms = int((time.perf_counter() - started) * 1000)
        log.exception(
            "request %s failed method=%s path=%s client=%s duration_ms=%d",
            request_id,
            request.method,
            request.url.path,
            _client_host(request),
            duration_ms,
        )
        raise
    duration_ms = int((time.perf_counter() - started) * 1000)
    response.headers["X-Request-Id"] = request_id
    path = request.url.path
    if path not in PUBLIC_PROBE_PATHS and response.status_code >= 500:
        log.error(
            "request %s returned %d method=%s path=%s client=%s duration_ms=%d",
            request_id,
            response.status_code,
            request.method,
            path,
            _client_host(request),
            duration_ms,
        )
    elif path not in PUBLIC_PROBE_PATHS and duration_ms >= SLOW_REQUEST_LOG_MS:
        log.info(
            "slow request %s returned %d method=%s path=%s duration_ms=%d",
            request_id,
            response.status_code,
            request.method,
            path,
            duration_ms,
        )
    return response


def _client_host(request: Request) -> str:
    return request.client.host if request.client else "-"

V1_PREFIX = "/v1"
app.include_router(folders_router, prefix=V1_PREFIX)
app.include_router(file_entries_router, prefix=V1_PREFIX)
app.include_router(files_router, prefix=V1_PREFIX)
app.include_router(pdf_revisions_router, prefix=V1_PREFIX)
app.include_router(upload_router, prefix=V1_PREFIX)
app.include_router(user_files_router, prefix=V1_PREFIX)
app.include_router(webdav_sync_router, prefix=V1_PREFIX)
app.include_router(sessions_router, prefix=V1_PREFIX)
app.include_router(chat_router, prefix=V1_PREFIX)
app.include_router(exports_router, prefix=V1_PREFIX)
app.include_router(tasks_router, prefix=V1_PREFIX)
app.include_router(tend_router, prefix=V1_PREFIX)
app.include_router(semantic_index_router, prefix=V1_PREFIX)
app.include_router(settings_router, prefix=V1_PREFIX)
app.include_router(stats_router, prefix=V1_PREFIX)
app.include_router(mcp_router, prefix=V1_PREFIX)


@app.get("/health", tags=["meta"])
async def health() -> dict[str, str]:
    s = get_settings()
    return {
        "status": "ok",
        "version": __version__,
        "git_sha": s.build_sha,
        "build_id": s.build_id,
        "environment": s.app_env,
        "storage_backend": s.storage_backend,
    }


@app.get("/live", tags=["meta"])
async def live() -> dict[str, str]:
    """Process liveness only; never depends on downstream services."""
    return {"status": "ok", "version": __version__}


async def _bounded_readiness(check, timeout_seconds: float) -> str:  # noqa: ANN001
    try:
        await asyncio.wait_for(check, timeout=timeout_seconds)
    except Exception:
        return "error"
    return "ok"


async def _database_ready() -> None:
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))


@app.get("/ready", tags=["meta"], response_model=None)
async def ready() -> Any:
    """Dependency readiness for traffic admission; never calls an LLM."""
    settings = get_settings()
    database, storage = await asyncio.gather(
        _bounded_readiness(
            _database_ready(), settings.readiness_timeout_seconds
        ),
        _bounded_readiness(
            get_storage().check_ready(), settings.readiness_timeout_seconds
        ),
    )
    payload = {
        "status": "ready" if database == storage == "ok" else "not_ready",
        "checks": {"database": database, "storage": storage},
    }
    if payload["status"] != "ready":
        return JSONResponse(payload, status_code=503)
    return payload
