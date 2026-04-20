"""Connectors — logical actions Lumen can execute. Exposed as LLM tools."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Coroutine

import yaml


class Connector:
    """A logical action Lumen can execute. Not tools, not skills — plugs."""

    def __init__(
        self,
        name: str,
        description: str,
        actions: list[str],
    ):
        self.name = name
        self.description = description
        self.actions = actions
        self._handlers: dict[str, Callable[..., Coroutine]] = {}

    def register_handler(self, action: str, handler: Callable[..., Coroutine]):
        self._handlers[action] = handler

    async def execute(self, action: str, params: dict | None = None) -> Any:
        if action not in self.actions:
            raise ValueError(f"Unknown action '{action}' for connector '{self.name}'")
        handler = self._handlers.get(action)
        if handler:
            return await handler(**(params or {}))
        return {
            "status": "ok",
            "connector": self.name,
            "action": action,
            "params": params,
        }


class ConnectorRegistry:
    """Central registry of all connectors. Loads from YAML, exposes as LLM tools.

    Connector → action → result (3 layers, not 5 like Hermes).
    """

    def __init__(self):
        self._connectors: dict[str, Connector] = {}
        self._tool_schemas: dict[str, dict] = {}
        self._explicit_tools: dict[str, dict[str, Any]] = {}
        self._tool_name_map: dict[str, str] = {}

    @staticmethod
    def _sanitize_tool_name(name: str) -> str:
        """Sanitize tool name for providers requiring ^[a-zA-Z0-9_-]+$ (e.g. OpenAI)."""
        return name.replace(".", "__")

    def _resolve_tool_name(self, name: str) -> str:
        """Resolve a possibly-sanitized name back to the original internal name."""
        return self._tool_name_map.get(name, name)

    def set_tool_schemas(self, schemas: dict[str, dict]):
        """Override tool schemas for specific tools (e.g. from handlers)."""
        self._tool_schemas.update(schemas)

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: dict,
        handler: Callable[..., Coroutine],
        metadata: dict | None = None,
    ):
        """Register a standalone tool that still flows through the connector seam."""
        self._explicit_tools[name] = {
            "description": description,
            "parameters": parameters,
            "handler": handler,
            "metadata": metadata or {},
        }

    def load(self, path: str | Path):
        """Load connectors from a YAML file."""
        with open(path, encoding="utf-8") as f:
            configs = yaml.safe_load(f) or []
        for config in configs:
            connector = Connector(
                name=config["name"],
                description=config.get("description", ""),
                actions=config["actions"],
            )
            self._connectors[config["name"]] = connector

    def register(self, connector: Connector):
        self._connectors[connector.name] = connector

    def get(self, name: str) -> Connector | None:
        return self._connectors.get(name)

    def list(self) -> list[dict]:
        return [
            {
                "name": c.name,
                "description": c.description,
                "actions": c.actions,
            }
            for c in self._connectors.values()
        ]

    def as_tools(self) -> list[dict]:
        """Format connectors as LLM function-calling tools (OpenAI format).

        Uses custom schemas from set_tool_schemas() when available,
        falls back to a generic schema otherwise.

        Tool names are sanitized to comply with provider constraints
        (e.g. OpenAI requires ^[a-zA-Z0-9_-]+$). Internal names with
        dots are mapped to double-underscore variants transparently.
        """
        self._tool_name_map = {}
        tools = []
        for connector in self._connectors.values():
            for action in connector.actions:
                tool_name = f"{connector.name}__{action}"

                # Use custom schema if registered, otherwise generic
                custom = self._tool_schemas.get(tool_name)
                if custom:
                    tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "description": custom["description"],
                                "parameters": custom["parameters"],
                            },
                        }
                    )
                else:
                    tools.append(
                        {
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "description": (f"{connector.description} — {action}"),
                                "parameters": {
                                    "type": "object",
                                    "properties": {
                                        "input": {
                                            "type": "string",
                                            "description": f"Input for {connector.name}.{action}",
                                        }
                                    },
                                },
                            },
                        }
                    )
        for name, tool in self._explicit_tools.items():
            sanitized = self._sanitize_tool_name(name)
            if sanitized != name:
                self._tool_name_map[sanitized] = name
            tools.append(
                {
                    "type": "function",
                    "function": {
                        "name": sanitized,
                        "description": tool["description"],
                        "parameters": tool["parameters"],
                    },
                }
            )
        return tools

    def has_tool(self, name: str) -> bool:
        return name in self._explicit_tools or name in self._tool_name_map

    def list_registered_tools(self) -> list[dict]:
        return [
            {
                "name": name,
                "description": tool["description"],
                "metadata": tool["metadata"],
            }
            for name, tool in self._explicit_tools.items()
        ]

    def unregister_tool(self, name: str):
        self._explicit_tools.pop(name, None)
        self._tool_name_map = {
            k: v for k, v in self._tool_name_map.items() if v != name
        }

    def parse_tool_name(self, tool_name: str) -> tuple[str, str]:
        """Parse 'connector__action' back into (connector_name, action)."""
        parts = tool_name.split("__", 1)
        if len(parts) != 2:
            raise ValueError(f"Invalid tool name format: {tool_name}")
        return parts[0], parts[1]

    async def execute(self, name: str, action: str, params: dict | None = None) -> Any:
        connector = self._connectors.get(name)
        if not connector:
            raise ValueError(f"Unknown connector: {name}")
        return await connector.execute(action, params)

    async def execute_tool(self, name: str, params: dict | None = None) -> Any:
        resolved = self._resolve_tool_name(name)
        tool = self._explicit_tools.get(resolved)
        if not tool:
            raise ValueError(f"Unknown tool: {name}")
        return await tool["handler"](**(params or {}))
