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
from lumen.core.paths import resolve_lumen_dir
from lumen.core.registry import CapabilityKind
from lumen.core.runtime import bootstrap_runtime, refresh_runtime_registry, rehydrate_runtime_config, reload_runtime_personality_surface, sync_runtime_modules

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


def _load_persisted_config(config_path: Path | None = None) -> dict:
    path = config_path or CONFIG_PATH
    if not path.exists():
        return {}
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _is_runtime_configured(config: dict | None = None) -> bool:
    loaded = config if config is not None else _load_persisted_config()
    return bool(loaded.get("model"))


def _prepare_runtime_if_configured(
    config: dict | None = None,
    *,
    lumen_dir: Path | None = None,
):
    loaded = config if config is not None else _load_persisted_config()
    if not _is_runtime_configured(loaded):
        return None, loaded

    if loaded.get("api_key") and loaded.get("api_key_env"):
        os.environ[loaded["api_key_env"]] = loaded["api_key"]

    resolved_lumen_dir = lumen_dir or LUMEN_DIR
    runtime = asyncio.run(
        bootstrap_runtime(
            loaded,
            pkg_dir=PKG_DIR,
            lumen_dir=resolved_lumen_dir,
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


# ── CLI Twin Wizard ──────────────────────────────────────────────────────────

WIZARD_PROVIDERS = {
    "1": ("deepseek/deepseek-chat", "DeepSeek API key", True),
    "2": ("openai/gpt-4o-mini", "OpenAI API key", True),
    "3": ("anthropic/claude-sonnet-4-20250514", "Anthropic API key", True),
    "4": ("ollama/llama3", None, False),
    "5": ("openrouter/openai/gpt-oss-120b:free", None, False),
}


def _run_cli_wizard(*, lumen_dir: Path | None = None) -> dict:
    """Run the onboarding wizard in the terminal.

    Twin of the web wizard — same steps, terminal presentation.
    Returns the config dict and saves config.yaml.

    Steps:
      1. Choose model provider
      2. Enter API key (if needed)
      3. Choose language
      4. Save config.yaml
    """
    target_dir = lumen_dir or resolve_lumen_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    config_path = target_dir / "config.yaml"

    console.print()
    console.print(Panel(
        "[bold cyan]🔮 Bienvenido a Lumen[/bold cyan]\n\n"
        "Vamos a configurar tu asistente.",
        expand=False,
        border_style=BRAND,
    ))

    # Step 1: Model provider
    console.print("\n[bold]¿Qué modelo querés usar?[/bold]")
    console.print("  1. DeepSeek (recomendado)")
    console.print("  2. OpenAI GPT-4o-mini")
    console.print("  3. Anthropic Claude")
    console.print("  4. Ollama (local, sin API key)")
    console.print("  5. OpenRouter (multi-model, tier gratuito)")

    provider = Prompt.ask(
        "\nElegí",
        choices=["1", "2", "3", "4", "5"],
        default="1",
    )

    model_info = WIZARD_PROVIDERS[provider]
    model = model_info[0]
    key_label = model_info[1]
    needs_key = model_info[2]

    # Step 2: API key
    api_key = None
    if needs_key and key_label:
        api_key = Prompt.ask(f"\n{key_label}")

    # Step 3: Language
    lang = Prompt.ask(
        "\nIdioma / Language",
        choices=["es", "en", "pt"],
        default="es",
    )

    # Step 4: Save
    config = {
        "language": lang,
        "model": model,
    }
    if api_key:
        config["api_key"] = api_key

    config_path.write_text(yaml.dump(config, default_flow_style=False), encoding="utf-8")

    # Summary
    console.print()
    console.print(Panel(
        f"  Model:    [bold]{model}[/bold]\n"
        f"  Language: [bold]{lang}[/bold]\n"
        f"  Instance: [bold]{target_dir.name if target_dir.name != '.lumen' else 'default'}[/bold]\n"
        f"  Config:   {config_path}",
        title="[green]✓ Configuración guardada[/green]",
        expand=False,
        border_style="green",
    ))

    return config


# ── command implementations ─────────────────────────────────────────────────


@app.command()
def run(
    port: int = typer.Option(3000, help="Dashboard port"),
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance (isolated data dir)"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data directory"),
    no_wizard: bool = typer.Option(False, "--no-wizard", help="Skip wizard, error if no config (CI/CD)"),
):
    """Start Lumen — opens the dashboard in your browser.

    If Lumen is not configured yet, runs the CLI setup wizard automatically.
    Use --no-wizard to skip wizard (useful for CI/CD).
    """
    from lumen.channels.web import app as web_app, configure, configure_access_mode

    # Resolve instance-aware lumen directory
    lumen_dir = resolve_lumen_dir(instance=instance, data_dir=data_dir)
    config_path = lumen_dir / "config.yaml"

    configure_access_mode("run")

    config = _load_persisted_config(config_path)

    # No config → run CLI wizard (unless --no-wizard)
    if not _is_runtime_configured(config):
        if no_wizard:
            console.print("[red]No configuration found and --no-wizard is set.[/red]")
            console.print("Create config.yaml manually or run without --no-wizard.")
            raise typer.Exit(1)
        config = _run_cli_wizard(lumen_dir=lumen_dir)

    runtime, config = _prepare_runtime_if_configured(config, lumen_dir=lumen_dir)

    if runtime is not None:
        configure(runtime.brain, runtime.locale, runtime.config, awareness=runtime.awareness, lumen_dir=lumen_dir)
        use_port = port or config.get("port", 3000)
        lang = config.get("language", "en")
        mcp_count = len(runtime.brain.registry.list_by_kind(CapabilityKind.MCP))

        console.print(
            Panel(
                f"[bold cyan]Lumen[/bold cyan] is running\n\n"
                f"  Dashboard:  [link]http://localhost:{use_port}[/link]\n"
                f"  Model:      {config.get('model')}\n"
                f"  Language:   {lang}\n"
                f"  Instance:   {instance or 'default'}\n"
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
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance (isolated data dir)"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data directory"),
    no_wizard: bool = typer.Option(False, "--no-wizard", help="Skip wizard, error if no config (CI/CD)"),
):
    """Start Lumen in hosted/server mode with authenticated access."""
    from lumen.channels.web import (
        app as web_app,
        configure,
        configure_access_mode,
        ensure_server_bootstrap,
    )

    # Resolve instance-aware lumen directory
    lumen_dir = resolve_lumen_dir(instance=instance, data_dir=data_dir)
    config_path = lumen_dir / "config.yaml"

    configure_access_mode("serve")

    config = _load_persisted_config(config_path)

    # No config → run CLI wizard (unless --no-wizard)
    if not _is_runtime_configured(config):
        if no_wizard:
            console.print("[red]No configuration found and --no-wizard is set.[/red]")
            raise typer.Exit(1)
        config = _run_cli_wizard(lumen_dir=lumen_dir)

    runtime, config = _prepare_runtime_if_configured(config, lumen_dir=lumen_dir)
    if runtime is not None:
        configure(runtime.brain, runtime.locale, runtime.config, awareness=runtime.awareness, lumen_dir=lumen_dir)

    setup_token = ensure_server_bootstrap(host=host, port=port)
    current = _load_persisted_config(config_path)
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
def status(
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance (isolated data dir)"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data directory"),
):
    """Show Lumen's current configuration and health."""
    lumen_dir = resolve_lumen_dir(instance=instance, data_dir=data_dir)
    config_path = lumen_dir / "config.yaml"
    config = _load_persisted_config(config_path)
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
            f"Instance:  {instance or 'default'}\n"
            f"Config:    {config_path}",
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


# ── config commands ──────────────────────────────────────────────────────────

config_app = typer.Typer(
    name="config",
    help="Manage module configuration and secrets.",
    no_args_is_help=True,
)
app.add_typer(config_app, name="config")


def _redact(value: str) -> str:
    """Show first 4 chars + **** for values > 4 chars."""
    if len(value) <= 4:
        return "****"
    return value[:4] + "****"


def _resolve_config_paths(instance: str | None, data_dir: str | None):
    """Resolve lumen_dir and configure secrets_store for config commands."""
    from lumen.core.secrets_store import configure_paths
    lumen_dir = resolve_lumen_dir(instance=instance, data_dir=data_dir)
    configure_paths(lumen_dir=lumen_dir)
    return lumen_dir


@config_app.command("set")
def config_set(
    key: str = typer.Argument(help="Module key (e.g. otto.store_id)"),
    value: str = typer.Argument(help="Value to set"),
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data dir"),
):
    """Set a module config value. Usage: lumen config set <module>.<key> <value>"""
    parts = key.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        console.print("[red]Invalid key format. Use: <module>.<key>[/red]")
        raise typer.Exit(1)

    module_name, config_key = parts
    _resolve_config_paths(instance, data_dir)

    from lumen.core.secrets_store import save_module
    save_module(module_name, {config_key: value})
    console.print(f"[green]✓[/green] {module_name}.{config_key} = {_redact(value)}")


@config_app.command("get")
def config_get(
    key: str = typer.Argument(help="Module key (e.g. otto.store_id)"),
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data dir"),
):
    """Get a module config value. Usage: lumen config get <module>.<key>"""
    parts = key.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        console.print("[red]Invalid key format. Use: <module>.<key>[/red]")
        raise typer.Exit(1)

    module_name, config_key = parts
    _resolve_config_paths(instance, data_dir)

    from lumen.core.secrets_store import load_module
    secrets = load_module(module_name)
    if config_key in secrets:
        console.print(secrets[config_key])
    else:
        console.print(f"[dim]Key {key} not found.[/dim]")
        raise typer.Exit(1)


@config_app.command("delete")
def config_delete(
    key: str = typer.Argument(help="Module key (e.g. otto.store_id)"),
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data dir"),
):
    """Delete a module config value. Usage: lumen config delete <module>.<key>"""
    parts = key.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        console.print("[red]Invalid key format. Use: <module>.<key>[/red]")
        raise typer.Exit(1)

    module_name, config_key = parts
    _resolve_config_paths(instance, data_dir)

    from lumen.core.secrets_store import delete_module_key
    delete_module_key(module_name, config_key)
    console.print(f"[green]✓[/green] Deleted {module_name}.{config_key}")


@config_app.command("list")
def config_list(
    module: str = typer.Argument(help="Module name"),
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data dir"),
):
    """List all config keys for a module (values redacted)."""
    _resolve_config_paths(instance, data_dir)

    from lumen.core.secrets_store import load_module
    secrets = load_module(module)
    if not secrets:
        console.print(f"[dim]No config found for module '{module}'.[/dim]")
        return

    for k, v in secrets.items():
        console.print(f"  {module}.{k} = {_redact(str(v))}")


@app.command()
def reload(
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance (isolated data dir)"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data directory"),
):
    """Reload Lumen's runtime — re-discover modules, refresh registry."""
    lumen_dir = resolve_lumen_dir(instance=instance, data_dir=data_dir)
    config_path = lumen_dir / "config.yaml"
    config = _load_persisted_config(config_path)

    if not _is_runtime_configured(config):
        console.print("[red]Lumen is not configured.[/red]")
        console.print("Run [bold]lumen run[/bold] to start the setup wizard.")
        raise typer.Exit(1)

    console.print("[dim]Reloading runtime...[/dim]")

    try:
        runtime = asyncio.run(
            bootstrap_runtime(
                config,
                pkg_dir=PKG_DIR,
                lumen_dir=lumen_dir,
                active_channels=["web"],
            )
        )
    except Exception as e:
        console.print(f"[red]Failed to bootstrap runtime: {e}[/red]")
        raise typer.Exit(1)

    if runtime is None or runtime.brain is None:
        console.print("[red]Runtime bootstrap returned no brain.[/red]")
        raise typer.Exit(1)

    brain = runtime.brain
    config = rehydrate_runtime_config(runtime.config, lumen_dir=lumen_dir)
    runtime.config = config

    try:
        asyncio.run(
            sync_runtime_modules(brain, config=config, pkg_dir=PKG_DIR, lumen_dir=lumen_dir)
        )
        refresh_runtime_registry(brain, pkg_dir=PKG_DIR, lumen_dir=lumen_dir, active_channels=["web"])
        reload_runtime_personality_surface(brain, config=config, pkg_dir=PKG_DIR, lumen_dir=lumen_dir)
    except Exception as e:
        console.print(f"[red]Reload failed: {e}[/red]")
        raise typer.Exit(1)

    cap_count = len(brain.registry.all()) if brain.registry else 0
    console.print(f"[green]✓[/green] Runtime reloaded — {cap_count} capabilities active")


# ── module install commands ──────────────────────────────────────────────────

def _parse_github_ref(ref: str) -> tuple[str | None, str | None]:
    """Parse a GitHub reference into (owner, repo).

    Supports:
      github:owner/repo
      https://github.com/owner/repo
      https://github.com/owner/repo.git
      owner/repo (bare shorthand)
    """
    ref = ref.strip()
    if not ref:
        return None, None

    # github:owner/repo
    if ref.startswith("github:"):
        ref = ref[7:]

    # https://github.com/owner/repo[.git]
    if ref.startswith("https://github.com/"):
        ref = ref[len("https://github.com/"):]
    elif ref.startswith("http://github.com/"):
        ref = ref[len("http://github.com/"):]

    # Strip trailing .git
    if ref.endswith(".git"):
        ref = ref[:-4]

    # Strip trailing slashes
    ref = ref.rstrip("/")

    parts = ref.split("/")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]

    return None, None


def _is_local_path(ref: str) -> bool:
    """Check if a ref refers to a local filesystem path."""
    if not ref:
        return False
    # Absolute paths
    if ref.startswith("/") or ref.startswith("\\"):
        return True
    # Relative paths
    if ref.startswith("./") or ref.startswith("../") or ref.startswith(".\\") or ref.startswith("..\\"):
        return True
    # Check if it's an existing path
    return Path(ref).exists()


module_app = typer.Typer(
    name="module",
    help="Install and manage modules.",
    no_args_is_help=True,
)
app.add_typer(module_app, name="module")


@module_app.command("install")
def module_install(
    ref: str = typer.Argument(help="Module reference: github:owner/repo, URL, or catalog name"),
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data dir"),
):
    """Install a module from GitHub or catalog.

    Examples:
      lumen module install github:acme/my-module
      lumen module install https://github.com/acme/my-module
      lumen module install my-module  (from catalog)
    """
    lumen_dir = resolve_lumen_dir(instance=instance, data_dir=data_dir)
    config_path = lumen_dir / "config.yaml"
    config = _load_persisted_config(config_path)

    if not _is_runtime_configured(config):
        console.print("[red]Lumen is not configured.[/red]")
        console.print("Run [bold]lumen run[/bold] to start the setup wizard.")
        raise typer.Exit(1)

    from lumen.core.installer import Installer
    from lumen.core.connectors import ConnectorRegistry

    # Memory is optional for install — pass None
    installer = Installer(
        PKG_DIR,
        ConnectorRegistry(),
        memory=None,
        lumen_dir=lumen_dir,
        config=config,
    )

    owner, repo = _parse_github_ref(ref)

    # Try local path first
    if _is_local_path(ref):
        console.print(f"[dim]Installing from local path: {ref}...[/dim]")
        result = installer.install_from_local_path(Path(ref))
    elif owner and repo:
        console.print(f"[dim]Installing from github.com/{owner}/{repo}...[/dim]")
        result = installer.install_from_github_ref(owner, repo)
    else:
        # Try catalog
        console.print(f"[dim]Installing '{ref}' from catalog...[/dim]")
        result = installer.install_from_catalog(ref)

    if result.get("status") == "installed":
        name = result.get("name", ref)
        console.print(f"[green]✓[/green] Installed {name}")
    else:
        error = result.get("error", "Unknown error")
        console.print(f"[red]✗[/red] {error}")
        raise typer.Exit(1)


# ── api-key commands ─────────────────────────────────────────────────────────

apikey_app = typer.Typer(
    name="api-key",
    help="Manage API keys for REST authentication.",
    no_args_is_help=True,
)
app.add_typer(apikey_app, name="api-key")


def _resolve_api_keys_path(instance: str | None, data_dir: str | None) -> Path:
    """Resolve the api_keys.yaml path for the given instance."""
    lumen_dir = resolve_lumen_dir(instance=instance, data_dir=data_dir)
    return lumen_dir / "api_keys.yaml"


@apikey_app.command("generate")
def apikey_generate(
    label: str = typer.Option(..., "--label", "-l", help="Label for this API key"),
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data dir"),
):
    """Generate a new API key. The key is shown ONCE — save it securely."""
    from lumen.core.api_keys import generate_api_key
    keys_path = _resolve_api_keys_path(instance, data_dir)
    result = generate_api_key(label=label, keys_path=keys_path)
    console.print(f"[green]✓[/green] API key generated for '{label}'")
    console.print(f"  [bold yellow]Key: {result['key']}[/bold yellow]")
    console.print(f"  Prefix: {result['prefix']}")
    console.print(f"  [dim]Save this key now — it won't be shown again.[/dim]")


@apikey_app.command("list")
def apikey_list(
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data dir"),
):
    """List all API keys (prefix and label only)."""
    from lumen.core.api_keys import list_api_keys
    keys_path = _resolve_api_keys_path(instance, data_dir)
    keys = list_api_keys(keys_path=keys_path)
    if not keys:
        console.print("[dim]No API keys found.[/dim]")
        return
    for k in keys:
        console.print(f"  {k['prefix']}...  {k['label']}  [dim]{k['created_at']}[/dim]")


@apikey_app.command("revoke")
def apikey_revoke(
    prefix: str = typer.Argument(help="Key prefix to revoke (first 8 chars)"),
    instance: str = typer.Option(None, "--instance", "-i", help="Named instance"),
    data_dir: str = typer.Option(None, "--data-dir", "-d", help="Custom data dir"),
):
    """Revoke an API key by its prefix."""
    from lumen.core.api_keys import revoke_api_key
    keys_path = _resolve_api_keys_path(instance, data_dir)
    removed = revoke_api_key(prefix, keys_path=keys_path)
    if removed:
        console.print(f"[green]✓[/green] Revoked key {prefix}...")
    else:
        console.print(f"[dim]No key found with prefix '{prefix}'.[/dim]")


@app.command()
def doctor():
    """Diagnose issues and attempt automatic fixes."""
    from lumen.cli.doctor import run_doctor

    run_doctor()


if __name__ == "__main__":
    app()
