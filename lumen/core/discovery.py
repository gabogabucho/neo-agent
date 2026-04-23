"""Discovery — scans the filesystem and registers everything with Lumen's consciousness.

On startup (and optionally on interval), discovery:
1. Scans skills/ for SKILL.md files → parse frontmatter → register
2. Scans connectors/ for YAML → parse → register (with handler status)
3. Scans modules/ for module.yaml (fallback manifest.yaml) → parse → register
4. Checks channels → register active ones
5. Checks MCP servers from config → register (available or error)

Everything that exists must self-declare. Discovery reads the declarations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from lumen.core.artifact_setup import (
    contract_from_mcp_server,
    contract_from_opaque_manifest,
    load_mcp_overlay,
    pending_setup_from_contract,
)
from lumen.core.cerebellum import (
    annotate_registry,
    normalize_agent_skill,
    normalize_module_manifest,
)
from lumen.core.connectors import ConnectorRegistry
from lumen.core.module_manifest import load_module_manifest
from lumen.core.module_setup import pending_setup_for_manifest
from lumen.core.registry import (
    Capability,
    CapabilityKind,
    CapabilityStatus,
    Registry,
)


def discover_all(
    registry: Registry,
    pkg_dir: Path,
    connectors: ConnectorRegistry,
    active_channels: list[str] | None = None,
    mcp_config: dict | None = None,
    model: str | None = None,
    config: dict | None = None,
    lumen_dir: Path | None = None,
) -> Registry:
    """Run full discovery and populate the registry."""
    # Built-in skills
    _discover_skills(registry, pkg_dir / "skills")

    # Skills inside installed modules (each module can have a SKILL.md
    # and/or declare additional skill files in module.yaml -> skills: [...])
    module_roots: list[Path] = []
    if lumen_dir is not None:
        module_roots.append(lumen_dir / "modules")
    module_roots.append(pkg_dir / "modules")

    seen_module_names: set[str] = set()
    for modules_dir in module_roots:
        if not modules_dir.exists():
            continue
        for module_dir in modules_dir.iterdir():
            if not module_dir.is_dir() or module_dir.name.startswith("_"):
                continue
            if module_dir.name in seen_module_names:
                continue
            seen_module_names.add(module_dir.name)
            skill_file = module_dir / "SKILL.md"
            if skill_file.exists():
                _discover_skill_file(registry, skill_file, module_dir.name, module_name=module_dir.name)
            _discover_declared_module_skills(registry, module_dir)

    _discover_connectors(registry, connectors)
    _discover_modules_multi(registry, module_roots, config=config)
    _discover_channels(registry, active_channels or ["web"])
    _discover_module_channels_multi(registry, module_roots)

    if mcp_config:
        _discover_mcps(registry, mcp_config, pkg_dir=pkg_dir)

    # Second pass: validate skill dependencies
    # If a skill requires a connector that has no handler, mark it as MISSING_DEPS
    _validate_skill_deps(registry)
    annotate_registry(registry, connectors, model=model)

    return registry


def _discover_skill_file(
    registry: Registry,
    skill_file: Path,
    fallback_name: str,
    *,
    module_name: str | None = None,
):
    """Discover a single SKILL.md file."""
    try:
        frontmatter = _parse_frontmatter(skill_file)
        normalized = normalize_agent_skill(frontmatter, path=str(skill_file))
        name = normalized.name or fallback_name

        # Don't register if already exists (built-in takes priority)
        if registry.get(CapabilityKind.SKILL, name):
            return

        registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name=name,
                description=normalized.description,
                status=CapabilityStatus.READY,
                provides=normalized.provides,
                requires=normalized.requires,
                min_capability=normalized.metadata.get("min_capability", "tier-1"),
                metadata={
                    "level": normalized.metadata.get("level", 1),
                    "path": str(skill_file),
                    "module_name": module_name,
                    "aliases": ([f"{module_name}/{name}"] if module_name else []),
                    "interoperability": normalized.metadata.get("interoperability"),
                },
            )
        )
    except Exception:
        pass


def _discover_skills(registry: Registry, skills_dir: Path):
    """Scan skills/ directory for SKILL.md files."""
    if not skills_dir.exists():
        return

    for skill_dir in skills_dir.iterdir():
        if not skill_dir.is_dir():
            continue

        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue

        try:
            frontmatter = _parse_frontmatter(skill_file)
            normalized = normalize_agent_skill(frontmatter, path=str(skill_file))
            name = normalized.name or skill_dir.name
            description = normalized.description
            requires = normalized.requires
            level = normalized.metadata.get("level", 1)

            registry.register(
                Capability(
                    kind=CapabilityKind.SKILL,
                    name=name,
                    description=description,
                    status=CapabilityStatus.READY,
                    provides=normalized.provides,
                    requires=requires,
                    min_capability=normalized.metadata.get("min_capability", "tier-1"),
                    metadata={
                        "level": level,
                        "path": str(skill_file),
                        "interoperability": normalized.metadata.get(
                            "interoperability"
                        ),
                    },
                )
            )
        except Exception:
            registry.register(
                Capability(
                    kind=CapabilityKind.SKILL,
                    name=skill_dir.name,
                    description="Failed to parse",
                    status=CapabilityStatus.ERROR,
                )
            )


def _discover_declared_module_skills(registry: Registry, module_dir: Path):
    """Discover skill markdown files declared in module.yaml -> skills: [...]."""
    try:
        manifest_path, manifest = load_module_manifest(module_dir)
        if manifest_path is None or not isinstance(manifest, dict):
            return
        declared = manifest.get("skills", [])
        if not isinstance(declared, list):
            return
        for rel_path in declared:
            if not rel_path or not isinstance(rel_path, str):
                continue
            skill_file = module_dir / rel_path
            if skill_file.exists() and skill_file.is_file():
                _discover_skill_file(registry, skill_file, skill_file.stem, module_name=module_dir.name)
    except Exception:
        pass


def _validate_skill_deps(registry: Registry):
    """Check if skills have their required connectors ready.

    A skill that requires connectors: [web] but web has no handler
    should NOT be listed as READY — it would mislead the LLM.
    """
    for cap in registry.list_by_kind(CapabilityKind.SKILL):
        if cap.status != CapabilityStatus.READY:
            continue
        required_connectors = cap.requires.get("connectors", [])
        if not required_connectors:
            continue
        for conn_name in required_connectors:
            conn = registry.get(CapabilityKind.CONNECTOR, conn_name)
            if conn and not conn.is_ready():
                cap.status = CapabilityStatus.MISSING_DEPS
                break


def _discover_connectors(registry: Registry, connectors: ConnectorRegistry):
    """Register connectors with handler status awareness."""
    for conn_info in connectors.list():
        name = conn_info["name"]
        connector = connectors.get(name)
        if not connector:
            continue

        # Check if connector has real handlers registered
        has_handlers = bool(connector._handlers)

        status = (
            CapabilityStatus.READY if has_handlers else CapabilityStatus.MISSING_HANDLER
        )

        registry.register(
            Capability(
                kind=CapabilityKind.CONNECTOR,
                name=name,
                description=conn_info.get("description", ""),
                status=status,
                provides=conn_info["actions"],
                metadata={"actions": conn_info["actions"]},
            )
        )


def _discover_modules(
    registry: Registry,
    modules_dir: Path,
    *,
    config: dict | None = None,
):
    """Scan modules/ for module.yaml files, with manifest.yaml fallback."""
    if not modules_dir.exists():
        return

    for module_dir in modules_dir.iterdir():
        if not module_dir.is_dir() or module_dir.name.startswith("_"):
            continue

        try:
            manifest_file, manifest = load_module_manifest(module_dir)
            if manifest_file is None:
                continue
            normalized = normalize_module_manifest(
                manifest,
                installed=True,
                manifest_path=str(manifest_file),
            )
            name = normalized.name or module_dir.name
            pending_setup = pending_setup_for_manifest(
                name,
                manifest,
                config,
                module_dir=module_dir,
            )

            # If no env-based pending setup, check for manual setup instructions
            if pending_setup is None:
                manual_contract = contract_from_opaque_manifest(name, manifest)
                if manual_contract and manual_contract.is_manual_only():
                    pending_setup = {
                        "kind": "manual",
                        "artifact_id": name,
                        "display_name": manual_contract.display_name,
                        "env_specs": [],
                        "flow": None,
                        "manual_instructions": manual_contract.manual_instructions,
                    }

            # Module is installed — ready if it has a root SKILL.md or declared skills that exist
            has_skill = (module_dir / "SKILL.md").exists()
            if not has_skill:
                declared_skills = manifest.get("skills", [])
                if isinstance(declared_skills, list):
                    has_skill = any(
                        isinstance(rel, str) and (module_dir / rel).exists()
                        for rel in declared_skills
                    )
            status = (
                CapabilityStatus.READY
                if has_skill and not pending_setup
                else CapabilityStatus.AVAILABLE
            )

            registry.register(
                Capability(
                    kind=CapabilityKind.MODULE,
                    name=name,
                    description=normalized.description,
                    status=status,
                    provides=normalized.provides,
                    requires=normalized.requires,
                    metadata={
                        "display_name": manifest.get("display_name", name),
                        "version": manifest.get("version", "0.0.0"),
                        "author": manifest.get("author", ""),
                        "path": str(module_dir),
                        "tags": normalized.metadata.get("tags", []),
                        "min_capability": manifest.get("min_capability", "tier-1"),
                        "manifest_path": str(manifest_file),
                        "interoperability": normalized.metadata.get(
                            "interoperability"
                        ),
                        "schema_aliases": normalized.metadata.get("schema_aliases", {}),
                        "x_lumen": normalized.metadata.get("x_lumen", {}),
                        "pending_setup": pending_setup,
                    },
                )
            )
        except Exception:
            registry.register(
                Capability(
                    kind=CapabilityKind.MODULE,
                    name=module_dir.name,
                    description="Failed to parse manifest",
                    status=CapabilityStatus.ERROR,
                )
            )


def _discover_modules_multi(
    registry: Registry,
    module_roots: list[Path],
    *,
    config: dict | None = None,
):
    seen: set[str] = set()
    for modules_dir in module_roots:
        if not modules_dir.exists():
            continue
        for module_dir in modules_dir.iterdir():
            if not module_dir.is_dir() or module_dir.name.startswith("_") or module_dir.name in seen:
                continue
            seen.add(module_dir.name)
            try:
                manifest_file, manifest = load_module_manifest(module_dir)
                if manifest_file is None:
                    continue
                normalized = normalize_module_manifest(
                    manifest,
                    installed=True,
                    manifest_path=str(manifest_file),
                )
                name = normalized.name or module_dir.name
                pending_setup = pending_setup_for_manifest(
                    name,
                    manifest,
                    config,
                    module_dir=module_dir,
                )
                if pending_setup is None:
                    manual_contract = contract_from_opaque_manifest(name, manifest)
                    if manual_contract and manual_contract.is_manual_only():
                        pending_setup = {
                            "kind": "manual",
                            "artifact_id": name,
                            "display_name": manual_contract.display_name,
                            "env_specs": [],
                            "flow": None,
                            "manual_instructions": manual_contract.manual_instructions,
                        }
                has_skill = (module_dir / "SKILL.md").exists()
                if not has_skill:
                    declared_skills = manifest.get("skills", [])
                    if isinstance(declared_skills, list):
                        has_skill = any(
                            isinstance(rel, str) and (module_dir / rel).exists()
                            for rel in declared_skills
                        )
                status = CapabilityStatus.READY if has_skill and not pending_setup else CapabilityStatus.AVAILABLE
                registry.register(
                    Capability(
                        kind=CapabilityKind.MODULE,
                        name=name,
                        description=normalized.description,
                        status=status,
                        provides=normalized.provides,
                        requires=normalized.requires,
                        metadata={
                            "display_name": manifest.get("display_name", name),
                            "version": manifest.get("version", "0.0.0"),
                            "author": manifest.get("author", ""),
                            "path": str(module_dir),
                            "tags": normalized.metadata.get("tags", []),
                            "min_capability": manifest.get("min_capability", "tier-1"),
                            "manifest_path": str(manifest_file),
                            "interoperability": normalized.metadata.get("interoperability"),
                            "schema_aliases": normalized.metadata.get("schema_aliases", {}),
                            "x_lumen": normalized.metadata.get("x_lumen", {}),
                            "pending_setup": pending_setup,
                        },
                    )
                )
            except Exception:
                registry.register(
                    Capability(
                        kind=CapabilityKind.MODULE,
                        name=module_dir.name,
                        description="Failed to parse manifest",
                        status=CapabilityStatus.ERROR,
                    )
                )


def _discover_channels(registry: Registry, active_channels: list[str]):
    """Register active channels."""
    channel_descriptions = {
        "web": "Web dashboard with real-time chat",
        "whatsapp": "WhatsApp via Evolution API",
        "telegram": "Telegram Bot API",
        "api": "REST API endpoint",
    }

    for channel in active_channels:
        registry.register(
            Capability(
                kind=CapabilityKind.CHANNEL,
                name=channel,
                description=channel_descriptions.get(channel, channel),
                status=CapabilityStatus.READY,
            )
        )


def _discover_module_channels(registry: Registry, modules_dir: Path):
    """Register external channels declared by installed modules.

    A module can declare:
      provides: [channel.web-app]
      x-lumen:
        channel:
          type: web-app
          auth: rest-api
          cors: [...]
    """
    if not modules_dir.exists():
        return

    for module_dir in modules_dir.iterdir():
        if not module_dir.is_dir() or module_dir.name.startswith("_"):
            continue
        try:
            manifest_path, manifest = load_module_manifest(module_dir)
            if manifest_path is None or not isinstance(manifest, dict):
                continue

            provides = manifest.get("provides", []) or []
            x_lumen = manifest.get("x-lumen", {}) or {}
            channel_meta = x_lumen.get("channel", {}) if isinstance(x_lumen, dict) else {}

            declared_channel = any(
                isinstance(item, str) and item.startswith("channel.")
                for item in provides
            )
            if not declared_channel or not isinstance(channel_meta, dict):
                continue

            channel_name = str(manifest.get("name") or module_dir.name)
            if registry.get(CapabilityKind.CHANNEL, channel_name):
                continue

            has_runtime_skill = (module_dir / "SKILL.md").exists()
            declared_skills = manifest.get("skills", [])
            if not has_runtime_skill and isinstance(declared_skills, list):
                has_runtime_skill = any(
                    isinstance(rel, str) and (module_dir / rel).exists()
                    for rel in declared_skills
                )

            registry.register(
                Capability(
                    kind=CapabilityKind.CHANNEL,
                    name=channel_name,
                    description=str(manifest.get("description") or f"External channel from {module_dir.name}"),
                    status=(CapabilityStatus.READY if has_runtime_skill else CapabilityStatus.AVAILABLE),
                    provides=[str(p) for p in provides if isinstance(p, str) and p.startswith("channel.")],
                    metadata={
                        "source_module": str(manifest.get("name") or module_dir.name),
                        "channel_type": channel_meta.get("type"),
                        "auth": channel_meta.get("auth"),
                        "cors": channel_meta.get("cors", []),
                        "response_format": channel_meta.get("response_format"),
                        "path": str(module_dir),
                    },
                )
            )
        except Exception:
            pass


def _discover_module_channels_multi(registry: Registry, module_roots: list[Path]):
    seen: set[str] = set()
    for modules_dir in module_roots:
        if not modules_dir.exists():
            continue
        for module_dir in modules_dir.iterdir():
            if not module_dir.is_dir() or module_dir.name.startswith("_") or module_dir.name in seen:
                continue
            seen.add(module_dir.name)
        _discover_module_channels(registry, modules_dir)


def _discover_mcps(registry: Registry, mcp_config: dict, *, pkg_dir: Path | None = None):
    """Register MCP servers from config."""
    servers = mcp_config.get("servers", {})
    for name, config in servers.items():
        overlay = load_mcp_overlay(name, pkg_dir)
        pending_setup = config.get("pending_setup")
        if not pending_setup:
            pending_setup = pending_setup_from_contract(
                contract_from_mcp_server(name, config, overlay=overlay)
            )

        status_name = config.get("status", CapabilityStatus.AVAILABLE.value)
        if pending_setup and pending_setup.get("env_specs"):
            status_name = CapabilityStatus.AVAILABLE.value
        try:
            status = CapabilityStatus(status_name)
        except ValueError:
            status = CapabilityStatus.ERROR

        registry.register(
            Capability(
                kind=CapabilityKind.MCP,
                name=name,
                description=config.get("description", f"MCP server: {name}"),
                status=status,
                provides=config.get("tools", []),
                metadata={
                    "display_name": config.get("display_name")
                    or (pending_setup or {}).get("display_name")
                    or (overlay or {}).get("display_name")
                    or name,
                    "command": config.get("command"),
                    "args": config.get("args", []),
                    "url": config.get("url"),
                    "tools": config.get("tools", []),
                    "error": config.get("error"),
                    "pending_setup": pending_setup,
                },
            )
        )


def _parse_frontmatter(path: Path) -> dict:
    """Parse YAML frontmatter from a markdown file."""
    content = path.read_text(encoding="utf-8")
    if not content.startswith("---"):
        return {}

    end = content.index("---", 3)
    frontmatter_text = content[3:end].strip()
    return yaml.safe_load(frontmatter_text) or {}
