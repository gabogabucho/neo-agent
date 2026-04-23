"""API Key Management — generate, verify, list, revoke.

Keys are stored hashed (SHA-256) in api_keys.yaml.
Only the prefix (first 8 chars) is stored for identification.
The plaintext key is shown ONCE at generation and never again.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _default_keys_path() -> Path:
    """Default path for API keys file."""
    from lumen.core.paths import resolve_lumen_dir
    return resolve_lumen_dir() / "api_keys.yaml"


def _load_keys(keys_path: Path | None = None) -> list[dict]:
    """Load keys from the YAML file."""
    path = keys_path or _default_keys_path()
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return data.get("keys", [])


def _save_keys(keys: list[dict], keys_path: Path | None = None) -> None:
    """Save keys to the YAML file."""
    path = keys_path or _default_keys_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump({"keys": keys}, default_flow_style=False),
        encoding="utf-8",
    )


def _hash_key(key: str) -> str:
    """Hash a key with SHA-256."""
    return hashlib.sha256(key.encode()).hexdigest()


def generate_api_key(
    label: str,
    *,
    keys_path: Path | None = None,
) -> dict:
    """Generate a new API key, store its hash, return the plaintext key.

    Returns: {"key": "...", "prefix": "...", "label": "..."}
    The "key" is the only time the plaintext is available.
    """
    # Generate a secure random key
    raw_key = secrets.token_urlsafe(32)
    prefix = raw_key[:8]
    key_hash = _hash_key(raw_key)

    keys = _load_keys(keys_path)
    keys.append({
        "label": label,
        "key_hash": key_hash,
        "prefix": prefix,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_keys(keys, keys_path)

    return {
        "key": raw_key,
        "prefix": prefix,
        "label": label,
    }


def verify_api_key(key: str, *, keys_path: Path | None = None) -> bool:
    """Verify a plaintext key against stored hashes.

    Returns True if the key matches any stored hash.
    """
    if not key:
        return False

    keys = _load_keys(keys_path)
    key_hash = _hash_key(key)
    return any(k.get("key_hash") == key_hash for k in keys)


def list_api_keys(*, keys_path: Path | None = None) -> list[dict]:
    """List all API keys (prefix and label only, no hash or key)."""
    keys = _load_keys(keys_path)
    return [
        {
            "label": k.get("label", ""),
            "prefix": k.get("prefix", ""),
            "created_at": k.get("created_at", ""),
        }
        for k in keys
    ]


def revoke_api_key(prefix: str, *, keys_path: Path | None = None) -> bool:
    """Revoke an API key by its prefix.

    Returns True if a key was removed, False if not found.
    """
    keys = _load_keys(keys_path)
    before = len(keys)
    keys = [k for k in keys if k.get("prefix") != prefix]
    _save_keys(keys, keys_path)
    return len(keys) < before
