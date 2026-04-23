"""Shared runtime bootstrap for CLI and web."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from lumen.core.awareness import CapabilityAwareness
from lumen.core.artifact_setup import collect_pending_artifact_setup_flows
from lumen.core.secrets_store import migrate_from_config
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
    integration_summary: dict | None = None


def refresh_runtime_registry(
    brain: Brain,
    *,
    pkg_dir: Path,
    lumen_dir: Path | None = None,
    active_channels: list[str] | None = None,
) -> Registry:
    """Refresh runtime discovery using the live runtime inputs.

    This preserves the runtime-owned truth (connectors, active MCP state, and
    active channels) instead of rebuilding discovery from installer-local data.

    Re-subscribes CapabilityAwareness to the new registry so events keep flowing.
    """
    old_registry = brain.registry
    previous_snapshot = old_registry.snapshot() if old_registry is not None else {}
    registry = Registry()
    discover_all(
        registry=registry,
        pkg_dir=pkg_dir,
        connectors=brain.connectors,
        active_channels=active_channels or ["web"],
        model=getattr(brain, "model", None),
        config=getattr(brain, "module_manager", None).config
        if getattr(brain, "module_manager", None)
        else None,
        lumen_dir=lumen_dir,
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
        brain.capability_awareness.ingest_snapshot_diff(previous_snapshot, registry.snapshot())

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
    lumen_dir: Path | None = None,
) -> None:
    """Reload only the personality + flows surface for the live runtime."""
    lang = config.get("language", "en")
    locale_personality_path = pkg_dir / "locales" / lang / "personality.yaml"
    active_personality_module = _resolve_active_personality_module(config, pkg_dir, lumen_dir)
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
    _load_pending_artifact_setup_flows(brain, pkg_dir=pkg_dir, config=config, lumen_dir=lumen_dir)


async def bootstrap_runtime(
    config: dict,
    *,
    pkg_dir: Path,
    lumen_dir: Path,
    active_channels: list[str] | None = None,
) -> RuntimeBootstrap:
    """Build a full Lumen runtime from config data."""
    active_channels = active_channels or ["web"]

    # Migrate secrets from config.yaml to secrets.yaml (one-time)
    config, _migrated = _migrate_secrets(config)

    # Hydrate config["secrets"] from the store so resolve_setting works
    from lumen.core.secrets_store import load_all as _load_all_secrets
    all_secrets = _load_all_secrets()
    if all_secrets:
        existing = config.get("secrets") or {}
        if not isinstance(existing, dict):
            existing = {}
        for mod_name, bucket in all_secrets.items():
            if isinstance(bucket, dict):
                existing.setdefault(mod_name, {}).update(bucket)
        config["secrets"] = existing

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

    mcp_manager = MCPManager(config.get("mcp"), pkg_dir=pkg_dir)
    await mcp_manager.start(connectors.register_tool)

    registry = Registry()
    discover_all(
        registry=registry,
        pkg_dir=pkg_dir,
        connectors=connectors,
        active_channels=active_channels,
        model=config.get("model"),
        config=config,
        lumen_dir=lumen_dir,
        mcp_config=mcp_manager.discovery_payload(),
    )

    awareness = CapabilityAwareness(registry)
    awareness.ingest_snapshot_diff({}, registry.snapshot())

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
        language=config.get("language", "en"),
        api_key_env=config.get("api_key_env"),
        config=config,
    )

    from lumen.core.inbox import Inbox
    brain.inbox = Inbox()

    module_manager = ModuleRuntimeManager(
        pkg_dir=pkg_dir,
        lumen_dir=lumen_dir,
        config=config,
        connectors=connectors,
        memory=memory,
        brain=brain,
    )
    brain.module_manager = module_manager
    await module_manager.sync()

    flows_dir = pkg_dir / "locales" / lang / "flows"
    brain.load_flows(flows_dir)
    onboarding_flow_path = _resolve_module_onboarding_flow(active_personality_module)
    if onboarding_flow_path is not None:
        brain.load_flows(onboarding_flow_path)
    _load_pending_artifact_setup_flows(brain, pkg_dir=pkg_dir, config=config, lumen_dir=lumen_dir)

    ui_path = pkg_dir / "locales" / lang / "ui.yaml"
    locale = {}
    if ui_path.exists():
        locale = yaml.safe_load(ui_path.read_text(encoding="utf-8")) or {}

    return RuntimeBootstrap(
        brain=brain,
        locale=locale,
        config=config,
        awareness=awareness,
        integration_summary=awareness.peek_summary(),
    )


def _migrate_secrets(config: dict) -> tuple[dict, list[str]]:
    """One-time migration of secrets from config.yaml to secrets.yaml."""
    config, migrated = migrate_from_config(config)
    if migrated:
        from pathlib import Path
        config_path = Path.home() / ".lumen" / "config.yaml"
        if config_path.exists():
            config_path.write_text(
                yaml.dump(config, default_flow_style=False),
                encoding="utf-8",
            )
    return config, migrated


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

    module_roots = []
    if lumen_dir is not None:
        module_roots.append(lumen_dir / "modules")
    module_roots.append(pkg_dir / "modules")

    module_dir = None
    manifest = None
    manifest_path = None
    for root in module_roots:
        candidate = root / str(module_name)
        candidate_manifest_path, candidate_manifest = load_module_manifest(candidate)
        if candidate_manifest_path is not None:
            module_dir = candidate
            manifest = candidate_manifest
            manifest_path = candidate_manifest_path
            break
    if manifest_path is None or module_dir is None or manifest is None:
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


def _load_pending_artifact_setup_flows(
    brain: Brain,
    *,
    pkg_dir: Path,
    config: dict,
    lumen_dir: Path | None = None,
) -> None:
    brain.flows.extend(collect_pending_artifact_setup_flows(pkg_dir, config, lumen_dir=lumen_dir))
