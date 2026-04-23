"""Path resolution for Lumen — single source of truth for data directories.

Supports three modes:
1. Default:           ~/.lumen/                    (single instance, backward compat)
2. Named instance:    ~/.lumen/instances/<id>/     (--instance <id>)
3. Custom data dir:   <path>/                      (--data-dir <path>)
"""

from __future__ import annotations

from pathlib import Path

# Base directory for all Lumen data
LUMEN_BASE: Path = Path.home() / ".lumen"


def resolve_lumen_dir(
    *,
    instance: str | None = None,
    data_dir: str | Path | None = None,
    base_dir: Path | None = None,
) -> Path:
    """Resolve the Lumen data directory for a given configuration.

    Args:
        instance: Named instance ID (e.g. "cliente-01").
        data_dir: Custom data directory (overrides instance).
        base_dir: Override base dir (for testing).

    Returns:
        Resolved Path for the Lumen data directory.
    """
    base = base_dir or LUMEN_BASE

    if data_dir is not None:
        return Path(data_dir)

    if instance is not None:
        return base / "instances" / instance

    return base
