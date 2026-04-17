"""Shared runtime bootstrap for CLI and web."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from lumen.core.brain import Brain
from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry
from lumen.core.consciousness import Consciousness
from lumen.core.discovery import discover_all
from lumen.core.handlers import register_builtin_handlers
from lumen.core.mcp import MCPManager
from lumen.core.memory import Memory
from lumen.core.marketplace import Marketplace
from lumen.core.personality import Personality
from lumen.core.registry import Registry


@dataclass
class RuntimeBootstrap:
    brain: Brain
    locale: dict
    config: dict


def refresh_runtime_registry(
    brain: Brain,
    *,
    pkg_dir: Path,
    active_channels: list[str] | None = None,
) -> Registry:
    """Refresh runtime discovery using the live runtime inputs.

    This preserves the runtime-owned truth (connectors, active MCP state, and
    active channels) instead of rebuilding discovery from installer-local data.
    """
    registry = Registry()
    discover_all(
        registry=registry,
        pkg_dir=pkg_dir,
        connectors=brain.connectors,
        active_channels=active_channels or ["web"],
        mcp_config=(
            brain.mcp_manager.discovery_payload()
            if getattr(brain, "mcp_manager", None)
            else None
        ),
    )
    brain.registry = registry

    if getattr(brain, "marketplace", None) is not None:
        brain.marketplace.sync_registry(registry)

    return registry


async def bootstrap_runtime(
    config: dict,
    *,
    pkg_dir: Path,
    lumen_dir: Path,
    active_channels: list[str] | None = None,
) -> RuntimeBootstrap:
    """Build a full Lumen runtime from config data."""
    active_channels = active_channels or ["web"]

    if config.get("api_key") and config.get("api_key_env"):
        os.environ[config["api_key_env"]] = config["api_key"]

    consciousness = Consciousness()
    lang = config.get("language", "en")
    personality = Personality(pkg_dir / "locales" / lang / "personality.yaml")
    memory = Memory(lumen_dir / "memory.db")

    connectors = ConnectorRegistry()
    built_in_path = pkg_dir / "connectors" / "built-in.yaml"
    if built_in_path.exists():
        connectors.load(built_in_path)
    register_builtin_handlers(connectors, memory)

    mcp_manager = MCPManager(config.get("mcp"))
    await mcp_manager.start(connectors.register_tool)

    registry = Registry()
    discover_all(
        registry=registry,
        pkg_dir=pkg_dir,
        connectors=connectors,
        active_channels=active_channels,
        mcp_config=mcp_manager.discovery_payload(),
    )

    catalog = Catalog()
    marketplace = Marketplace(
        catalog=catalog,
        registry=registry,
        connectors=connectors,
        config=config,
    )
    brain = Brain(
        consciousness=consciousness,
        personality=personality,
        memory=memory,
        connectors=connectors,
        registry=registry,
        catalog=catalog,
        model=config.get("model", "deepseek/deepseek-chat"),
        mcp_manager=mcp_manager,
        marketplace=marketplace,
    )

    flows_dir = pkg_dir / "locales" / lang / "flows"
    brain.load_flows(flows_dir)

    ui_path = pkg_dir / "locales" / lang / "ui.yaml"
    locale = {}
    if ui_path.exists():
        locale = yaml.safe_load(ui_path.read_text(encoding="utf-8")) or {}

    return RuntimeBootstrap(brain=brain, locale=locale, config=config)
