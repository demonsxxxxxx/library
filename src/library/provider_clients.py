"""Event-loop-local HTTP clients for model-adjacent providers."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from weakref import WeakKeyDictionary

import httpx
from anthropic import AsyncAnthropic, DefaultAsyncHttpxClient as AnthropicHttpClient
from openai import AsyncOpenAI, DefaultAsyncHttpxClient as OpenAIHttpClient


_PROVIDER_HTTP_TIMEOUT_SECONDS = 60.0
_PROVIDER_HTTP_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)

@dataclass(slots=True)
class _ProviderClientPool:
    http: httpx.AsyncClient
    openai_compatible: dict[tuple[str, str], AsyncOpenAI] = field(default_factory=dict)
    anthropic: dict[tuple[str, str], AsyncAnthropic] = field(default_factory=dict)


_pools: WeakKeyDictionary[asyncio.AbstractEventLoop, _ProviderClientPool] = (
    WeakKeyDictionary()
)


async def _check_ingest_request(_request: httpx.Request) -> None:
    # HTTP hooks run for SDK-internal retries too, not just the first call.
    from library.tasks.ingest_guard import check_ingest_delivery
    await check_ingest_delivery()


def _current_pool() -> _ProviderClientPool:
    loop = asyncio.get_running_loop()
    pool = _pools.get(loop)
    if pool is None or pool.http.is_closed:
        pool = _ProviderClientPool(http=httpx.AsyncClient(
            timeout=_PROVIDER_HTTP_TIMEOUT_SECONDS,
            limits=_PROVIDER_HTTP_LIMITS,
            event_hooks={"request": [_check_ingest_request]},
        ))
        _pools[loop] = pool
    return pool


def get_provider_http_client() -> httpx.AsyncClient:
    """Return the shared outbound HTTP client for the current event loop."""
    return _current_pool().http


def get_openai_compatible_client(
    *,
    api_key: str,
    base_url: str | None,
) -> AsyncOpenAI:
    """Reuse an OpenAI-compatible SDK client for one endpoint credential."""
    pool = _current_pool()
    key = (str(base_url or "").rstrip("/"), str(api_key or ""))
    client = pool.openai_compatible.get(key)
    if client is None:
        client = AsyncOpenAI(
            api_key=api_key, base_url=base_url,
            http_client=OpenAIHttpClient(event_hooks={"request": [_check_ingest_request]}),
        )
        pool.openai_compatible[key] = client
    return client


def get_anthropic_client(
    *,
    api_key: str,
    base_url: str | None,
) -> AsyncAnthropic:
    """Reuse an Anthropic SDK client for one endpoint credential."""
    pool = _current_pool()
    key = (str(base_url or "").rstrip("/"), str(api_key or ""))
    client = pool.anthropic.get(key)
    if client is None:
        kwargs: dict[str, str] = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        client = AsyncAnthropic(
            **kwargs,
            http_client=AnthropicHttpClient(event_hooks={"request": [_check_ingest_request]}),
        )
        pool.anthropic[key] = client
    return client


async def close_provider_clients() -> None:
    """Close and discard the provider client owned by the current loop."""
    loop = asyncio.get_running_loop()
    pool = _pools.pop(loop, None)
    if pool is None:
        return
    if pool.openai_compatible:
        await asyncio.gather(
            *(client.close() for client in pool.openai_compatible.values()),
            return_exceptions=True,
        )
    if pool.anthropic:
        await asyncio.gather(
            *(client.close() for client in pool.anthropic.values()),
            return_exceptions=True,
        )
    if not pool.http.is_closed:
        await pool.http.aclose()
