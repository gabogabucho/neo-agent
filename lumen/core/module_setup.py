"""Module setup — chat-driven onboarding for modules that need env vars.

When a module declares ``x-lumen.runtime.env`` in its manifest, each entry
lists something Lumen needs from the user (typically a token or id) before
the module can work. This file normalizes the declaration and turns it into
a runtime flow that Lumen runs in chat to collect the missing pieces.

Two public helpers:

- ``parse_env_specs(raw)`` — accepts the legacy list-of-strings format or
  the richer list-of-objects format and always returns a list of ``EnvSpec``.
- ``build_setup_flow(module_name, specs)`` — produces the flow dict that
  Brain's flow system can execute to ask the user for each spec.

Wiring into the installer (trigger + persistence + module reload) is done
elsewhere; this module is intentionally pure so it can be tested without
spinning up a runtime.
"""

from __future__ import annotations

import os
import importlib.util
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from lumen.core.interoperability import INTEROP_OPAQUE
from lumen.core.module_manifest import load_module_manifest


@dataclass(frozen=True)
class EnvSpec:
    """A single env var a module needs from the user."""

    name: str
    label: str
    hint: str
    secret: bool
    expected_type: str = "text"
    pattern: str = ""
    examples: list[str] = field(default_factory=list)
    format_guidance: str = ""

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "name": self.name,
            "label": self.label,
            "hint": self.hint,
            "secret": self.secret,
            "expected_type": self.expected_type,
        }
        if self.pattern:
            payload["pattern"] = self.pattern
        if self.examples:
            payload["examples"] = list(self.examples)
        if self.format_guidance:
            payload["format_guidance"] = self.format_guidance
        return payload


def parse_env_specs(raw: Any) -> list[EnvSpec]:
    """Normalize ``x-lumen.runtime.env`` entries into ``EnvSpec`` objects.

    Accepted inputs:

    - ``None`` → ``[]``
    - ``["FOO", "BAR"]`` (legacy) → each string becomes a spec with a
      sensible default label and no hint.
    - ``[{"name": "FOO", "label": "...", "hint": "...", "secret": true}]``
      (rich) → fields honored, missing fields filled in.
    - Mixed lists (some strings, some dicts) are allowed.
    """

    if not raw:
        return []

    specs: list[EnvSpec] = []
    for entry in raw:
        if isinstance(entry, str):
            name = entry.strip()
            if not name:
                continue
            specs.append(
                EnvSpec(
                    name=name,
                    label=_humanize(name),
                    hint="",
                    secret=_looks_like_secret(name),
                    expected_type="text",
                )
            )
            continue
        if isinstance(entry, dict):
            name = str(entry.get("name") or "").strip()
            if not name:
                continue
            specs.append(
                EnvSpec(
                    name=name,
                    label=str(entry.get("label") or _humanize(name)),
                    hint=str(entry.get("hint") or ""),
                    secret=bool(
                        entry.get("secret", _looks_like_secret(name))
                    ),
                    expected_type=str(
                        entry.get("expected_type")
                        or entry.get("type")
                        or "text"
                    ).strip()
                    or "text",
                    pattern=str(entry.get("pattern") or "").strip(),
                    examples=_normalize_examples(entry.get("examples")),
                    format_guidance=str(
                        entry.get("format_guidance")
                        or entry.get("format")
                        or ""
                    ).strip(),
                )
            )
    return specs


def missing_env_specs(
    specs: list[EnvSpec],
    config: dict | None = None,
) -> list[EnvSpec]:
    """Return the subset of ``specs`` with no value yet.

    A spec is considered satisfied if any of these is true:
      - it exists in ``os.environ``
      - it exists in ``config["secrets"][<module>]`` (caller pre-flattens)
      - it exists as a top-level key in ``config`` (api_key_env style)
    """

    config = config or {}
    flat_secrets: dict[str, str] = {}
    for bucket in (config.get("secrets") or {}).values():
        if isinstance(bucket, dict):
            flat_secrets.update({str(k): str(v) for k, v in bucket.items()})

    missing: list[EnvSpec] = []
    for spec in specs:
        if os.environ.get(spec.name):
            continue
        if flat_secrets.get(spec.name):
            continue
        if config.get(spec.name):
            continue
        missing.append(spec)
    return missing


def build_setup_flow(
    module_name: str,
    specs: list[EnvSpec],
) -> dict[str, Any]:
    """Build a flow dict that Brain can run to collect ``specs`` via chat.

    The flow declares one slot per spec. The ``ask`` combines label and hint
    so the user sees context when Lumen asks. ``on_complete`` carries the
    module name so the handler knows where to persist the captured values.
    """

    if not module_name:
        raise ValueError("module_name is required")

    slots: dict[str, Any] = {}
    for spec in specs:
        ask = _build_slot_prompt(spec)
        slots[spec.name] = {
            "ask": ask,
            "type": "text",
            "required": True,
            "secret": spec.secret,
        }

    return {
        "intent": f"module-setup-{module_name}",
        "triggers": [f"setup:{module_name}"],
        "slots": slots,
        "on_complete": f"save_module_env:{module_name}",
        "first_message": (
            f"Para que *{module_name}* funcione necesito algunos datos. "
            "Te los pido de a uno."
        ),
    }


def env_specs_from_manifest(manifest: dict | None) -> list[EnvSpec]:
    """Extract and normalize env specs from a loaded module manifest.

    Reads ``manifest["x-lumen"]["runtime"]["env"]`` and returns the parsed
    specs. Returns an empty list if the manifest is missing the field or if
    the module doesn't declare any env requirements.
    """
    if not isinstance(manifest, dict):
        return []
    x_lumen = manifest.get("x-lumen") or manifest.get("x_lumen") or {}
    if not isinstance(x_lumen, dict):
        return []
    runtime = x_lumen.get("runtime") or {}
    if not isinstance(runtime, dict):
        return []
    return parse_env_specs(runtime.get("env"))


def supports_chat_setup(manifest: dict | None) -> bool:
    """Return whether this manifest is eligible for chat-driven setup."""
    if not isinstance(manifest, dict):
        return False
    specs = env_specs_from_manifest(manifest)
    if not specs:
        return False

    x_lumen = manifest.get("x-lumen") or manifest.get("x_lumen") or {}
    interoperability = {}
    if isinstance(x_lumen, dict):
        interoperability = x_lumen.get("interoperability") or {}
    level = str((interoperability or {}).get("level") or "").strip().lower()
    return level != INTEROP_OPAQUE


def pending_setup_for_manifest(
    module_name: str,
    manifest: dict | None,
    config: dict | None = None,
    *,
    module_dir: Path | None = None,
) -> dict | None:
    """Return pending-setup info for a module, or ``None`` if nothing is missing.

    Shape of the returned dict::

        {
          "module": <module_name>,
          "env_specs": [<EnvSpec.to_dict()>, ...],
          "flow": <flow dict produced by build_setup_flow>,
        }
    """
    if not supports_chat_setup(manifest):
        return None
    specs = env_specs_from_manifest(manifest)
    if not specs:
        return None
    missing = missing_env_specs(specs, config)
    if not missing:
        readiness = run_module_setup_readiness_check(
            module_name,
            manifest,
            config,
            module_dir=module_dir,
        )
        if readiness is None:
            return None
        return {
            "module": module_name,
            "env_specs": [],
            "flow": None,
            "readiness": readiness,
        }
    return {
        "module": module_name,
        "env_specs": [spec.to_dict() for spec in missing],
        "flow": build_setup_flow(module_name, missing),
    }


def collect_pending_setup_flows(
    modules_dir: Path,
    config: dict | None = None,
) -> list[dict[str, Any]]:
    """Collect active chat setup flows for installed modules."""
    if not modules_dir.exists():
        return []

    flows: list[dict[str, Any]] = []
    for module_dir in sorted(modules_dir.iterdir(), key=lambda item: item.name):
        if not module_dir.is_dir() or module_dir.name.startswith("_"):
            continue
        _, manifest = load_module_manifest(module_dir)
        module_name = str((manifest or {}).get("name") or module_dir.name)
        pending = pending_setup_for_manifest(
            module_name,
            manifest,
            config,
            module_dir=module_dir,
        )
        if pending and pending.get("flow"):
            flows.append(dict(pending["flow"]))
    return flows


def normalize_module_setup_values(
    values: dict[str, Any] | None,
    *,
    module_name: str,
    manifest: dict | None = None,
    specs: list[EnvSpec] | None = None,
    module_dir: Path | None = None,
    config: dict | None = None,
) -> dict[str, Any]:
    """Normalize and validate captured setup values before persistence."""

    values = values or {}
    resolved_specs = specs if specs is not None else env_specs_from_manifest(manifest)
    spec_map = {spec.name: spec for spec in resolved_specs}
    normalized: dict[str, str] = {}
    errors: dict[str, str] = {}

    for name, raw_value in values.items():
        spec = spec_map.get(name)
        if spec is None:
            continue
        result = _normalize_single_value(
            raw_value,
            spec,
            module_name=module_name,
            manifest=manifest,
            module_dir=module_dir,
            config=config,
        )
        if result.get("error"):
            errors[name] = str(result["error"])
            continue
        value = str(result.get("value") or "").strip()
        if value:
            normalized[name] = value

    return {
        "values": normalized,
        "errors": errors,
        "accepted": sorted(normalized.keys()),
    }


def merge_module_setup_config(
    config: dict | None,
    module_name: str,
    values: dict[str, Any] | None,
    *,
    manifest: dict | None = None,
    specs: list[EnvSpec] | None = None,
    module_dir: Path | None = None,
    config_for_validation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a config copy with captured module setup values persisted.

    Values are stored under ``config["secrets"][module_name]`` keyed by the
    declared env var names. Unknown keys and blank values are ignored.
    """

    if not module_name:
        raise ValueError("module_name is required")

    config = config or {}
    resolved_specs = specs if specs is not None else env_specs_from_manifest(manifest)
    normalized = normalize_module_setup_values(
        values,
        module_name=module_name,
        manifest=manifest,
        specs=resolved_specs,
        module_dir=module_dir,
        config=config_for_validation or config,
    )
    allowed_names = {spec.name for spec in resolved_specs}

    merged: dict[str, Any] = dict(config)
    secrets = {
        str(bucket_name): dict(bucket)
        for bucket_name, bucket in (config.get("secrets") or {}).items()
        if isinstance(bucket, dict)
    }
    module_bucket = dict(secrets.get(module_name) or {})

    for name in allowed_names:
        raw_value = normalized["values"].get(name)
        if raw_value is None:
            continue
        value = str(raw_value).strip()
        if not value:
            continue
        module_bucket[name] = value

    if module_bucket:
        secrets[module_name] = module_bucket

    if secrets:
        merged["secrets"] = secrets
    return merged


def _humanize(env_name: str) -> str:
    """Turn ``TELEGRAM_BOT_TOKEN`` into ``Telegram bot token``."""
    parts = [p for p in env_name.replace("-", "_").split("_") if p]
    if not parts:
        return env_name
    head, *rest = parts
    return " ".join([head.capitalize(), *[p.lower() for p in rest]])


_SECRET_HINTS = ("TOKEN", "KEY", "SECRET", "PASSWORD", "PASS")


def _looks_like_secret(env_name: str) -> bool:
    upper = env_name.upper()
    return any(hint in upper for hint in _SECRET_HINTS)


def run_module_setup_readiness_check(
    module_name: str,
    manifest: dict | None,
    config: dict | None,
    *,
    module_dir: Path | None = None,
) -> dict[str, Any] | None:
    module = _load_runtime_module(module_name, module_dir)
    if module is None:
        return None

    checker = getattr(module, "check_setup_readiness", None)
    if not callable(checker):
        return None

    context = _build_setup_context(module_name, manifest, config, module_dir)
    result = checker(context)
    if result is None or result is True:
        return None
    if result is False:
        return {"status": "failed", "reason": "Smoke/readiness check failed."}
    if isinstance(result, str):
        reason = result.strip()
        return {"status": "failed", "reason": reason or "Smoke/readiness check failed."}
    if isinstance(result, dict):
        ok = result.get("ok")
        if ok in {None, True}:
            return None
        reason = str(result.get("reason") or result.get("message") or "").strip()
        payload = {"status": "failed", "reason": reason or "Smoke/readiness check failed."}
        if result.get("details") is not None:
            payload["details"] = result.get("details")
        return payload
    return {"status": "failed", "reason": "Smoke/readiness check failed."}


def _build_slot_prompt(spec: EnvSpec) -> str:
    parts = [spec.label]
    if spec.hint:
        parts.append(spec.hint)
    parts.append("Respondé con el valor crudo únicamente, sin frases, sin etiquetas y sin explicación.")

    format_bits: list[str] = []
    if spec.format_guidance:
        format_bits.append(spec.format_guidance)
    elif spec.expected_type and spec.expected_type not in {"text", "string"}:
        format_bits.append(f"Tipo esperado: {spec.expected_type}.")
    if spec.pattern:
        format_bits.append(f"Patrón esperado: {spec.pattern}")
    if spec.examples:
        examples = ", ".join(f"`{example}`" for example in spec.examples[:2])
        format_bits.append(f"Ejemplo: {examples}")
    if format_bits:
        parts.append("Formato: " + " ".join(bit.strip() for bit in format_bits if bit.strip()))
    return "\n".join(parts)


def _normalize_examples(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else []
    if isinstance(raw, list):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


def _normalize_single_value(
    raw_value: Any,
    spec: EnvSpec,
    *,
    module_name: str,
    manifest: dict | None,
    module_dir: Path | None,
    config: dict | None,
) -> dict[str, Any]:
    value = _strip_common_wrappers(raw_value)
    if not value:
        return {"error": f"{spec.label}: falta un valor."}

    value = _apply_expected_type(value, spec)
    if spec.pattern:
        pattern_result = _coerce_pattern_match(value, spec)
        if pattern_result is None:
            return {"error": _validation_error_message(spec)}
        value = pattern_result

    hook_result = _run_setup_value_hook(
        module_name,
        manifest,
        config,
        module_dir=module_dir,
        spec=spec,
        value=value,
    )
    if hook_result.get("error"):
        return hook_result
    return {"value": hook_result.get("value", value)}


def _strip_common_wrappers(raw_value: Any) -> str:
    value = str(raw_value or "").strip()
    if value.startswith("```") and value.endswith("```"):
        value = value[3:-3].strip()
    while len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'", "`"}:
        value = value[1:-1].strip()
    return value


def _apply_expected_type(value: str, spec: EnvSpec) -> str:
    expected_type = (spec.expected_type or "text").strip().lower()
    if expected_type in {"int", "integer"}:
        if not re.fullmatch(r"[+-]?\d+", value):
            return value
        return str(int(value))
    return value.strip()


def _coerce_pattern_match(value: str, spec: EnvSpec) -> str | None:
    try:
        pattern = re.compile(spec.pattern)
    except re.error:
        return value if value else None
    if pattern.fullmatch(value):
        return value
    matches = list(pattern.finditer(value))
    if len(matches) != 1:
        return None
    return matches[0].group(0).strip() or None


def _validation_error_message(spec: EnvSpec) -> str:
    details: list[str] = []
    if spec.format_guidance:
        details.append(spec.format_guidance)
    if spec.examples:
        details.append("Ejemplo: " + ", ".join(spec.examples[:2]))
    suffix = f" {'. '.join(details)}" if details else ""
    return f"{spec.label}: el valor no tiene el formato esperado.{suffix}".strip()


def _run_setup_value_hook(
    module_name: str,
    manifest: dict | None,
    config: dict | None,
    *,
    module_dir: Path | None,
    spec: EnvSpec,
    value: str,
) -> dict[str, Any]:
    module = _load_runtime_module(module_name, module_dir)
    if module is None:
        return {"value": value}

    normalizer = getattr(module, "normalize_setup_value", None)
    if not callable(normalizer):
        return {"value": value}

    context = _build_setup_context(module_name, manifest, config, module_dir)
    result = normalizer(context, spec.to_dict(), value)
    if result is None or result is True:
        return {"value": value}
    if result is False:
        return {"error": _validation_error_message(spec)}
    if isinstance(result, str):
        normalized = result.strip()
        return {"value": normalized} if normalized else {"error": _validation_error_message(spec)}
    if isinstance(result, dict):
        if result.get("ok") is False:
            return {"error": str(result.get("error") or result.get("message") or _validation_error_message(spec)).strip()}
        normalized = str(result.get("value") or value).strip()
        return {"value": normalized} if normalized else {"error": _validation_error_message(spec)}
    return {"value": value}


def _load_runtime_module(module_name: str, module_dir: Path | None):
    if module_dir is None:
        return None
    connector_path = module_dir / "connector.py"
    if not connector_path.exists():
        return None
    spec = importlib.util.spec_from_file_location(
        f"lumen_setup_module_{module_name.replace('-', '_')}", connector_path
    )
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_setup_context(
    module_name: str,
    manifest: dict | None,
    config: dict | None,
    module_dir: Path | None,
) -> Any:
    from lumen.core.module_runtime import ModuleRuntimeContext

    resolved_dir = module_dir or Path.cwd()
    return ModuleRuntimeContext(
        name=module_name,
        module_dir=resolved_dir,
        runtime_dir=resolved_dir / ".setup-check",
        manifest=manifest or {},
        config=config or {},
    )
