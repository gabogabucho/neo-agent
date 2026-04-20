"""Lumen CLI — the bootstrapper, not the experience."""

import asyncio
import os
import sys
import webbrowser
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from lumen import __version__
from lumen.core.registry import CapabilityKind
from lumen.core.runtime import bootstrap_runtime

BRAND = "#3d3d6d"
BRAND_DIM = "#6b6baa"

LUMEN_BANNER = r"""
                ▄
        ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
    ▄▄▄▄▄▄▄▄▄▄███████▄▄▄▄▄▄▄▄▄
   ▄▄▄▄▄▄▄▄▄█▄▄▄▄▄▄▄██▄▄▄▄▄▄▄▄▄▄
 ▄▄▄▄▄▄▄▄▄▄▄▄▄▄███▄█▄▄▄▄▄▄▄▄▄▄▄▄▄▄
▄▄▄▄▄▄▄▄▄▄█▄▄▄█████▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
▄▄▄▄▄▄▄▄▄▄█▄▄▄█████▄▄▄█▄▄▄▄▄▄▄▄▄▄▄▄
  ▄▄▄▄▄▄▄▄▄█▄▄▄▄▄▄▄▄█▄▄▄▄▄▄▄▄▄▄
    ▄▄▄▄▄▄▄▄▄██▄▄▄█▄▄▄▄▄▄▄▄▄
       ▄▄▄▄▄▄▄▄▄▄▄▄▄▄█▄▄▄
          ▄▄▄▄▄▄▄▄▄▄▄▄▄
                ▀

   _     _   _  __  __  _____  _   _
  | |   | | | ||  \/  ||  ___|| \ | |
  | |   | | | || \  / || |__  |  \| |
  | |   | | | || |\/| ||  __| | . ` |
  | |___| |_| || |  | || |___ | |\  |
  |_____|\___/ |_|  |_||_____||_| \_|
"""

LUMEN_DIR = Path.home() / ".lumen"
CONFIG_PATH = LUMEN_DIR / "config.yaml"
PKG_DIR = Path(__file__).parent.parent

app = typer.Typer(
    name="lumen",
    help="Lumen — Open-source AI agent engine.",
    no_args_is_help=False,
    rich_markup_mode="rich",
)
console = Console()


# ── helpers ──────────────────────────────────────────────────────────────────


def _load_persisted_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _is_runtime_configured(config: dict | None = None) -> bool:
    loaded = config if config is not None else _load_persisted_config()
    return bool(loaded.get("model"))


def _prepare_runtime_if_configured(config: dict | None = None):
    loaded = config if config is not None else _load_persisted_config()
    if not _is_runtime_configured(loaded):
        return None, loaded

    if loaded.get("api_key") and loaded.get("api_key_env"):
        os.environ[loaded["api_key_env"]] = loaded["api_key"]

    runtime = asyncio.run(
        bootstrap_runtime(
            loaded,
            pkg_dir=PKG_DIR,
            lumen_dir=LUMEN_DIR,
            active_channels=["web"],
        )
    )
    return runtime, loaded


def _supports_unicode() -> bool:
    encoding = (sys.stdout.encoding or "").lower()
    return "utf" in encoding


def _render_landing():
    """Show Lumen landing: banner + status + commands."""
    config = _load_persisted_config()
    configured = _is_runtime_configured(config)

    # Banner
    if _supports_unicode():
        console.print(f"[bold {BRAND}]{LUMEN_BANNER}[/bold {BRAND}]")
    else:
        console.print(f"[bold {BRAND}](o) LUMEN[/bold {BRAND}]")

    console.print(f"  Open-source AI agent engine  [dim]v{__version__}[/dim]")
    console.print()

    # Status
    if configured:
        mcp = config.get("mcp") or {}
        mcp_count = len(mcp.get("servers", {}))
        console.print(f"  [bold]Model[/bold]    {config.get('model', '—')}")
        console.print(f"  [bold]Language[/bold] {config.get('language', 'en')}")
        console.print(f"  [bold]MCP[/bold]      {mcp_count} servers")
        console.print(f"  [bold]Config[/bold]   {CONFIG_PATH}")
    else:
        console.print(f"  [dim]Not configured.[/dim] Run [bold]lumen run[/bold] to start the setup wizard.")

    console.print()
    console.print(f"  [bold {BRAND}]Commands[/bold {BRAND}]")
    console.print(f"  [bold]run[/bold]      Start dashboard locally")
    console.print(f"  [bold]server[/bold]   Start in server mode")
    console.print(f"  [bold]update[/bold]   Check for updates")
    console.print(f"  [bold]doctor[/bold]   Diagnose and fix issues")
    console.print()
    console.print(f"  [dim]lumen <command> --help for details[/dim]")


# ── landing (no args) ────────────────────────────────────────────────────────


@app.callback(invoke_without_command=True)
def _main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        _render_landing()


# ── commands ─────────────────────────────────────────────────────────────────


@app.command()
def run(
    port: int = typer.Option(3000, help="Dashboard port"),
):
    """Start Lumen — opens the dashboard in your browser.

    If Lumen is not configured yet, the browser will show the setup wizard.
    """
    from lumen.channels.web import app as web_app, configure, configure_access_mode

    configure_access_mode("run")

    config = _load_persisted_config()
    runtime, config = _prepare_runtime_if_configured(config)

    if runtime is not None:
        configure(runtime.brain, runtime.locale, runtime.config, awareness=runtime.awareness)
        use_port = port or config.get("port", 3000)
        lang = config.get("language", "en")
        mcp_count = len(runtime.brain.registry.list_by_kind(CapabilityKind.MCP))

        console.print(
            Panel(
                f"[bold cyan]Lumen[/bold cyan] is running\n\n"
                f"  Dashboard:  [link]http://localhost:{use_port}[/link]\n"
                f"  Model:      {config.get('model')}\n"
                f"  Language:   {lang}\n"
                f"  Flows:      {len(runtime.brain.flows)}\n"
                f"  MCP:        {mcp_count}",
                title="Lumen",
                expand=False,
                border_style=BRAND,
            )
        )
    else:
        use_port = port
        console.print(
            Panel(
                f"[bold cyan]Lumen[/bold cyan] — First time setup\n\n"
                f"  Opening [link]http://localhost:{use_port}[/link]\n"
                f"  Follow the setup wizard in your browser.",
                title="Lumen",
                expand=False,
                border_style=BRAND,
            )
        )

    webbrowser.open(f"http://localhost:{use_port}")

    import uvicorn

    uvicorn.run(web_app, host="0.0.0.0", port=use_port, log_level="warning")


@app.command()
def server(
    host: str = typer.Option("0.0.0.0", help="Server bind host"),
    port: int = typer.Option(3000, help="Server bind port"),
):
    """Start Lumen in hosted/server mode with authenticated access."""
    from lumen.channels.web import (
        app as web_app,
        configure,
        configure_access_mode,
        ensure_server_bootstrap,
    )

    configure_access_mode("serve")

    config = _load_persisted_config()
    runtime, config = _prepare_runtime_if_configured(config)
    if runtime is not None:
        configure(runtime.brain, runtime.locale, runtime.config, awareness=runtime.awareness)

    setup_token = ensure_server_bootstrap(host=host, port=port)
    current = _load_persisted_config()
    has_owner_secret = bool(current.get("owner_secret_hash"))

    display_host = "localhost" if host in ("0.0.0.0", "::") else host
    body = f"[bold cyan]Lumen[/bold cyan] server mode\n\n  Dashboard:  [link]http://{display_host}:{port}[/link]"
    if not _is_runtime_configured(current):
        body += f"\n  Auth:       setup token required"
        body += f"\n  Setup token: [bold]{setup_token}[/bold]\n\n  Open /setup and enter this one-time token to begin onboarding."
    elif not has_owner_secret:
        body += f"\n  Auth:       owner password setup required"
        body += f"\n  Setup token: [bold]{setup_token}[/bold]\n\n  Open /login and enter this token to create your owner password."
    else:
        body += f"\n  Auth:       owner login required"
        body += "\n\n  Open /login and sign in with the owner password or PIN."

    console.print(
        Panel(
            body,
            title="Lumen",
            expand=False,
            border_style=BRAND,
        )
    )

    import uvicorn

    uvicorn.run(web_app, host=host, port=port, log_level="warning")


@app.command()
def status():
    """Show Lumen's current configuration and health."""
    config = _load_persisted_config()
    if not _is_runtime_configured(config):
        console.print("[red]Lumen is not installed.[/red]")
        console.print("Run [bold]lumen run[/bold] to start the setup wizard.")
        raise typer.Exit(1)

    mcp = config.get("mcp") or {}
    console.print(
        Panel(
            f"Model:     {config.get('model', 'not set')}\n"
            f"Language:  {config.get('language', 'en')}\n"
            f"Port:      {config.get('port', 3000)}\n"
            f"MCP:       {len(mcp.get('servers', {}))} servers\n"
            f"Config:    {CONFIG_PATH}",
            title="Lumen — Status",
            expand=False,
            border_style=BRAND,
        )
    )


@app.command()
def install():
    """Set up Lumen for the first time (CLI-based)."""
    console.print(
        Panel(
            "[bold cyan]Lumen[/bold cyan] — First time setup",
            expand=False,
        )
    )

    LUMEN_DIR.mkdir(parents=True, exist_ok=True)

    # Language
    lang = Prompt.ask(
        "\nLanguage / Idioma",
        choices=["en", "es"],
        default="en",
    )

    # Model
    console.print("\n[bold]Available models:[/bold]")
    console.print("  1. DeepSeek  (deepseek-chat) — affordable, recommended")
    console.print("  2. OpenAI    (gpt-4o-mini)")
    console.print("  3. Anthropic (claude-sonnet-4-20250514)")
    console.print("  4. Ollama    (local, no API key)")

    provider = Prompt.ask(
        "\nChoose provider",
        choices=["1", "2", "3", "4"],
        default="1",
    )

    model_map = {
        "1": ("deepseek/deepseek-chat", "DEEPSEEK_API_KEY", "DeepSeek API key"),
        "2": ("gpt-4o-mini", "OPENAI_API_KEY", "OpenAI API key"),
        "3": ("claude-sonnet-4-20250514", "ANTHROPIC_API_KEY", "Anthropic API key"),
        "4": ("ollama/llama3", None),
    }

    model_info = model_map[provider]
    model = model_info[0]
    env_key = model_info[1]
    key_label = model_info[2] if len(model_info) > 2 else "API key"

    api_key = None
    if env_key:
        api_key = Prompt.ask(f"\n{key_label}")

    # Port
    port = int(Prompt.ask("\nDashboard port", default="3000"))

    # Save config
    config = {
        "language": lang,
        "model": model,
        "port": port,
    }
    if env_key:
        config["api_key_env"] = env_key
    if api_key:
        config["api_key"] = api_key

    CONFIG_PATH.write_text(yaml.dump(config, default_flow_style=False))

    console.print(f"\n[green]>[/green] Config saved to {CONFIG_PATH}")
    console.print(f"[green]>[/green] Model: [bold]{model}[/bold]")
    console.print(f"[green]>[/green] Language: [bold]{lang}[/bold]")
    console.print(f"[green]>[/green] Port: [bold]{port}[/bold]")
    console.print(
        "\n[bold cyan]Run [white]lumen run[/white] to start Lumen.[/bold cyan]"
    )


@app.command()
def update():
    """Check for updates and install if available."""
    import importlib.metadata

    current = __version__
    console.print(f"  Current version: [bold]{current}[/bold]")
    console.print("[dim]  Checking for updates...[/dim]")

    try:
        import subprocess

        result = subprocess.run(
            [sys.executable, "-m", "pip", "index", "versions", "enlumen"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode == 0 and "enlumen" in result.stdout:
            # Parse latest version from pip index output
            versions_line = result.stdout.strip()
            console.print(f"  [dim]{versions_line}[/dim]")
            console.print(f"  [green]You're on the latest version.[/green]")
        else:
            console.print("  [dim]Could not check for updates (not installed via pip).[/dim]")
            console.print("  [dim]If running from source, pull the latest from git.[/dim]")
    except Exception:
        console.print("  [dim]Could not reach PyPI. Check your connection.[/dim]")


@app.command()
def doctor():
    """Diagnose issues and attempt automatic fixes."""
    from lumen.cli.doctor import run_doctor

    run_doctor()


if __name__ == "__main__":
    app()
