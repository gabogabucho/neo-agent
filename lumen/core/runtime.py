"""Shared runtime bootstrap for CLI and web."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from lumen.core.awareness import CapabilityAwareness
from lumen.core.brain import Brain
from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry
from lumen.core.consciousness import Consciousness
from lumen.core.discovery import discover_all
from lumen.core.handlers import register_builtin_handlers
from lumen.core.mcp import MCPManager
from lumen.core.memory import Memory
from lumen.core.module_runtime import ModuleRuntimeManager
from lumen.core.marketplace import Marketplace
from lumen.core.module_manifest import load_module_manifest
from lumen.core.personality import Personality
from lumen.core.registry import Registry


@dataclass
class RuntimeBootstrap:
    brain: Brain
    locale: dict
    config: dict
    awareness: CapabilityAwareness | None = None


def refresh_runtime_registry(
    brain: Brain,
    *,
    pkg_dir: Path,
    active_channels: list[str] | None = None,
) -> Registry:
    """Refresh runtime discovery using the live runtime inputs.

    This preserves the runtime-owned truth (connectors, active MCP state, and
    active channels) instead of rebuilding discovery from installer-local data.

    Re-subscribes CapabilityAwareness to the new registry so events keep flowing.
    """
    old_registry = brain.registry
    registry = Registry()
    discover_all(
        registry=registry,
        pkg_dir=pkg_dir,
        connectors=brain.connectors,
        active_channels=active_channels or ["web"],
        model=getattr(brain, "model", None),
        mcp_config=(
            brain.mcp_manager.discovery_payload()
            if getattr(brain, "mcp_manager", None)
            else None
        ),
    )
    brain.registry = registry

    # Re-attach awareness subscription to the new registry
    if brain.capability_awareness:
        registry.subscribe(brain.capability_awareness._on_registry_event)

    if getattr(brain, "marketplace", None) is not None:
        brain.marketplace.sync_registry(registry)

    return registry


async def sync_runtime_modules(
    brain: Brain,
    *,
    config: dict,
    pkg_dir: Path,
    lumen_dir: Path,
) -> None:
    manager = getattr(brain, "module_manager", None)
    if not isinstance(manager, ModuleRuntimeManager):
        manager = ModuleRuntimeManager(
            pkg_dir=pkg_dir,
            lumen_dir=lumen_dir,
            config=config,
            connectors=brain.connectors,
            memory=brain.memory,
            brain=brain,
        )
        brain.module_manager = manager
    else:
        manager.config = config
        manager.brain = brain

    await manager.sync()


def reload_runtime_personality_surface(
    brain: Brain,
    *,
    config: dict,
    pkg_dir: Path,
) -> None:
    """Reload only the personality + flows surface for the live runtime."""
    lang = config.get("language", "en")
    locale_personality_path = pkg_dir / "locales" / lang / "personality.yaml"
    active_personality_module = _resolve_active_personality_module(config, pkg_dir)
    personality_path = _resolve_personality_path(
        active_personality_module, locale_personality_path
    )

    brain.personality = Personality(personality_path)
    brain.flows = []

    flows_dir = pkg_dir / "locales" / lang / "flows"
    brain.load_flows(flows_dir)

    onboarding_flow_path = _resolve_module_onboarding_flow(active_personality_module)
    if onboarding_flow_path is not None:
        brain.load_flows(onboarding_flow_path)


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
    locale_personality_path = pkg_dir / "locales" / lang / "personality.yaml"
    active_personality_module = _resolve_active_personality_module(
        config, pkg_dir, lumen_dir
    )
    personality_path = _resolve_personality_path(
        active_personality_module, locale_personality_path
    )
    personality = Personality(personality_path)
    memory = Memory(lumen_dir / "memory.db")

    connectors = ConnectorRegistry()
    built_in_path = pkg_dir / "connectors" / "built-in.yaml"
    if built_in_path.exists():
        connectors.load(built_in_path)
    register_builtin_handlers(connectors, memory)

    mcp_manager = MCPManager(config.get("mcp"))
    await mcp_manager.start(connectors.register_tool)

    module_manager = ModuleRuntimeManager(
        pkg_dir=pkg_dir,
        lumen_dir=lumen_dir,
        config=config,
        connectors=connectors,
        memory=memory,
    )
    await module_manager.sync()

    registry = Registry()
    discover_all(
        registry=registry,
        pkg_dir=pkg_dir,
        connectors=connectors,
        active_channels=active_channels,
        model=config.get("model"),
        mcp_config=mcp_manager.discovery_payload(),
    )

    awareness = CapabilityAwareness(registry)

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
        capability_awareness=awareness,
    )
    brain.module_manager = module_manager
    module_manager.brain = brain

    flows_dir = pkg_dir / "locales" / lang / "flows"
    brain.load_flows(flows_dir)
    onboarding_flow_path = _resolve_module_onboarding_flow(active_personality_module)
    if onboarding_flow_path is not None:
        brain.load_flows(onboarding_flow_path)

    ui_path = pkg_dir / "locales" / lang / "ui.yaml"
    locale = {}
    if ui_path.exists():
        locale = yaml.safe_load(ui_path.read_text(encoding="utf-8")) or {}

    return RuntimeBootstrap(brain=brain, locale=locale, config=config, awareness=awareness)


def _resolve_active_personality_module(
    config: dict, pkg_dir: Path, lumen_dir: Path | None = None
) -> dict | None:
    module_name = config.get("active_personality")

    if lumen_dir is not None:
        config_path = lumen_dir / "config.yaml"
        if config_path.exists():
            try:
                import yaml

                disk_config = (
                    yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
                )
                if "active_personality" in disk_config:
                    module_name = disk_config["active_personality"]
                    config["active_personality"] = module_name
            except Exception as e:
                import logging

                logging.warning(
                    f"Failed to load active_personality from disk config: {e}"
                )

    if not module_name:
        return None

    module_dir = pkg_dir / "modules" / str(module_name)
    manifest_path, manifest = load_module_manifest(module_dir)
    if manifest_path is None:
        return None

    tags = manifest.get("tags") or []
    if "personality" not in tags:
        return None

    return {"dir": module_dir, "manifest": manifest}


def _resolve_personality_path(
    active_module: dict | None, locale_personality_path: Path
) -> Path:
    if active_module is None:
        return locale_personality_path

    personality_path = _resolve_module_asset_path(
        active_module["dir"], active_module["manifest"].get("personality")
    )
    return personality_path or locale_personality_path


def _resolve_module_onboarding_flow(active_module: dict | None) -> Path | None:
    if active_module is None:
        return None

    return _resolve_module_asset_path(
        active_module["dir"], active_module["manifest"].get("onboarding_flow")
    )


def _resolve_module_asset_path(
    module_dir: Path, relative_path: str | None
) -> Path | None:
    if not relative_path:
        return None

    candidate = (module_dir / relative_path).resolve()
    module_root = module_dir.resolve()
    if candidate != module_root and module_root not in candidate.parents:
        return None

    return candidate if candidate.exists() else None
