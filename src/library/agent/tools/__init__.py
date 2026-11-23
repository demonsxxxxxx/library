"""Agent runtime types + tool registry.

Each tool is a Python coroutine that takes:
  - `db`: an open AsyncSession (caller controls the transaction)
  - `ctx`: ToolContext (conversation_id / session_id; future: cancel hooks)
  - `args`: dict from the LLM (already JSON-parsed and schema-validated)

…and returns a JSON-serializable dict that becomes the tool_result content
fed back to the model AND appended to conversations.tool_calls.

A tool registers itself via `@tool(name=..., description=..., schema=...)`
at import time. agent/tools/__init__.py imports the concrete tool modules
to trigger registration.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping, MutableMapping

from sqlalchemy.ext.asyncio import AsyncSession

from library.agent.read_delivery import ReadDelivery
from library.llm import ToolDef


@dataclass(slots=True)
class ToolContext:
    session_id: str
    conversation_id: str
    user_message: str = ""
    # Stateless callers (MCP, legacy probes) have no turn to resume.
    read_delivery: ReadDelivery | None = None
    numeric_evidence: dict[str, dict] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolPolicy:
    """Execution, replay, and concurrency policy for one tool."""

    access: Literal["read", "write"] = "read"
    replay: Literal["safe", "idempotent", "never"] = "safe"
    confirmation: Literal["never", "write", "always"] = "never"
    timeout_seconds: float | None = 120.0
    concurrency: Literal["parallel", "session_serial", "global_serial"] = "parallel"


ToolHandler = Callable[
    [AsyncSession, ToolContext, Mapping[str, Any]],
    Awaitable[dict[str, Any]],
]


@dataclass(slots=True)
class ToolRegistration:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    policy: ToolPolicy = ToolPolicy()


_REGISTRY: MutableMapping[str, ToolRegistration] = {}


def tool(
    *,
    name: str,
    description: str,
    schema: dict[str, Any],
    policy: ToolPolicy | None = None,
) -> Callable[[ToolHandler], ToolHandler]:
    """Register a tool. The handler signature must match ToolHandler."""
    def decorator(fn: ToolHandler) -> ToolHandler:
        if name in _REGISTRY:
            raise RuntimeError(f"tool {name!r} already registered")
        _REGISTRY[name] = ToolRegistration(
            name=name,
            description=description,
            input_schema=schema,
            handler=fn,
            policy=policy or ToolPolicy(),
        )
        return fn
    return decorator


def get_tool(name: str) -> ToolRegistration | None:
    return _REGISTRY.get(name)


def all_tool_defs() -> list[ToolDef]:
    return [
        ToolDef(
            name=_REGISTRY[name].name,
            description=_REGISTRY[name].description,
            input_schema=_canonical_schema(_REGISTRY[name].input_schema),
        )
        for name in sorted(_REGISTRY)
    ]


def _canonical_schema(value: Any) -> Any:
    """Stabilize object-key order without changing array semantics."""
    if isinstance(value, dict):
        return {key: _canonical_schema(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_schema(item) for item in value]
    return value


def registered_tools() -> list[str]:
    return sorted(_REGISTRY)


# Import concrete tool modules to trigger their @tool registrations.
# Order doesn't matter; the LLM picks among them by name.
def _bootstrap() -> None:
    from library.agent.tools import analyze_container  # noqa: F401
    from library.agent.tools import finish_research  # noqa: F401
    from library.agent.tools import generate_chart  # noqa: F401
    from library.agent.tools import list_catalogs  # noqa: F401
    from library.agent.tools import list_folder  # noqa: F401
    from library.agent.tools import materialize_view  # noqa: F401
    from library.agent.tools import query_log  # noqa: F401
    from library.agent.tools import query_sql  # noqa: F401
    from library.agent.tools import read_catalog  # noqa: F401
    from library.agent.tools import read_entries_metadata  # noqa: F401
    from library.agent.tools import read_files  # noqa: F401
    from library.agent.tools import resolve_tag  # noqa: F401
    from library.agent.tools import search_journal  # noqa: F401
    from library.agent.tools import search_metadata  # noqa: F401
    from library.agent.tools import recall_knowledge  # noqa: F401


_bootstrap()
