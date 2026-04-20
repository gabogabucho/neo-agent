"""Artifact setup — unified chat-driven onboarding for ANY installable artifact.

Lumen can install four kinds of artifacts:

- ``native``   — x-lumen modules (declare env via ``x-lumen.runtime.env``)
- ``mcp``      — MCP servers (env lives in ``mcp_config.servers[id].env``)
- ``external`` — third-party installers (ClawHub / npx) that own their own
  credential prompts; Lumen only shows manual instructions
- ``manual``   — opaque artifacts with a human-in-the-loop setup note

All four share the same lifecycle: "something is missing → ask the user →
persist → reload". This file introduces the single abstraction that lets
Brain drive that lifecycle uniformly, regardless of source.

The existing native path (``module_setup.py``) keeps working unchanged —
this module is additive. As later phases land, ``build_setup_flow`` will
start consuming ``ArtifactSetupContract`` under the hood, and the new MCP
sink will plug in without any schema churn for modules that already work.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lumen.core.module_setup import (
    EnvSpec,
    build_setup_flow,
    env_specs_from_manifest,
    missing_env_specs,
    parse_env_specs,
)


KIND_NATIVE = "native"
KIND_MCP = "mcp"
KIND_EXTERNAL = "external"
KIND_MANUAL = "manual"

_VALID_KINDS = frozenset({KIND_NATIVE, KIND_MCP, KIND_EXTERNAL, KIND_MANUAL})


@dataclass(frozen=True)
class ArtifactSetupContract:
    """Everything Brain needs to run a setup flow for one artifact.

    ``kind`` + ``artifact_id`` uniquely identify the target. ``specs`` is the
    list of env values still to collect from the user (may be empty for
    ``manual``/``external``). ``sink`` is an opaque descriptor that later
    phases will hand to the corresponding persister — Brain itself does not
    interpret it. ``manual_instructions`` is set only for ``manual``/
    ``external`` contracts that have nothing to capture and just need to
    display guidance.
    """

    kind: str
    artifact_id: str
    display_name: str
    specs: list[EnvSpec] = field(default_factory=list)
    sink: dict[str, Any] = field(default_factory=dict)
    manual_instructions: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise ValueError(
                f"invalid artifact kind '{self.kind}'; "
                f"expected one of {sorted(_VALID_KINDS)}"
            )
        if not self.artifact_id:
            raise ValueError("artifact_id is required")

    @property
    def action_string(self) -> str:
        """The ``on_complete`` action Brain should execute when slots fill."""
        return f"save_artifact_env:{self.kind}:{self.artifact_id}"

    @property
    def legacy_action_string(self) -> str:
        """Backwards-compatible action string used by the native path.

        Pre-existing flows and any serialized sessions still reference
        ``save_module_env:{module}``. The action dispatcher in Brain treats
        this as an alias of ``save_artifact_env:native:{module}``.
        """
        return f"save_module_env:{self.artifact_id}"

    def has_pending_values(self) -> bool:
        return bool(self.specs)

    def is_manual_only(self) -> bool:
        return self.kind in {KIND_MANUAL, KIND_EXTERNAL} and not self.specs


def contract_from_native_manifest(
    module_name: str,
    manifest: dict | None,
    config: dict | None = None,
) -> ArtifactSetupContract | None:
    """Build a contract for a native x-lumen module.

    Returns ``None`` when the manifest does not declare any env requirements
    — callers should treat that as "nothing to configure". A contract with
    an empty ``specs`` list is still returned when the module declares env
    but everything is already satisfied; callers that only care about
    pending setup should check ``has_pending_values()``.
    """

    if not isinstance(manifest, dict):
        return None

    specs = env_specs_from_manifest(manifest)
    if not specs:
        return None

    pending = missing_env_specs(specs, config or {})
    display = str(
        manifest.get("display_name")
        or manifest.get("name")
        or module_name
    ).strip() or module_name

    return ArtifactSetupContract(
        kind=KIND_NATIVE,
        artifact_id=module_name,
        display_name=display,
        specs=list(pending),
        sink={"type": "native_secrets", "module_name": module_name},
    )


def contract_from_mcp_server(
    server_id: str,
    server_config: dict | None,
    *,
    overlay: dict | None = None,
    config: dict | None = None,
) -> ArtifactSetupContract | None:
    """Build a contract for an MCP server.

    Env specs are resolved from three sources, in order of precedence:

    1. ``overlay["env"]`` — curated metadata shipped under
       ``lumen/catalog/mcp_overrides/<server_id>.yaml``. Same shape as
       ``x-lumen.runtime.env`` (list of dicts with name/label/hint/secret/
       pattern/examples/format_guidance).
    2. ``server_config["x-lumen-env"]`` — the server author's own
       annotations (forward-compatible for a future MCP schema extension).
    3. ``server_config["env"]`` keys with blank or missing values —
       structural fallback. Specs get default metadata (``_looks_like_secret``
       decides the secret flag by name).

    Returns ``None`` when there is nothing to declare. A contract with an
    empty ``specs`` list is returned when the server declares env but all
    values are present — callers should check ``has_pending_values()``.
    """

    if not server_id or not isinstance(server_config, dict):
        return None

    current_env = server_config.get("env") or {}
    if not isinstance(current_env, dict):
        current_env = {}

    specs: list[EnvSpec] = []
    if overlay and isinstance(overlay.get("env"), list):
        specs = parse_env_specs(overlay["env"])
    if not specs and isinstance(server_config.get("x-lumen-env"), list):
        specs = parse_env_specs(server_config["x-lumen-env"])
    if not specs and current_env:
        specs = parse_env_specs(list(current_env.keys()))

    if not specs:
        return None

    def _is_blank(value: Any) -> bool:
        return value is None or str(value).strip() == ""

    pending = [spec for spec in specs if _is_blank(current_env.get(spec.name))]

    display = str(
        (overlay or {}).get("display_name")
        or server_config.get("display_name")
        or server_config.get("description")
        or server_id
    ).strip() or server_id

    return ArtifactSetupContract(
        kind=KIND_MCP,
        artifact_id=server_id,
        display_name=display,
        specs=pending,
        sink={"type": "mcp_server_env", "server_id": server_id},
    )


def load_mcp_overlay(server_id: str, pkg_dir: Path | str | None) -> dict | None:
    """Load an MCP overlay YAML by server id, if one exists.

    Overlays live at ``<pkg_dir>/catalog/mcp_overrides/<server_id>.yaml``.
    Missing file, unreadable file, or empty payload all return ``None`` —
    callers treat that as "no curated metadata" and fall back to the
    server's own declarations.
    """

    if not server_id or not pkg_dir:
        return None
    try:
        import yaml  # local import keeps the module lean for tests
    except ImportError:
        return None

    overlay_path = Path(pkg_dir) / "catalog" / "mcp_overrides" / f"{server_id}.yaml"
    if not overlay_path.is_file():
        return None
    try:
        data = yaml.safe_load(overlay_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def build_flow_from_contract(contract: ArtifactSetupContract) -> dict[str, Any] | None:
    """Build a runtime flow for a setup contract with pending values."""

    if not contract.has_pending_values():
        return None

    flow = build_setup_flow(
        contract.artifact_id,
        contract.specs,
        display_name=contract.display_name,
        kind=contract.kind,
    )
    if contract.kind == KIND_NATIVE:
        return flow

    flow["intent"] = f"artifact-setup-{contract.kind}-{contract.artifact_id}"
    flow["triggers"] = [
        f"setup:{contract.artifact_id}",
        f"setup:{contract.kind}:{contract.artifact_id}",
    ]
    flow["on_complete"] = contract.action_string
    return flow


def pending_setup_from_contract(contract: ArtifactSetupContract | None) -> dict[str, Any] | None:
    """Project a contract into the discovery/readiness payload shape."""

    if contract is None:
        return None
    if contract.is_manual_only():
        return {
            "kind": contract.kind,
            "artifact_id": contract.artifact_id,
            "display_name": contract.display_name,
            "env_specs": [],
            "flow": None,
            "manual_instructions": contract.manual_instructions,
        }
    if not contract.has_pending_values():
        return None
    return {
        "kind": contract.kind,
        "artifact_id": contract.artifact_id,
        "display_name": contract.display_name,
        "env_specs": [spec.to_dict() for spec in contract.specs],
        "flow": build_flow_from_contract(contract),
    }


def collect_pending_artifact_setup_flows(
    pkg_dir: Path,
    config: dict | None = None,
) -> list[dict[str, Any]]:
    """Collect pending native + MCP setup flows from the unified contract layer."""

    config = config or {}
    flows: list[dict[str, Any]] = []

    modules_dir = pkg_dir / "modules"
    if modules_dir.exists():
        from lumen.core.module_manifest import load_module_manifest

        for module_dir in sorted(modules_dir.iterdir(), key=lambda item: item.name):
            if not module_dir.is_dir() or module_dir.name.startswith("_"):
                continue
            _, manifest = load_module_manifest(module_dir)
            module_name = str((manifest or {}).get("name") or module_dir.name)
            contract = contract_from_native_manifest(module_name, manifest, config)
            flow = build_flow_from_contract(contract) if contract else None
            if flow:
                flows.append(flow)

    mcp_servers = ((config.get("mcp") or {}).get("servers") or {})
    if isinstance(mcp_servers, dict):
        for server_id in sorted(mcp_servers):
            server_config = mcp_servers.get(server_id)
            contract = contract_from_mcp_server(
                server_id,
                server_config,
                overlay=load_mcp_overlay(server_id, pkg_dir),
            )
            flow = build_flow_from_contract(contract) if contract else None
            if flow:
                flows.append(flow)

    return flows


def parse_artifact_action(action: str) -> tuple[str, str] | None:
    """Parse a Brain ``on_complete`` action string into ``(kind, artifact_id)``.

    Accepts both the new ``save_artifact_env:{kind}:{id}`` format and the
    legacy ``save_module_env:{id}`` (treated as ``native``). Returns
    ``None`` if the action is not a setup action at all.
    """

    if not action:
        return None
    action = action.strip()
    if action.startswith("save_artifact_env:"):
        rest = action[len("save_artifact_env:"):]
        parts = rest.split(":", 1)
        if len(parts) != 2:
            return None
        kind, artifact_id = parts[0].strip(), parts[1].strip()
        if kind not in _VALID_KINDS or not artifact_id:
            return None
        return (kind, artifact_id)
    if action.startswith("save_module_env:"):
        artifact_id = action[len("save_module_env:"):].strip()
        if not artifact_id:
            return None
        return (KIND_NATIVE, artifact_id)
    return None
