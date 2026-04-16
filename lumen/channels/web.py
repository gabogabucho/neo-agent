"""Web channel — FastAPI dashboard + WebSocket chat. UI-FIRST.

Routing logic:
  /  → no config? → /setup
  /  → config but not awakened? → awakening animation
  /  → config and awakened? → /dashboard
"""

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from lumen.core.session import SessionManager


# State — initialized lazily after web setup or by CLI
_brain = None
_locale: dict = {}
_config: dict = {}

LUMEN_DIR = Path.home() / ".lumen"
CONFIG_PATH = LUMEN_DIR / "config.yaml"
PKG_DIR = Path(__file__).parent.parent


def configure(brain, locale: dict, config: dict):
    """Configure the web channel (called by CLI when config exists)."""
    global _brain, _locale, _config
    _brain = brain
    _locale = locale
    _config = config


def _has_config() -> bool:
    return CONFIG_PATH.exists()


def _has_awakened() -> bool:
    return (LUMEN_DIR / ".awakened").exists()


def _mark_awakened():
    LUMEN_DIR.mkdir(parents=True, exist_ok=True)
    (LUMEN_DIR / ".awakened").write_text("1")


def _init_brain_from_config():
    """Lazy brain initialization — runs once after web setup saves config."""
    global _brain, _locale, _config

    if _brain is not None:
        return True

    if not _has_config():
        return False

    # Late imports to avoid circular deps
    from lumen.core.brain import Brain
    from lumen.core.catalog import Catalog
    from lumen.core.connectors import ConnectorRegistry
    from lumen.core.consciousness import Consciousness
    from lumen.core.discovery import discover_all
    from lumen.core.handlers import register_builtin_handlers
    from lumen.core.memory import Memory
    from lumen.core.personality import Personality
    from lumen.core.registry import Registry

    _config = yaml.safe_load(CONFIG_PATH.read_text())

    # Set API key in environment
    if _config.get("api_key") and _config.get("api_key_env"):
        os.environ[_config["api_key_env"]] = _config["api_key"]

    consciousness = Consciousness()

    lang = _config.get("language", "en")
    personality = Personality(PKG_DIR / "locales" / lang / "personality.yaml")

    memory = Memory(LUMEN_DIR / "memory.db")

    connectors = ConnectorRegistry()
    built_in_path = PKG_DIR / "connectors" / "built-in.yaml"
    if built_in_path.exists():
        connectors.load(built_in_path)

    register_builtin_handlers(connectors, memory)

    registry = Registry()
    discover_all(
        registry=registry,
        pkg_dir=PKG_DIR,
        connectors=connectors,
        active_channels=["web"],
    )

    catalog = Catalog()

    _brain = Brain(
        consciousness=consciousness,
        personality=personality,
        memory=memory,
        connectors=connectors,
        registry=registry,
        catalog=catalog,
        model=_config.get("model", "deepseek/deepseek-chat"),
    )

    # Load flows
    flows_dir = PKG_DIR / "locales" / lang / "flows"
    _brain.load_flows(flows_dir)

    # Load UI locale
    ui_path = PKG_DIR / "locales" / lang / "ui.yaml"
    if ui_path.exists():
        _locale = yaml.safe_load(ui_path.read_text(encoding="utf-8")) or {}

    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize async resources on startup."""
    if _brain:
        await _brain.memory.init()
    yield
    if _brain:
        await _brain.memory.close()


app = FastAPI(title="Lumen", version="0.1.0", lifespan=lifespan)
templates_dir = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_dir))
session_manager = SessionManager()


# ─── Routes ───


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Smart routing: setup → awakening → dashboard."""
    if not _has_config():
        return templates.TemplateResponse("setup.html", {"request": request})

    _init_brain_from_config()

    # Init memory if brain just loaded
    if _brain and _brain.memory._db is None:
        await _brain.memory.init()

    if not _has_awakened():
        return templates.TemplateResponse(
            "awakening.html",
            {"request": request, "language": _config.get("language", "en")},
        )

    return RedirectResponse(url="/dashboard")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Setup wizard — for manual access or re-configuration."""
    return templates.TemplateResponse("setup.html", {"request": request})


@app.post("/api/setup")
async def api_setup(request: Request):
    """Save configuration from the web setup wizard."""
    try:
        body = await request.json()

        config = {
            "language": body.get("language", "en"),
            "model": body.get("model", "deepseek/deepseek-chat"),
            "port": body.get("port", 3000),
        }

        if body.get("api_key_env"):
            config["api_key_env"] = body["api_key_env"]
        if body.get("api_key"):
            config["api_key"] = body["api_key"]

        LUMEN_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(yaml.dump(config, default_flow_style=False))

        # Initialize brain with new config
        _init_brain_from_config()

        if _brain and _brain.memory._db is None:
            await _brain.memory.init()

        return {"status": "ok"}

    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"status": "error", "error": str(e)},
        )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """The main dashboard — Lumen's UI-FIRST experience."""
    if not _has_config():
        return RedirectResponse(url="/")

    _init_brain_from_config()

    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "ui": ui,
            "model": _config.get("model", "not configured"),
            "language": _config.get("language", "en"),
            "version": "0.1.0",
            "connectors_count": len(_brain.connectors.list()) if _brain else 0,
            "flows_count": len(_brain.flows) if _brain else 0,
        },
    )


@app.post("/api/awakened")
async def mark_awakened_endpoint():
    """Called by the awakening animation when it completes."""
    _mark_awakened()
    return {"status": "ok"}


@app.websocket("/ws/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """Real-time chat via WebSocket."""
    await websocket.accept()
    session = session_manager.get_or_create(session_id)

    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            user_text = payload.get("content", "").strip()

            if not user_text or not _brain:
                continue

            await websocket.send_text(
                json.dumps({"type": "typing", "status": True})
            )

            result = await _brain.think(user_text, session)

            await websocket.send_text(
                json.dumps(
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": result["message"],
                    }
                )
            )

            await websocket.send_text(
                json.dumps({"type": "typing", "status": False})
            )

    except WebSocketDisconnect:
        session_manager.remove(session_id)


# ─── Module Management API ───


@app.get("/api/modules/catalog")
async def api_modules_catalog():
    """List all available modules from the catalog."""
    if not _brain:
        return {"modules": []}
    return {"modules": _brain.catalog.list_all()}


@app.get("/api/modules/installed")
async def api_modules_installed():
    """List installed modules."""
    if not _brain:
        return {"modules": []}
    from lumen.core.installer import Installer

    installer = Installer(
        PKG_DIR, _brain.connectors, _brain.memory, _brain.catalog
    )
    return {"modules": installer.list_installed()}


@app.post("/api/modules/install/{name}")
async def api_modules_install(name: str):
    """Install a module from the catalog. Neo knows."""
    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Neo not ready"})
    from lumen.core.installer import Installer

    installer = Installer(
        PKG_DIR, _brain.connectors, _brain.memory, _brain.catalog
    )
    result = installer.install_from_catalog(name)

    # Re-discover — Neo becomes aware of new capability
    if result["status"] == "installed":
        _brain.registry = installer.rediscover()

    return result


@app.delete("/api/modules/uninstall/{name}")
async def api_modules_uninstall(name: str):
    """Uninstall a module. Neo forgets."""
    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Neo not ready"})
    from lumen.core.installer import Installer

    installer = Installer(
        PKG_DIR, _brain.connectors, _brain.memory, _brain.catalog
    )
    result = installer.uninstall(name)

    # Re-discover — Neo forgets the capability
    if result["status"] == "uninstalled":
        _brain.registry = installer.rediscover()

    return result


@app.post("/api/modules/upload")
async def api_modules_upload(request: Request):
    """Upload and install a module from a ZIP file. WordPress-style."""
    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Neo not ready"})
    from lumen.core.installer import Installer

    body = await request.body()
    installer = Installer(
        PKG_DIR, _brain.connectors, _brain.memory, _brain.catalog
    )
    result = installer.install_from_zip(body)

    if result["status"] == "installed":
        _brain.registry = installer.rediscover()

    return result


# ─── Status API ───


@app.get("/api/status")
async def api_status():
    """Lumen's current status — from the Body (registry)."""
    registry = _brain.registry if _brain else None

    flows_info = []
    if _brain:
        for flow in _brain.flows:
            flows_info.append(
                {
                    "intent": flow.get("intent", "unknown"),
                    "triggers": flow.get("triggers", []),
                    "slots": list(flow.get("slots", {}).keys()),
                }
            )

    capabilities = []
    if registry:
        for cap in registry.all():
            capabilities.append(cap.to_dict())

    return {
        "status": "active" if _brain else "not_configured",
        "version": "0.1.0",
        "model": _config.get("model", "not configured"),
        "language": _config.get("language", "en"),
        "capabilities": capabilities,
        "summary": registry.summary() if registry else {},
        "flows": flows_info,
        "ready": len(registry.ready()) if registry else 0,
        "gaps": len(registry.gaps()) if registry else 0,
    }
