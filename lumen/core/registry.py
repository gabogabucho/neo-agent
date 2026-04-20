"""Registry (the Body) — WHAT Lumen has. Discovered at startup.

If Lumen doesn't know something exists, it doesn't exist.
Every skill, connector, module, channel, and MCP server must register here.

The Body is separate from Consciousness (WHO Lumen is) and Brain (HOW Lumen thinks).
Consciousness is immutable. The Body changes as you install or remove things.

When the body changes, it emits events. Subscribers react.
That's how Lumen feels its own growth.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from lumen.core.capability_consciousness import classify_capability
from lumen.core.events import CapabilityEvent, EventCallback

logger = logging.getLogger(__name__)


class CapabilityKind(str, Enum):
    SKILL = "skill"
    CONNECTOR = "connector"
    MODULE = "module"
    CHANNEL = "channel"
    MCP = "mcp"


class CapabilityStatus(str, Enum):
    READY = "ready"  # Fully functional
    AVAILABLE = "available"  # Declared but not configured
    MISSING_HANDLER = "no_handler"  # Connector without implementation
    MISSING_DEPS = "missing_deps"  # Requirements not met
    ERROR = "error"  # Failed to load/connect


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
            "metadata": self.metadata,
            "consciousness": classify_capability(self),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Capability":
        return cls(
            kind=CapabilityKind(payload["kind"]),
            name=payload["name"],
            description=payload.get("description", ""),
            status=CapabilityStatus(payload.get("status", CapabilityStatus.AVAILABLE.value)),
            provides=list(payload.get("provides", [])),
            requires=dict(payload.get("requires", {})),
            min_capability=payload.get("min_capability", "tier-1"),
            metadata=dict(payload.get("metadata", {})),
        )


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
        self._subscribers: list[EventCallback] = []

    def subscribe(self, callback: EventCallback):
        """Subscribe to capability changes. Callback receives CapabilityEvent."""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: EventCallback):
        """Remove a subscriber."""
        self._subscribers.discard(callback) if hasattr(self._subscribers, "discard") else None
        try:
            self._subscribers.remove(callback)
        except ValueError:
            pass

    def register(self, capability: Capability):
        """Register a capability. Emits lifecycle events if new."""
        key = f"{capability.kind.value}:{capability.name}"
        is_new = key not in self._capabilities
        self._capabilities[key] = capability
        if is_new:
            self._emit("capability_discovered", capability)
            readiness_event = _readiness_event_kind(capability)
            if readiness_event is not None:
                self._emit(readiness_event, capability)
            logger.debug("Capability registered: %s (%s)", capability.name, capability.kind.value)

    def unregister(self, kind: CapabilityKind, name: str):
        """Remove a capability. Emits lifecycle events."""
        key = f"{kind.value}:{name}"
        cap = self._capabilities.pop(key, None)
        if cap:
            self._emit("capability_removed", cap)
            logger.debug("Capability unregistered: %s (%s)", name, kind.value)

    def update_status(self, kind: CapabilityKind, name: str, status: CapabilityStatus):
        """Change a capability's status. Emits lifecycle events if different."""
        cap = self.get(kind, name)
        if cap and cap.status != status:
            old_status = cap.status
            cap.status = status
            event_kind = _status_transition_event_kind(cap, old_status, status)
            self._emit(
                event_kind,
                cap,
                details={"from": old_status.value, "to": status.value},
            )
            logger.debug("Capability status changed: %s %s → %s", name, old_status.value, status.value)

    def _emit(self, kind: str, capability: Capability, details: dict[str, Any] | None = None):
        """Emit a capability event to all subscribers."""
        event = CapabilityEvent(
            kind=kind,
            capability=capability,
            details=details or {},
        )
        for callback in self._subscribers:
            try:
                callback(event)
            except Exception:
                logger.exception("Event subscriber error for %s event", kind)

    def get(self, kind: CapabilityKind, name: str) -> Capability | None:
        return self._capabilities.get(f"{kind.value}:{name}")

    def snapshot(self) -> dict[str, dict[str, Any]]:
        """Stable view of current capabilities for diffing."""
        return {key: cap.to_dict() for key, cap in sorted(self._capabilities.items())}

    def list_by_kind(self, kind: CapabilityKind) -> list[Capability]:
        return [c for c in self._capabilities.values() if c.kind == kind]

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
            "## Body (my capabilities and readiness right now)",
            "",
            "IMPORTANT: Only capabilities listed under 'What I CAN do' are READY "
            "and usable right now.",
            "Installed or present is NOT the same as ready.",
            "Anything under 'Installed but NOT ready yet' already exists in my "
            "body, but I must describe it truthfully as unavailable until its "
            "setup, dependencies, or errors are resolved.",
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
                elif c.kind == CapabilityKind.MCP:
                    tools = c.metadata.get("tools", [])
                    if tools:
                        hint = f" → connected tools: {', '.join(tools[:5])}"

                lines.append(f"- **{c.name}** ({c.kind.value}): {c.description}{hint}")

        # Installed but not ready — discovered, but cannot be used yet
        if all_gaps:
            lines.append("\n### Installed but NOT ready yet (do not present as usable)")
            lines.append(
                "These capabilities exist in my body, but I must NOT claim I can use "
                "them right now. I should explain the blocker instead."
            )
            has_pending_setup = any(
                (gap.metadata.get("pending_setup") or {}).get("env_specs")
                for gap in all_gaps
            )
            if has_pending_setup:
                lines.append(
                    "IMPORTANT: When a user provides configuration values (tokens, keys, IDs) "
                    "for any of these, I MUST persist them with neo__save_artifact_setup "
                    "(or the legacy neo__save_module_setup alias for native modules). "
                    "I should NEVER ask the user to manually set env vars — I do it myself "
                    "via the tool. After saving, confirm whether the capability is now configured."
                )
            for gap in all_gaps:
                lines.append(self._format_not_ready_context_line(gap))
            lines.append(
                "\nFor these, say the capability exists but is not ready yet. "
                "Explain the missing setup or problem, and invite the user to "
                "configure/fix it instead of talking as if it already works."
            )

        return "\n".join(lines)

    @staticmethod
    def _format_not_ready_context_line(capability: Capability) -> str:
        label = capability.metadata.get("display_name") or capability.name
        detail = capability.description
        status_label = _format_status_label(capability.status)
        blocker = _describe_not_ready_blocker(capability)
        return (
            f"- **{label}** ({capability.kind.value}): present in my body, but NOT READY "
            f"[{status_label}] — {blocker}. {detail}"
        )


def _format_env_spec_name(spec: dict[str, Any]) -> str:
    name = str(spec.get("name") or "").strip()
    label = str(spec.get("label") or "").strip()
    if name and label and label.lower() != name.lower():
        return f"{name} ({label})"
    return name or label or "unknown"


def _format_status_label(status: CapabilityStatus) -> str:
    return status.value.replace("_", " ")


def _describe_not_ready_blocker(capability: Capability) -> str:
    pending_setup = capability.metadata.get("pending_setup") or {}
    env_specs = pending_setup.get("env_specs") or []
    if env_specs:
        missing = ", ".join(_format_env_spec_name(spec) for spec in env_specs)
        return f"needs setup/configuration before use: {missing}"

    error = str(capability.metadata.get("error") or "").strip()
    if error:
        return f"currently failing with an error: {error}"

    if capability.status == CapabilityStatus.ERROR:
        return "currently failing and cannot be used"

    if capability.status == CapabilityStatus.MISSING_DEPS:
        return "missing required dependencies before it can work"

    if capability.status == CapabilityStatus.MISSING_HANDLER:
        return "missing its runtime handler before it can work"

    if capability.status == CapabilityStatus.AVAILABLE:
        return "discovered, but not configured or activated yet"

    return "not ready for use yet"


def diff_capability_snapshots(
    previous: dict[str, dict[str, Any]] | None,
    current: dict[str, dict[str, Any]] | None,
) -> list[CapabilityEvent]:
    """Compare capability snapshots and emit lifecycle events."""
    previous = previous or {}
    current = current or {}
    events: list[CapabilityEvent] = []

    previous_keys = set(previous)
    current_keys = set(current)

    for key in sorted(previous_keys - current_keys):
        capability = Capability.from_dict(previous[key])
        events.append(CapabilityEvent(kind="capability_removed", capability=capability))

    for key in sorted(current_keys - previous_keys):
        capability = Capability.from_dict(current[key])
        events.append(CapabilityEvent(kind="capability_discovered", capability=capability))
        readiness_event = _readiness_event_kind(capability)
        if readiness_event is not None:
            events.append(CapabilityEvent(kind=readiness_event, capability=capability))

    for key in sorted(previous_keys & current_keys):
        before = previous[key]
        after = current[key]
        if before == after:
            continue

        capability = Capability.from_dict(after)
        old_status = CapabilityStatus(before.get("status", CapabilityStatus.AVAILABLE.value))
        new_status = capability.status

        if old_status != new_status:
            events.append(
                CapabilityEvent(
                    kind=_status_transition_event_kind(capability, old_status, new_status),
                    capability=capability,
                    details={"from": old_status.value, "to": new_status.value},
                )
            )

    return events


def _readiness_event_kind(capability: Capability) -> str | None:
    if not capability.is_ready():
        return None
    kind_label = classify_capability(capability)["kind_label"]
    if kind_label in {"mind", "transformation"}:
        return "capability_integrated"
    return "capability_connected"


def _status_transition_event_kind(
    capability: Capability,
    old_status: CapabilityStatus,
    new_status: CapabilityStatus,
) -> str:
    if new_status == CapabilityStatus.READY and old_status != CapabilityStatus.READY:
        readiness_event = _readiness_event_kind(capability)
        if readiness_event is not None:
            return readiness_event

    if _status_rank(new_status) < _status_rank(old_status):
        return "capability_degraded"

    if new_status != old_status and new_status != CapabilityStatus.READY:
        return "capability_degraded"

    readiness_event = _readiness_event_kind(capability)
    return readiness_event or "capability_degraded"


def _status_rank(status: CapabilityStatus) -> int:
    return {
        CapabilityStatus.READY: 4,
        CapabilityStatus.AVAILABLE: 3,
        CapabilityStatus.MISSING_HANDLER: 2,
        CapabilityStatus.MISSING_DEPS: 2,
        CapabilityStatus.ERROR: 1,
    }.get(status, 0)
