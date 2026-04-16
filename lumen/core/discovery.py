"""Discovery — scans the filesystem and registers everything with Lumen's consciousness.

On startup (and optionally on interval), discovery:
1. Scans skills/ for SKILL.md files → parse frontmatter → register
2. Scans connectors/ for YAML → parse → register (with handler status)
3. Scans modules/ for manifest.yaml → parse → register
4. Checks channels → register active ones
5. Checks MCP servers from config → register (available or error)

Everything that exists must self-declare. Discovery reads the declarations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from lumen.core.connectors import ConnectorRegistry
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
) -> Registry:
    """Run full discovery and populate the registry."""
    # Built-in skills
    _discover_skills(registry, pkg_dir / "skills")

    # Skills inside installed modules (each module can have a SKILL.md)
    modules_dir = pkg_dir / "modules"
    if modules_dir.exists():
        for module_dir in modules_dir.iterdir():
            if module_dir.is_dir() and not module_dir.name.startswith("_"):
                skill_file = module_dir / "SKILL.md"
                if skill_file.exists():
                    _discover_skill_file(registry, skill_file, module_dir.name)

    _discover_connectors(registry, connectors)
    _discover_modules(registry, pkg_dir / "modules")
    _discover_channels(registry, active_channels or ["web"])

    if mcp_config:
        _discover_mcps(registry, mcp_config)

    # Second pass: validate skill dependencies
    # If a skill requires a connector that has no handler, mark it as MISSING_DEPS
    _validate_skill_deps(registry)

    return registry


def _discover_skill_file(registry: Registry, skill_file: Path, fallback_name: str):
    """Discover a single SKILL.md file."""
    try:
        frontmatter = _parse_frontmatter(skill_file)
        name = frontmatter.get("name", fallback_name)

        # Don't register if already exists (built-in takes priority)
        if registry.get(CapabilityKind.SKILL, name):
            return

        registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name=name,
                description=frontmatter.get("description", ""),
                status=CapabilityStatus.READY,
                provides=frontmatter.get("provides", []),
                requires=frontmatter.get("requires", {}),
                min_capability=frontmatter.get("min_capability", "tier-1"),
                metadata={"level": frontmatter.get("level", 1), "path": str(skill_file)},
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
            name = frontmatter.get("name", skill_dir.name)
            description = frontmatter.get("description", "")
            requires = frontmatter.get("requires", {})
            level = frontmatter.get("level", 1)

            registry.register(
                Capability(
                    kind=CapabilityKind.SKILL,
                    name=name,
                    description=description,
                    status=CapabilityStatus.READY,
                    provides=frontmatter.get("provides", []),
                    requires=requires,
                    min_capability=frontmatter.get(
                        "min_capability", "tier-1"
                    ),
                    metadata={
                        "level": level,
                        "path": str(skill_file),
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
            CapabilityStatus.READY
            if has_handlers
            else CapabilityStatus.MISSING_HANDLER
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


def _discover_modules(registry: Registry, modules_dir: Path):
    """Scan modules/ for manifest.yaml files."""
    if not modules_dir.exists():
        return

    for module_dir in modules_dir.iterdir():
        if not module_dir.is_dir() or module_dir.name.startswith("_"):
            continue

        manifest_file = module_dir / "manifest.yaml"
        if not manifest_file.exists():
            continue

        try:
            with open(manifest_file, encoding="utf-8") as f:
                manifest = yaml.safe_load(f) or {}

            name = manifest.get("name", module_dir.name)

            # Module is installed (it's in modules/ dir) — check if its skill is ready
            has_skill = (module_dir / "SKILL.md").exists()
            status = CapabilityStatus.READY if has_skill else CapabilityStatus.AVAILABLE

            registry.register(
                Capability(
                    kind=CapabilityKind.MODULE,
                    name=name,
                    description=manifest.get("description", ""),
                    status=status,
                    requires={
                        "skills": manifest.get("skills_required", []),
                        "channels": manifest.get("channels_supported", []),
                    },
                    metadata={
                        "display_name": manifest.get("display_name", name),
                        "version": manifest.get("version", "0.0.0"),
                        "author": manifest.get("author", ""),
                        "min_capability": manifest.get(
                            "min_capability", "tier-1"
                        ),
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


def _discover_mcps(registry: Registry, mcp_config: dict):
    """Register MCP servers from config."""
    servers = mcp_config.get("servers", {})
    for name, config in servers.items():
        registry.register(
            Capability(
                kind=CapabilityKind.MCP,
                name=name,
                description=config.get("description", f"MCP server: {name}"),
                status=CapabilityStatus.AVAILABLE,
                provides=config.get("provides", []),
                metadata={
                    "command": config.get("command"),
                    "url": config.get("url"),
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
