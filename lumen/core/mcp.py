"""Minimal stdio MCP client support for Lumen.

This keeps the runtime small: stdio only, happy path only, no reconnect/auth.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from lumen.core.artifact_setup import (
    contract_from_mcp_server,
    load_mcp_overlay,
    pending_setup_from_contract,
)


JsonDict = dict[str, Any]


class MCPError(RuntimeError):
    """Raised when an MCP server fails during startup or request handling."""


@dataclass
class MCPServerState:
    """Discovery-friendly state for one configured MCP server."""

    server_id: str
    description: str
    command: str | None = None
    args: list[str] = field(default_factory=list)
    cwd: str | None = None
    status: str = "available"
    tools: list[str] = field(default_factory=list)
    error: str | None = None
    display_name: str | None = None
    pending_setup: JsonDict | None = None

    def to_discovery_entry(self) -> JsonDict:
        return {
            "description": self.description,
            "command": self.command,
            "args": self.args,
            "cwd": self.cwd,
            "status": self.status,
            "tools": self.tools,
            "error": self.error,
            "display_name": self.display_name,
            "pending_setup": self.pending_setup,
        }


class StdioMCPServer:
    """Very small MCP stdio client for one subprocess-backed server."""

    def __init__(self, server_id: str, config: JsonDict):
        self.server_id = server_id
        self.config = config
        self.process: asyncio.subprocess.Process | None = None
        self.tools: list[JsonDict] = []
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None
        self._write_lock = asyncio.Lock()

    async def start(self):
        command = self.config.get("command")
        if not command:
            raise MCPError("Missing command")

        args = [str(arg) for arg in self.config.get("args", [])]
        cwd = self.config.get("cwd")
        env = os.environ.copy()
        env.update({str(k): str(v) for k, v in (self.config.get("env") or {}).items()})

        try:
            self.process = await asyncio.create_subprocess_exec(
                str(command),
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(cwd) if cwd else None,
                env=env,
            )
        except Exception as exc:
            raise MCPError(f"Failed to start process: {exc}") from exc

        self._reader_task = asyncio.create_task(self._reader_loop())

        init_result = await self.request(
            "initialize",
            {
                "protocolVersion": self.config.get("protocol_version", "2024-11-05"),
                "capabilities": {},
                "clientInfo": {"name": "lumen", "version": "0.4.2"},
            },
        )
        if init_result.get("error"):
            raise MCPError(init_result["error"].get("message", "Initialize failed"))

        await self.notify("notifications/initialized", {})

        tools_result = await self.request("tools/list", {})
        if tools_result.get("error"):
            raise MCPError(tools_result["error"].get("message", "tools/list failed"))

        self.tools = tools_result.get("result", {}).get("tools", []) or []

    async def stop(self):
        if self._reader_task:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
            self._reader_task = None

        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), timeout=2)
            except asyncio.TimeoutError:
                self.process.kill()
                await self.process.wait()

    async def notify(self, method: str, params: JsonDict | None = None):
        await self._send_message(
            {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
            }
        )

    async def request(
        self,
        method: str,
        params: JsonDict | None = None,
        timeout: float = 10.0,
    ) -> JsonDict:
        if not self.process or not self.process.stdin:
            raise MCPError("Server process is not running")

        self._request_id += 1
        request_id = self._request_id
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self._pending[request_id] = future

        await self._send_message(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params or {},
            }
        )

        try:
            return await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError as exc:
            self._pending.pop(request_id, None)
            raise MCPError(f"Timed out waiting for '{method}'") from exc

    async def call_tool(
        self, tool_name: str, arguments: JsonDict | None = None
    ) -> JsonDict:
        response = await self.request(
            "tools/call",
            {"name": tool_name, "arguments": arguments or {}},
        )
        if response.get("error"):
            return {
                "server": self.server_id,
                "tool": tool_name,
                "is_error": True,
                "error": response["error"],
            }

        result = response.get("result", {})
        content = result.get("content", []) or []
        text_parts = [
            item.get("text", "") for item in content if item.get("type") == "text"
        ]
        payload = {
            "server": self.server_id,
            "tool": tool_name,
            "content": content,
            "structured_content": result.get("structuredContent"),
            "is_error": bool(result.get("isError")),
        }
        if text_parts:
            payload["text"] = "\n".join(part for part in text_parts if part)
        return payload

    async def _send_message(self, payload: JsonDict):
        if not self.process or not self.process.stdin:
            raise MCPError("Server process is not running")

        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")

        async with self._write_lock:
            self.process.stdin.write(header + body)
            await self.process.stdin.drain()

    async def _reader_loop(self):
        if not self.process or not self.process.stdout:
            return

        stdout = self.process.stdout
        while True:
            headers = await stdout.readuntil(b"\r\n\r\n")
            content_length = 0
            for raw_header in headers.decode("ascii", errors="ignore").split("\r\n"):
                if raw_header.lower().startswith("content-length:"):
                    content_length = int(raw_header.split(":", 1)[1].strip())
                    break

            if content_length <= 0:
                continue

            body = await stdout.readexactly(content_length)
            message = json.loads(body.decode("utf-8"))
            request_id = message.get("id")
            if request_id is None:
                continue

            future = self._pending.pop(request_id, None)
            if future and not future.done():
                future.set_result(message)


class MCPManager:
    """Starts configured stdio servers and exposes their tools through Lumen."""

    def __init__(self, mcp_config: JsonDict | None = None, *, pkg_dir: Path | None = None):
        self.mcp_config = mcp_config or {}
        self.pkg_dir = Path(pkg_dir) if pkg_dir is not None else None
        self.servers: dict[str, StdioMCPServer] = {}
        self.server_states: dict[str, MCPServerState] = {}

    async def start(
        self,
        register_tool: Callable[
            [str, str, JsonDict, Callable[..., Awaitable[Any]], JsonDict | None], None
        ],
    ):
        for server_id, config in (self.mcp_config.get("servers") or {}).items():
            if config.get("transport", "stdio") != "stdio" or config.get("disabled"):
                continue

            overlay = load_mcp_overlay(server_id, self.pkg_dir)
            setup_contract = contract_from_mcp_server(
                server_id,
                config,
                overlay=overlay,
            )
            pending_setup = pending_setup_from_contract(setup_contract)
            display_name = (
                (pending_setup or {}).get("display_name")
                or (overlay or {}).get("display_name")
                or config.get("display_name")
                or config.get("description")
                or server_id
            )

            state = MCPServerState(
                server_id=server_id,
                description=config.get("description", f"MCP server: {server_id}"),
                command=config.get("command"),
                args=[str(arg) for arg in config.get("args", [])],
                cwd=config.get("cwd"),
                display_name=str(display_name),
                pending_setup=pending_setup,
            )
            self.server_states[server_id] = state

            if pending_setup and pending_setup.get("env_specs"):
                state.status = "available"
                continue

            server = StdioMCPServer(server_id, config)
            try:
                await server.start()
                state.status = "ready"
                state.tools = [
                    tool.get("name", "") for tool in server.tools if tool.get("name")
                ]
                self.servers[server_id] = server

                for tool in server.tools:
                    remote_tool_name = tool.get("name")
                    if not remote_tool_name:
                        continue
                    lumen_tool_name = f"mcp__{server_id}__{remote_tool_name}"

                    async def handler(
                        _tool_name: str = remote_tool_name,
                        _server_id: str = server_id,
                        **params: Any,
                    ):
                        active_server = self.servers[_server_id]
                        return await active_server.call_tool(_tool_name, params)

                    register_tool(
                        lumen_tool_name,
                        tool.get("description")
                        or f"MCP tool '{remote_tool_name}' from server '{server_id}'",
                        tool.get("inputSchema") or {"type": "object", "properties": {}},
                        handler,
                        {
                            "kind": "mcp",
                            "server_id": server_id,
                            "remote_tool_name": remote_tool_name,
                        },
                    )
            except Exception as exc:
                await server.stop()
                state.status = "error"
                state.error = str(exc)

    async def close(self):
        for server in list(self.servers.values()):
            await server.stop()
        self.servers.clear()

    def discovery_payload(self) -> JsonDict:
        return {
            "servers": {
                server_id: state.to_discovery_entry()
                for server_id, state in self.server_states.items()
            }
        }
