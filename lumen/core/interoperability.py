"""Interoperability classification for external capability adoption."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from lumen.core.registry import Capability


INTEROP_NATIVE = "native"
INTEROP_ADAPTED = "adapted"
INTEROP_OPAQUE = "opaque"


def classify_interoperability(
    *,
    source_type: str | None = None,
    metadata: dict[str, Any] | None = None,
    manifest_path: str | None = None,
    can_install: bool | None = None,
    install_spec: dict[str, Any] | None = None,
    remote_transport: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Classify how naturally an artifact can join Lumen's ecosystem."""
    metadata = metadata or {}
    explicit = _explicit_interoperability(metadata)
    if explicit is not None:
        return explicit

    source = str(source_type or metadata.get("source_type") or "").strip().lower()
    manifest_name = Path(manifest_path).name.lower() if manifest_path else ""
    transport_type = str((remote_transport or {}).get("type") or "").strip().lower()

    if transport_type and transport_type != "stdio":
        return _payload(
            INTEROP_OPAQUE,
            install_path="manual",
            summary="Known externally, but still requires manual or unsupported transport bridging.",
        )

    if manifest_name == "manifest.yaml":
        return _payload(
            INTEROP_ADAPTED,
            install_path="adapted",
            summary="Accepted through a compatibility layer instead of the native manifest path.",
        )

    if source in {"clawhub", "openclaw", "mcp-registry"}:
        level = INTEROP_ADAPTED if can_install is not False else INTEROP_OPAQUE
        install_path = "adapted" if level == INTEROP_ADAPTED else "manual"
        summary = (
            "Adopted from an external ecosystem through a lightweight adapter."
            if level == INTEROP_ADAPTED
            else "Visible to Lumen, but not naturally installable yet."
        )
        return _payload(level, install_path=install_path, summary=summary)

    if source in {"agent_skills", "module_manifest", "catalog_entry", "runtime"}:
        return _payload(
            INTEROP_NATIVE,
            install_path="native",
            summary="Already fits Lumen's install and runtime model.",
        )

    if install_spec:
        return _payload(
            INTEROP_ADAPTED,
            install_path="adapted",
            summary="Installable in Lumen through an adapter instead of a native artifact shape.",
        )

    return _payload(
        INTEROP_ADAPTED,
        install_path="adapted",
        summary="Adopted through a compatibility path by default.",
    )


def classify_capability_interoperability(
    capability: "Capability | dict[str, Any]",
) -> dict[str, str]:
    metadata = _read(capability, "metadata") or {}
    source_type = metadata.get("source_type") or _runtime_source_type(capability)
    return metadata.get("interoperability") or classify_interoperability(
        source_type=source_type,
        metadata=metadata,
        manifest_path=metadata.get("manifest_path") or metadata.get("path"),
        remote_transport=metadata.get("remote_transport"),
    )


def awareness_interoperability_note(
    capability: "Capability | dict[str, Any] | dict[str, str]",
) -> dict[str, str] | None:
    interoperability = capability
    if not isinstance(capability, dict) or "level" not in capability:
        interoperability = classify_capability_interoperability(capability)

    level = str((interoperability or {}).get("level") or "").strip().lower()
    if level == INTEROP_ADAPTED:
        return {
            "label": "adapted",
            "summary": "adapted into Lumen from an external ecosystem",
            "sentence": "It reaches me through an adapted bridge instead of a native path.",
        }
    if level == INTEROP_OPAQUE:
        return {
            "label": "opaque",
            "summary": "still opaque to Lumen from an external ecosystem",
            "sentence": "I can see it, but it still depends on an opaque external bridge.",
        }
    return None


def _explicit_interoperability(metadata: dict[str, Any]) -> dict[str, str] | None:
    explicit = metadata.get("interoperability")
    if isinstance(explicit, dict) and explicit.get("level"):
        return dict(explicit)
    x_lumen = metadata.get("x-lumen") or metadata.get("x_lumen") or {}
    explicit = x_lumen.get("interoperability")
    if isinstance(explicit, dict) and explicit.get("level"):
        return dict(explicit)
    return None


def _payload(level: str, *, install_path: str, summary: str) -> dict[str, str]:
    return {
        "level": level,
        "label": level.title(),
        "install_path": install_path,
        "summary": summary,
    }


def _read(capability: "Capability | dict[str, Any]", field: str) -> Any:
    if isinstance(capability, dict):
        return capability.get(field)
    return getattr(capability, field)


def _value(raw: Any) -> Any:
    return getattr(raw, "value", raw)


def _runtime_source_type(capability: "Capability | dict[str, Any]") -> str:
    kind = str(_value(_read(capability, "kind")) or "").strip().lower()
    if kind in {"skill", "module", "mcp", "connector", "channel"}:
        return "runtime"
    return kind or "runtime"
