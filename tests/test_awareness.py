"""End-to-end tests for the Capability Awareness pipeline.

Verifies that registry changes flow correctly into awareness outputs.
Protects against regressions like the circular import that broke
module boot (events.py ↔ registry.py).
"""

from __future__ import annotations

from lumen.core.awareness import CapabilityAwareness
from lumen.core.capability_consciousness import classify_capability
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
    summary = awareness.peek_summary()
    assert summary["counts"]["capability_discovered"] == 1
    assert "capability_connected" not in summary["counts"]


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
    assert "removed telegram" in prompt


def test_status_change_emits_event():
    registry = Registry()
    awareness = CapabilityAwareness(registry)
    registry.register(_cap(kind=CapabilityKind.MCP, name="demo"))
    awareness.format_for_prompt()  # drain additions

    registry.update_status(CapabilityKind.MCP, "demo", CapabilityStatus.READY)

    prompt = awareness.format_for_prompt()
    assert prompt is not None
    assert "demo" in prompt
    assert "connected" in prompt


def test_double_register_does_not_reemit():
    registry = Registry()
    awareness = CapabilityAwareness(registry)
    registry.register(_cap())
    awareness.format_for_prompt()

    # Re-registering the same capability must not fire a duplicate event
    registry.register(_cap())
    assert not awareness.has_pending()


def test_classifier_distinguishes_mind_hands_and_transformation():
    skill = _cap(name="planner", kind=CapabilityKind.SKILL)
    module = _cap(name="browser", kind=CapabilityKind.MODULE)
    kit = Capability(
        kind=CapabilityKind.MODULE,
        name="persona-shift",
        description="Personality kit",
        status=CapabilityStatus.READY,
        metadata={"tags": ["personality"], "path": "catalog/kits/persona-shift"},
    )

    assert classify_capability(skill)["kind_label"] == "mind"
    assert classify_capability(module)["kind_label"] == "hands"
    assert classify_capability(kit)["kind_label"] == "transformation"


def test_awareness_summary_exposes_structured_event_payloads():
    registry = Registry()
    awareness = CapabilityAwareness(registry)
    registry.register(
        Capability(
            kind=CapabilityKind.SKILL,
            name="faq",
            description="faq capability",
            status=CapabilityStatus.READY,
        )
    )

    summary = awareness.peek_summary()

    assert summary["pending"] == 2
    assert summary["effects"]["mind"] == 2
    assert summary["events"][1]["classification"]["kind_label"] == "mind"
    assert "new way of thinking" in summary["events"][1]["announce_text"]


def test_awareness_mentions_external_adoption_level_for_adapted_capabilities():
    registry = Registry()
    awareness = CapabilityAwareness(registry)
    registry.register(
        Capability(
            kind=CapabilityKind.SKILL,
            name="bridge-skill",
            description="external skill",
            status=CapabilityStatus.READY,
            metadata={
                "interoperability": {
                    "level": "adapted",
                    "label": "Adapted",
                    "install_path": "adapted",
                    "summary": "Adopted from an external ecosystem through a lightweight adapter.",
                }
            },
        )
    )

    proactive = awareness.format_for_proactive()
    prompt = awareness.format_for_prompt()
    summary = awareness.peek_summary()

    assert proactive is not None
    assert "adapted bridge" in proactive
    assert prompt is not None
    assert "adapted into Lumen from an external ecosystem" in prompt
    assert summary["pending"] == 0


def test_awareness_surfaces_pending_module_setup_state():
    registry = Registry()
    awareness = CapabilityAwareness(registry)
    registry.register(
        Capability(
            kind=CapabilityKind.MODULE,
            name="pending-module",
            description="Needs secrets",
            status=CapabilityStatus.AVAILABLE,
            metadata={
                "display_name": "Pending Module",
                "pending_setup": {
                    "module": "pending-module",
                    "env_specs": [{"name": "DEMO_TOKEN", "secret": True}],
                },
            },
        )
    )

    proactive = awareness.format_for_proactive()
    prompt = awareness.format_for_prompt()

    assert proactive is not None
    assert "still needs 1 setup value" in proactive
    assert prompt is not None
    assert "installed but not ready" in prompt
    assert "DEMO_TOKEN" in prompt


def test_awareness_surfaces_pending_mcp_setup_state():
    registry = Registry()
    awareness = CapabilityAwareness(registry)
    registry.register(
        Capability(
            kind=CapabilityKind.MCP,
            name="github",
            description="GitHub MCP",
            status=CapabilityStatus.AVAILABLE,
            metadata={
                "display_name": "GitHub",
                "pending_setup": {
                    "kind": "mcp",
                    "artifact_id": "github",
                    "env_specs": [
                        {"name": "GITHUB_PERSONAL_ACCESS_TOKEN", "secret": True}
                    ],
                },
            },
        )
    )

    proactive = awareness.format_for_proactive()
    prompt = awareness.format_for_prompt()

    assert proactive is not None
    assert "GitHub is installed" in proactive
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in proactive
    assert prompt is not None
    assert "installed but not ready" in prompt
    assert "GITHUB_PERSONAL_ACCESS_TOKEN" in prompt


def test_consciousness_defaults_plain_runtime_capabilities_to_native_interoperability():
    classification = classify_capability(
        Capability(
            kind=CapabilityKind.SKILL,
            name="plain-skill",
            description="plain capability",
            status=CapabilityStatus.READY,
        )
    )

    assert "adapted bridge" not in classification["announce_text"]
