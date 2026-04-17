"""Web channel — FastAPI dashboard + WebSocket chat. UI-FIRST.

Routing logic:
  /  → no config? → /setup
  /  → config but not awakened? → awakening animation
  /  → config and awakened? → /dashboard
"""

import json
from contextlib import asynccontextmanager
from pathlib import Path

import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from lumen.core.registry import CapabilityKind
from lumen.core.runtime import bootstrap_runtime, refresh_runtime_registry
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


async def _init_brain_from_config():
    """Lazy brain initialization — runs once after web setup saves config."""
    global _brain, _locale, _config

    if _brain is not None:
        return True

    if not _has_config():
        return False

    _config = yaml.safe_load(CONFIG_PATH.read_text())

    runtime = await bootstrap_runtime(
        _config,
        pkg_dir=PKG_DIR,
        lumen_dir=LUMEN_DIR,
        active_channels=["web"],
    )
    _brain = runtime.brain
    _locale = runtime.locale
    _config = runtime.config

    return True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize async resources on startup."""
    if _brain:
        await _brain.memory.init()
    yield
    if _brain:
        if getattr(_brain, "mcp_manager", None):
            await _brain.mcp_manager.close()
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
        return templates.TemplateResponse(request, "setup.html")

    await _init_brain_from_config()

    # Init memory if brain just loaded
    if _brain and _brain.memory._db is None:
        await _brain.memory.init()

    if not _has_awakened():
        return templates.TemplateResponse(
            request,
            "awakening.html",
            context={"language": _config.get("language", "en")},
        )

    return RedirectResponse(url="/dashboard")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Setup wizard — for manual access or re-configuration."""
    return templates.TemplateResponse(request, "setup.html")


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
        await _init_brain_from_config()

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

    await _init_brain_from_config()

    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context={
            "ui": ui,
            "model": _config.get("model", "not configured"),
            "language": _config.get("language", "en"),
            "version": "0.1.0",
            "connectors_count": len(_brain.connectors.list()) if _brain else 0,
            "flows_count": len(_brain.flows) if _brain else 0,
            "mcp_count": len(_brain.registry.list_by_kind(CapabilityKind.MCP))
            if _brain
            else 0,
        },
    )


@app.post("/api/awakened")
async def mark_awakened_endpoint():
    """Called by the awakening animation when it completes."""
    _mark_awakened()
    return {"status": "ok"}


@app.get("/api/history/{session_id}")
async def api_history(session_id: str):
    """Load conversation history for a session from persistent memory."""
    if not _brain:
        return {"messages": []}
    try:
        messages = await _brain.memory.load_conversation(session_id, limit=50)
        return {"messages": messages}
    except Exception:
        return {"messages": []}


@app.websocket("/ws/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """Real-time chat via WebSocket."""
    await websocket.accept()
    session = session_manager.get_or_create(session_id)

    # Hydrate session from persistent memory if empty (reconnect/refresh)
    if not session.history and _brain:
        try:
            stored = await _brain.memory.load_conversation(session_id, limit=50)
            for msg in stored:
                session.add_message(msg["role"], msg["content"])
        except Exception:
            pass

    try:
        while True:
            data = await websocket.receive_text()
            payload = json.loads(data)
            user_text = payload.get("content", "").strip()

            if not user_text or not _brain:
                continue

            await websocket.send_text(json.dumps({"type": "typing", "status": True}))

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

            await websocket.send_text(json.dumps({"type": "typing", "status": False}))

    except WebSocketDisconnect:
        session_manager.remove(session_id)


# ─── Debug API ───


@app.get("/api/debug/prompt")
async def api_debug_prompt():
    """Show the exact system prompt the LLM receives. For debugging."""
    if not _brain:
        return {"error": "Brain not initialized"}

    from lumen.core.session import Session

    session = Session()
    context = {
        "consciousness": _brain.consciousness.as_context(),
        "personality": _brain.personality.as_context(),
        "body": _brain.registry.as_context(),
        "catalog": _brain.catalog.as_context(
            installed_names={
                c.name for c in _brain.registry.list_by_kind(CapabilityKind.MODULE)
            },
            registry=_brain.registry,
            connectors=_brain.connectors,
        ),
        "active_flow": None,
        "filled_slots": {},
        "pending_slots": [],
        "memories": [],
        "available_flows": _brain.flows,
    }
    messages = _brain._build_prompt(context, "test", session)
    return {
        "system_prompt": messages[0]["content"],
        "length": len(messages[0]["content"]),
    }


# ─── Module Management API ───


@app.get("/api/modules/catalog")
async def api_modules_catalog():
    """List all available modules from the catalog."""
    if not _brain:
        return {"modules": []}
    marketplace = getattr(_brain, "marketplace", None)
    if marketplace is not None:
        return {"modules": marketplace.kits_catalog()}
    return {
        "modules": _brain.catalog.list_all(
            registry=_brain.registry,
            connectors=_brain.connectors,
        )
    }


@app.get("/api/modules/installed")
async def api_modules_installed():
    """List installed modules."""
    if not _brain:
        return {"modules": []}
    marketplace = getattr(_brain, "marketplace", None)
    if marketplace is not None:
        return {"modules": marketplace.kits_installed()}
    from lumen.core.installer import Installer

    installer = Installer(PKG_DIR, _brain.connectors, _brain.memory, _brain.catalog)
    return {"modules": installer.list_installed()}


@app.get("/api/marketplace")
async def api_marketplace():
    """Aggregated marketplace read model: Body + Kits Lumen + remote feeds."""
    if not _brain:
        return {
            "generated_at": None,
            "feeds": [],
            "tabs": [],
            "skills": {"items": [], "installed": [], "available": [], "counts": {}},
            "mcps": {"items": [], "installed": [], "available": [], "counts": {}},
            "kits_lumen": {
                "items": [],
                "installed": [],
                "available": [],
                "counts": {},
                "upload_enabled": True,
            },
        }

    marketplace = getattr(_brain, "marketplace", None)
    if marketplace is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Marketplace service not initialized"},
        )
    return marketplace.snapshot()


@app.post("/api/modules/install/{name}")
async def api_modules_install(name: str):
    """Install a module from the catalog. Lumen knows."""
    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Lumen not ready"})
    from lumen.core.installer import Installer

    installer = Installer(PKG_DIR, _brain.connectors, _brain.memory, _brain.catalog)
    result = installer.install_from_catalog(name)

    if result["status"] == "installed":
        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])

    return result


@app.delete("/api/modules/uninstall/{name}")
async def api_modules_uninstall(name: str):
    """Uninstall a module. Lumen forgets."""
    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Lumen not ready"})
    from lumen.core.installer import Installer

    installer = Installer(PKG_DIR, _brain.connectors, _brain.memory, _brain.catalog)
    result = installer.uninstall(name)

    if result["status"] == "uninstalled":
        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])

    return result


@app.post("/api/modules/upload")
async def api_modules_upload(request: Request):
    """Upload and install a module from a ZIP file. WordPress-style."""
    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Lumen not ready"})
    from lumen.core.installer import Installer

    body = await request.body()
    installer = Installer(PKG_DIR, _brain.connectors, _brain.memory, _brain.catalog)
    result = installer.install_from_zip(body)

    if result["status"] == "installed":
        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])

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
