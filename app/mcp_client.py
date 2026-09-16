"""Connect to the MCP servers and expose their tools to the agent.

The two servers are launched as stdio subprocesses of this process. Connecting
is **per-server and independently fault-tolerant**: if the weather server fails
to start, its tools are dropped, the failure is recorded in `degraded_tools`,
and the application still answers knowledge-base questions and converts
currency. Requirement 14 of the brief -- handle unavailable tools without
fabricating an answer -- starts here, because a tool that is missing from the
prompt cannot be hallucinated into a fake result.
"""

from __future__ import annotations

import json
import logging
import sys
from dataclasses import dataclass, field
from typing import Any

from langchain_core.tools import BaseTool
from langchain_mcp_adapters.client import MultiServerMCPClient

logger = logging.getLogger(__name__)

#: server name -> module to run with `python -m`. The server name is what the
#: UI shows next to a tool call, so keep it short and human-readable.
SERVER_MODULES: dict[str, str] = {
    "weather": "app.mcp_servers.weather_server",
    "currency": "app.mcp_servers.currency_server",
}

#: What each server provides, for the degraded-mode message shown to the user.
SERVER_CAPABILITIES: dict[str, str] = {
    "weather": "current conditions and weather forecasts",
    "currency": "currency conversion and exchange rates",
}


@dataclass
class McpToolset:
    """The MCP tools that are actually available, plus what is not."""

    tools: list[BaseTool] = field(default_factory=list)
    #: server name -> reason it is unavailable.
    degraded: dict[str, str] = field(default_factory=dict)
    #: server name -> tool names it contributed.
    tools_by_server: dict[str, list[str]] = field(default_factory=dict)

    @property
    def tool_names(self) -> list[str]:
        return [tool.name for tool in self.tools]

    def server_of(self, tool_name: str) -> str | None:
        """Which server a tool came from, for provenance labelling."""
        for server, names in self.tools_by_server.items():
            if tool_name in names:
                return server
        return None

    def degraded_summary(self) -> list[dict[str, str]]:
        """Serialisable form for the API and the UI banner."""
        return [
            {
                "server": server,
                "capability": SERVER_CAPABILITIES.get(server, server),
                "reason": reason,
            }
            for server, reason in sorted(self.degraded.items())
        ]

    def prompt_note(self) -> str:
        """A line for the system prompt naming what is currently unavailable.

        Telling the model which capability is missing is what lets it say "I
        cannot check the forecast right now" instead of improvising one.
        """
        if not self.degraded:
            return ""
        parts = [
            f"{SERVER_CAPABILITIES.get(server, server)} ({server} tool unavailable)"
            for server in sorted(self.degraded)
        ]
        return (
            "TOOL AVAILABILITY: the following live-information capabilities are "
            "currently unavailable: " + "; ".join(parts) + ". If the user asks "
            "for any of it, say plainly that it cannot be retrieved right now. "
            "Never estimate or recall it."
        )


def _describe_failure(exc: BaseException) -> str:
    """Flatten an exception into something a user can act on.

    The MCP client runs each server in an anyio task group, so a server that
    fails to start surfaces as `ExceptionGroup: unhandled errors in a
    TaskGroup (1 sub-exception)` -- true but useless. Unwrap to the innermost
    real cause.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    message = str(exc).strip()
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__


def _connection(module: str) -> dict:
    """A stdio connection spec for one server.

    `sys.executable` is used rather than a bare "python" so the subprocess runs
    in this virtual environment regardless of PATH.
    """
    return {
        "command": sys.executable,
        "args": ["-m", module],
        "transport": "stdio",
    }


async def connect() -> McpToolset:
    """Start the MCP servers and collect their tools.

    Each server is connected separately so that one broken server cannot take
    the others down with it.
    """
    toolset = McpToolset()

    for server_name, module in SERVER_MODULES.items():
        client = MultiServerMCPClient({server_name: _connection(module)})
        try:
            tools = await client.get_tools(server_name=server_name)
        except Exception as exc:
            reason = _describe_failure(exc)
            logger.warning("MCP server %r unavailable: %s", server_name, reason)
            toolset.degraded[server_name] = reason
            continue

        if not tools:
            toolset.degraded[server_name] = "server started but exposed no tools"
            logger.warning("MCP server %r exposed no tools", server_name)
            continue

        toolset.tools.extend(tools)
        toolset.tools_by_server[server_name] = [tool.name for tool in tools]
        logger.info(
            "MCP server %r provided %d tools: %s",
            server_name,
            len(tools),
            ", ".join(tool.name for tool in tools),
        )

    return toolset


def parse_tool_payload(result: Any) -> dict[str, Any]:
    """Pull our `{ok, source, retrieved_at, data|error}` envelope back out.

    The adapter hands the same payload back in three shapes depending on how a
    tool was invoked, so provenance extraction has one place to deal with it:

    * a `ToolMessage` whose `artifact["structured_content"]` is the dict -- the
      shape the agent sees, and the cheapest to read;
    * a list of MCP content blocks, each `{"type": "text", "text": <json>}`;
    * a bare JSON string.

    Returns `{}` when nothing parseable is present, so callers can treat an
    unreadable result as "no provenance" rather than crashing a chat turn.
    """
    artifact = getattr(result, "artifact", None)
    if isinstance(artifact, dict):
        structured = artifact.get("structured_content")
        if isinstance(structured, dict):
            return structured

    content = getattr(result, "content", result)

    if isinstance(content, list):
        for block in content:
            text = (
                block.get("text")
                if isinstance(block, dict)
                else getattr(block, "text", None)
            )
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    if isinstance(content, str):
        try:
            parsed = json.loads(content)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    if isinstance(content, dict):
        return content
    return {}


def server_of_or_none(toolset: McpToolset, tool_name: str) -> str | None:
    """Convenience wrapper for callers that only have the toolset and a name."""
    return toolset.server_of(tool_name)


def describe(toolset: McpToolset) -> str:
    """Human-readable startup summary, so 'the tools are available' is visible."""
    lines = []
    for server, names in sorted(toolset.tools_by_server.items()):
        lines.append(f"  {server}: {', '.join(names)}")
    for server, reason in sorted(toolset.degraded.items()):
        lines.append(f"  {server}: UNAVAILABLE -- {reason}")
    if not lines:
        lines.append("  (no MCP servers reachable)")
    return "\n".join(lines)
