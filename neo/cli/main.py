"""Neo CLI — install, run, status. The bootstrapper, not the experience."""

import os
import webbrowser
from pathlib import Path

import typer
import yaml
from rich.console import Console
from rich.panel import Panel
from rich.prompt import Prompt

app = typer.Typer(
    name="neo",
    help="Neo — Open-source AI agent engine. Modular. No limits.",
    no_args_is_help=True,
)
console = Console()

NEO_DIR = Path.home() / ".neo"
CONFIG_PATH = NEO_DIR / "config.yaml"
PKG_DIR = Path(__file__).parent.parent


@app.command()
def install():
    """Set up Neo for the first time."""
    console.print(
        Panel(
            "[bold cyan]Neo[/bold cyan] — First time setup",
            expand=False,
        )
    )

    NEO_DIR.mkdir(parents=True, exist_ok=True)

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
    port = int(
        Prompt.ask("\nDashboard port", default="3000")
    )

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
        "\n[bold cyan]Run [white]neo run[/white] to start Neo.[/bold cyan]"
    )


@app.command()
def run(
    port: int = typer.Option(None, help="Override dashboard port"),
):
    """Start Neo — opens the dashboard in your browser."""
    if not CONFIG_PATH.exists():
        console.print(
            "[red]Neo is not installed. Run [bold]neo install[/bold] first.[/red]"
        )
        raise typer.Exit(1)

    config = yaml.safe_load(CONFIG_PATH.read_text())

    # Set API key in environment
    if config.get("api_key") and config.get("api_key_env"):
        os.environ[config["api_key_env"]] = config["api_key"]

    # Initialize core
    from neo.channels.web import app as web_app, configure
    from neo.core.brain import Brain
    from neo.core.connectors import ConnectorRegistry
    from neo.core.consciousness import Consciousness
    from neo.core.handlers import register_builtin_handlers
    from neo.core.memory import Memory
    from neo.core.personality import Personality

    consciousness = Consciousness()

    lang = config.get("language", "en")
    personality = Personality(PKG_DIR / "locales" / lang / "personality.yaml")

    memory = Memory(NEO_DIR / "memory.db")

    connectors = ConnectorRegistry()
    built_in_path = PKG_DIR / "connectors" / "built-in.yaml"
    if built_in_path.exists():
        connectors.load(built_in_path)

    # Register real handlers — this is what makes Neo DO things, not just talk
    register_builtin_handlers(connectors, memory)

    brain = Brain(
        consciousness=consciousness,
        personality=personality,
        memory=memory,
        connectors=connectors,
        model=config.get("model", "deepseek/deepseek-chat"),
    )

    # Load flows for the selected language
    flows_dir = PKG_DIR / "locales" / lang / "flows"
    brain.load_flows(flows_dir)

    # Load UI locale
    ui_path = PKG_DIR / "locales" / lang / "ui.yaml"
    locale = {}
    if ui_path.exists():
        locale = yaml.safe_load(ui_path.read_text(encoding="utf-8")) or {}

    # Wire up web channel — memory.init() happens in FastAPI lifespan
    configure(brain, locale, config)

    use_port = port or config.get("port", 3000)

    console.print(
        Panel(
            f"[bold cyan]Neo[/bold cyan] is running\n\n"
            f"  Dashboard:  [link]http://localhost:{use_port}[/link]\n"
            f"  Model:      {config.get('model')}\n"
            f"  Language:   {lang}\n"
            f"  Flows:      {len(brain.flows)}",
            title="Neo",
            expand=False,
            border_style="cyan",
        )
    )

    webbrowser.open(f"http://localhost:{use_port}")

    import uvicorn

    uvicorn.run(web_app, host="0.0.0.0", port=use_port, log_level="warning")


@app.command()
def status():
    """Show Neo's current configuration."""
    if not CONFIG_PATH.exists():
        console.print("[red]Neo is not installed.[/red]")
        raise typer.Exit(1)

    config = yaml.safe_load(CONFIG_PATH.read_text())
    console.print(
        Panel(
            f"Model:     {config.get('model', 'not set')}\n"
            f"Language:  {config.get('language', 'en')}\n"
            f"Port:      {config.get('port', 3000)}\n"
            f"Config:    {CONFIG_PATH}",
            title="Neo — Status",
            expand=False,
            border_style="cyan",
        )
    )


if __name__ == "__main__":
    app()
