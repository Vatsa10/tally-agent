"""MCP server exposing the tool registry.

Two rules, both enforced here rather than trusted to the client:
  - read tools execute directly;
  - write tools never mutate. They validate, queue, and return an approval
    ticket. An MCP client is another program, not a person, and only a person
    approves a posting.

The `mcp` SDK is an optional dependency (``uv sync --extra mcp``). Everything in
this module except the transport binding works without it, which is what lets
the behaviour be tested without the SDK installed.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
from dataclasses import dataclass
from typing import Any

from tallyagent_core.errors import NotConfiguredError
from tallyagent_tools import registry
from tallyagent_tools.base import ToolContext, ToolResult

log = logging.getLogger(__name__)

SERVER_NAME = "tallyagent"
SERVER_VERSION = "0.1.0"

WRITE_TOOL_NOTE = (
    "\n\nThis is a write tool. Over MCP it does not post to Tally: it validates "
    "the draft and returns an approval-queue ticket. A human approves it in the "
    "tallyagent UI."
)


def list_tools(read_only: bool = False) -> list[dict[str, Any]]:
    """Tool schemas in MCP shape.

    In read-only mode write tools are hidden rather than listed-and-refused:
    a tool that is advertised and always fails wastes a client's turn.
    """
    tools = []
    for tool in registry.TOOLS:
        if read_only and tool.mutating:
            continue
        schema = tool.schema()
        if tool.mutating:
            schema["description"] += WRITE_TOOL_NOTE
        tools.append(schema)
    return tools


@dataclass(slots=True)
class ToolCallOutcome:
    """What an MCP client gets back."""

    text: str
    is_error: bool = False
    ticket: str = ""
    data: Any = None

    def to_content(self) -> list[dict[str, str]]:
        body = self.text
        if self.data is not None:
            body += "\n" + json.dumps(self.data, default=str, indent=2)
        return [{"type": "text", "text": body}]


async def call_tool(
    ctx: ToolContext, name: str, arguments: dict[str, Any]
) -> ToolCallOutcome:
    """Dispatch one MCP tool call."""
    try:
        tool = registry.get(name)
    except KeyError as exc:
        return ToolCallOutcome(str(exc), is_error=True)

    if tool.mutating and ctx.enqueue is None:
        # Refusing beats silently executing: a write tool with nowhere to queue
        # would otherwise fall through to the backend under policy.
        return ToolCallOutcome(
            f"{name} is a write tool but this server has no approval queue "
            "configured, so it cannot accept writes.",
            is_error=True,
        )

    try:
        result: ToolResult = await registry.call(ctx, name, arguments)
    except TypeError as exc:
        return ToolCallOutcome(f"{name}: wrong arguments: {exc}", is_error=True)
    except Exception as exc:  # noqa: BLE001 - report, never crash the server
        log.exception("MCP tool %s failed", name)
        return ToolCallOutcome(f"{name} failed: {type(exc).__name__}: {exc}", is_error=True)

    ticket = ""
    if isinstance(result.data, dict):
        ticket = str(result.data.get("ticket") or "")

    if tool.mutating and not ticket and result.write is not None:
        # Policy auto-approved it. Say so loudly; an MCP client should never be
        # surprised by a mutation it did not know could happen.
        return ToolCallOutcome(
            f"{result.message} (auto-approved by policy, not by a person)",
            is_error=not result.write.ok,
            data=result.data,
        )

    return ToolCallOutcome(
        result.message, is_error=not result.ok, ticket=ticket, data=result.data
    )


# --- transports -------------------------------------------------------------


def bearer_token(explicit: str = "") -> str:
    """HTTP transport token. Env or keyring, never a config file."""
    token = explicit or os.environ.get("TALLYAGENT_MCP_TOKEN", "")
    if not token:
        raise NotConfiguredError(
            "TALLYAGENT_MCP_TOKEN is not set. The streamable-HTTP MCP transport "
            "refuses to start without a bearer token - an unauthenticated MCP "
            "endpoint exposes the whole chart of accounts to the LAN."
        )
    return token


def check_bearer(header: str | None, expected: str) -> bool:
    """Constant-time comparison of an Authorization header."""
    if not header or not header.lower().startswith("bearer "):
        return False
    return secrets.compare_digest(header.split(" ", 1)[1].strip(), expected)


def _require_sdk():  # type: ignore[no-untyped-def]
    try:
        from mcp.server import Server  # type: ignore[import-not-found]
    except ImportError as exc:
        raise NotConfiguredError(
            "the MCP SDK is not installed. Run: uv sync --extra mcp"
        ) from exc
    return Server


def build_server(ctx: ToolContext, read_only: bool = False):  # type: ignore[no-untyped-def]
    """Wire the registry into an `mcp` Server instance.

    The low-level Server is used rather than the tool-decorator API because our
    schemas are hand-written (they are the prompt the model reads) and must not
    be re-derived from Python signatures.
    """
    Server = _require_sdk()
    import mcp.types as types  # type: ignore[import-not-found]

    async def on_list_tools(_context, _params) -> types.ListToolsResult:  # type: ignore[no-untyped-def]
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=schema["name"],
                    description=schema["description"],
                    inputSchema=schema["input_schema"],
                )
                for schema in list_tools(read_only=read_only)
            ]
        )

    async def on_call_tool(_context, params) -> types.CallToolResult:  # type: ignore[no-untyped-def]
        name = params.name
        arguments = dict(params.arguments or {})
        if read_only and name in registry.BY_NAME and registry.BY_NAME[name].mutating:
            return types.CallToolResult(
                content=[
                    types.TextContent(
                        type="text",
                        text=f"{name} is a write tool; this server is read-only.",
                    )
                ],
                is_error=True,
            )
        outcome = await call_tool(ctx, name, arguments)
        return types.CallToolResult(
            content=[
                types.TextContent(type="text", text=block["text"])
                for block in outcome.to_content()
            ],
            is_error=outcome.is_error,
        )

    return Server(
        SERVER_NAME,
        version=SERVER_VERSION,
        on_list_tools=on_list_tools,
        on_call_tool=on_call_tool,
    )


async def run_stdio(ctx: ToolContext, read_only: bool = False) -> None:
    """Serve over stdio - the transport a desktop MCP client uses."""
    from mcp.server.stdio import stdio_server  # type: ignore[import-not-found]

    server = build_server(ctx, read_only=read_only)
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def build_http_app(ctx: ToolContext, token: str = "", read_only: bool = False):  # type: ignore[no-untyped-def]
    """Streamable-HTTP transport behind a bearer-token gate.

    The gate is ASGI middleware rather than anything inside MCP: an
    unauthenticated MCP endpoint hands the whole chart of accounts to the LAN,
    and that must fail before a single MCP frame is parsed.
    """
    expected = bearer_token(token)
    from starlette.responses import JSONResponse

    server = build_server(ctx, read_only=read_only)
    inner = server.streamable_http_app()

    async def guarded(scope, receive, send):  # type: ignore[no-untyped-def]
        if scope["type"] != "http":
            await inner(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if not check_bearer(headers.get("authorization"), expected):
            response = JSONResponse({"error": "unauthorised"}, status_code=401)
            await response(scope, receive, send)
            return
        await inner(scope, receive, send)

    return guarded
