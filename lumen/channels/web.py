"""Web channel — FastAPI dashboard + WebSocket chat. UI-FIRST.

Routing logic:
  /  → no config? → /setup
  /  → config but not awakened? → awakening animation
  /  → config and awakened? → /dashboard
"""

import base64
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
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from lumen.core.artifact_setup import (
    contract_from_mcp_server,
    load_mcp_overlay,
    parse_artifact_action,
    pending_setup_from_contract,
)
from lumen.core.registry import CapabilityKind
from lumen.core.runtime import (
    bootstrap_runtime,
    refresh_runtime_registry,
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


# State — initialized lazily after web setup or by CLI
_brain = None
_locale: dict = {}
_config: dict = {}
_access_mode = "run"
_awareness = None  # CapabilityAwareness — set during bootstrap
_active_websockets: set[WebSocket] = set()  # Track connected clients
_watchers = None  # FilePoller — started in lifespan

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


def configure(brain, locale: dict, config: dict, awareness=None):
    """Configure the web channel (called by CLI when config exists)."""
    global _brain, _locale, _config, _awareness
    _brain = brain
    _locale = locale
    _config = config
    _awareness = awareness
    _attach_brain_runtime_handlers()


def _attach_brain_runtime_handlers():
    if _brain is not None:
        _brain.flow_action_handler = _handle_flow_action


async def _handle_flow_action(action: str, slots: dict, *, session=None) -> dict:
    parsed = parse_artifact_action(action)
    if parsed is None:
        return {"status": "ignored", "message": "Listo."}

    kind, artifact_id = parsed
    if kind == "native":
        return await _persist_module_setup_slots(artifact_id, slots)
    if kind == "mcp":
        return await _persist_mcp_setup_slots(artifact_id, slots)
    return {
        "status": "error",
        "message": f"Todavía no sé guardar configuraciones para artefactos de tipo {kind}.",
    }


async def _persist_module_setup_slots(module_name: str, values: dict | None) -> dict:
    global _config

    module_dir = PKG_DIR / "modules" / module_name
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
    current_saved = set((((merged.get("secrets") or {}).get(module_name) or {}).keys()))
    saved_env = sorted(current_saved - previous_saved)

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

    _config = _merge_save_config({"secrets": merged.get("secrets", {})})

    if _brain is not None:
        # Unload the module first so sync re-activates with updated config
        manager = getattr(_brain, "module_manager", None)
        if manager:
            await manager.unload(module_name)
        await sync_runtime_modules(_brain, config=_config, pkg_dir=PKG_DIR, lumen_dir=LUMEN_DIR)
        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR)
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
        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR)
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
    if not _is_configured(loaded):
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

    refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])

    if str(_config.get("language") or "en") != previous_language:
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR)

    return True


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
    global _watchers
    if _brain:
        await _brain.memory.init()

    # Start capability watchers if brain is ready
    if _brain and _awareness:
        from lumen.core.watchers import FilePoller

        modules_dir = PKG_DIR / "modules"
        skills_dir = PKG_DIR / "skills"
        watched = [d for d in [modules_dir, skills_dir] if d.exists()]

        async def _on_file_change():
            """Called by FilePoller when filesystem changes detected."""
            refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])
            await broadcast_awareness()

        if watched:
            _watchers = FilePoller(_brain.registry, watched, on_change=_on_file_change)
            await _watchers.start(interval=120)

    yield

    # Cleanup
    if _watchers:
        await _watchers.stop()
    if _brain:
        if isinstance(getattr(_brain, "module_manager", None), ModuleRuntimeManager):
            await _brain.module_manager.close()
        if getattr(_brain, "mcp_manager", None):
            await _brain.mcp_manager.close()
        await _brain.memory.close()


app = FastAPI(title="Lumen", version="0.1.0", lifespan=lifespan)
templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(templates_dir))
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
session_manager = SessionManager()


# ─── Routes ───


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
            "version": "0.1.0",
            "connectors_count": len(_brain.connectors.list()) if _brain else 0,
            "flows_count": len(_brain.flows) if _brain else 0,
            "mcp_count": len(_brain.registry.list_by_kind(CapabilityKind.MCP))
            if _brain
            else 0,
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

    _config = _merge_save_config(updates, removals=removals)
    await _refresh_runtime_from_config(loaded)

    return {
        "status": "ok",
        "config": {
            "provider": _infer_provider_name(_config),
            "model": _config.get("model", ""),
            "api_key_env": _config.get("api_key_env", ""),
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

        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR)

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
        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR)

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

        refresh_runtime_registry(_brain, pkg_dir=PKG_DIR, active_channels=["web"])
        reload_runtime_personality_surface(_brain, config=_config, pkg_dir=PKG_DIR)

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

    module_dir = PKG_DIR / "modules" / name
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
        "version": "0.1.0",
        "model": _config.get("model", "not configured"),
        "language": _config.get("language", "en"),
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


# ─── OpenRouter model catalog ───

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
            headers={"User-Agent": "lumen-agent/0.1.0", "Accept": "application/json"},
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
