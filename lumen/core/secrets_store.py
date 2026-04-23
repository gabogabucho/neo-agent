"""Secrets store — persistent, file-based secrets for Lumen modules.

All module env values (tokens, API keys, chat IDs) live in
``~/.lumen/secrets.yaml``, separate from the user-visible ``config.yaml``.
"""

import os
import stat
from pathlib import Path

import yaml

LUMEN_DIR = Path.home() / ".lumen"
SECRETS_PATH = LUMEN_DIR / "secrets.yaml"


def configure_paths(*, lumen_dir: Path | None = None) -> None:
    """Override paths — used by tests or alternative data directories."""
    global LUMEN_DIR, SECRETS_PATH
    if lumen_dir is not None:
        LUMEN_DIR = lumen_dir
        SECRETS_PATH = lumen_dir / "secrets.yaml"


def _set_file_permissions(path: Path) -> None:
    """Restrict file to owner read/write only. Best-effort on Windows."""
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def load_all() -> dict[str, dict[str, str]]:
    """Load all secrets from the store. Returns {module_name: {key: value}}."""
    if not SECRETS_PATH.exists():
        return {}
    try:
        data = yaml.safe_load(SECRETS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (yaml.YAMLError, OSError):
        return {}


def load_module(module_name: str) -> dict[str, str]:
    """Load secrets for a single module."""
    all_secrets = load_all()
    bucket = all_secrets.get(module_name)
    return dict(bucket) if isinstance(bucket, dict) else {}


def save_module(module_name: str, values: dict[str, str]) -> None:
    """Save or update secrets for a single module."""
    all_secrets = load_all()
    bucket = all_secrets.get(module_name)
    if not isinstance(bucket, dict):
        bucket = {}
    bucket.update({k: str(v) for k, v in values.items() if v is not None})
    all_secrets[module_name] = bucket
    _write(all_secrets)


def delete_module(module_name: str) -> None:
    """Remove all secrets for a module."""
    all_secrets = load_all()
    all_secrets.pop(module_name, None)
    _write(all_secrets)


def delete_module_key(module_name: str, key: str) -> None:
    """Remove a single key from a module's secrets."""
    all_secrets = load_all()
    bucket = all_secrets.get(module_name)
    if isinstance(bucket, dict):
        bucket.pop(key, None)
        if not bucket:
            all_secrets.pop(module_name, None)
        _write(all_secrets)


def _write(all_secrets: dict) -> None:
    """Write the full secrets dict to disk."""
    LUMEN_DIR.mkdir(parents=True, exist_ok=True)
    SECRETS_PATH.write_text(
        yaml.dump(all_secrets, default_flow_style=False),
        encoding="utf-8",
    )
    _set_file_permissions(SECRETS_PATH)


def migrate_from_config(config: dict) -> tuple[dict, list[str]]:
    """Migrate module env values from config.yaml to secrets.yaml.

    Detects top-level keys matching ``x-lumen-*`` that contain env-var-like
    dicts, and the legacy ``secrets`` key. Returns (cleaned_config, migrated_modules).
    """
    migrated: list[str] = []
    all_secrets = load_all()
    changed = False

    # 1. Migrate legacy config["secrets"][module] entries
    existing_secrets = config.pop("secrets", None)
    if isinstance(existing_secrets, dict):
        for mod_name, bucket in existing_secrets.items():
            if isinstance(bucket, dict) and bucket:
                all_secrets.setdefault(mod_name, {}).update(bucket)
                migrated.append(mod_name)
                changed = True

    # 2. Migrate top-level x-lumen-* entries that look like env var dicts
    keys_to_remove: list[str] = []
    for key, value in list(config.items()):
        if (
            key.startswith("x-lumen-")
            and isinstance(value, dict)
            and any(k.isupper() for k in value)
        ):
            all_secrets.setdefault(key, {}).update(
                {str(k): str(v) for k, v in value.items()}
            )
            migrated.append(key)
            keys_to_remove.append(key)
            changed = True

    for key in keys_to_remove:
        config.pop(key, None)

    if changed:
        _write(all_secrets)

    return config, migrated
