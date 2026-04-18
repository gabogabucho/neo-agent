"""CapabilityAwareness — the bridge between Body changes and Consciousness.

Consciousness feels. Brain orchestrates. Body changes.
This is the layer that connects them.

The Registry emits events when capabilities change (added, removed, status changed).
CapabilityAwareness subscribes to those events, buffers them, and provides two outputs:

  1. Proactive announcement — used by think_proactive() to tell the user independently
  2. Prompt context — used by Brain._build_prompt() to inject into the next conversation turn

These are SEPARATE drains. Proactive drains thoughts (for the announcement).
Prompt drains events (for structured context). This prevents the double-drain race condition
where proactive announcement empties the buffer before the user's next message sees it.

Neither Consciousness nor Brain touches the Registry directly.
"""

from __future__ import annotations

import logging

from lumen.core.events import CapabilityEvent
from lumen.core.registry import Registry

logger = logging.getLogger(__name__)


class CapabilityAwareness:
    """Subscribes to Registry events and translates them for the mind layers.

    Two separate read paths:
      - format_for_proactive() → drains thoughts, generates standalone announcement
      - format_for_prompt() → drains events, generates structured prompt section

    Each path drains ONLY its own buffer. No race condition.
    """

    def __init__(self, registry: Registry):
        self._pending_events: list[CapabilityEvent] = []
        self._pending_thoughts: list[str] = []
        self._proactive_done: bool = False  # Track if proactive was already sent
        registry.subscribe(self._on_registry_event)

    def _on_registry_event(self, event: CapabilityEvent):
        """Callback: Registry emits, Awareness receives and translates."""
        self._pending_events.append(event)
        thought = self._generate_internal_thought(event)
        if thought:
            self._pending_thoughts.append(thought)
        self._proactive_done = False  # New events reset proactive state
        logger.debug(
            "Awareness received: %s %s",
            event.kind,
            event.capability.name,
        )

    def _generate_internal_thought(self, event: CapabilityEvent) -> str | None:
        """Translate a capability event into an internal impression."""
        cap = event.capability
        if event.is_addition():
            return f"I gained a new {cap.kind.value}: {cap.name}"
        if event.is_removal():
            return f"I lost a {cap.kind.value}: {cap.name}"
        if event.is_status_change():
            to_status = event.details.get("to", "unknown")
            return f"My {cap.kind.value} '{cap.name}' is now {to_status}"
        return None

    # --- Read interface ---

    def has_pending(self) -> bool:
        """Are there buffered changes waiting to be processed?"""
        return len(self._pending_events) > 0

    def has_pending_proactive(self) -> bool:
        """Are there pending thoughts that haven't been announced proactively?"""
        return len(self._pending_thoughts) > 0 and not self._proactive_done

    def format_for_proactive(self) -> str | None:
        """Generate a standalone proactive announcement. Drains thoughts ONLY.

        Used by Brain.think_proactive(). Returns the raw thought lines
        for the LLM to translate into natural language.
        After this is called, has_pending_proactive() returns False,
        but has_pending() still returns True (events remain for prompt context).
        """
        if not self._pending_thoughts or self._proactive_done:
            return None

        thoughts = self._pending_thoughts[:]
        self._pending_thoughts.clear()
        self._proactive_done = True

        lines = ["## Something changed in my body", ""]
        for thought in thoughts:
            lines.append(f"- {thought}")
        lines.append("")
        lines.append(
            "Briefly and naturally tell the user about this. "
            "One or two sentences max. Your own words."
        )
        return "\n".join(lines)

    def format_for_prompt(self) -> str | None:
        """Format pending events for Brain context injection. Drains events ONLY.

        Used by Brain._build_prompt(). Returns a structured section
        for the system prompt. Events are drained so they don't repeat.
        """
        if not self._pending_events:
            return None

        events = self._pending_events[:]
        self._pending_events.clear()

        # Also clear any remaining thoughts (proactive already handled them,
        # or proactive wasn't needed)
        self._pending_thoughts.clear()

        lines = ["## Something changed in my body", ""]

        for event in events:
            lines.append(f"- {event.summary()}")

        lines.append("")
        lines.append(
            "You may mention this naturally if it seems relevant. "
            "Use your own words — not technical jargon."
        )
        return "\n".join(lines)
