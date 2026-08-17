"""Minimal stateless MCP client over Streamable HTTP.

Why not use langchain-mcp-adapters / the official mcp SDK?
  - Our in-cluster MCP servers run behind `supergateway` which operates
    in STATELESS mode: it returns NO Mcp-Session-Id header on initialize
    and does not maintain per-session state on the server side.
  - Newer `mcp>=1.9` python client insists on stateful session handling
    and negotiated protocol version pinning that supergateway doesn't
    honor, leading to 400 Bad Request on tools/list.
  - Older `mcp<1.9` doesn't support streamable_http transport at all.
  - The MCP JSON-RPC handshake is trivial. We do it directly with httpx
    and expose each remote tool as a LangChain StructuredTool.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field, create_model

log = logging.getLogger("mcp_httpx")


def _parse_sse_body(text: str) -> dict[str, Any]:
    """Extract the first `data: {...}` JSON payload from an SSE response body."""
    for line in text.splitlines():
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload:
                return json.loads(payload)
    # server may have returned plain JSON
    return json.loads(text)


def _jsonschema_to_pydantic(name: str, schema: dict[str, Any]) -> type[BaseModel]:
    """Best-effort JSON-Schema -> pydantic model conversion for tool args."""
    if not schema or schema.get("type") != "object":
        return create_model(name)
    props: dict[str, Any] = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    type_map = {
        "string": (str, ...),
        "integer": (int, ...),
        "number": (float, ...),
        "boolean": (bool, ...),
        "array": (list, ...),
        "object": (dict, ...),
    }
    fields: dict[str, Any] = {}
    for pname, pdef in props.items():
        ptype_name = pdef.get("type", "string")
        if isinstance(ptype_name, list):  # anyOf-style
            ptype_name = next((t for t in ptype_name if t != "null"), "string")
        py_type, _ = type_map.get(ptype_name, (Any, ...))
        desc = pdef.get("description", "")
        if pname in required:
            fields[pname] = (py_type, Field(..., description=desc))
        else:
            default = pdef.get("default", None)
            fields[pname] = (py_type | None, Field(default=default, description=desc))  # type: ignore[operator]
    return create_model(name, **fields)  # type: ignore[call-overload]


class MCPHTTPClient:
    """One instance per MCP server URL."""

    _ACCEPT = "application/json, text/event-stream"

    def __init__(self, name: str, url: str, headers: dict[str, str] | None = None,
                 timeout: float = 30.0) -> None:
        self.name = name
        self.url = url
        self.base_headers = {
            "Content-Type": "application/json",
            "Accept": self._ACCEPT,
            **(headers or {}),
        }
        self.timeout = timeout
        self._req_id = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def _post(self, client: httpx.AsyncClient, payload: dict[str, Any],
                    extra_headers: dict[str, str] | None = None) -> httpx.Response:
        hdrs = dict(self.base_headers)
        if extra_headers:
            hdrs.update(extra_headers)
        return await client.post(self.url, json=payload, headers=hdrs, timeout=self.timeout)

    async def _handshake(self, client: httpx.AsyncClient) -> dict[str, str]:
        """Do initialize + notifications/initialized. Returns session headers
        (only Mcp-Session-Id if the server issued one; usually empty for
        supergateway stateless servers)."""
        init_resp = await self._post(client, {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "aigw-agent-api", "version": "0.1"},
            },
        })
        init_resp.raise_for_status()
        session_hdrs: dict[str, str] = {}
        sid = init_resp.headers.get("mcp-session-id") or init_resp.headers.get("Mcp-Session-Id")
        if sid:
            session_hdrs["Mcp-Session-Id"] = sid
        # notifications/initialized
        notif_resp = await self._post(client, {
            "jsonrpc": "2.0",
            "method": "notifications/initialized",
        }, extra_headers=session_hdrs)
        # 202 Accepted is expected; ignore body
        if notif_resp.status_code >= 400:
            log.warning("[%s] notifications/initialized returned %d: %s",
                        self.name, notif_resp.status_code, notif_resp.text[:200])
        return session_hdrs

    async def list_tools(self) -> list[dict[str, Any]]:
        async with httpx.AsyncClient() as client:
            sess = await self._handshake(client)
            resp = await self._post(client, {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/list",
            }, extra_headers=sess)
            resp.raise_for_status()
            data = _parse_sse_body(resp.text)
            if "error" in data:
                raise RuntimeError(f"tools/list error: {data['error']}")
            return data.get("result", {}).get("tools", [])

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> str:
        async with httpx.AsyncClient() as client:
            sess = await self._handshake(client)
            resp = await self._post(client, {
                "jsonrpc": "2.0",
                "id": self._next_id(),
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments or {}},
            }, extra_headers=sess)
            resp.raise_for_status()
            data = _parse_sse_body(resp.text)
            if "error" in data:
                return f"[tool error] {data['error']}"
            result = data.get("result", {})
            content = result.get("content", [])
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    if item.get("type") == "text":
                        parts.append(item.get("text", ""))
                    else:
                        parts.append(json.dumps(item))
                else:
                    parts.append(str(item))
            return "\n".join(parts) if parts else json.dumps(result)


async def load_tools_from_servers(
    servers: dict[str, dict[str, Any]],
) -> list[StructuredTool]:
    """servers = { name: {url, headers?} }. Returns LangChain tools."""
    all_tools: list[StructuredTool] = []
    for srv_name, cfg in servers.items():
        client = MCPHTTPClient(name=srv_name, url=cfg["url"], headers=cfg.get("headers"))
        try:
            remote_tools = await client.list_tools()
            log.info("[%s] discovered %d MCP tools", srv_name, len(remote_tools))
        except Exception as exc:
            log.error("[%s] list_tools failed: %s", srv_name, exc)
            continue

        for t in remote_tools:
            t_name = t.get("name", "unknown")
            t_desc = t.get("description", "")[:1000]
            schema = t.get("inputSchema") or {}
            # Prefix tool name with server so names don't collide
            prefixed = f"{srv_name}__{t_name}"
            args_model = _jsonschema_to_pydantic(f"{prefixed}_Args", schema)

            # Bind client + tool_name into the closure
            def _make_runner(_client: MCPHTTPClient, _tool_name: str):
                async def _run(**kwargs: Any) -> str:
                    return await _client.call_tool(_tool_name, kwargs)
                return _run

            all_tools.append(
                StructuredTool.from_function(
                    coroutine=_make_runner(client, t_name),
                    name=prefixed,
                    description=t_desc or f"{srv_name} tool {t_name}",
                    args_schema=args_model,
                )
            )
    return all_tools
