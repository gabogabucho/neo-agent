"""End-to-end tests for the Capability Awareness pipeline.

Verifies that registry changes flow correctly into awareness outputs.
Protects against regressions like the circular import that broke
module boot (events.py ↔ registry.py).
"""

from __future__ import annotations

from lumen.core.awareness import CapabilityAwareness
from lumen.core.registry import (
    Capability,
    CapabilityKind,
    CapabilityStatus,
    Registry,
)


def _cap(name: str = "telegram", kind: CapabilityKind = CapabilityKind.CHANNEL) -> Capability:
    return Capability(
        kind=kind,
        name=name,
        description=f"{name} capability",
        status=CapabilityStatus.AVAILABLE,
    )


def test_imports_are_not_circular():
    """Smoke test: importing the whole chain must not raise.

    This protects against the events.py/registry.py cycle that
    broke the entire module at load time.
    """
    from lumen.core import awareness, brain, events, registry, runtime, watchers  # noqa: F401


def test_registry_emits_added_event_and_awareness_receives_it():
    registry = Registry()
    awareness = CapabilityAwareness(registry)

    assert not awareness.has_pending()

    registry.register(_cap())

    assert awareness.has_pending()
    assert awareness.has_pending_proactive()


def test_proactive_drain_does_not_empty_prompt_buffer():
    """Proactive and prompt drains are independent."""
    registry = Registry()
    awareness = CapabilityAwareness(registry)
    registry.register(_cap())

    proactive = awareness.format_for_proactive()
    assert proactive is not None
    assert "telegram" in proactive

    # Proactive drained, but prompt section still has the event
    assert not awareness.has_pending_proactive()
    assert awareness.has_pending()

    prompt = awareness.format_for_prompt()
    assert prompt is not None
    assert "telegram" in prompt
    assert not awareness.has_pending()


def test_unregister_emits_removed_event():
    registry = Registry()
    awareness = CapabilityAwareness(registry)
    registry.register(_cap())
    awareness.format_for_prompt()  # drain additions

    registry.unregister(CapabilityKind.CHANNEL, "telegram")

    prompt = awareness.format_for_prompt()
    assert prompt is not None
    assert "telegram" in prompt
    assert "- " in prompt  # removal marker from CapabilityEvent.summary()


def test_status_change_emits_event():
    registry = Registry()
    awareness = CapabilityAwareness(registry)
    registry.register(_cap(kind=CapabilityKind.MCP, name="demo"))
    awareness.format_for_prompt()  # drain additions

    registry.update_status(CapabilityKind.MCP, "demo", CapabilityStatus.READY)

    prompt = awareness.format_for_prompt()
    assert prompt is not None
    assert "demo" in prompt
    assert "ready" in prompt


def test_double_register_does_not_reemit():
    registry = Registry()
    awareness = CapabilityAwareness(registry)
    registry.register(_cap())
    awareness.format_for_prompt()

    # Re-registering the same capability must not fire a duplicate event
    registry.register(_cap())
    assert not awareness.has_pending()
