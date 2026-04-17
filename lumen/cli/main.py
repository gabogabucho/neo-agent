"""Lumen CLI — install, run, status. The bootstrapper, not the experience."""

import asyncio
import os
import webbrowser
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

from lumen.core.registry import CapabilityKind
from lumen.core.runtime import bootstrap_runtime

app = typer.Typer(
    name="lumen",
    help="Lumen — Open-source AI agent engine. Modular. No limits.",
    no_args_is_help=True,
)
console = Console()

LUMEN_DIR = Path.home() / ".lumen"
CONFIG_PATH = LUMEN_DIR / "config.yaml"
PKG_DIR = Path(__file__).parent.parent


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
        "1": ("deepseek/deepseek-chat", "DEEPSEEK_API_KEY"),
        "2": ("gpt-4o-mini", "OPENAI_API_KEY"),
        "3": ("claude-sonnet-4-20250514", "ANTHROPIC_API_KEY"),
        "4": ("ollama/llama3", None),
    }

    model, env_key = model_map[provider]

    api_key = None
    if env_key:
        api_key = Prompt.ask(f"\n{env_key}")

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
    from lumen.channels.web import app as web_app, configure

    # If config exists, pre-initialize brain (faster first page load)
    if CONFIG_PATH.exists():
        config = yaml.safe_load(CONFIG_PATH.read_text())

        if config.get("api_key") and config.get("api_key_env"):
            os.environ[config["api_key_env"]] = config["api_key"]

        runtime = asyncio.run(
            bootstrap_runtime(
                config,
                pkg_dir=PKG_DIR,
                lumen_dir=LUMEN_DIR,
                active_channels=["web"],
            )
        )

        configure(runtime.brain, runtime.locale, runtime.config)
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
                border_style="cyan",
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
                border_style="cyan",
            )
        )

    webbrowser.open(f"http://localhost:{use_port}")

    import uvicorn

    uvicorn.run(web_app, host="0.0.0.0", port=use_port, log_level="warning")


@app.command()
def status():
    """Show Lumen's current configuration."""
    if not CONFIG_PATH.exists():
        console.print("[red]Lumen is not installed.[/red]")
        raise typer.Exit(1)

    config = yaml.safe_load(CONFIG_PATH.read_text())
    console.print(
        Panel(
            f"Model:     {config.get('model', 'not set')}\n"
            f"Language:  {config.get('language', 'en')}\n"
            f"Port:      {config.get('port', 3000)}\n"
            f"MCP:       {len((config.get('mcp') or {}).get('servers', {}))}\n"
            f"Config:    {CONFIG_PATH}",
            title="Lumen — Status",
            expand=False,
            border_style="cyan",
        )
    )


if __name__ == "__main__":
    app()
