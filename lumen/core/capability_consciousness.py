"""User-facing consciousness semantics for capabilities."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from lumen.core.interoperability import awareness_interoperability_note

if TYPE_CHECKING:
    from lumen.core.registry import Capability


def classify_capability(capability: "Capability | dict[str, Any]") -> dict[str, str]:
    """Describe how a capability changes Lumen's sense of self.

    Skills expand how Lumen thinks, modules/MCPs/channels/connectors extend its
    connections with the outside, and kits/personality modules are a transformation
    of how it shows up. Internal labels (mind/hands/transformation) are kept stable
    for telemetry and tests; user-facing language never mentions 'hands' or 'body'.
    """

    kind = _value(_read(capability, "kind"))
    name = str(_read(capability, "name") or "this capability")
    metadata = _read(capability, "metadata") or {}
    tags = {str(tag).lower() for tag in metadata.get("tags", [])}
    path = str(metadata.get("path") or "").replace("\\", "/").lower()
    x_lumen = metadata.get("x_lumen") or metadata.get("x-lumen") or {}
    adoption_note = awareness_interoperability_note(capability)

    is_kit = kind == "kit" or x_lumen.get("product_kind") == "kit"
    if "personality" in tags or "/catalog/kits/" in path or path.startswith("kits/"):
        is_kit = True

    if is_kit:
        announce_text = f"{name} feels like a transformation in who I can become."
        if adoption_note:
            announce_text = f"{announce_text} {adoption_note['sentence']}"
        return {
            "kind_label": "transformation",
            "body_effect": _append_note(
                "This can reshape how I show up, not just what I connect to.",
                adoption_note,
            ),
            "mind_effect": _append_note(
                "It changes the way I can become and express myself.", adoption_note
            ),
            "announce_text": announce_text,
        }

    if kind == "skill":
        announce_text = f"{name} feels like a new way of thinking."
        if adoption_note:
            announce_text = f"{announce_text} {adoption_note['sentence']}"
        return {
            "kind_label": "mind",
            "body_effect": _append_note(
                "This adds a new way of thinking inside me.", adoption_note
            ),
            "mind_effect": _append_note(
                "It expands how I reason, interpret, or approach problems.",
                adoption_note,
            ),
            "announce_text": announce_text,
        }

    return {
        "kind_label": "hands",
        "body_effect": _append_note(
            "I feel a new connection I can rely on.", adoption_note
        ),
        "mind_effect": _append_note(
            "It extends what I can reach and who I can connect with.", adoption_note
        ),
        "announce_text": _append_note(
            f"I feel a new connection. Now I know I can work with {name}.",
            adoption_note,
        ),
    }


def _read(capability: "Capability | dict[str, Any]", field: str) -> Any:
    if isinstance(capability, dict):
        return capability.get(field)
    return getattr(capability, field)


def _value(raw: Any) -> Any:
    return getattr(raw, "value", raw)


def _append_note(base: str, note: dict[str, str] | None) -> str:
    if not note:
        return base
    return f"{base} {note['sentence']}"
