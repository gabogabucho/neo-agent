"""Cerebellum — read-only normalization and compatibility summaries.

This layer stays outside the Brain execution loop. It only normalizes metadata,
maps declared requirements against the concrete runtime surface, and produces
deterministic compatibility summaries for discovery/catalog consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lumen.core.connectors import ConnectorRegistry
from lumen.core.interoperability import classify_interoperability
from lumen.core.model_tiers import (
    MODEL_TIER_UNKNOWN,
    is_model_tier_below_minimum,
    normalize_capability_tier,
    resolve_configured_model_tier,
)
from lumen.core.module_setup import EnvSpec
from lumen.core.registry import Capability, CapabilityKind, CapabilityStatus, Registry


COMPAT_READY = "ready"
COMPAT_INSTALLABLE = "installable"
COMPAT_PARTIAL = "partial"
COMPAT_BLOCKED = "blocked"


# ---------------------------------------------------------------------------
# Bidirectional translation (ida y vuelta)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class HumanizedSlot:
    """A setup slot translated for human consumption."""

    name: str
    ask: str
    type: str
    required: bool
    secret: bool


def translate_slot_for_user(spec: EnvSpec) -> HumanizedSlot:
    """Translate an EnvSpec into a user-facing slot (ida: system → user).

    Builds a human-friendly prompt from label, hint, examples, and
    format_guidance. Technical instructions (like ``respond with raw
    value``) are NOT included — the Brain's value capture layer handles
    extraction automatically.
    """
    parts = [spec.label]
    if spec.hint:
        parts.append(spec.hint)

    format_bits: list[str] = []
    if spec.format_guidance:
        format_bits.append(spec.format_guidance)
    elif spec.examples:
        examples = ", ".join(f"`{ex}`" for ex in spec.examples[:2])
        format_bits.append(f"Ejemplo: {examples}")

    if format_bits:
        parts.append(" ".join(bit.strip() for bit in format_bits if bit.strip()))

    return HumanizedSlot(
        name=spec.name,
        ask="\n".join(parts),
        type="text",
        required=True,
        secret=spec.secret,
    )


@dataclass
class NormalizedArtifact:
    name: str
    kind: str
    source_type: str
    description: str = ""
    provides: list[str] = field(default_factory=list)
    requires: dict[str, list[str]] = field(default_factory=dict)
    tool_refs: list[str] = field(default_factory=list)
    installed: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind,
            "source_type": self.source_type,
            "description": self.description,
            "provides": self.provides,
            "requires": self.requires,
            "tool_refs": self.tool_refs,
            "installed": self.installed,
            "metadata": self.metadata,
        }


def normalize_agent_skill(
    frontmatter: dict[str, Any], *, path: str | None = None
) -> NormalizedArtifact:
    requires = normalize_requires(frontmatter)
    provides = _normalize_string_list(frontmatter.get("provides"))
    tool_refs = _extract_tool_refs(provides + requires.get("tools", []))
    return NormalizedArtifact(
        name=str(frontmatter.get("name", "unknown")),
        kind="skill",
        source_type="agent_skills",
        description=str(frontmatter.get("description", "")),
        provides=provides,
        requires=requires,
        tool_refs=tool_refs,
        installed=True,
        metadata={
            "path": path,
            "level": frontmatter.get("level"),
            "min_capability": frontmatter.get("min_capability", "tier-1"),
            "interoperability": classify_interoperability(
                source_type="agent_skills",
                metadata=frontmatter,
                manifest_path=path,
            ),
        },
    )


def normalize_openclaw_metadata(
    metadata: dict[str, Any], *, path: str | None = None
) -> NormalizedArtifact:
    normalized = dict(metadata)
    if "provides" not in normalized:
        normalized["provides"] = (
            metadata.get("capabilities")
            or metadata.get("tools")
            or metadata.get("tool_refs")
            or []
        )

    raw_requires = metadata.get("requires") or metadata.get("requirements") or {}
    if not isinstance(raw_requires, dict):
        raw_requires = {}

    if metadata.get("tool_refs") or metadata.get("required_tools"):
        raw_requires["tools"] = _merge_unique(
            _normalize_string_list(raw_requires.get("tools")),
            _normalize_string_list(metadata.get("tool_refs")),
            _normalize_string_list(metadata.get("required_tools")),
        )

    if metadata.get("connectors") or metadata.get("connectors_required"):
        raw_requires["connectors"] = _merge_unique(
            _normalize_string_list(raw_requires.get("connectors")),
            _normalize_string_list(metadata.get("connectors")),
            _normalize_string_list(metadata.get("connectors_required")),
        )

    normalized["requires"] = raw_requires
    requires = normalize_requires(normalized)
    provides = _normalize_string_list(normalized.get("provides"))
    tool_refs = _extract_tool_refs(provides + requires.get("tools", []))

    return NormalizedArtifact(
        name=str(metadata.get("name", "unknown")),
        kind="skill",
        source_type="openclaw",
        description=str(metadata.get("description", "")),
        provides=provides,
        requires=requires,
        tool_refs=tool_refs,
        installed=True,
        metadata={
            "path": path,
            "activation": metadata.get("activation"),
            "min_capability": metadata.get("min_capability", "tier-1"),
            "metadata_keys": sorted(metadata.keys()),
            "interoperability": classify_interoperability(
                source_type="openclaw",
                metadata=metadata,
                manifest_path=path,
            ),
        },
    )


def normalize_module_manifest(
    manifest: dict[str, Any],
    *,
    installed: bool = False,
    source_type: str = "module_manifest",
    manifest_path: str | None = None,
) -> NormalizedArtifact:
    requires = normalize_requires(manifest)
    provides = _normalize_string_list(manifest.get("provides"))
    tool_refs = _extract_tool_refs(requires.get("tools", []))
    x_lumen = _normalize_x_lumen(manifest.get("x-lumen"))
    return NormalizedArtifact(
        name=str(manifest.get("name", "unknown")),
        kind="module",
        source_type=source_type,
        description=str(manifest.get("description", "")),
        provides=provides,
        requires=requires,
        tool_refs=tool_refs,
        installed=installed,
        metadata={
            "display_name": manifest.get("display_name"),
            "version": manifest.get("version", "0.0.0"),
            "path": manifest.get("path"),
            "tags": _normalize_string_list(manifest.get("tags")),
            "min_capability": manifest.get("min_capability", "tier-1"),
            "interoperability": classify_interoperability(
                source_type=source_type,
                metadata=manifest,
                manifest_path=manifest_path,
            ),
            "schema_aliases": {
                "skills_required": _normalize_string_list(
                    manifest.get("skills_required")
                ),
                "channels_supported": _normalize_string_list(
                    manifest.get("channels_supported")
                ),
            },
            "x_lumen": x_lumen,
        },
    )


def normalize_catalog_entry(entry: dict[str, Any]) -> NormalizedArtifact:
    return normalize_module_manifest(
        entry, installed=False, source_type="catalog_entry"
    )


def normalize_requires(payload: dict[str, Any]) -> dict[str, list[str]]:
    requires = payload.get("requires") or payload.get("requirements") or {}
    if not isinstance(requires, dict):
        requires = {}

    normalized: dict[str, list[str]] = {}
    for key, value in requires.items():
        normalized[str(key)] = _normalize_string_list(value)

    alias_groups = {
        "skills": [payload.get("skills_required"), payload.get("skills")],
        "channels": [payload.get("channels_supported"), payload.get("channels")],
        "connectors": [payload.get("connectors_required"), payload.get("connectors")],
        "tools": [
            payload.get("tools_required"),
            payload.get("tool_refs"),
            payload.get("required_tools"),
        ],
        "mcps": [payload.get("mcps_required"), payload.get("mcp_servers")],
    }

    for key, aliases in alias_groups.items():
        normalized[key] = _merge_unique(
            normalized.get(key, []),
            *(_normalize_string_list(alias) for alias in aliases),
        )

    return {key: value for key, value in normalized.items() if value}


def build_runtime_surface(
    connectors: ConnectorRegistry,
    registry: Registry | None = None,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    resolved_model_tier = resolve_configured_model_tier(model)
    surface: dict[str, Any] = {
        "tools": {},
        "connectors": {},
        "mcps": {},
        "channels": {},
        "skills": {},
        "modules": {},
        "model": {
            "name": model,
            "tier": resolved_model_tier,
            "resolved": resolved_model_tier != MODEL_TIER_UNKNOWN,
        },
    }

    for conn_info in connectors.list():
        name = conn_info["name"]
        connector = connectors.get(name)
        if connector is None:
            continue
        action_status = {
            action: (
                CapabilityStatus.READY.value
                if action in connector._handlers
                else CapabilityStatus.MISSING_HANDLER.value
            )
            for action in connector.actions
        }
        connector_status = (
            CapabilityStatus.READY.value
            if connector._handlers
            else CapabilityStatus.MISSING_HANDLER.value
        )
        surface["connectors"][name] = {
            "name": name,
            "description": conn_info.get("description", ""),
            "status": connector_status,
            "actions": list(connector.actions),
            "action_status": action_status,
        }
        for action in connector.actions:
            tool_name = f"{name}__{action}"
            surface["tools"][tool_name] = {
                "name": tool_name,
                "origin": "connector",
                "connector": name,
                "action": action,
                "status": action_status[action],
            }

    for tool in connectors.list_registered_tools():
        metadata = tool.get("metadata") or {}
        origin = "mcp" if metadata.get("kind") == "mcp" else "tool"
        surface["tools"][tool["name"]] = {
            "name": tool["name"],
            "origin": origin,
            "status": CapabilityStatus.READY.value,
            "metadata": metadata,
        }
        if origin == "mcp":
            server_id = metadata.get("server_id")
            server = surface["mcps"].setdefault(
                server_id,
                {
                    "name": server_id,
                    "status": CapabilityStatus.READY.value,
                    "tools": [],
                },
            )
            server["tools"].append(tool["name"])

    if registry is None:
        return surface

    for kind, bucket in (
        (CapabilityKind.CHANNEL, "channels"),
        (CapabilityKind.SKILL, "skills"),
        (CapabilityKind.MODULE, "modules"),
        (CapabilityKind.MCP, "mcps"),
    ):
        for cap in registry.list_by_kind(kind):
            entry = {
                "name": cap.name,
                "status": cap.status.value,
                "provides": list(cap.provides),
            }
            if kind == CapabilityKind.MCP:
                entry["tools"] = list(cap.metadata.get("tools", []))
            surface[bucket][cap.name] = entry

    return surface


def match_declared_tools(
    tool_refs: list[str], runtime_surface: dict[str, Any]
) -> list[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for ref in tool_refs:
        matches.append(_match_tool_ref(ref, runtime_surface))
    return matches


def calculate_compatibility(
    artifact: NormalizedArtifact, runtime_surface: dict[str, Any]
) -> dict[str, Any]:
    reasons: list[str] = []
    warnings: list[str] = []
    matched: list[dict[str, Any]] = []
    degraded = False

    for connector_name in artifact.requires.get("connectors", []):
        connector = runtime_surface["connectors"].get(connector_name)
        if not connector:
            reasons.append(f"missing connector '{connector_name}'")
            continue
        if connector["status"] != CapabilityStatus.READY.value:
            reasons.append(f"connector '{connector_name}' is {connector['status']}")

    for channel_name in artifact.requires.get("channels", []):
        channel = runtime_surface["channels"].get(channel_name)
        if not channel:
            reasons.append(f"missing channel '{channel_name}'")
            continue
        if channel["status"] != CapabilityStatus.READY.value:
            reasons.append(f"channel '{channel_name}' is {channel['status']}")

    for skill_name in artifact.requires.get("skills", []):
        skill = runtime_surface["skills"].get(skill_name)
        if not skill:
            reasons.append(f"missing skill '{skill_name}'")
            continue
        if skill["status"] != CapabilityStatus.READY.value:
            reasons.append(f"skill '{skill_name}' is {skill['status']}")

    for mcp_name in artifact.requires.get("mcps", []):
        mcp = runtime_surface["mcps"].get(mcp_name)
        if not mcp:
            reasons.append(f"missing MCP '{mcp_name}'")
            continue
        if mcp["status"] != CapabilityStatus.READY.value:
            reasons.append(f"MCP '{mcp_name}' is {mcp['status']}")

    advisory_mcps = (
        artifact.metadata.get("x_lumen", {})
        .get("advisory_requires", {})
        .get("mcps", [])
    )
    for mcp_name in advisory_mcps:
        mcp = runtime_surface["mcps"].get(mcp_name)
        if not mcp:
            warnings.append(f"advisory MCP '{mcp_name}' is not connected")
            continue
        if mcp["status"] != CapabilityStatus.READY.value:
            warnings.append(f"advisory MCP '{mcp_name}' is {mcp['status']}")

    matched = match_declared_tools(artifact.tool_refs, runtime_surface)
    for match in matched:
        if match["status"] == COMPAT_BLOCKED:
            reasons.append(match["reason"])
        elif match["status"] == COMPAT_PARTIAL:
            degraded = True
            warnings.append(match["reason"])

    if artifact.metadata.get("schema_aliases", {}).get("skills_required"):
        warnings.append(
            "normalized legacy field 'skills_required' into requires.skills"
        )
    if artifact.metadata.get("schema_aliases", {}).get("channels_supported"):
        warnings.append(
            "normalized legacy field 'channels_supported' into requires.channels"
        )

    runtime_model = runtime_surface.get("model") or {}
    runtime_model_tier = runtime_model.get("tier", MODEL_TIER_UNKNOWN)
    min_capability = normalize_capability_tier(
        artifact.metadata.get("min_capability", "tier-1")
    )
    if is_model_tier_below_minimum(runtime_model_tier, min_capability):
        model_name = runtime_model.get("name") or "configured model"
        warnings.append(
            f"configured model '{model_name}' resolves to {runtime_model_tier} but '{artifact.name}' recommends {min_capability}"
        )

    status = COMPAT_READY
    if reasons:
        status = COMPAT_BLOCKED
    elif degraded:
        status = COMPAT_PARTIAL
    elif artifact.kind == "module" and not artifact.installed:
        status = COMPAT_INSTALLABLE

    return {
        "status": status,
        "reasons": _unique(reasons),
        "warnings": _unique(warnings),
        "matched_tools": matched,
        "normalized": artifact.to_dict(),
    }


def annotate_registry(
    registry: Registry,
    connectors: ConnectorRegistry,
    *,
    model: str | None = None,
) -> None:
    runtime_surface = build_runtime_surface(connectors, registry, model=model)
    for capability in registry.all():
        artifact = normalize_capability(capability)
        if artifact is None:
            continue
        capability.metadata["cerebelo"] = calculate_compatibility(
            artifact, runtime_surface
        )


def compatibility_for_catalog_entry(
    entry: dict[str, Any],
    registry: Registry,
    connectors: ConnectorRegistry,
    *,
    model: str | None = None,
) -> dict[str, Any]:
    artifact = normalize_catalog_entry(entry)
    runtime_surface = build_runtime_surface(connectors, registry, model=model)
    return calculate_compatibility(artifact, runtime_surface)


def normalize_capability(capability: Capability) -> NormalizedArtifact | None:
    if capability.kind == CapabilityKind.SKILL:
        return NormalizedArtifact(
            name=capability.name,
            kind="skill",
            source_type="registry_skill",
            description=capability.description,
            provides=list(capability.provides),
            requires=normalize_requires(capability.to_dict()),
            tool_refs=_extract_tool_refs(
                list(capability.provides)
                + normalize_requires(capability.to_dict()).get("tools", [])
            ),
            installed=True,
            metadata={"registry_status": capability.status.value},
        )
    if capability.kind == CapabilityKind.MODULE:
        payload = capability.to_dict()
        payload.setdefault("display_name", capability.metadata.get("display_name"))
        payload.setdefault("path", capability.metadata.get("path"))
        payload.setdefault("tags", capability.metadata.get("tags", []))
        payload.setdefault(
            "skills_required",
            capability.metadata.get("schema_aliases", {}).get("skills_required", []),
        )
        payload.setdefault(
            "channels_supported",
            capability.metadata.get("schema_aliases", {}).get("channels_supported", []),
        )
        payload.setdefault("x-lumen", capability.metadata.get("x_lumen", {}))
        return normalize_module_manifest(
            payload,
            installed=True,
            source_type="registry_module",
        )
    if capability.kind == CapabilityKind.MCP:
        return NormalizedArtifact(
            name=capability.name,
            kind="mcp",
            source_type="runtime_mcp",
            description=capability.description,
            provides=list(capability.provides),
            requires=normalize_requires(capability.to_dict()),
            tool_refs=_extract_tool_refs(list(capability.provides)),
            installed=True,
            metadata={"registry_status": capability.status.value},
        )
    if capability.kind == CapabilityKind.CONNECTOR:
        return NormalizedArtifact(
            name=capability.name,
            kind="connector",
            source_type="runtime_connector",
            description=capability.description,
            provides=[
                f"{capability.name}__{action}"
                for action in capability.metadata.get("actions", [])
            ],
            requires=normalize_requires(capability.to_dict()),
            tool_refs=[
                f"{capability.name}__{action}"
                for action in capability.metadata.get("actions", [])
            ],
            installed=True,
            metadata={"registry_status": capability.status.value},
        )
    return None


def _match_tool_ref(ref: str, runtime_surface: dict[str, Any]) -> dict[str, Any]:
    cleaned = _clean_tool_ref(ref)
    candidates = [cleaned]
    if "__" not in cleaned and cleaned.count(".") == 1:
        candidates.append(cleaned.replace(".", "__", 1))

    for candidate in _unique(candidates):
        tool = runtime_surface["tools"].get(candidate)
        if not tool:
            continue
        if tool["status"] == CapabilityStatus.READY.value:
            return {
                "declared": ref,
                "resolved": candidate,
                "status": COMPAT_READY,
                "reason": f"matched runtime tool '{candidate}'",
            }
        return {
            "declared": ref,
            "resolved": candidate,
            "status": COMPAT_PARTIAL,
            "reason": f"tool '{candidate}' is {tool['status']}",
        }

    if "__" in cleaned:
        connector_name, action = cleaned.split("__", 1)
    elif cleaned.count(".") == 1:
        connector_name, action = cleaned.split(".", 1)
    else:
        connector_name, action = cleaned, ""

    connector = runtime_surface["connectors"].get(connector_name)
    if connector:
        if action and action in connector.get("actions", []):
            action_status = connector.get("action_status", {}).get(
                action, CapabilityStatus.MISSING_HANDLER.value
            )
            resolved = f"{connector_name}__{action}"
            if action_status == CapabilityStatus.READY.value:
                return {
                    "declared": ref,
                    "resolved": resolved,
                    "status": COMPAT_READY,
                    "reason": f"mapped declared ref '{ref}' to connector tool '{resolved}'",
                }
            return {
                "declared": ref,
                "resolved": resolved,
                "status": COMPAT_PARTIAL,
                "reason": f"connector action '{resolved}' is {action_status}",
            }
        if not action:
            connector_status = connector["status"]
            if connector_status == CapabilityStatus.READY.value:
                return {
                    "declared": ref,
                    "resolved": connector_name,
                    "status": COMPAT_READY,
                    "reason": f"matched connector '{connector_name}'",
                }
            return {
                "declared": ref,
                "resolved": connector_name,
                "status": COMPAT_PARTIAL,
                "reason": f"connector '{connector_name}' is {connector_status}",
            }

    return {
        "declared": ref,
        "resolved": None,
        "status": COMPAT_BLOCKED,
        "reason": f"no runtime tool or connector matches '{ref}'",
    }


def _extract_tool_refs(values: list[str]) -> list[str]:
    refs: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = _clean_tool_ref(value)
        if "__" in cleaned or cleaned.count(".") == 1:
            refs.append(cleaned)
    return _unique(refs)


def _clean_tool_ref(value: str) -> str:
    cleaned = str(value).strip()
    if " with " in cleaned:
        cleaned = cleaned.split(" with ", 1)[0].strip()
    if "(" in cleaned:
        cleaned = cleaned.split("(", 1)[0].strip()
    return cleaned


def _normalize_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _normalize_x_lumen(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}

    if "advisory_requires" in value:
        advisory_requires = value.get("advisory_requires") or {}
        mcps = _normalize_string_list(advisory_requires.get("mcps"))
        return {"advisory_requires": {"mcps": mcps}} if mcps else {}

    requires = value.get("requires")
    if not isinstance(requires, dict):
        requires = {}

    advisory = requires.get("advisory")
    if not isinstance(advisory, dict):
        advisory = {}

    mcps = _normalize_string_list(advisory.get("mcps"))
    return {"advisory_requires": {"mcps": mcps}} if mcps else {}


def _merge_unique(*values: list[str]) -> list[str]:
    merged: list[str] = []
    for value in values:
        merged.extend(value or [])
    return _unique(merged)


def _unique(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result
