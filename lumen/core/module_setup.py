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
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class EnvSpec:
    """A single env var a module needs from the user."""

    name: str
    label: str
    hint: str
    secret: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "hint": self.hint,
            "secret": self.secret,
        }


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
        ask = spec.label
        if spec.hint:
            ask = f"{spec.label}\n{spec.hint}"
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
