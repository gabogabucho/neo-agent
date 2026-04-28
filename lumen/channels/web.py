"""Web channel — FastAPI dashboard + WebSocket chat. UI-FIRST.

Routing logic:
  /  → no config? → /setup
  /  → config but not awakened? → awakening animation
  /  → config and awakened? → /dashboard
"""

import base64
import asyncio
import hashlib
import hmac
import json
import os
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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lumen import __version__
from lumen.core.artifact_setup import (
    contract_from_mcp_server,
    load_mcp_overlay,
    parse_artifact_action,
    pending_setup_from_contract,
)
from lumen.core.secrets_store import load_module as load_secrets_for_module
from lumen.core.secrets_store import save_module as save_secrets_for_module
from lumen.core.registry import CapabilityKind
from lumen.core.runtime import (
    apply_provider_runtime_env,
    bootstrap_runtime,
    refresh_runtime_registry,
    rehydrate_runtime_config,
    reload_runtime_personality_surface,
    sync_runtime_modules,
)
from lumen.core.module_runtime import ModuleRuntimeManager
from lumen.core.marketplace import humanize_module_name
from lumen.core.mcp import MCPManager
from lumen.core.session import SessionManager
from lumen.core.module_manifest import load_module_manifest
from lumen.core.module_setup import (
    merge_module_setup_config,
    normalize_module_setup_values,
    pending_setup_for_manifest,
)
from lumen.core.model_router import ModelRouter, ModelRouterConfig, VALID_ROLES
from lumen.core.provider_health import ProviderHealthTracker
from lumen.core.agent_status import AgentStatusCollector
from lumen.core.tool_policy import ToolPolicy, SecurityConfig


# State — initialized lazily after web setup or by CLI
_brain = None
_locale: dict = {}
_config: dict = {}
_access_mode = "run"
_awareness = None  # CapabilityAwareness — set during bootstrap
_active_websockets: set[WebSocket] = set()  # Track connected clients
_watchers = None  # FilePoller — started in lifespan
_reload_ipc_task = None
_web_start_time = time.monotonic()  # For uptime calculation

LUMEN_DIR = Path.home() / ".lumen"
CONFIG_PATH = LUMEN_DIR / "config.yaml"
PKG_DIR = Path(__file__).parent.parent
OPENROUTER_AUTH_URL = "https://openrouter.ai/auth"
OPENROUTER_KEYS_URL = "https://openrouter.ai/api/v1/auth/keys"
OPENROUTER_CURATED_MODELS = {
    "openai/gpt-oss-120b:free",
    "qwen/qwen3-coder:free",
    "google/gemma-3-27b-it:free",
    "nousresearch/hermes-3-llama-3.1-405b:free",
}
OPENROUTER_STATE_TTL_SECONDS = 600
DEFAULT_OPENROUTER_MODEL = "openai/gpt-oss-120b:free"
DEFAULT_QUICK_PERSONALITY = "x-lumen-personal"
AUTH_COOKIE_NAME = "lumen_owner"
SETUP_COOKIE_NAME = "lumen_setup"
COOKIE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30
RELOAD_REQUEST_FILE = ".lumen-reload-request.json"
RELOAD_ACK_FILE = ".lumen-reload-ack.json"
LEGACY_ENTRY_PATH_MAP = {
    "uso_personal": "rapido",
    "negocio": "elegir_personality",
    "desde_cero": "custom_module",
}
VALID_ENTRY_PATHS = {"rapido", "elegir_personality", "custom_module"}
PERSONALITY_ENTRY_TAGS = {
    "rapido": {"personality", "personal"},
    "elegir_personality": {"personality"},
}
_oauth_state_store: dict[str, dict] = {}
_oauth_state_lock = threading.Lock()
_agent_status_collector: AgentStatusCollector | None = None

# Pending tool confirmations: call_id -> asyncio.Future
_pending_confirmations: dict[str, asyncio.Future] = {}
# SSE confirm event queues: one per active SSE stream
_sse_confirm_queues: list[asyncio.Queue] = []


def configure(brain, locale: dict, config: dict, awareness=None, *, lumen_dir: Path | None = None):
    """Configure the web channel (called by CLI when config exists).
    
    Args:
        lumen_dir: Instance-aware data directory. If None, uses default ~/.lumen/
    """
    global _brain, _locale, _config, _awareness, LUMEN_DIR, CONFIG_PATH
    if lumen_dir is not None:
        LUMEN_DIR = lumen_dir
        CONFIG_PATH = lumen_dir / "config.yaml"
    _brain = brain
    _locale = locale
    _config = config
    _awareness = awareness
    _attach_brain_runtime_handlers()
    # Set up confirmation handler for web channel
    if _brain:
        _brain.confirmation_gate.set_handler(_web_confirm_handler)


def _attach_brain_runtime_handlers():
    if _brain is not None:
        _brain.flow_action_handler = _handle_flow_action
        _start_inbox_consumer()
        manager = getattr(_brain, "module_manager", None)
        if manager and hasattr(manager, "set_broadcast_callback"):
            manager.set_broadcast_callback(broadcast_event)


_inbox_consumer_task = None


def _start_inbox_consumer():
    global _inbox_consumer_task
    inbox = getattr(_brain, "inbox", None)
    if inbox is None:
        return
    import asyncio
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    _inbox_consumer_task = loop.create_task(
        inbox.start_consumer(_brain, session_manager)
    )


async def _perform_runtime_reload() -> None:
    global _config
    _config = rehydrate_runtime_config(_config, lumen_dir=LUMEN_DIR)
    if getattr(_brain, "config", None) is not None:
        _brain.config = _config
    if getattr(_brain, "connectors", None) is not None and hasattr(_brain.connectors, "set_runtime_config"):
        _brain.connectors.set_runtime_config(_config)
    await sync_runtime_modules(_brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR)
    refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR, active_channels=["web"])
    reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR)


async def _reload_ipc_loop(interval: float = 1.0):
    request_path = LUMEN_DIR / RELOAD_REQUEST_FILE
    ack_path = LUMEN_DIR / RELOAD_ACK_FILE
    while True:
        try:
            await asyncio.sleep(interval)
            if not request_path.exists() or not _brain:
                continue
            payload = json.loads(request_path.read_text(encoding="utf-8"))
            request_id = payload.get("id")
            try:
                await _perform_runtime_reload()
                ack = {"id": request_id, "status": "ok", "ts": time()}
            except Exception as exc:
                ack = {"id": request_id, "status": "error", "error": str(exc), "ts": time()}
            ack_path.write_text(json.dumps(ack), encoding="utf-8")
            request_path.unlink(missing_ok=True)
        except asyncio.CancelledError:
            break
        except Exception:
            pass


async def _handle_flow_action(action: str, slots: dict, *, session=None) -> dict:
    parsed = parse_artifact_action(action)
    if parsed is None:
        return {"status": "ignored", "message": "Listo."}

    kind, artifact_id = parsed
    if kind == "native":
        return await _persist_module_setup_slots(artifact_id, slots)
    if kind == "mcp":
        return await _persist_mcp_setup_slots(artifact_id, slots)
    if kind in ("manual", "external"):
        return {"status": "ok", "message": "Listo. Seguí las instrucciones para completar la configuración."}
    return {
        "status": "error",
        "message": f"Todavía no sé guardar configuraciones para artefactos de tipo {kind}.",
    }


async def _persist_module_setup_slots(module_name: str, values: dict | None) -> dict:
    global _config

    module_dir = _installed_module_dir(module_name)
    manifest_path, manifest = load_module_manifest(module_dir)
    if manifest_path is None:
        return {"status": "error", "message": f"El módulo {module_name} ya no está instalado."}

    before_pending = pending_setup_for_manifest(
        module_name,
        manifest,
        _config,
        module_dir=module_dir,
    )
    if before_pending is None:
        return {
            "status": "ok",
            "module": module_name,
            "saved_env": [],
            "pending_setup": None,
            "message": f"{module_name} ya estaba listo.",
        }

    normalized = normalize_module_setup_values(
        values,
        module_name=module_name,
        manifest=manifest,
        module_dir=module_dir,
        config=_config,
    )
    merged = merge_module_setup_config(
        _config,
        module_name,
        normalized.get("values"),
        manifest=manifest,
        module_dir=module_dir,
        config_for_validation=_config,
    )
    after_pending = pending_setup_for_manifest(
        module_name,
        manifest,
        merged,
        module_dir=module_dir,
    )
    previous_saved = set((((_config.get("secrets") or {}).get(module_name) or {}).keys()))
    module_secrets = (merged.get("secrets") or {}).get(module_name) or {}
    saved_env = sorted(set(module_secrets.keys()) - previous_saved)

    validation_errors = normalized.get("errors") or {}
    if validation_errors and not saved_env:
        return {
            "status": "error",
            "module": module_name,
            "saved_env": [],
            "pending_setup": before_pending,
            "errors": validation_errors,
            "message": "No guardé esos datos porque el formato no es válido todavía.",
        }

    # Persist secrets to dedicated store AND config (migration cleans config later)
    if module_secrets:
        save_secrets_for_module(module_name, module_secrets)

        # Trigger on_configure lifecycle hook if module defines one
        from lumen.core.module_runtime import run_module_configure_hook
        module_dir = _installed_module_dir(module_name)
        if module_dir.is_dir():
            run_module_configure_hook(
                name=module_name,
                module_dir=module_dir,
                runtime_root=LUMEN_DIR / "modules",
                config=_config,
                lumen_dir=LUMEN_DIR,
            )

    _config = _merge_save_config({"secrets": merged.get("secrets", {})})

    if _brain is not None:
        # Unload the module first so sync re-activates with updated config
        manager = getattr(_brain, "module_manager", None)
        if manager:
            await manager.unload(module_name)
        await sync_runtime_modules(_brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR)
        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR, active_channels=["web"])
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR)
        await broadcast_awareness()

    if after_pending is None:
        message = f"Listo, {module_name} ya quedó listo para usar."
    else:
        remaining = len(after_pending.get("env_specs") or [])
        readiness = after_pending.get("readiness") or {}
        reason = str(readiness.get("reason") or "").strip()
        if remaining:
            message = f"Guardé lo válido, pero todavía necesito {remaining} dato{'s' if remaining != 1 else ''} para {module_name}."
        elif reason:
            message = f"Guardé los datos, pero {module_name} todavía no está listo: {reason}"
        else:
            message = f"Guardé los datos, pero {module_name} todavía no quedó listo."

    if validation_errors:
        message = f"{message} También rechacé algunos valores por formato inválido."

    return {
        "status": "ok" if not validation_errors else "partial",
        "module": module_name,
        "saved_env": saved_env,
        "pending_setup": after_pending,
        "errors": validation_errors,
        "message": message,
    }


async def _restart_mcp_runtime() -> None:
    if _brain is None:
        return

    for tool in list(_brain.connectors.list_registered_tools()):
        metadata = tool.get("metadata") or {}
        if metadata.get("kind") == "mcp":
            _brain.connectors.unregister_tool(tool.get("name", ""))

    if getattr(_brain, "mcp_manager", None):
        await _brain.mcp_manager.close()

    manager = MCPManager(_config.get("mcp"), pkg_dir=PKG_DIR)
    await manager.start(_brain.connectors.register_tool)
    _brain.mcp_manager = manager


async def _persist_mcp_setup_slots(server_id: str, values: dict | None) -> dict:
    global _config

    servers = (((_config.get("mcp") or {}).get("servers")) or {})
    server_config = servers.get(server_id)
    if not isinstance(server_config, dict):
        return {"status": "error", "message": f"El servidor MCP {server_id} ya no está configurado."}

    overlay = load_mcp_overlay(server_id, PKG_DIR)
    before_contract = contract_from_mcp_server(server_id, server_config, overlay=overlay)
    before_pending = pending_setup_from_contract(before_contract)
    if before_contract is None:
        return {
            "status": "ok",
            "server_id": server_id,
            "saved_env": [],
            "pending_setup": None,
            "message": f"{server_id} ya estaba listo.",
        }

    normalized = normalize_module_setup_values(
        values,
        module_name=server_id,
        specs=list(before_contract.specs),
    )

    merged = _load_config()
    merged_mcp = dict(merged.get("mcp") or {})
    merged_servers = dict(merged_mcp.get("servers") or {})
    merged_server = dict(merged_servers.get(server_id) or {})
    merged_env = dict(merged_server.get("env") or {})
    previous_saved = set(str(key) for key in merged_env.keys())
    merged_env.update(normalized.get("values") or {})
    merged_server["env"] = merged_env
    merged_servers[server_id] = merged_server
    merged_mcp["servers"] = merged_servers
    merged["mcp"] = merged_mcp

    after_contract = contract_from_mcp_server(server_id, merged_server, overlay=overlay)
    after_pending = pending_setup_from_contract(after_contract)
    current_saved = set(str(key) for key in merged_env.keys())
    saved_env = sorted(current_saved - previous_saved)

    validation_errors = normalized.get("errors") or {}
    if validation_errors and not saved_env:
        return {
            "status": "error",
            "server_id": server_id,
            "saved_env": [],
            "pending_setup": before_pending,
            "errors": validation_errors,
            "message": "No guardé esos datos porque el formato no es válido todavía.",
        }

    _config = _merge_save_config({"mcp": merged_mcp})

    if _brain is not None:
        await _restart_mcp_runtime()
        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR, active_channels=["web"])
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR)
        await broadcast_awareness()

    if after_pending is None:
        message = f"Listo, {server_id} ya quedó listo para usar."
    else:
        remaining = len(after_pending.get("env_specs") or [])
        if remaining:
            message = f"Guardé lo válido, pero todavía necesito {remaining} dato{'s' if remaining != 1 else ''} para {server_id}."
        else:
            message = f"Guardé los datos, pero {server_id} todavía no quedó listo."

    if validation_errors:
        message = f"{message} También rechacé algunos valores por formato inválido."

    return {
        "status": "ok" if not validation_errors else "partial",
        "server_id": server_id,
        "saved_env": saved_env,
        "pending_setup": after_pending,
        "errors": validation_errors,
        "message": message,
    }


def configure_access_mode(mode: str = "run"):
    """Configure whether the web app runs locally or as hosted server."""
    global _access_mode
    _access_mode = "serve" if str(mode).strip().lower() == "serve" else "run"


def _has_config() -> bool:
    return _is_configured(_load_config())


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    loaded = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
    if not isinstance(loaded, dict):
        return {}

    entry_path = loaded.get("entry_path")
    if entry_path in LEGACY_ENTRY_PATH_MAP:
        loaded["entry_path"] = LEGACY_ENTRY_PATH_MAP[entry_path]

    # Merge secrets from store into in-memory config["secrets"]
    from lumen.core.secrets_store import load_all as load_all_secrets
    all_secrets = load_all_secrets()
    if all_secrets:
        existing = loaded.get("secrets") or {}
        if not isinstance(existing, dict):
            existing = {}
        for mod_name, bucket in all_secrets.items():
            if isinstance(bucket, dict):
                existing.setdefault(mod_name, {}).update(bucket)
        loaded["secrets"] = existing

    return loaded


def _is_configured(config: dict | None = None) -> bool:
    loaded = config if config is not None else _load_config()
    return bool(loaded.get("model"))


def _is_serve_mode() -> bool:
    return _access_mode == "serve"


def _server_secret(config: dict | None = None) -> str | None:
    loaded = config if config is not None else _load_config()
    secret = loaded.get("server_secret")
    return str(secret) if secret else None


def _hash_secret(value: str, *, salt: str | None = None) -> str:
    used_salt = salt or secrets.token_urlsafe(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        value.encode("utf-8"),
        used_salt.encode("utf-8"),
        260000,
    )
    encoded = base64.urlsafe_b64encode(digest).decode("utf-8").rstrip("=")
    return f"pbkdf2_sha256$260000${used_salt}${encoded}"


def _verify_secret(value: str, stored_hash: str | None) -> bool:
    if not value or not stored_hash:
        return False
    try:
        algorithm, iterations, salt, digest = str(stored_hash).split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256",
            value.encode("utf-8"),
            salt.encode("utf-8"),
            int(iterations),
        )
        encoded = base64.urlsafe_b64encode(computed).decode("utf-8").rstrip("=")
        return hmac.compare_digest(encoded, digest)
    except (TypeError, ValueError):
        return False


def _sign_cookie(payload: dict, secret: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload).encode("utf-8")).decode("utf-8")
    signature = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256)
    digest = base64.urlsafe_b64encode(signature.digest()).decode("utf-8").rstrip("=")
    return f"{body}.{digest}"


def _read_signed_cookie(value: str | None, secret: str | None) -> dict | None:
    if not value or not secret:
        return None
    try:
        body, digest = value.split(".", 1)
    except ValueError:
        return None
    expected = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256)
    expected_digest = (
        base64.urlsafe_b64encode(expected.digest()).decode("utf-8").rstrip("=")
    )
    if not hmac.compare_digest(expected_digest, digest):
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(f"{body}==").decode("utf-8"))
    except Exception:
        return None
    if payload.get("exp", 0) <= time():
        return None
    return payload


def _issue_cookie(
    scope: str, secret: str, *, ttl_seconds: int = COOKIE_MAX_AGE_SECONDS
) -> str:
    return _sign_cookie(
        {
            "scope": scope,
            "exp": int(time()) + ttl_seconds,
            "nonce": secrets.token_urlsafe(8),
        },
        secret,
    )


def _request_has_setup_access(request: Request, config: dict | None = None) -> bool:
    payload = _read_signed_cookie(
        request.cookies.get(SETUP_COOKIE_NAME),
        _server_secret(config),
    )
    return bool(payload and payload.get("scope") == "setup")


def _request_has_owner_access(request: Request, config: dict | None = None) -> bool:
    payload = _read_signed_cookie(
        request.cookies.get(AUTH_COOKIE_NAME),
        _server_secret(config),
    )
    return bool(payload and payload.get("scope") == "owner")


def _websocket_has_owner_access(
    websocket: WebSocket, config: dict | None = None
) -> bool:
    payload = _read_signed_cookie(
        websocket.cookies.get(AUTH_COOKIE_NAME),
        _server_secret(config),
    )
    return bool(payload and payload.get("scope") == "owner")


def _require_setup_access(
    request: Request, config: dict | None = None
) -> JSONResponse | None:
    loaded = config if config is not None else _load_config()
    if (
        _is_serve_mode()
        and not _is_configured(loaded)
        and not _request_has_setup_access(request, loaded)
    ):
        return JSONResponse(status_code=401, content={"error": "setup_token_required"})
    return None


def _require_owner_access(
    request: Request, config: dict | None = None
) -> JSONResponse | None:
    loaded = config if config is not None else _load_config()
    if (
        _is_serve_mode()
        and _is_configured(loaded)
        and not _request_has_owner_access(request, loaded)
    ):
        return JSONResponse(
            status_code=401, content={"error": "authentication_required"}
        )
    return None


def ensure_server_bootstrap(*, host: str = "0.0.0.0", port: int = 3000) -> str:
    """Ensure hosted mode has a token-protected bootstrap state."""
    loaded = _load_config()
    updates = {
        "server_mode": True,
        "host": host,
        "port": int(port),
        "server_secret": loaded.get("server_secret") or secrets.token_urlsafe(32),
    }

    token = secrets.token_urlsafe(18)
    needs_setup_token = not _is_configured(loaded) or not loaded.get("owner_secret_hash")
    if needs_setup_token:
        updates["setup_token_hash"] = _hash_secret(token)

    _merge_save_config(updates)
    return token


def _load_ui_locale(language: str | None) -> dict:
    lang = str(language or "en").strip().lower() or "en"
    ui_path = PKG_DIR / "locales" / lang / "ui.yaml"
    if not ui_path.exists() and lang != "en":
        ui_path = PKG_DIR / "locales" / "en" / "ui.yaml"
    if not ui_path.exists():
        return {}
    loaded = yaml.safe_load(ui_path.read_text(encoding="utf-8")) or {}
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
    if entry_path in LEGACY_ENTRY_PATH_MAP:
        entry_path = LEGACY_ENTRY_PATH_MAP[entry_path]
        sanitized["entry_path"] = entry_path

    if entry_path not in VALID_ENTRY_PATHS:
        sanitized.pop("entry_path", None)

    active_personality = sanitized.get("active_personality")
    if active_personality and not _is_installed_personality_module(active_personality):
        sanitized.pop("active_personality", None)

    return sanitized


def _normalize_optional_text(value) -> str | None:
    if value is None:
        return None
    return str(value).strip()


def _infer_provider_name(config: dict | None = None) -> str:
    loaded = config if config is not None else _config
    loaded = loaded or {}

    if loaded.get("api_key_env") == "OPENROUTER_API_KEY" and loaded.get("api_key"):
        return "OpenRouter"

    provider = _normalize_optional_text(loaded.get("provider"))
    if provider:
        return provider

    model = str(loaded.get("model") or "").strip().lower()
    if model.startswith("deepseek/"):
        return "DeepSeek"
    if model.startswith("gpt-") or model.startswith("openai/"):
        return "OpenAI"
    if model.startswith("claude") or model.startswith("anthropic/"):
        return "Anthropic"
    if model.startswith("ollama/"):
        return "Ollama"
    if ":free" in model or model.startswith("meta-") or model.startswith("google/"):
        return "OpenRouter"
    return "Custom"


def _is_openrouter_model(model: str | None) -> bool:
    return str(model or "").strip() in OPENROUTER_CURATED_MODELS


def _openrouter_redirect_target(value: str | None = None) -> str:
    target = str(value or "").strip()
    if target == "/dashboard":
        return "/dashboard?panel=config&openrouter=connected"
    if target.startswith("/dashboard?"):
        joiner = "&" if "?" in target else "?"
        return f"{target}{joiner}openrouter=connected"
    return "/"


def _openrouter_error_redirect(target: str | None, normalized_error: str) -> str:
    resolved = str(target or "").strip() or "/setup"
    joiner = "&" if "?" in resolved else "?"
    return f"{resolved}{joiner}oauth_error={normalized_error}"


def _apply_config_api_key_env(config: dict, previous_config: dict | None = None) -> None:
    previous = previous_config or {}
    previous_env = _normalize_optional_text(previous.get("api_key_env"))
    previous_key = _normalize_optional_text(previous.get("api_key"))
    current_env = _normalize_optional_text(config.get("api_key_env"))
    current_key = _normalize_optional_text(config.get("api_key"))

    if previous_env and previous_env != current_env and os.environ.get(previous_env) == previous_key:
        os.environ.pop(previous_env, None)

    if current_env and current_key:
        os.environ[current_env] = current_key

    previous_api_base = _normalize_optional_text(previous.get("api_base"))
    current_api_base = _normalize_optional_text(config.get("api_base"))
    if previous_api_base and previous_api_base != current_api_base and os.environ.get("OPENAI_API_BASE") == previous_api_base:
        os.environ.pop("OPENAI_API_BASE", None)
    if current_api_base:
        os.environ["OPENAI_API_BASE"] = current_api_base


async def _refresh_runtime_from_config(previous_config: dict | None = None) -> bool:
    """Apply saved config changes to the live web runtime."""
    global _config, _locale

    latest_config = _load_config() if _has_config() else {}
    if not latest_config:
        return False

    _apply_config_api_key_env(latest_config, previous_config)
    previous_language = str((previous_config or {}).get("language") or "en")

    if _brain is None:
        return await _init_brain_from_config()

    _config = latest_config
    _locale = _load_ui_locale(_config.get("language", "en"))
    _brain.model = _config.get("model", _brain.model)
    _brain.api_key_env = _config.get("api_key_env")
    _brain.language = str(_config.get("language") or "en").lower()

    if getattr(_brain, "marketplace", None) is not None:
        _brain.marketplace.config = _config

    if isinstance(getattr(_brain, "module_manager", None), ModuleRuntimeManager):
        _brain.module_manager.config = _config

    if getattr(_brain, "mcp_manager", None) is not None:
        await _restart_mcp_runtime()

    refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR, active_channels=["web"])

    if str(_config.get("language") or "en") != previous_language:
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR)

    return True


def _normalize_module_tags(
    tags: list[str] | tuple[str, ...] | set[str] | None,
) -> set[str]:
    return {str(tag).strip().lower() for tag in (tags or [])}


def _required_personality_tags(entry_path: str | None) -> set[str] | None:
    return PERSONALITY_ENTRY_TAGS.get(str(entry_path or "").strip().lower())


def _installed_module_dir(module_name: str) -> Path:
    instance_dir = LUMEN_DIR / "modules" / str(module_name)
    if instance_dir.exists():
        return instance_dir
    return PKG_DIR / "modules" / str(module_name)


def _installed_personality_manifest(module_name: str) -> dict | None:
    module_dir = _installed_module_dir(module_name)
    manifest_path, manifest = load_module_manifest(module_dir)
    if manifest_path is None:
        return None
    return manifest


def _find_marketplace_item(name: str) -> dict | None:
    if not _brain or not getattr(_brain, "marketplace", None):
        return None
    snapshot = _brain.marketplace.snapshot()
    for section_key in ("modules", "kits", "skills"):
        section = snapshot.get(section_key) or {}
        for bucket in ("installed", "available", "items"):
            for item in section.get(bucket, []) or []:
                if item.get("name") == name:
                    return item
    return None


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
    catalog, installer = _build_setup_installer()

    if normalized_entry_path == "rapido":
        selected_name = str(module_name or DEFAULT_QUICK_PERSONALITY).strip()
        if _is_valid_personality_for_entry_path(selected_name, normalized_entry_path):
            return selected_name

        module_info = catalog.get(selected_name)
        if not module_info or not _module_matches_setup_personality_tags(
            module_info, normalized_entry_path
        ):
            return None

        result = installer.install_from_catalog(selected_name)
        if result.get("status") not in {"installed", "already_installed"}:
            return None

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
        return humanize_module_name(str(active_personality))

    if _brain is not None:
        identity = (_brain.personality.current() or {}).get("identity") or {}
        personality_name = identity.get("name")
        if personality_name:
            return humanize_module_name(str(personality_name))

    return "default"


def _current_personality_ui() -> dict:
    """Return UI rendering hints declared by the active personality."""
    if not _brain or not getattr(_brain, "personality", None):
        return {"tag": "agent-ui", "surfaces": []}

    raw = (_brain.personality.current() or {}).get("ui") or {}
    if not isinstance(raw, dict):
        return {"tag": "agent-ui", "surfaces": []}

    tag = str(raw.get("tag") or "agent-ui").strip() or "agent-ui"
    surfaces = raw.get("surfaces")
    if not isinstance(surfaces, list):
        surfaces = []

    return {
        "tag": tag,
        "surfaces": [str(surface) for surface in surfaces if str(surface).strip()],
    }


async def _init_brain_from_config():
    """Lazy brain initialization — runs once after web setup saves config."""
    global _brain, _locale, _config

    latest_config = _load_config() if _has_config() else {}

    if latest_config:
        _apply_config_api_key_env(latest_config, _config)

    if _brain is not None:
        if latest_config:
            _config = latest_config
            _locale = _load_ui_locale(_config.get("language", "en"))
        return True

    if not _has_config():
        return False

    _config = latest_config

    runtime = await bootstrap_runtime(
        _config,
        pkg_dir=PKG_DIR,
        lumen_dir=LUMEN_DIR,
        active_channels=["web"],
    )
    _brain = runtime.brain
    _locale = runtime.locale
    _config = runtime.config
    _awareness = runtime.awareness
    _attach_brain_runtime_handlers()

    # Wire broadcast callback so modules can push real-time events
    manager = getattr(_brain, "module_manager", None)
    if manager and hasattr(manager, "set_broadcast_callback"):
        manager.set_broadcast_callback(broadcast_event)

    # Pre-load lessons for prompt injection
    await _brain.load_lessons()

    # Set up confirmation handler for web channel
    _brain.confirmation_gate.set_handler(_web_confirm_handler)

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
    global _watchers, _inbox_consumer_task, _reload_ipc_task
    if _brain:
        await _brain.memory.init()

    # Start the inbox consumer if brain was pre-initialized by CLI.
    # CLI uses asyncio.run() which destroys its event loop — all async
    # tasks (poll loops, watchers) die with it. We need to restart them
    # here inside uvicorn's event loop.
    if _brain and _inbox_consumer_task is None:
        import asyncio as _asyncio

        inbox = getattr(_brain, "inbox", None)
        if inbox is not None:
            _inbox_consumer_task = _asyncio.create_task(
                inbox.start_consumer(_brain, session_manager)
            )

        # Re-activate gateway modules so their poll/watcher tasks run
        # in this event loop instead of the dead CLI one.
        manager = getattr(_brain, "module_manager", None)
        if isinstance(manager, ModuleRuntimeManager):
            manager.brain = _brain
            # Unload all so sync() re-activates with fresh async tasks
            for name in list(manager._loaded):
                await manager.unload(name)
            await manager.sync()

    # Start capability watchers if brain is ready
    if _brain and _awareness:
        from lumen.core.watchers import FilePoller

        modules_dir = LUMEN_DIR / "modules"
        skills_dir = PKG_DIR / "skills"
        watched = [d for d in [modules_dir, skills_dir] if d.exists()]

        async def _on_file_change():
            """Called by FilePoller when filesystem changes detected."""
            refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR, active_channels=["web"])
            await broadcast_awareness()

        if watched:
            _watchers = FilePoller(_brain.registry, watched, on_change=_on_file_change)
            await _watchers.start(interval=120)

    if _brain and _reload_ipc_task is None:
        _reload_ipc_task = asyncio.create_task(_reload_ipc_loop())

    yield

    # Cleanup
    if _watchers:
        await _watchers.stop()
    if _reload_ipc_task is not None:
        _reload_ipc_task.cancel()
        try:
            await _reload_ipc_task
        except asyncio.CancelledError:
            pass
        _reload_ipc_task = None
    if _inbox_consumer_task is not None:
        _inbox_consumer_task.cancel()
    if _brain:
        if isinstance(getattr(_brain, "module_manager", None), ModuleRuntimeManager):
            await _brain.module_manager.close()
        if getattr(_brain, "mcp_manager", None):
            await _brain.mcp_manager.close()
        await _brain.memory.close()


app = FastAPI(title="Lumen", version=__version__, lifespan=lifespan)
templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(templates_dir))
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
session_manager = SessionManager()


# ─── Routes ───


# ─── Health Check ───


@app.get("/health")
async def health_check():
    """Public health endpoint for monitoring and load balancers.

    No authentication required. Returns basic status info.
    """
    modules_ready = 0
    if _brain and _brain.registry:
        modules_ready = len(
            [
                c
                for c in _brain.registry.list_by_kind(CapabilityKind.MODULE)
                if c.is_ready()
            ]
        )

    model = ""
    provider_status = "unknown"
    if _brain:
        model = _brain.model or ""
        if _brain.provider_health:
            best = _brain.provider_health.get_best_provider()
            provider_status = best.status.value if best else "unknown"

    return {
        "ok": _brain is not None,
        "version": __version__,
        "modules_ready": modules_ready,
        "model": model,
        "provider_status": provider_status,
    }


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Smart routing: setup → awakening → dashboard."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return RedirectResponse(url="/setup")

    if _is_serve_mode() and not _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/login")

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
    loaded = _load_config()
    if _is_configured(loaded):
        if _is_serve_mode() and not _request_has_owner_access(request, loaded):
            return RedirectResponse(url="/login")
        return RedirectResponse(url="/")

    if _is_serve_mode() and not _request_has_setup_access(request, loaded):
        return templates.TemplateResponse(request, "setup_gate.html")

    return templates.TemplateResponse(
        request,
        "setup.html",
        context={"hosted_mode": _is_serve_mode()},
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    loaded = _load_config()
    if not _is_serve_mode() or not _is_configured(loaded):
        return RedirectResponse(url="/")
    if _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/")
    if not loaded.get("owner_secret_hash"):
        return templates.TemplateResponse(request, "owner_setup.html")
    return templates.TemplateResponse(request, "login.html")


@app.post("/api/setup/token")
async def api_setup_token(request: Request):
    loaded = _load_config()
    if not _is_serve_mode() or _is_configured(loaded):
        return {"status": "ok"}

    body = await request.json()
    token = str(body.get("token") or "").strip()
    if not _verify_secret(token, loaded.get("setup_token_hash")):
        return JSONResponse(
            status_code=401, content={"status": "error", "error": "invalid_setup_token"}
        )

    response = JSONResponse(content={"status": "ok"})
    response.set_cookie(
        SETUP_COOKIE_NAME,
        _issue_cookie(
            "setup",
            _server_secret(loaded) or secrets.token_urlsafe(32),
            ttl_seconds=3600,
        ),
        httponly=True,
        samesite="lax",
        max_age=3600,
    )
    return response


@app.post("/api/login")
async def api_login(request: Request):
    loaded = _load_config()
    if not _is_serve_mode() or not _is_configured(loaded):
        return JSONResponse(
            status_code=400, content={"status": "error", "error": "login_not_available"}
        )

    body = await request.json()
    secret = str(body.get("secret") or "").strip()
    if not _verify_secret(secret, loaded.get("owner_secret_hash")):
        return JSONResponse(
            status_code=401, content={"status": "error", "error": "invalid_credentials"}
        )

    response = JSONResponse(content={"status": "ok"})
    response.set_cookie(
        AUTH_COOKIE_NAME,
        _issue_cookie("owner", _server_secret(loaded) or secrets.token_urlsafe(32)),
        httponly=True,
        samesite="lax",
        max_age=COOKIE_MAX_AGE_SECONDS,
    )
    return response


@app.post("/api/logout")
async def api_logout():
    response = JSONResponse(content={"status": "ok"})
    response.delete_cookie(AUTH_COOKIE_NAME)
    return response


@app.post("/api/setup/owner")
async def api_setup_owner(request: Request):
    """Create owner password for run→serve transition. Requires setup token."""
    loaded = _load_config()
    if not _is_serve_mode() or not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"status": "error", "error": "not_available"})
    if loaded.get("owner_secret_hash"):
        return JSONResponse(status_code=400, content={"status": "error", "error": "owner_already_set"})

    body = await request.json()
    token = str(body.get("token") or "").strip()
    owner_secret = str(body.get("owner_secret") or "").strip()

    if not token or not owner_secret:
        return JSONResponse(status_code=400, content={"status": "error", "error": "token_and_password_required"})

    if not _verify_secret(token, loaded.get("setup_token_hash")):
        return JSONResponse(status_code=401, content={"status": "error", "error": "invalid_setup_token"})

    if len(owner_secret) < 4:
        return JSONResponse(status_code=400, content={"status": "error", "error": "password_too_short"})

    _merge_save_config(
        {"owner_secret_hash": _hash_secret(owner_secret)},
        removals={"setup_token_hash"},
    )

    response = JSONResponse(content={"status": "ok"})
    response.set_cookie(
        AUTH_COOKIE_NAME,
        _issue_cookie("owner", _server_secret(_load_config()) or secrets.token_urlsafe(32)),
        httponly=True,
        samesite="lax",
        max_age=COOKIE_MAX_AGE_SECONDS,
    )
    response.delete_cookie(SETUP_COOKIE_NAME)
    return response


@app.get("/api/setup/personalities")
async def api_setup_personalities(request: Request, entry_path: str | None = None):
    """List setup-safe personality modules for the selected entry path."""
    guard = _require_setup_access(request)
    if guard is not None:
        return guard
    return {"modules": _list_setup_personality_modules(entry_path)}


@app.post("/api/setup")
async def api_setup(request: Request):
    """Save configuration from the web setup wizard."""
    try:
        loaded = _load_config()
        guard = _require_setup_access(request, loaded)
        if guard is not None:
            return guard

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
        if _is_serve_mode():
            owner_secret = str(body.get("owner_secret") or "").strip()
            if not owner_secret:
                return JSONResponse(
                    status_code=400,
                    content={
                        "status": "error",
                        "error": "Owner password or PIN is required.",
                    },
                )
            config["owner_secret_hash"] = _hash_secret(owner_secret)
            config["server_mode"] = True
            config["server_secret"] = loaded.get(
                "server_secret"
            ) or secrets.token_urlsafe(32)

        _merge_save_config(config, removals={"setup_token_hash"})

        # Initialize brain with new config
        await _init_brain_from_config()

        if _brain and _brain.memory._db is None:
            await _brain.memory.init()

        response = JSONResponse(content={"status": "ok"})
        if _is_serve_mode():
            response.set_cookie(
                AUTH_COOKIE_NAME,
                _issue_cookie(
                    "owner", _server_secret(_load_config()) or secrets.token_urlsafe(32)
                ),
                httponly=True,
                samesite="lax",
                max_age=COOKIE_MAX_AGE_SECONDS,
            )
            response.delete_cookie(SETUP_COOKIE_NAME)
        return response

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
    redirect_to: str | None = None,
):
    """Start a local-only OpenRouter PKCE flow."""
    loaded = _load_config()
    guard = (
        _require_owner_access(request, loaded)
        if _is_configured(loaded)
        else _require_setup_access(request, loaded)
    )
    if guard is not None:
        return guard

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
            "redirect_to": "/dashboard" if str(redirect_to or "").strip() == "/dashboard" else "/setup",
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
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
):
    """Complete a local-only OpenRouter PKCE flow and save config."""
    loaded = _load_config()
    redirect_target = "/setup"

    if error:
        normalized = _normalize_openrouter_oauth_error(error=error)
        return RedirectResponse(url=_openrouter_error_redirect(redirect_target, normalized))
    if not code or not state:
        normalized = _normalize_openrouter_oauth_error(error="missing_code_or_state")
        return RedirectResponse(url=_openrouter_error_redirect(redirect_target, normalized))

    oauth_state = _pop_oauth_state(state)
    if not oauth_state:
        normalized = _normalize_openrouter_oauth_error(error="invalid_or_expired_state")
        return RedirectResponse(url=_openrouter_error_redirect(redirect_target, normalized))

    redirect_target = str(oauth_state.get("redirect_to") or "/setup")
    guard = (
        _require_owner_access(request, loaded)
        if _is_configured(loaded)
        else _require_setup_access(request, loaded)
    )
    if guard is not None:
        return guard

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
                "provider": "openrouter",
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
        return RedirectResponse(
            url=_openrouter_error_redirect(redirect_target, normalized)
        )

    return RedirectResponse(url=_openrouter_redirect_target(redirect_target))


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """The main dashboard — Lumen's UI-FIRST experience."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return RedirectResponse(url="/")
    if _is_serve_mode() and not _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/login")

    await _init_brain_from_config()

    if _brain and _brain.memory._db is None:
        await _brain.memory.init()

    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        context={
            "ui": ui,
            "provider": _infer_provider_name(_config),
            "model": _config.get("model", "not configured"),
            "api_key_env": _config.get("api_key_env", ""),
            "has_api_key": bool(_config.get("api_key")),
            "openrouter_connected": _config.get("api_key_env") == "OPENROUTER_API_KEY"
            and bool(_config.get("api_key")),
            "openrouter_model": _config.get("model")
            if _is_openrouter_model(_config.get("model"))
            else DEFAULT_OPENROUTER_MODEL,
            "language": _config.get("language", "en"),
            "current_personality": _current_dashboard_personality(),
            "personality_ui": _current_personality_ui(),
            "version": __version__,
            "connectors_count": len(_brain.connectors.list()) if _brain else 0,
            "flows_count": len(_brain.flows) if _brain else 0,
            "mcp_count": len(_brain.registry.list_by_kind(CapabilityKind.MCP))
            if _brain
            else 0,
        },
    )


@app.get("/settings")
async def page_settings_index(request: Request):
    """Settings landing page — redirect to general."""
    return RedirectResponse("/settings/general")


@app.get("/settings/general")
async def page_settings_general(request: Request):
    """General settings page (provider, model, API key, theme)."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return RedirectResponse("/setup")
    if _is_serve_mode() and not _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/login")
    await _init_brain_from_config()

    # Gather data from brain/config (same as dashboard panel-config)
    personality_data = {}
    if _brain and getattr(_brain, "personality", None):
        personality_data = _brain.personality.current() or {}

    openrouter_connected = bool(_config.get("openrouter_token"))
    openrouter_model = loaded.get("openrouter_model", DEFAULT_OPENROUTER_MODEL)

    return templates.TemplateResponse(
        "settings_general.html",
        {
            "request": request,
            "config": loaded,
            "language": _config.get("language", "en"),
            "provider": loaded.get("provider", ""),
            "model": loaded.get("model", ""),
            "api_key_env": loaded.get("api_key_env", ""),
            "has_api_key": bool(loaded.get("api_key")),
            "current_personality": personality_data.get("name", "default"),
            "openrouter_connected": openrouter_connected,
            "openrouter_model": openrouter_model,
        },
    )


@app.get("/settings/models")
async def page_models(request: Request):
    """Model routing & provider health settings page."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return RedirectResponse("/setup")
    if _is_serve_mode() and not _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/login")
    await _init_brain_from_config()
    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        "models.html",
        {
            "request": request,
            "config": loaded,
            "ui": ui,
            "language": _config.get("language", "en"),
        },
    )


@app.get("/settings/providers")
async def page_providers(request: Request):
    """Provider page — merged into models. Redirect there."""
    return RedirectResponse("/settings/models")


@app.get("/settings/tools")
async def page_tools(request: Request):
    """Tool policy settings page."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return RedirectResponse("/setup")
    if _is_serve_mode() and not _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/login")
    await _init_brain_from_config()
    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        "tools.html",
        {
            "request": request,
            "config": loaded,
            "ui": ui,
            "language": _config.get("language", "en"),
        },
    )


@app.get("/settings/security")
async def page_security(request: Request):
    """Security settings page."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return RedirectResponse("/setup")
    if _is_serve_mode() and not _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/login")
    await _init_brain_from_config()
    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        "security.html",
        {
            "request": request,
            "config": loaded,
            "ui": ui,
            "language": _config.get("language", "en"),
        },
    )


@app.get("/settings/channels")
async def page_channels(request: Request):
    """Channel gateway status page."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return RedirectResponse("/setup")
    if _is_serve_mode() and not _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/login")
    await _init_brain_from_config()
    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        "channels.html",
        {
            "request": request,
            "config": loaded,
            "ui": ui,
            "language": _config.get("language", "en"),
        },
    )


@app.get("/settings/outputs")
async def page_outputs(request: Request):
    """Structured output types page."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return RedirectResponse("/setup")
    if _is_serve_mode() and not _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/login")
    await _init_brain_from_config()
    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        "outputs.html",
        {
            "request": request,
            "config": loaded,
            "ui": ui,
            "language": _config.get("language", "en"),
        },
    )


@app.get("/settings/confirmations")
async def page_confirmations(request: Request):
    """Tool confirmations page."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return RedirectResponse("/setup")
    if _is_serve_mode() and not _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/login")
    await _init_brain_from_config()
    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        "confirmations.html",
        {
            "request": request,
            "config": loaded,
            "ui": ui,
            "language": _config.get("language", "en"),
            "locale": _locale.get("confirmations", {}),
        },
    )


@app.get("/agent-status")
async def page_agent_status(request: Request):
    """Agent diagnostic status page."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return RedirectResponse("/setup")
    if _is_serve_mode() and not _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/login")
    await _init_brain_from_config()
    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        "agent-status.html",
        {
            "request": request,
            "config": loaded,
            "ui": ui,
            "language": _config.get("language", "en"),
        },
    )


@app.get("/memory")
async def page_memory(request: Request):
    """Memory management page."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return RedirectResponse("/setup")
    if _is_serve_mode() and not _request_has_owner_access(request, loaded):
        return RedirectResponse(url="/login")
    await _init_brain_from_config()
    ui = _locale.get("dashboard", {})
    return templates.TemplateResponse(
        "memory.html",
        {
            "request": request,
            "config": loaded,
            "ui": ui,
            "language": _config.get("language", "en"),
        },
    )


@app.post("/api/settings")
async def api_settings(request: Request):
    """Update dashboard settings without re-running onboarding."""
    global _config

    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})

    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    body = await request.json()
    model = _normalize_optional_text(body.get("model"))
    if not model:
        return JSONResponse(
            status_code=400,
            content={"status": "error", "error": "Model is required."},
        )

    updates = {"model": model}
    removals: set[str] = set()

    provider = _normalize_optional_text(body.get("provider"))
    if provider:
        updates["provider"] = provider
    elif "provider" in body:
        removals.add("provider")

    api_key_env = _normalize_optional_text(body.get("api_key_env"))
    if api_key_env:
        updates["api_key_env"] = api_key_env
    elif "api_key_env" in body:
        removals.add("api_key_env")

    api_key = _normalize_optional_text(body.get("api_key"))
    if api_key:
        updates["api_key"] = api_key

    api_base = _normalize_optional_text(body.get("api_base"))
    if api_base:
        updates["api_base"] = api_base
    elif "api_base" in body:
        removals.add("api_base")

    _config = _merge_save_config(updates, removals=removals)
    await _refresh_runtime_from_config(loaded)

    return {
        "status": "ok",
        "config": {
            "provider": _infer_provider_name(_config),
            "model": _config.get("model", ""),
            "api_key_env": _config.get("api_key_env", ""),
            "api_base": _config.get("api_base", ""),
            "has_api_key": bool(_config.get("api_key")),
        },
    }


@app.post("/api/awakened")
async def mark_awakened_endpoint(request: Request):
    """Called by the awakening animation when it completes."""
    guard = _require_owner_access(request)
    if guard is not None:
        return guard
    _mark_awakened()
    return {"status": "ok"}


@app.get("/api/history/{session_id}")
async def api_history(request: Request, session_id: str):
    """Load conversation history for a session from persistent memory."""
    guard = _require_owner_access(request)
    if guard is not None:
        return guard
    if not _brain:
        return {"messages": []}
    try:
        messages = await _brain.memory.load_conversation(session_id, limit=50)
        return {"messages": messages}
    except Exception:
        return {"messages": []}


# ─── REST Chat API ───


def _validate_bearer_token(request: Request) -> str | None:
    """Validate Authorization: Bearer <key> header.

    Checks against:
    1. LUMEN_API_KEY env var
    2. config.api.rest_key (from _config AND directly from CONFIG_PATH)
    3. api_keys.yaml (hashed keys from lumen api-key generate)
    Returns None if valid, or an error string if invalid/missing.
    """
    auth_header = request.headers.get("authorization", "")
    if not auth_header.startswith("Bearer "):
        return "unauthorized"

    token = auth_header[7:].strip()
    if not token:
        return "unauthorized"

    # Check env var first
    valid_key = os.environ.get("LUMEN_API_KEY")
    if valid_key and token == valid_key:
        return None  # Valid

    # Check _config (in-memory, may lack api section)
    api_config = _config.get("api", {})
    if isinstance(api_config, dict):
        config_key = api_config.get("rest_key")
        if config_key and token == config_key:
            return None  # Valid

    # Check CONFIG_PATH directly (raw YAML, always has api section)
    if CONFIG_PATH.exists():
        try:
            raw = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                raw_api = raw.get("api", {})
                if isinstance(raw_api, dict):
                    raw_key = raw_api.get("rest_key")
                    if raw_key and token == raw_key:
                        return None  # Valid
        except Exception:
            pass

    # Check api_keys.yaml (hashed keys)
    try:
        from lumen.core.api_keys import verify_api_key
        keys_path = LUMEN_DIR / "api_keys.yaml"
        if verify_api_key(token, keys_path=keys_path):
            return None  # Valid
    except Exception:
        pass  # If api_keys module fails, continue with other checks

    return "unauthorized"


@app.post("/api/chat")
async def api_chat(request: Request):
    """HTTP REST chat endpoint for external applications.

    Body: {"message": "...", "session_id?": "...", "stream?": false}
    Response: {"response": "...", "session_id": "..."} or SSE stream
    Auth: Bearer token via LUMEN_API_KEY env or config.api.rest_key
    """
    auth_error = _validate_bearer_token(request)
    if auth_error:
        return JSONResponse(status_code=401, content={"error": auth_error})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "invalid JSON"})

    if not isinstance(body, dict):
        return JSONResponse(status_code=400, content={"error": "expected JSON object"})

    message = str(body.get("message", "")).strip()
    if not message:
        return JSONResponse(
            status_code=400, content={"error": "message is required"}
        )

    if not _brain:
        return JSONResponse(
            status_code=503, content={"error": "Lumen not ready"}
        )

    session_id = body.get("session_id")
    session = session_manager.get_or_create(session_id)

    # Parse stream flag — only literal True enables streaming
    stream = body.get("stream") is True

    if not stream:
        try:
            result = await _brain.think(message, session)
        except Exception as e:
            return JSONResponse(
                status_code=500, content={"error": str(e)}
            )

        return {
            "response": result.get("message", ""),
            "session_id": session.session_id,
        }

    # Streaming mode
    async def sse_generator():
        confirm_queue: asyncio.Queue = asyncio.Queue(maxsize=10)
        _sse_confirm_queues.append(confirm_queue)
        try:
            yield f"event: session\ndata: {json.dumps({'session_id': session.session_id})}\n\n"

            # Create tasks for brain stream and confirm queue
            brain_stream = _brain.think_stream(message, session)

            async def brain_chunks():
                async for chunk in brain_stream:
                    yield chunk

            brain_iter = brain_chunks()

            pending_brain = asyncio.ensure_future(anext(brain_iter, None))
            pending_confirm = asyncio.ensure_future(confirm_queue.get())

            while True:
                done, pending = await asyncio.wait(
                    [pending_brain, pending_confirm],
                    return_when=asyncio.FIRST_COMPLETED,
                )

                if pending_brain in done:
                    chunk = pending_brain.result()
                    if chunk is None:
                        # Brain stream finished — drain remaining confirms
                        break

                    if chunk.get("type") == "delta":
                        text = chunk.get("content", "")
                        yield f"event: delta\ndata: {json.dumps({'text': text})}\n\n"
                    elif chunk.get("type") == "error":
                        error_msg = chunk.get("content", "unknown error")
                        yield f"event: error\ndata: {json.dumps({'error': error_msg})}\n\n"
                        break
                    elif chunk.get("type") == "tool_progress":
                        yield f"event: tool_progress\ndata: {json.dumps({'tool': chunk.get('tool'), 'iteration': chunk.get('iteration'), 'total_calls': chunk.get('total_calls')})}\n\n"
                    elif chunk.get("type") == "tool_result":
                        data = {"tool": chunk.get("tool")}
                        if chunk.get("error"):
                            data["error"] = chunk["error"]
                        else:
                            data["truncated_result"] = chunk.get("truncated_result", "")
                        yield f"event: tool_result\ndata: {json.dumps(data)}\n\n"
                    elif chunk.get("type") == "tool_status":
                        yield f"event: tool_status\ndata: {json.dumps({'iteration': chunk.get('iteration'), 'max_iterations': chunk.get('max_iterations'), 'tools_this_round': chunk.get('tools_this_round'), 'total_so_far': chunk.get('total_so_far')})}\n\n"
                    elif chunk.get("type") == "tool_confirm_result":
                        yield f"event: tool_confirm_result\ndata: {json.dumps({'tool': chunk.get('tool'), 'decision': chunk.get('decision'), 'reason': chunk.get('reason', '')})}\n\n"

                    # Schedule next brain chunk
                    pending_brain = asyncio.ensure_future(anext(brain_iter, None))

                if pending_confirm in done:
                    confirm_data = pending_confirm.result()
                    yield f"event: tool_confirm_request\ndata: {json.dumps(confirm_data)}\n\n"
                    # Schedule next confirm listen
                    pending_confirm = asyncio.ensure_future(confirm_queue.get())

                # If both done, exit
                if not pending:
                    break

            # Cancel any pending tasks
            for t in [pending_brain, pending_confirm]:
                if not t.done():
                    t.cancel()

        except Exception as e:
            yield f"event: error\ndata: {json.dumps({'error': str(e)})}\n\n"
        finally:
            _sse_confirm_queues.remove(confirm_queue)

        yield f"event: done\ndata: [DONE]\n\n"

    return StreamingResponse(sse_generator(), media_type="text/event-stream")


@app.websocket("/ws/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    """Real-time chat via WebSocket."""
    if (
        _is_serve_mode()
        and _is_configured(_load_config())
        and not _websocket_has_owner_access(websocket)
    ):
        await websocket.close(code=4401)
        return
    await websocket.accept()
    session = session_manager.get_or_create(session_id)
    _active_websockets.add(websocket)

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
                # Check for pending capability awareness — proactive announcement
                if _awareness and _awareness.has_pending_proactive() and _brain:
                    try:
                        announcement = await _brain.think_proactive()
                        if announcement:
                            await websocket.send_text(
                                json.dumps(
                                    {
                                        "type": "awareness",
                                        "content": announcement,
                                    }
                                )
                            )
                    except Exception:
                        pass
                await websocket.send_text(json.dumps({"type": "pong"}))
                continue

            # Handle tool confirmation response from WebSocket client
            if payload.get("type") == "tool_confirm_response":
                call_id = payload.get("call_id", "")
                decision_str = payload.get("decision", "").lower()
                msg = payload.get("message", "")
                from lumen.core.confirmation_gate import ConfirmDecision, ConfirmResponse
                try:
                    decision = ConfirmDecision(decision_str)
                    future = _pending_confirmations.get(call_id)
                    if future and not future.done():
                        future.set_result(
                            ConfirmResponse(call_id=call_id, decision=decision, message=msg)
                        )
                except ValueError:
                    pass
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
    finally:
        _active_websockets.discard(websocket)


async def broadcast_event(event_type: str, payload: dict) -> int:
    """Broadcast a real-time event to all connected WebSocket clients.

    Returns the number of clients that successfully received the event.
    Stale or disconnected sockets are caught, removed from the active set,
    and do not count toward the returned total.
    """
    if not _active_websockets:
        return 0

    message = json.dumps({"type": event_type, "payload": payload})
    stale = []
    sent = 0
    for ws in _active_websockets:
        try:
            await ws.send_text(message)
            sent += 1
        except Exception:
            stale.append(ws)
    for ws in stale:
        _active_websockets.discard(ws)
    return sent


async def broadcast_awareness():
    """Send proactive capability announcements to all connected clients.

    Called after module install/uninstall or MCP status changes.
    Only sends if awareness has pending changes and brain is ready.
    """
    if not _awareness or not _brain or not _active_websockets:
        return

    if not _awareness.has_pending():
        return

    try:
        announcement = await _brain.think_proactive()
        if not announcement:
            return

        message = json.dumps(
            {
                "type": "awareness",
                "content": announcement,
            }
        )
        # Send to all active connections, remove failed ones
        stale = []
        for ws in _active_websockets:
            try:
                await ws.send_text(message)
            except Exception:
                stale.append(ws)
        for ws in stale:
            _active_websockets.discard(ws)
    except Exception:
        pass


# ─── Debug API ───


@app.get("/api/debug/prompt")
async def api_debug_prompt(request: Request):
    """Show the exact system prompt the LLM receives. For debugging."""
    guard = _require_owner_access(request)
    if guard is not None:
        return guard
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
async def api_modules_catalog(request: Request):
    """List all available modules from the catalog."""
    guard = _require_owner_access(request)
    if guard is not None:
        return guard
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
async def api_modules_installed(request: Request):
    """List installed modules."""
    guard = _require_owner_access(request)
    if guard is not None:
        return guard
    if not _brain:
        return {"modules": []}
    marketplace = getattr(_brain, "marketplace", None)
    if marketplace is not None:
        return {"modules": marketplace.kits_installed()}
    from lumen.core.installer import Installer

    installer = Installer(
        PKG_DIR,
        _brain.connectors,
        _brain.memory,
        _brain.catalog,
        config=_config,
    )
    return {"modules": installer.list_installed()}


@app.get("/api/marketplace")
async def api_marketplace(request: Request):
    """Aggregated marketplace read model: kits, modules, skills, and feeds."""
    guard = _require_owner_access(request)
    if guard is not None:
        return guard
    if not _brain:
        return {
            "generated_at": None,
            "feeds": [],
            "tabs": [],
            "skills": {"items": [], "installed": [], "available": [], "counts": {}},
            "modules": {
                "items": [],
                "installed": [],
                "available": [],
                "counts": {},
                "upload_enabled": True,
            },
            "kits": {
                "items": [],
                "installed": [],
                "available": [],
                "counts": {},
                "upload_enabled": False,
            },
        }

    marketplace = getattr(_brain, "marketplace", None)
    if marketplace is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Marketplace service not initialized"},
        )
    return marketplace.snapshot()


@app.post("/api/reload")
async def api_reload(request: Request):
    """Trigger a runtime reload — re-discovers modules, refreshes registry.

    Auth: Bearer token required (LUMEN_API_KEY env or config.api.rest_key).
    """
    global _config

    auth_error = _validate_bearer_token(request)
    if auth_error:
        return JSONResponse(status_code=401, content={"error": auth_error})

    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Lumen not ready"})

    try:
        await _perform_runtime_reload()

        # Count capabilities in the refreshed registry
        module_count = 0
        if _brain.registry:
            all_caps = _brain.registry.list_by_kind(CapabilityKind.MODULE) if hasattr(CapabilityKind, "MODULE") else []
            module_count = len(all_caps) if all_caps else 0

        return JSONResponse(
            status_code=200,
            content={
                "status": "reloaded",
                "modules": module_count,
            },
        )
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"reload failed: {e}"},
        )


@app.post("/api/modules/install/{name}")
async def api_modules_install(request: Request, name: str):
    """Install a module from the catalog. Lumen knows."""
    global _config

    guard = _require_owner_access(request)
    if guard is not None:
        return guard
    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Lumen not ready"})
    from lumen.core.installer import Installer

    installer = Installer(
        PKG_DIR,
        _brain.connectors,
        _brain.memory,
        _brain.catalog,
        lumen_dir=LUMEN_DIR,
        config=_config,
    )
    result = installer.install_from_catalog(name)
    if result.get("status") == "not_found":
        remote_item = _find_marketplace_item(name)
        if remote_item and remote_item.get("actions", {}).get("can_install"):
            result = installer.install_marketplace_item(remote_item)

    if result["status"] == "installed":
        installed_name = result.get("name", name)
        await sync_runtime_modules(
            _brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR
        )
        manifest = _installed_personality_manifest(installed_name)
        if manifest:
            tags = _normalize_module_tags(manifest.get("tags"))
            if "personality" in tags:
                _config = _merge_save_config({"active_personality": installed_name})

        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR, active_channels=["web"])
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR)

        # Proactively announce the new capability to connected clients
        await broadcast_awareness()

    return result


@app.delete("/api/modules/uninstall/{name}")
async def api_modules_uninstall(request: Request, name: str):
    """Uninstall a module. Lumen forgets."""
    global _config

    guard = _require_owner_access(request)
    if guard is not None:
        return guard

    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Lumen not ready"})
    from lumen.core.installer import Installer

    installer = Installer(
        PKG_DIR,
        _brain.connectors,
        _brain.memory,
        _brain.catalog,
        lumen_dir=LUMEN_DIR,
        config=_config,
    )
    was_active_personality = _config.get("active_personality") == name
    if isinstance(getattr(_brain, "module_manager", None), ModuleRuntimeManager):
        await _brain.module_manager.unload(name)
    result = installer.uninstall(name)

    if result["status"] == "uninstalled":
        if was_active_personality:
            _config = _merge_save_config({}, removals={"active_personality"})
        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR, active_channels=["web"])
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR)

        await broadcast_awareness()

    return result


@app.post("/api/modules/upload")
async def api_modules_upload(request: Request):
    """Upload and install a module from a ZIP file. WordPress-style."""
    global _config

    if not _brain:
        setup_guard = _require_setup_access(request)
        if setup_guard is not None:
            return setup_guard
    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Lumen not ready"})
    from lumen.core.installer import Installer

    body = await request.body()
    installer = Installer(
        PKG_DIR,
        _brain.connectors,
        _brain.memory,
        _brain.catalog,
        lumen_dir=LUMEN_DIR,
        config=_config,
    )
    result = installer.install_from_zip(body)

    if result["status"] == "installed":
        await sync_runtime_modules(
            _brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR
        )
        # result typically includes the 'name' of the installed module
        module_name = result.get("name")
        if module_name:
            manifest = _installed_personality_manifest(module_name)
            if manifest:
                tags = _normalize_module_tags(manifest.get("tags"))
                if "personality" in tags:
                    _config = _merge_save_config({"active_personality": module_name})

        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR, active_channels=["web"])
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR)

        await broadcast_awareness()

    return result


@app.post("/api/modules/setup/{name}")
async def api_modules_complete_setup(request: Request, name: str):
    """Persist collected module setup values without exposing them back."""
    global _config

    guard = _require_owner_access(request)
    if guard is not None:
        return guard
    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Lumen not ready"})

    body = await request.json()
    values = body.get("values") if isinstance(body, dict) and "values" in body else body
    if not isinstance(values, dict):
        return JSONResponse(status_code=400, content={"error": "Expected a JSON object of setup values"})

    module_dir = _installed_module_dir(name)
    manifest_path, _ = load_module_manifest(module_dir)
    if manifest_path is None:
        return JSONResponse(status_code=404, content={"error": "Module not installed"})
    result = await _persist_module_setup_slots(name, values)
    if result.get("status") == "error":
        return JSONResponse(status_code=400, content=result)
    return result


# ─── Status API ───


@app.get("/api/status")
async def api_status(request: Request):
    """Lumen's current status — from the Body (registry)."""
    guard = _require_owner_access(request)
    if guard is not None:
        return guard
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
        "version": __version__,
        "model": _config.get("model", "not configured"),
        "language": _config.get("language", "en"),
        "personality_ui": _current_personality_ui(),
        "capabilities": capabilities,
        "summary": registry.summary() if registry else {},
        "awareness": (
            getattr(getattr(_brain, "capability_awareness", None), "peek_summary", lambda: {
                "pending": 0,
                "counts": {},
                "effects": {},
                "events": [],
            })()
            if _brain
            else {"pending": 0, "counts": {}, "effects": {}, "events": []}
        ),
        "flows": flows_info,
        "module_setup": {
            "pending": sum(
                1
                for cap in capabilities
                if cap.get("kind") == "module"
                and (cap.get("metadata") or {}).get("pending_setup")
            ),
        },
        "artifact_setup": {
            "pending": sum(
                1
                for cap in capabilities
                if (cap.get("metadata") or {}).get("pending_setup")
            ),
            "by_kind": {
                kind: sum(
                    1
                    for cap in capabilities
                    if cap.get("kind") == kind
                    and (cap.get("metadata") or {}).get("pending_setup")
                )
                for kind in ("module", "mcp")
            },
        },
        "ready": len(registry.ready()) if registry else 0,
        "gaps": len(registry.gaps()) if registry else 0,
    }


# ─── Model Routing API ───


@app.get("/api/models")
async def api_models_list(request: Request):
    """Return current model routing configuration."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})

    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    router_cfg = ModelRouterConfig.from_config(loaded)
    router = ModelRouter(router_cfg)

    return {
        "default": router_cfg.default,
        "fallback": router_cfg.fallback,
        "use_default_for_all": router_cfg.use_default_for_all,
        "roles": {k: v for k, v in router_cfg.roles.items()},
        "all_roles": list(VALID_ROLES),
        "resolved": {role: router.get_model(role) for role in VALID_ROLES if role != "main"},
    }


@app.post("/api/models")
async def api_models_update(request: Request):
    """Update model routing configuration."""
    global _config
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})

    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    body = await request.json()

    # Ensure models section exists
    if "models" not in _config or not isinstance(_config.get("models"), dict):
        _config["models"] = {}

    updates: dict = {}

    # Update default
    default = body.get("default")
    if default and isinstance(default, str):
        _config["models"]["default"] = default
        _config["model"] = default  # Legacy key
        updates["default"] = default

    # Update fallback
    fallback = body.get("fallback")
    if fallback and isinstance(fallback, str):
        _config["models"]["fallback"] = fallback
        updates["fallback"] = fallback

    # Update toggle
    if "use_default_for_all" in body:
        _config["models"]["use_default_for_all"] = bool(body["use_default_for_all"])
        updates["use_default_for_all"] = bool(body["use_default_for_all"])

    # Update role-specific
    roles = body.get("roles")
    if isinstance(roles, dict):
        if "roles" not in _config["models"] or not isinstance(_config["models"].get("roles"), dict):
            _config["models"]["roles"] = {}
        for role, model in roles.items():
            if role in VALID_ROLES and isinstance(model, str):
                _config["models"]["roles"][role] = model
        updates["roles"] = _config["models"]["roles"]

    _merge_save_config(_config)
    await _refresh_runtime_from_config(loaded)

    return {"status": "ok", "updates": updates}


# ─── Provider Health API ───


@app.get("/api/providers")
async def api_providers_status(request: Request):
    """Return health status of all configured providers."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})

    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    tracker = ProviderHealthTracker.from_config(loaded)

    # If brain has a live tracker, use that instead (has real health data)
    if _brain and hasattr(_brain, "provider_health") and _brain.provider_health:
        summary = _brain.provider_health.get_summary()
    else:
        summary = tracker.get_summary()

    return summary


@app.post("/api/providers/retry")
async def api_providers_retry(request: Request):
    """Manually retry a degraded/down provider."""
    body = await request.json()
    name = body.get("name", "")

    if not name:
        return JSONResponse(status_code=400, content={"error": "name is required"})

    if _brain and hasattr(_brain, "provider_health") and _brain.provider_health:
        if _brain.provider_health.retry_provider(name):
            return {"status": "ok", "message": f"Provider '{name}' reset"}
        return JSONResponse(status_code=404, content={"error": f"Provider '{name}' not found"})

    return JSONResponse(status_code=400, content={"error": "No live provider tracker"})


# ─── Tool Policy & Security API ───


@app.get("/api/tools")
async def api_tools_list(request: Request):
    """List all tools with risk classification."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})
    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    policy = ToolPolicy()
    policy.load_defaults()
    policy.load_config(loaded)

    return policy.get_all_policies()


@app.get("/api/security")
async def api_security_show(request: Request):
    """Show current security settings."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})
    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    sec = SecurityConfig.from_config(loaded)
    return sec.to_dict()


@app.post("/api/security")
async def api_security_update(request: Request):
    """Update security settings."""
    global _config
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})
    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    body = await request.json()
    valid_keys = {"confirm_deletions", "confirm_terminal", "confirm_system_actions", "auto_approve_read_only", "confirmation_timeout"}

    if "security" not in _config or not isinstance(_config.get("security"), dict):
        _config["security"] = {}

    updates = {}
    for key, value in body.items():
        if key in valid_keys:
            if key == "confirmation_timeout":
                _config["security"][key] = int(value)
            else:
                _config["security"][key] = bool(value)
            updates[key] = _config["security"][key]

    _merge_save_config(_config)
    await _refresh_runtime_from_config(loaded)

    return {"status": "ok", "updates": updates}


# ─── Tool Confirmation API ───


async def _web_confirm_handler(request_obj):
    """Confirmation handler for web channel.

    Pushes the confirm request to all active SSE streams and WebSocket clients,
    then waits for the user to resolve via POST /api/tools/confirm.
    """
    # Push to SSE streams
    confirm_data = request_obj.to_dict()
    for q in list(_sse_confirm_queues):
        try:
            q.put_nowait(confirm_data)
        except asyncio.QueueFull:
            pass

    # Broadcast to WebSocket clients
    await broadcast_event("tool_confirm_request", confirm_data)

    # Wait for the user to resolve via POST /api/tools/confirm
    loop = asyncio.get_running_loop()
    future = loop.create_future()
    _pending_confirmations[request_obj.call_id] = future

    try:
        return await future
    finally:
        _pending_confirmations.pop(request_obj.call_id, None)


@app.post("/api/tools/{call_id}/confirm")
async def api_tool_confirm(call_id: str, request: Request):
    """Resolve a pending tool confirmation (approve or reject)."""
    from lumen.core.confirmation_gate import ConfirmDecision

    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})
    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    body = await request.json()
    decision_str = body.get("decision", "").lower()
    message = body.get("message", "")

    try:
        decision = ConfirmDecision(decision_str)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": f"Invalid decision: {decision_str}"})

    future = _pending_confirmations.get(call_id)
    if not future or future.done():
        return JSONResponse(status_code=404, content={"error": "No pending confirmation for this call_id"})

    from lumen.core.confirmation_gate import ConfirmResponse
    future.set_result(ConfirmResponse(call_id=call_id, decision=decision, message=message))

    return {"status": "ok", "call_id": call_id, "decision": decision.value}


@app.get("/api/tools/confirmations")
async def api_confirmations_list(request: Request):
    """List recent confirmation history and any pending confirmations."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})
    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    if not _brain:
        return {"pending": [], "history": []}

    gate = _brain.confirmation_gate
    return {
        "pending": gate.get_pending(),
        "pending_count": gate.get_pending_count(),
        "history": gate.get_history(limit=50),
    }


# ─── Channel Gateway & Output API ───


@app.get("/api/channels")
async def api_channels_status(request: Request):
    """Return status of all registered channels."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})
    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    # Collect channel info from multiple sources
    channels = []

    # Web channel (always present)
    channels.append({
        "name": "web",
        "status": "connected",
        "type": "internal",
        "message_count": len(_active_websockets),
    })

    # Module-based channels from config
    modules = loaded.get("modules", {})
    if isinstance(modules, dict):
        for mod_name, mod_cfg in modules.items():
            if isinstance(mod_cfg, dict) and mod_cfg.get("type") == "channel":
                channels.append({
                    "name": mod_name,
                    "status": "available",
                    "type": "module",
                    "display_name": mod_cfg.get("display_name", mod_name),
                })

    # Live inbox status if available
    if _brain and hasattr(_brain, "inbox") and _brain.inbox:
        if hasattr(_brain.inbox, "get_channel_status"):
            inbox_status = _brain.inbox.get_channel_status()
            inbox_map = {s["name"]: s for s in inbox_status}
            for ch in channels:
                if ch["name"] in inbox_map:
                    live = inbox_map[ch["name"]]
                    ch["status"] = live["status"]
                    ch["message_count"] = live["message_count"]
                    ch["last_activity"] = live["last_activity"]
                    ch["error"] = live["error"]

    return {"channels": channels, "count": len(channels)}


@app.get("/api/outputs")
async def api_outputs_list(request: Request, limit: int = 50, session_id: str | None = None, output_type: str | None = None):
    """List structured outputs persisted from tool executions."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})
    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    from lumen.core.output_types import OutputType

    if not _brain or not _brain.memory:
        return {"outputs": [], "count": 0, "available_types": [t.value for t in OutputType]}

    try:
        outputs = await _brain.memory.get_outputs(
            session_id=session_id, output_type=output_type, limit=limit
        )
        total = await _brain.memory.count_outputs(session_id=session_id)
    except Exception:
        outputs, total = [], 0

    return {
        "outputs": outputs,
        "count": total,
        "available_types": [t.value for t in OutputType],
    }


# ─── Agent Status API ───


@app.get("/api/agent/status")
async def api_agent_status(request: Request):
    """Return consolidated agent status snapshot.

    Calculates everything directly from live brain state — no stale callbacks.
    Format matches exactly what agent-status.html renderStatus() expects.
    """
    warnings: list[str] = []

    # Basic info
    version = __version__
    uptime = time.monotonic() - _web_start_time
    model = ""
    provider = ""
    provider_status = "unknown"
    degraded_mode = False
    tools: list[str] = []
    active_modules = 0
    total_modules = 0
    memory_total = 0
    memory_sessions = 0

    if _brain:
        # Model
        try:
            model = (
                _brain.model_router.get_model("main")
                if _brain.model_router
                else (_brain.model or "")
            )
        except Exception as e:
            warnings.append(f"Model callback error: {e}")

        # Provider
        try:
            provider = _config.get("provider", "unknown") if _config else "unknown"
        except Exception as e:
            warnings.append(f"Provider callback error: {e}")

        # Provider health
        try:
            if _brain.provider_health:
                best = _brain.provider_health.get_best_provider()
                if best:
                    provider_status = best.status.value
                degraded_mode = _brain.provider_health.is_degraded_mode()
        except Exception as e:
            warnings.append(f"Provider health error: {e}")

        # Tools (as_tools returns list[dict], extract function names)
        try:
            if _brain.connectors:
                tools = [
                    t["function"]["name"]
                    for t in _brain.connectors.as_tools()
                    if isinstance(t, dict) and "function" in t and "name" in t["function"]
                ]
        except Exception as e:
            warnings.append(f"Tools callback error: {e}")

        # Modules from registry
        try:
            if _brain.registry:
                all_caps = _brain.registry.all()
                module_caps = [c for c in all_caps if c.kind and c.kind.value == "module"]
                total_modules = len(module_caps)
                active_modules = sum(
                    1 for m in module_caps if getattr(m, "status", None) != "error"
                )
        except Exception as e:
            warnings.append(f"Modules callback error: {e}")

        # Memory stats
        try:
            if _brain.memory:
                stats = await _brain.memory.get_stats()
                memory_total = stats.get("total_memories", 0)
                memory_sessions = stats.get("total_sessions", 0)
        except Exception as e:
            warnings.append(f"Memory callback error: {e}")

    # Auto-detect warnings
    if degraded_mode:
        warnings.append("All providers are down — running in degraded mode")
    if provider_status == "down":
        warnings.append(f"Current provider '{provider}' is down")
    elif provider_status == "degraded":
        warnings.append(f"Current provider '{provider}' is degraded")

    return {
        "version": version,
        "uptime_seconds": round(uptime, 1),
        "model": model,
        "provider": provider,
        "provider_status": provider_status,
        "degraded_mode": degraded_mode,
        "active_modules": active_modules,
        "total_modules": total_modules,
        "tools": tools,
        "memory": {
            "total_memories": memory_total,
            "sessions": memory_sessions,
        },
        "warnings": warnings,
    }


# ─── Memory & Lessons API ───


@app.get("/api/memory/facts")
async def api_memory_facts(
    request: Request,
    query: str = "",
    limit: int = 20,
):
    """List distilled session facts."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})
    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    if _brain and _brain.memory:
        facts = await _brain.memory.list_session_facts(query=query, limit=limit)
        return {"facts": facts, "count": len(facts)}
    return JSONResponse(status_code=503, content={"error": "Memory not available"})


@app.get("/api/memory/sessions")
async def api_memory_sessions(request: Request, limit: int = 20):
    """List session summaries."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})
    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    if _brain and _brain.memory:
        summaries = await _brain.memory.list_session_summaries(limit=limit)
        return {"sessions": summaries, "count": len(summaries)}
    return JSONResponse(status_code=503, content={"error": "Memory not available"})


@app.get("/api/lessons")
async def api_lessons_list(request: Request, limit: int = 50):
    """List persistent lessons."""
    loaded = _load_config()
    if not _is_configured(loaded):
        return JSONResponse(status_code=400, content={"error": "not_configured"})
    guard = _require_owner_access(request, loaded)
    if guard is not None:
        return guard

    if _brain and _brain.memory:
        lessons = await _brain.memory.list_lessons(limit=limit)
        return {"lessons": lessons, "count": len(lessons)}
    return JSONResponse(status_code=503, content={"error": "Memory not available"})


@app.post("/api/lessons")
async def api_lesson_create(request: Request):
    """Create a new lesson."""
    from lumen.core.lessons import VALID_CATEGORIES

    body = await request.json()
    rule = body.get("rule", "")
    category = body.get("category", "general")

    if not rule:
        return JSONResponse(status_code=400, content={"error": "rule is required"})
    if category not in VALID_CATEGORIES:
        category = "general"

    if _brain and _brain.memory:
        lid = await _brain.memory.save_lesson(
            rule=rule, category=category, source="user:web"
        )
        return {"status": "ok", "id": lid}
    return JSONResponse(status_code=503, content={"error": "Memory not available"})


@app.delete("/api/lessons/{lesson_id}")
async def api_lesson_delete(request: Request, lesson_id: int):
    """Delete a lesson."""
    if _brain and _brain.memory:
        existing = await _brain.memory.get_lesson(lesson_id)
        if not existing:
            return JSONResponse(status_code=404, content={"error": "Lesson not found"})
        await _brain.memory.delete_lesson(lesson_id)
        return {"status": "ok"}
    return JSONResponse(status_code=503, content={"error": "Memory not available"})


@app.post("/api/lessons/{lesson_id}/pin")
async def api_lesson_pin(request: Request, lesson_id: int):
    """Pin or unpin a lesson."""
    body = await request.json()
    pinned = bool(body.get("pinned", True))

    if _brain and _brain.memory:
        existing = await _brain.memory.get_lesson(lesson_id)
        if not existing:
            return JSONResponse(status_code=404, content={"error": "Lesson not found"})
        await _brain.memory.update_lesson(lesson_id, pinned=1 if pinned else 0)
        return {"status": "ok", "pinned": pinned}
    return JSONResponse(status_code=503, content={"error": "Memory not available"})

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_openrouter_models_cache: dict[str, object] = {"fetched_at": 0.0, "items": []}
_OPENROUTER_MODELS_TTL = 300.0


def _fetch_openrouter_models() -> list[dict]:
    """Fetch and cache OpenRouter's public model catalog (300s TTL)."""
    now = time()
    if (
        _openrouter_models_cache.get("items")
        and now - float(_openrouter_models_cache.get("fetched_at") or 0) < _OPENROUTER_MODELS_TTL
    ):
        return list(_openrouter_models_cache["items"])  # type: ignore[arg-type]

    try:
        req = UrlRequest(
            OPENROUTER_MODELS_URL,
            headers={"User-Agent": "enlumen/0.2.0", "Accept": "application/json"},
        )
        with urlopen(req, timeout=8) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, ValueError):
        return list(_openrouter_models_cache.get("items") or [])

    raw = payload.get("data") if isinstance(payload, dict) else None
    items: list[dict] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "").strip()
        if not model_id:
            continue
        pricing = entry.get("pricing") or {}
        prompt_cost = str(pricing.get("prompt") or "0")
        completion_cost = str(pricing.get("completion") or "0")
        is_free = model_id.endswith(":free") or (
            prompt_cost in {"0", "0.0", "0.00", "0.000", ""}
            and completion_cost in {"0", "0.0", "0.00", "0.000", ""}
        )
        items.append(
            {
                "id": model_id,
                "name": str(entry.get("name") or model_id),
                "description": str(entry.get("description") or "")[:220],
                "context_length": entry.get("context_length"),
                "is_free": bool(is_free),
            }
        )

    _openrouter_models_cache["items"] = items
    _openrouter_models_cache["fetched_at"] = now
    return items


@app.get("/api/openrouter/models")
async def api_openrouter_models(request: Request):
    """List OpenRouter models (curated + full catalog) for the settings picker."""
    guard = _require_owner_access(request)
    if guard is not None:
        return guard

    models = _fetch_openrouter_models()
    free_models = [m for m in models if m["is_free"]]
    # Curated first: keep canonical ordering from the constant.
    curated = []
    curated_lookup = {m["id"]: m for m in models}
    for model_id in [
        "openai/gpt-oss-120b:free",
        "qwen/qwen3-coder:free",
        "google/gemma-3-27b-it:free",
        "nousresearch/hermes-3-llama-3.1-405b:free",
    ]:
        if model_id in curated_lookup:
            curated.append(curated_lookup[model_id])

    return {
        "curated": curated,
        "free": free_models,
        "all": models,
        "total": len(models),
    }


# ─── Capability Hooks ───


@app.post("/api/hooks/capability")
async def api_capability_hook(request: Request):
    """External systems can push capability changes here.

    The registry updates, which emits an event, which the awareness
    catches and translates into a feeling Lumen expresses naturally.

    Body: {"kind": "mcp|skill|connector|module|channel",
           "name": "...", "description": "...",
           "status": "ready|available|error", "provides": [...]}
    """
    guard = _require_owner_access(request)
    if guard is not None:
        return guard

    if not _brain:
        return JSONResponse(status_code=503, content={"error": "Lumen not ready"})

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "Invalid JSON"})

    kind_str = body.get("kind", "").lower()
    name = body.get("name", "")
    if not kind_str or not name:
        return JSONResponse(
            status_code=400, content={"error": "kind and name are required"}
        )

    try:
        kind = CapabilityKind(kind_str)
    except ValueError:
        return JSONResponse(
            status_code=400,
            content={
                "error": f"Invalid kind. Must be one of: {[k.value for k in CapabilityKind]}"
            },
        )

    status_str = body.get("status", "available")
    try:
        status = CapabilityStatus(status_str)
    except ValueError:
        status = CapabilityStatus.AVAILABLE

    _brain.registry.register(
        Capability(
            kind=kind,
            name=name,
            description=body.get("description", ""),
            status=status,
            provides=body.get("provides", []),
        )
    )

    await broadcast_awareness()

    return {"status": "registered", "name": name, "kind": kind_str}
