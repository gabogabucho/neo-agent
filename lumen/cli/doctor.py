"""Lumen doctor — diagnose and fix common issues."""

import os
import stat
import sys
from pathlib import Path

import yaml
from rich.console import Console
from rich.panel import Panel

console = Console()

LUMEN_DIR = Path.home() / ".lumen"
CONFIG_PATH = LUMEN_DIR / "config.yaml"
SECRETS_PATH = LUMEN_DIR / "secrets.yaml"
PKG_DIR = Path(__file__).parent.parent.parent

BRAND = "#3d3d6d"


def _check(status: str, ok: bool, fix: str = "") -> dict:
    icon = "[green]ok[/green]" if ok else "[red]FAIL[/red]"
    line = f"  {icon}  {status}"
    if not ok and fix:
        line += f"\n       [dim]Fix: {fix}[/dim]"
    console.print(line)
    return {"status": status, "ok": ok, "fix": fix}


def _file_is_owner_only(path: Path) -> bool:
    """Check if file is readable only by owner. Best-effort."""
    try:
        mode = path.stat().st_mode
        # Check group and other have no read/write
        return not (mode & (stat.S_IRGRP | stat.S_IWGRP | stat.S_IROTH | stat.S_IWOTH))
    except OSError:
        return False


def run_doctor():
    """Run all diagnostic checks and suggest fixes."""
    console.print()
    console.print(f"  [bold {BRAND}]Lumen Doctor[/bold {BRAND}] — running diagnostics...")
    console.print(f"  {'─' * 45}")
    console.print()

    issues = []

    # 1. Config file exists
    if CONFIG_PATH.exists():
        _check("Config file exists", True)
    else:
        issues.append(
            _check("Config file exists", False, "Run 'lumen run' to start the setup wizard.")
        )

    # 2. Config is valid YAML
    if CONFIG_PATH.exists():
        try:
            config = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(config, dict):
                _check("Config is valid YAML", True)
            else:
                issues.append(
                    _check("Config is valid YAML", False, f"Config at {CONFIG_PATH} is not a dict.")
                )
                config = {}
        except yaml.YAMLError as e:
            issues.append(_check("Config is valid YAML", False, str(e)))
            config = {}
    else:
        config = {}

    # 3. Model configured
    if config.get("model"):
        _check("Model configured", True, f"({config['model']})")
    else:
        issues.append(
            _check("Model configured", False, "Run 'lumen run' and complete the setup wizard.")
        )

    # 4. API key available
    api_key_env = config.get("api_key_env")
    has_key = bool(config.get("api_key")) or (
        api_key_env and bool(os.environ.get(api_key_env))
    )
    if has_key:
        _check("API key available", True)
    elif config.get("model", "").startswith("ollama/"):
        _check("API key available", True, "(Ollama — no key needed)")
    else:
        issues.append(
            _check(
                "API key available",
                False,
                f"Set {api_key_env} env var or run 'lumen install' to re-enter it.",
            )
        )

    # 5. Secrets store
    if SECRETS_PATH.exists():
        _check("Secrets store exists", True)
        # Check permissions
        if _file_is_owner_only(SECRETS_PATH):
            _check("Secrets store permissions (owner-only)", True)
        else:
            issues.append(
                _check(
                    "Secrets store permissions (owner-only)",
                    False,
                    f"Run: chmod 600 {SECRETS_PATH}",
                )
            )
        # Verify it's valid YAML
        try:
            secrets_data = yaml.safe_load(SECRETS_PATH.read_text(encoding="utf-8"))
            if isinstance(secrets_data, dict):
                module_count = len(secrets_data)
                _check("Secrets store readable", True, f"({module_count} module(s))")
            else:
                issues.append(_check("Secrets store readable", False, "secrets.yaml is not a valid dict."))
        except yaml.YAMLError as e:
            issues.append(_check("Secrets store readable", False, str(e)))
    else:
        _check("Secrets store exists", True, "(no secrets yet — will be created on first module setup)")

    # 6. No secrets leaked in config.yaml
    leaked = []
    for key, value in config.items():
        if key.startswith("x-lumen-") and isinstance(value, dict):
            leaked.append(key)
    if config.get("secrets") and isinstance(config["secrets"], dict):
        # Legacy: secrets key in config.yaml (should have been migrated)
        leaked.extend(config["secrets"].keys())
    if not leaked:
        _check("No secrets leaked in config.yaml", True)
    else:
        issues.append(
            _check(
                f"Secrets leaked in config.yaml ({', '.join(leaked[:3])})",
                False,
                "Run 'lumen doctor --fix' or start Lumen to auto-migrate.",
            )
        )

    # 7. Serve mode: owner password set
    if config.get("server_mode"):
        if config.get("owner_secret_hash"):
            _check("Owner password set (server mode)", True)
        else:
            issues.append(
                _check(
                    "Owner password set (server mode)",
                    False,
                    "Run 'lumen server' — it will show a setup token to create your password.",
                )
            )

    # 8. Core modules importable
    core_ok = True
    for mod_name in ["lumen.core.brain", "lumen.core.memory", "lumen.core.registry"]:
        try:
            __import__(mod_name)
        except ImportError as e:
            core_ok = False
            issues.append(_check(f"Core module: {mod_name}", False, str(e)))
            break
    if core_ok:
        _check("Core modules importable", True)

    # 9. Data directory writable
    try:
        LUMEN_DIR.mkdir(parents=True, exist_ok=True)
        test_file = LUMEN_DIR / ".doctor_write_test"
        test_file.write_text("ok")
        test_file.unlink()
        _check("Data directory writable", True)
    except OSError as e:
        issues.append(_check("Data directory writable", False, str(e)))

    # Summary
    console.print()
    console.print(f"  {'─' * 45}")
    if not issues:
        console.print("  [bold green]All checks passed.[/bold green] Lumen looks healthy.")
    else:
        console.print(
            f"  [bold red]{len(issues)} issue(s) found.[/bold red] Review the fixes above."
        )
    console.print()
