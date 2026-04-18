"""Lumen CLI — install, run, status. The bootstrapper, not the experience."""

import asyncio
import os
import sys
import time
import webbrowser
from pathlib import Path

import typer
import yaml
from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.prompt import Prompt

from lumen.core.registry import CapabilityKind
from lumen.core.runtime import bootstrap_runtime


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


app = typer.Typer(
    name="lumen",
    help="Lumen — Open-source AI agent engine. Modular. No limits.",
    no_args_is_help=True,
)
console = Console()

LUMEN_DIR = Path.home() / ".lumen"
CONFIG_PATH = LUMEN_DIR / "config.yaml"
PKG_DIR = Path(__file__).parent.parent


def _supports_unicode() -> bool:
    encoding = (sys.stdout.encoding or "").lower()
    return "utf" in encoding


def render_eye_boot():
    """Show a tiny Lumen eye boot animation before the panel."""
    if not console.is_terminal:
        return

    if not _supports_unicode():
        console.print("[bold #3d3dd6](o)[/bold #3d3dd6]")
        return

    frames = [
        "      ·\n",
        "      ·\n   ╭──┴──╮\n   │  ●  │\n   ╰──┬──╯\n      ·",
        "   ◌─────◌\n   ╱  ◉  ╲\n ◌───────◌\n   ╲     ╱\n    ◌───◌",
    ]

    with Live(console=console, refresh_per_second=12, transient=True) as live:
        for frame in frames:
            live.update(Align.center(f"[bold #3d3dd6]{frame}[/bold #3d3dd6]"))
            time.sleep(0.2)


@app.command()
def install():
    """Set up Lumen for the first time."""
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
def run(
    port: int = typer.Option(3000, help="Dashboard port"),
):
    """Start Lumen — opens the dashboard in your browser.

    If Lumen is not configured yet, the browser will show the setup wizard.
    No 'neo install' needed — the web handles everything.
    """
    from lumen.channels.web import app as web_app, configure, configure_access_mode

    render_eye_boot()
    configure_access_mode("run")

    # If config exists, pre-initialize brain (faster first page load)
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
                border_style="#3d3dd6",
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
                border_style="#3d3dd6",
            )
        )

    webbrowser.open(f"http://localhost:{use_port}")

    import uvicorn

    uvicorn.run(web_app, host="0.0.0.0", port=use_port, log_level="warning")


@app.command()
def serve(
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

    render_eye_boot()
    configure_access_mode("serve")

    config = _load_persisted_config()
    runtime, config = _prepare_runtime_if_configured(config)
    if runtime is not None:
        configure(runtime.brain, runtime.locale, runtime.config, awareness=runtime.awareness)

    setup_token = ensure_server_bootstrap(host=host, port=port)
    current = _load_persisted_config()

    body = (
        f"[bold cyan]Lumen[/bold cyan] server mode\n\n"
        f"  Dashboard:  [link]http://{host}:{port}[/link]\n"
        f"  Auth:       {'owner login required' if _is_runtime_configured(current) else 'setup token required'}"
    )
    if not _is_runtime_configured(current):
        body += f"\n  Setup token: [bold]{setup_token}[/bold]\n\n  Open /setup and enter this one-time token to begin onboarding."
    else:
        body += "\n\n  Open /login and sign in with the owner password or PIN."

    console.print(
        Panel(
            body,
            title="Lumen",
            expand=False,
            border_style="#3d3dd6",
        )
    )

    import uvicorn

    uvicorn.run(web_app, host=host, port=port, log_level="warning")


@app.command()
def status():
    """Show Lumen's current configuration."""
    config = _load_persisted_config()
    if not _is_runtime_configured(config):
        console.print("[red]Lumen is not installed.[/red]")
        raise typer.Exit(1)

    console.print(
        Panel(
            f"Model:     {config.get('model', 'not set')}\n"
            f"Language:  {config.get('language', 'en')}\n"
            f"Port:      {config.get('port', 3000)}\n"
            f"MCP:       {len((config.get('mcp') or {}).get('servers', {}))}\n"
            f"Config:    {CONFIG_PATH}",
            title="Lumen — Status",
            expand=False,
            border_style="#3d3dd6",
        )
    )


if __name__ == "__main__":
    app()
