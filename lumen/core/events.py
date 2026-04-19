"""Capability Events — the pulse of Lumen's body.

When the Registry changes (something added, removed, or changed status),
it emits a CapabilityEvent. Subscribers receive it and react.

This is Level 1 of the Capability Awareness system.
Consciousness feels. Brain orchestrates. Body changes.
Events are how the body tells the rest.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable

from lumen.core.capability_consciousness import classify_capability
from lumen.core.interoperability import awareness_interoperability_note

if TYPE_CHECKING:
    from lumen.core.registry import Capability


@dataclass
class CapabilityEvent:
    """A single change in Lumen's body."""

    kind: str
    capability: "Capability"
    timestamp: float = field(default_factory=time.time)
    details: dict[str, Any] = field(default_factory=dict)

    def is_addition(self) -> bool:
        return self.kind in {
            "capability_discovered",
            "capability_connected",
            "capability_integrated",
        }

    def is_removal(self) -> bool:
        return self.kind == "capability_removed"

    def is_status_change(self) -> bool:
        return self.kind in {
            "capability_connected",
            "capability_integrated",
            "capability_degraded",
        }

    def classification(self) -> dict[str, str]:
        return classify_capability(self.capability)

    def announce_text(self) -> str | None:
        return self.details.get("announce_text") or self.classification().get("announce_text")

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.kind,
            "timestamp": self.timestamp,
            "summary": self.summary(),
            "announce_text": self.announce_text(),
            "capability": self.capability.to_dict(),
            "classification": self.classification(),
            "details": self.details,
        }

    def summary(self) -> str:
        cap = self.capability
        lens = self.classification()["kind_label"]
        note = awareness_interoperability_note(cap)
        if self.kind == "capability_discovered":
            summary = f"+ discovered {cap.name} ({cap.kind.value}, {lens}): {cap.description}"
            return _append_interoperability_summary(summary, note)
        if self.kind == "capability_connected":
            return _append_interoperability_summary(
                f"+ connected {cap.name} ({cap.kind.value}, {lens})", note
            )
        if self.kind == "capability_integrated":
            return _append_interoperability_summary(
                f"+ integrated {cap.name} ({cap.kind.value}, {lens})", note
            )
        if self.is_removal():
            return _append_interoperability_summary(
                f"- removed {cap.name} ({cap.kind.value}, {lens})", note
            )
        if self.kind == "capability_degraded":
            frm = self.details.get("from", "?")
            to = self.details.get("to", "?")
            return _append_interoperability_summary(
                f"~ degraded {cap.name} ({cap.kind.value}, {lens}): {frm} → {to}",
                note,
            )
        return f"? {cap.name}: {self.kind}"


# Type alias for clarity
EventCallback = Callable[[CapabilityEvent], None]


def _append_interoperability_summary(
    base: str, note: dict[str, str] | None
) -> str:
    if not note:
        return base
    return f"{base} — {note['summary']}"
