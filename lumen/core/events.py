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

if TYPE_CHECKING:
    from lumen.core.registry import Capability


@dataclass
class CapabilityEvent:
    """A single change in Lumen's body."""

    kind: str  # "added" | "removed" | "status_changed"
    capability: "Capability"
    timestamp: float = field(default_factory=time.time)
    details: dict[str, Any] = field(default_factory=dict)

    def is_addition(self) -> bool:
        return self.kind == "added"

    def is_removal(self) -> bool:
        return self.kind == "removed"

    def is_status_change(self) -> bool:
        return self.kind == "status_changed"

    def summary(self) -> str:
        cap = self.capability
        if self.is_addition():
            return f"+ {cap.name} ({cap.kind.value}): {cap.description}"
        if self.is_removal():
            return f"- {cap.name} ({cap.kind.value}): {cap.description}"
        if self.is_status_change():
            frm = self.details.get("from", "?")
            to = self.details.get("to", "?")
            return f"~ {cap.name}: {frm} → {to}"
        return f"? {cap.name}: {self.kind}"


# Type alias for clarity
EventCallback = Callable[[CapabilityEvent], None]
