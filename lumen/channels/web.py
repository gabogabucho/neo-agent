"""Web channel — FastAPI dashboard + WebSocket chat. UI-FIRST.

Routing logic:
  /  → no config? → /setup
  /  → config but not awakened? → awakening animation
  /  → config and awakened? → /dashboard
"""

import base64
import hashlib
import json
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from time import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request as UrlRequest, urlopen

import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from lumen.core.registry import CapabilityKind
from lumen.core.runtime import (
    bootstrap_runtime,
    refresh_runtime_registry,
    reload_runtime_personality_surface,
)
from lumen.core.session import SessionManager
from lumen.core.module_manifest import load_module_manifest


# State — initialized lazily after web setup or by CLI
_brain = None
_locale: dict = {}
_config: dict = {}

LUMEN_DIR = Path.home() / ".lumen"
CONFIG_PATH = LUMEN_DIR / "config.yaml"
PKG_DIR = Path(__file__).parent.parent
OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
OPENROUTER_CURATED_MODELS = {
    "meta-llama/llama-3.3-70b-instruct:free",
    "deepseek/deepseek-chat:free",
    "mistralai/mistral-7b-instruct:free",
    "google/gemma-3-27b-it:free",
}
OPENROUTER_STATE_TTL_SECONDS = 600
DEFAULT_QUICK_PERSONALITY = "x-lumen-personal"
VALID_ENTRY_PATHS = {"rapido", "elegir_personality", "custom_module"}
PERSONALITY_ENTRY_TAGS = {
    "rapido": {"personality", "personal"},
    "elegir_personality": {"personality"},
}
_oauth_state_store: dict[str, dict] = {}
_oauth_state_lock = threading.Lock()


def configure(brain, locale: dict, config: dict):
    """Configure the web channel (called by CLI when config exists)."""
    global _brain, _locale, _config
    _brain = brain
    _locale = locale
    _config = config


def _has_config() -> bool:
    return CONFIG_PATH.exists()


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return loaded if isinstance(loaded, dict) else {}


def _merge_save_config(updates: dict, *, removals: set[str] | None = None) -> dict:
    merged = _load_config()
    merged.update(_sanitize_config_updates(updates))
    for key in removals or set():
        merged.pop(key, None)
    _enforce_personality_selection_rules(merged)
    LUMEN_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(
        yaml.dump(merged, default_flow_style=False),
        encoding="utf-8",
    )
    return merged


def _sanitize_config_updates(updates: dict) -> dict:
    sanitized = {k: v for k, v in updates.items() if v is not None}

    entry_path = sanitized.get("entry_path")
    if entry_path not in VALID_ENTRY_PATHS:
        sanitized.pop("entry_path", None)

    active_personality = sanitized.get("active_personality")
    if active_personality and not _is_installed_personality_module(active_personality):
        sanitized.pop("active_personality", None)

    return sanitized


def _normalize_module_tags(
    tags: list[str] | tuple[str, ...] | set[str] | None,
) -> set[str]:
    return {str(tag).strip().lower() for tag in (tags or [])}


def _required_personality_tags(entry_path: str | None) -> set[str] | None:
    return PERSONALITY_ENTRY_TAGS.get(str(entry_path or "").strip().lower())


def _installed_personality_manifest(module_name: str) -> dict | None:
    module_dir = PKG_DIR / "modules" / str(module_name)
    manifest_path, manifest = load_module_manifest(module_dir)
    if manifest_path is None:
        return None
    return manifest


def _module_matches_setup_personality_tags(
    module_info: dict, entry_path: str | None
) -> bool:
    required_tags = _required_personality_tags(entry_path)
    if required_tags is None:
        return False
    return required_tags.issubset(_normalize_module_tags(module_info.get("tags")))


def _is_valid_personality_for_entry_path(
    module_name: str, entry_path: str | None = None
) -> bool:
    manifest = _installed_personality_manifest(module_name)
    if manifest is None:
        return False

    tags = _normalize_module_tags(manifest.get("tags"))
    if "personality" not in tags:
        return False

    required_tags = _required_personality_tags(entry_path)
    if required_tags is None:
        return True

    return required_tags.issubset(tags)


def _enforce_personality_selection_rules(config: dict):
    active_personality = config.get("active_personality")
    if not active_personality:
        return

    entry_path = config.get("entry_path")
    if entry_path == "custom_module":
        if not _is_valid_personality_for_entry_path(active_personality):
            config.pop("active_personality", None)
        return

    if not _is_valid_personality_for_entry_path(active_personality, entry_path):
        config.pop("active_personality", None)


def _is_installed_personality_module(module_name: str) -> bool:
    return _is_valid_personality_for_entry_path(module_name)


def _build_setup_installer():
    from lumen.core.catalog import Catalog
    from lumen.core.connectors import ConnectorRegistry
    from lumen.core.installer import Installer

    catalog = Catalog(PKG_DIR / "catalog" / "index.yaml")
    installer = Installer(PKG_DIR, ConnectorRegistry(), memory=None, catalog=catalog)
    return catalog, installer


def _list_setup_personality_modules(entry_path: str | None) -> list[dict]:
    required_tags = _required_personality_tags(entry_path)
    if required_tags is None:
        return []

    catalog, installer = _build_setup_installer()
    modules_by_name: dict[str, dict] = {}

    for module in catalog.list_all():
        if not _module_matches_setup_personality_tags(module, entry_path):
            continue
        modules_by_name[module["name"]] = {
            "name": module["name"],
            "display_name": module.get("display_name", module["name"]),
            "description": module.get("description", ""),
            "tags": module.get("tags", []),
            "installed": False,
        }

    for module in installer.list_installed():
        if not _module_matches_setup_personality_tags(module, entry_path):
            continue
        item = modules_by_name.setdefault(
            module["name"],
            {
                "name": module["name"],
                "display_name": module.get("display_name", module["name"]),
                "description": module.get("description", ""),
                "tags": module.get("tags", []),
                "installed": True,
            },
        )
        item["installed"] = True

    return sorted(
        modules_by_name.values(),
        key=lambda item: (
            str(item.get("display_name") or item["name"]).lower(),
            item["name"],
        ),
    )


def _resolve_setup_active_personality(
    entry_path: str | None, module_name: str | None
) -> str | None:
    normalized_entry_path = str(entry_path or "").strip().lower()

    if normalized_entry_path == "rapido":
        selected_name = str(module_name or DEFAULT_QUICK_PERSONALITY).strip()
        return (
            selected_name
            if _is_valid_personality_for_entry_path(
                selected_name, normalized_entry_path
            )
            else None
        )

    if not module_name:
        return None

    selected_name = str(module_name).strip()
    if not selected_name:
        return None

    if normalized_entry_path == "custom_module":
        return (
            selected_name
            if _is_valid_personality_for_entry_path(selected_name)
            else None
        )

    if not _required_personality_tags(normalized_entry_path):
        return None

    if _is_valid_personality_for_entry_path(selected_name, normalized_entry_path):
        return selected_name

    catalog, installer = _build_setup_installer()
    module_info = catalog.get(selected_name)
    if not module_info or not _module_matches_setup_personality_tags(
        module_info, normalized_entry_path
    ):
        return None

    result = installer.install_from_catalog(selected_name)
    if result.get("status") not in {"installed", "already_installed"}:
        return None

    if _is_valid_personality_for_entry_path(selected_name, normalized_entry_path):
        return selected_name

    return None


def _has_awakened() -> bool:
    return (LUMEN_DIR / ".awakened").exists()


def _mark_awakened():
    LUMEN_DIR.mkdir(parents=True, exist_ok=True)
    (LUMEN_DIR / ".awakened").write_text("1")


def _current_dashboard_personality() -> str:
    active_personality = _config.get("active_personality")
    if active_personality:
        return str(active_personality)

    if _brain is not None:
        identity = (_brain.personality.current() or {}).get("identity") or {}
        personality_name = identity.get("name")
        if personality_name:
            return str(personality_name)

    return "default"


async def _init_brain_from_config():
    """Lazy brain initialization — runs once after web setup saves config."""
    global _brain, _locale, _config

    if _brain is not None:
        return True

    if not _has_config():
        return False

    _config = _load_config()

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


def _base64url_sha256(value: str) -> str:
    digest = hashlib.sha256(value.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")


def _cleanup_expired_oauth_states(now: float | None = None):
    now = now or time()
    expired = [
        key
        for key, payload in _oauth_state_store.items()
        if payload.get("expires_at", 0) <= now
    ]
    for key in expired:
        _oauth_state_store.pop(key, None)


def _store_oauth_state(state: str, payload: dict):
    with _oauth_state_lock:
        _cleanup_expired_oauth_states()
        _oauth_state_store[state] = payload


def _pop_oauth_state(state: str) -> dict | None:
    with _oauth_state_lock:
        _cleanup_expired_oauth_states()
        return _oauth_state_store.pop(state, None)


def _normalize_openrouter_oauth_error(
    error: str | None = None, exc: Exception | None = None
) -> str:
    normalized = str(error or "").strip().lower()
    if normalized in {
        "access_denied",
        "cancelled",
        "canceled",
        "cancel",
        "user_cancelled",
        "user_canceled",
        "canceled_auth",
    }:
        return "canceled_auth"
    if normalized in {
        "invalid_or_expired_state",
        "expired_state",
        "invalid_state",
    }:
        return "invalid_or_expired_state"
    if normalized in {
        "missing_code_or_state",
        "missing_code",
        "missing_state",
    }:
        return "missing_code_or_state"

    if exc is not None:
        details = str(exc).strip().lower()
        if "openrouter key exchange failed" in details:
            return "exchange_failed"

    return "oauth_failed"


def _exchange_openrouter_code(code: str, code_verifier: str) -> str:
    payload = json.dumps(
        {
            "code": code,
            "code_verifier": code_verifier,
            "code_challenge_method": "S256",
        }
    ).encode("utf-8")
    request = UrlRequest(
        OPENROUTER_KEYS_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urlopen(request, timeout=15) as response:
            body = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter key exchange failed: {details}") from exc
    except URLError as exc:
        raise RuntimeError("OpenRouter key exchange failed: network error") from exc

    api_key = body.get("key")
    if not api_key:
        raise RuntimeError("OpenRouter key exchange failed: missing API key")
    return api_key


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
        ui = _locale.get("awakening", {})
        return templates.TemplateResponse(
            request,
            "awakening.html",
            context={"language": _config.get("language", "en"), "ui": ui},
        )

    return RedirectResponse(url="/dashboard")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(request: Request):
    """Setup wizard — for manual access or re-configuration."""
    return templates.TemplateResponse(request, "setup.html")


@app.get("/api/setup/personalities")
async def api_setup_personalities(entry_path: str | None = None):
    """List setup-safe personality modules for the selected entry path."""
    return {"modules": _list_setup_personality_modules(entry_path)}


@app.post("/api/setup")
async def api_setup(request: Request):
    """Save configuration from the web setup wizard."""
    try:
        body = await request.json()
        resolved_personality = _resolve_setup_active_personality(
            body.get("entry_path"),
            body.get("active_personality"),
        )

        config = {
            "language": body.get("language", "en"),
            "model": body.get("model", "deepseek/deepseek-chat"),
            "port": body.get("port", 3000),
            "entry_path": body.get("entry_path"),
        }

        if body.get("api_key_env"):
            config["api_key_env"] = body["api_key_env"]
        if body.get("api_key"):
            config["api_key"] = body["api_key"]
        if resolved_personality:
            config["active_personality"] = resolved_personality

        _merge_save_config(config)

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


@app.get("/oauth/openrouter/start")
async def openrouter_oauth_start(
    request: Request,
    language: str = "en",
    model: str = "deepseek/deepseek-chat:free",
    port: int = 3000,
    entry_path: str | None = None,
    active_personality: str | None = None,
):
    """Start a local-only OpenRouter PKCE flow."""
    if model not in OPENROUTER_CURATED_MODELS:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Unsupported OpenRouter model."},
        )

    selected_language = language if language in {"en", "es"} else "en"
    code_verifier = secrets.token_urlsafe(64)
    state = secrets.token_urlsafe(32)
    callback_url = str(request.url_for("openrouter_oauth_callback"))

    _store_oauth_state(
        state,
        {
            "code_verifier": code_verifier,
            "model": model,
            "language": selected_language,
            "port": port,
            "entry_path": entry_path,
            "active_personality": active_personality,
            "expires_at": time() + OPENROUTER_STATE_TTL_SECONDS,
        },
    )

    params = urlencode(
        {
            "callback_url": callback_url,
            "code_challenge": _base64url_sha256(code_verifier),
            "code_challenge_method": "S256",
            "state": state,
        }
    )
    return RedirectResponse(url=f"{OPENROUTER_AUTH_URL}?{params}")


@app.get("/oauth/openrouter/callback", name="openrouter_oauth_callback")
async def openrouter_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Complete a local-only OpenRouter PKCE flow and save config."""
    if error:
        normalized = _normalize_openrouter_oauth_error(error=error)
        return RedirectResponse(url=f"/setup?oauth_error={normalized}")
    if not code or not state:
        normalized = _normalize_openrouter_oauth_error(error="missing_code_or_state")
        return RedirectResponse(url=f"/setup?oauth_error={normalized}")

    oauth_state = _pop_oauth_state(state)
    if not oauth_state:
        normalized = _normalize_openrouter_oauth_error(error="invalid_or_expired_state")
        return RedirectResponse(url=f"/setup?oauth_error={normalized}")

    try:
        api_key = _exchange_openrouter_code(code, oauth_state["code_verifier"])
        resolved_personality = _resolve_setup_active_personality(
            oauth_state.get("entry_path"),
            oauth_state.get("active_personality"),
        )
        _merge_save_config(
            {
                "language": oauth_state.get("language", "en"),
                "port": oauth_state.get("port", 3000),
                "model": oauth_state["model"],
                "entry_path": oauth_state.get("entry_path"),
                "active_personality": resolved_personality,
                "api_key": api_key,
                "api_key_env": "OPENROUTER_API_KEY",
            }
        )

        await _init_brain_from_config()

        if _brain and _brain.memory._db is None:
            await _brain.memory.init()
    except Exception as exc:
        normalized = _normalize_openrouter_oauth_error(exc=exc)
        return RedirectResponse(url=f"/setup?oauth_error={normalized}")

    return RedirectResponse(url="/")


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
            "current_personality": _current_dashboard_personality(),
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
            session_manager.touch(session_id)
            payload = json.loads(data)

            if payload.get("type") == "ping":
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

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
        global _config
        is_personality = False
        manifest = _installed_personality_manifest(name)
        if manifest:
            tags = _normalize_module_tags(manifest.get("tags"))
            if "personality" in tags:
                is_personality = True
                _config = _merge_save_config({"active_personality": name})

        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])

        if is_personality:
            reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR)

    return result


@app.delete("/api/modules/uninstall/{name}")
async def api_modules_uninstall(name: str):
    """Uninstall a module. Lumen forgets."""
    global _config

    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Lumen not ready"})
    from lumen.core.installer import Installer

    installer = Installer(PKG_DIR, _brain.connectors, _brain.memory, _brain.catalog)
    was_active_personality = _config.get("active_personality") == name
    result = installer.uninstall(name)

    if result["status"] == "uninstalled":
        if was_active_personality:
            _config = _merge_save_config({}, removals={"active_personality"})
        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])
        if was_active_personality:
            reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR)

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
        global _config
        is_personality = False

        # result typically includes the 'name' of the installed module
        module_name = result.get("name")
        if module_name:
            manifest = _installed_personality_manifest(module_name)
            if manifest:
                tags = _normalize_module_tags(manifest.get("tags"))
                if "personality" in tags:
                    is_personality = True
                    _config = _merge_save_config({"active_personality": module_name})

        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])

        if is_personality:
            reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR)

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
