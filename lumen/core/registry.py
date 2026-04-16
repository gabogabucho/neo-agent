"""Registry (the Body) — WHAT Lumen has. Discovered at startup.

If Lumen doesn't know something exists, it doesn't exist.
Every skill, connector, module, channel, and MCP server must register here.

The Body is separate from Consciousness (WHO Lumen is) and Brain (HOW Lumen thinks).
Consciousness is immutable. The Body changes as you install or remove things.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CapabilityKind(str, Enum):
    SKILL = "skill"
    CONNECTOR = "connector"
    MODULE = "module"
    CHANNEL = "channel"
    MCP = "mcp"


class CapabilityStatus(str, Enum):
    READY = "ready"              # Fully functional
    AVAILABLE = "available"      # Declared but not configured
    MISSING_HANDLER = "no_handler"  # Connector without implementation
    MISSING_DEPS = "missing_deps"   # Requirements not met
    ERROR = "error"              # Failed to load/connect


@dataclass
class Capability:
    """A single thing Lumen knows it has (or is missing)."""

    kind: CapabilityKind
    name: str
    description: str
    status: CapabilityStatus = CapabilityStatus.AVAILABLE
    provides: list[str] = field(default_factory=list)
    requires: dict[str, list[str]] = field(default_factory=dict)
    min_capability: str = "tier-1"  # Recommended LLM tier (not enforced)
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_ready(self) -> bool:
        return self.status == CapabilityStatus.READY

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "name": self.name,
            "description": self.description,
            "status": self.status.value,
            "provides": self.provides,
            "requires": self.requires,
            "min_capability": self.min_capability,
        }


class Registry:
    """Lumen's self-awareness map. Knows what exists, what works, what's missing.

    Usage:
        registry = Registry()
        registry.register(Capability(
            kind=CapabilityKind.SKILL,
            name="web-search",
            description="Search the web for information",
            status=CapabilityStatus.READY,
            provides=["web_search"],
            requires={"connectors": ["web"]},
        ))

        # What can I do?
        ready = registry.ready()

        # What am I missing?
        gaps = registry.gaps()

        # Full self-awareness context for the LLM
        context = registry.as_context()
    """

    def __init__(self):
        self._capabilities: dict[str, Capability] = {}

    def register(self, capability: Capability):
        key = f"{capability.kind.value}:{capability.name}"
        self._capabilities[key] = capability

    def get(self, kind: CapabilityKind, name: str) -> Capability | None:
        return self._capabilities.get(f"{kind.value}:{name}")

    def list_by_kind(self, kind: CapabilityKind) -> list[Capability]:
        return [
            c for c in self._capabilities.values() if c.kind == kind
        ]

    def ready(self) -> list[Capability]:
        """Everything that's fully functional."""
        return [c for c in self._capabilities.values() if c.is_ready()]

    def gaps(self) -> list[Capability]:
        """Everything that's declared but NOT ready."""
        return [c for c in self._capabilities.values() if not c.is_ready()]

    def all(self) -> list[Capability]:
        return list(self._capabilities.values())

    def summary(self) -> dict[str, dict[str, int]]:
        """Quick count by kind and status."""
        result: dict[str, dict[str, int]] = {}
        for cap in self._capabilities.values():
            kind = cap.kind.value
            status = cap.status.value
            if kind not in result:
                result[kind] = {}
            result[kind][status] = result[kind].get(status, 0) + 1
        return result

    def as_context(self) -> str:
        """Format the Body for the LLM system prompt.

        This tells the LLM exactly what Lumen has and what's missing
        RIGHT NOW — discovered at startup, not hardcoded.
        """
        all_ready = self.ready()
        all_gaps = self.gaps()

        lines = [
            "## Body (my active capabilities)",
            "",
            "IMPORTANT: Everything below under 'What I CAN do' is READY and "
            "ACTIVE. I do NOT need to install anything for these.",
        ]

        # Ready capabilities — name + description + one-line action hint
        if all_ready:
            lines.append("\n### What I CAN do (READY — use immediately)")
            lines.append(
                "For detailed instructions on any skill, call "
                "neo__read_skill with the skill name."
            )
            lines.append("")

            for c in all_ready:
                hint = ""
                # For skills, add a one-line action hint from metadata
                if c.kind == CapabilityKind.SKILL:
                    provides = c.provides
                    if provides:
                        hint = f" → uses: {', '.join(provides[:3])}"
                    else:
                        # Extract action hint from required connectors
                        req_conns = c.requires.get("connectors", [])
                        if req_conns:
                            hint = f" → uses connectors: {', '.join(req_conns)}"

                lines.append(
                    f"- **{c.name}** ({c.kind.value}): "
                    f"{c.description}{hint}"
                )

        # Gaps — things that need extension
        if all_gaps:
            lines.append("\n### What I CANNOT do yet (needs extension)")
            for gap in all_gaps:
                reason = gap.status.value.replace("_", " ")
                lines.append(f"- {gap.name}: {gap.description} [{reason}]")
            lines.append(
                "\nFor these, explain what's missing and suggest "
                "installing a module from the catalog."
            )

        return "\n".join(lines)
