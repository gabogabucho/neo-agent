"""Runtime hooks for installed x-lumen modules."""

from __future__ import annotations

import asyncio
import importlib.util
import inspect
import json
import logging
import os
import shutil

logger = logging.getLogger(__name__)
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

from lumen.core.connectors import ConnectorRegistry
from lumen.core.memory import Memory
from lumen.core.module_manifest import load_module_manifest


@dataclass
class ModuleRuntimeContext:
    name: str
    module_dir: Path
    runtime_dir: Path
    manifest: dict[str, Any]
    config: dict[str, Any]
    connectors: ConnectorRegistry | None = None
    memory: Memory | None = None
    lumen_dir: Path | None = None
    brain: Any = None
    registered_tools: list[str] = field(default_factory=list)
    inbox: Any = None

    def ensure_runtime_dir(self) -> Path:
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        return self.runtime_dir

    def read_runtime_state(self) -> dict[str, Any]:
        state_path = self.runtime_dir / "runtime.json"
        if not state_path.exists():
            return {}
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def write_runtime_state(self, payload: dict[str, Any]) -> None:
        self.ensure_runtime_dir()
        state_path = self.runtime_dir / "runtime.json"
        state_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )

    def resolve_setting(self, key: str, env_name: str | None = None) -> str | None:
        module_settings = ((self.config or {}).get("modules") or {}).get(self.name, {})
        if key in module_settings and module_settings[key] not in {None, ""}:
            return str(module_settings[key])
        module_secrets = ((self.config or {}).get("secrets") or {}).get(self.name, {})
        if env_name and env_name in module_secrets and module_secrets[env_name] not in {None, ""}:
            return str(module_secrets[env_name])
        if key in module_secrets and module_secrets[key] not in {None, ""}:
            return str(module_secrets[key])
        if env_name:
            env_value = os.environ.get(env_name)
            if env_value:
                return env_value
        return None

    def register_tool(
        self,
        name: str,
        description: str,
        parameters: dict[str, Any],
        handler,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if self.connectors is None:
            raise RuntimeError("Connectors registry not available")
        self.connectors.register_tool(name, description, parameters, handler, metadata)
        if name not in self.registered_tools:
            self.registered_tools.append(name)

    def unregister_registered_tools(self) -> None:
        if self.connectors is None:
            return
        for tool_name in list(self.registered_tools):
            self.connectors.unregister_tool(tool_name)
        self.registered_tools.clear()


def _load_runtime_module(module_dir: Path, name: str) -> ModuleType | None:
    connector_path = module_dir / "connector.py"
    if not connector_path.exists():
        return None

    spec = importlib.util.spec_from_file_location(
        f"lumen_module_{name.replace('-', '_')}", connector_path
    )
    if spec is None or spec.loader is None:
        return None

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_context(
    *,
    name: str,
    module_dir: Path,
    runtime_root: Path,
    config: dict[str, Any] | None,
    connectors: ConnectorRegistry | None = None,
    memory: Memory | None = None,
    lumen_dir: Path | None = None,
    brain: Any = None,
    inbox: Any = None,
) -> ModuleRuntimeContext:
    _, manifest = load_module_manifest(module_dir)
    return ModuleRuntimeContext(
        name=name,
        module_dir=module_dir,
        runtime_dir=runtime_root / name,
        manifest=manifest,
        config=config or {},
        connectors=connectors,
        memory=memory,
        lumen_dir=lumen_dir,
        brain=brain,
        inbox=inbox,
    )


def run_module_install_hook(
    *,
    name: str,
    module_dir: Path,
    runtime_root: Path,
    config: dict[str, Any] | None = None,
    lumen_dir: Path | None = None,
) -> None:
    module = _load_runtime_module(module_dir, name)
    if module is None or not hasattr(module, "install"):
        return

    context = _build_context(
        name=name,
        module_dir=module_dir,
        runtime_root=runtime_root,
        config=config,
        lumen_dir=lumen_dir,
    )
    context.ensure_runtime_dir()
    module.install(context)


def run_module_uninstall_hook(
    *,
    name: str,
    module_dir: Path,
    runtime_root: Path,
    config: dict[str, Any] | None = None,
    lumen_dir: Path | None = None,
) -> None:
    module = _load_runtime_module(module_dir, name)
    context = _build_context(
        name=name,
        module_dir=module_dir,
        runtime_root=runtime_root,
        config=config,
        lumen_dir=lumen_dir,
    )
    if module is not None and hasattr(module, "uninstall"):
        module.uninstall(context)
    shutil.rmtree(context.runtime_dir, ignore_errors=True)


@dataclass
class LoadedModuleRuntime:
    module: ModuleType
    context: ModuleRuntimeContext
    state: Any = None


class ModuleRuntimeManager:
    def __init__(
        self,
        *,
        pkg_dir: Path,
        lumen_dir: Path,
        config: dict[str, Any],
        connectors: ConnectorRegistry,
        memory: Memory,
        brain: Any = None,
    ):
        self.pkg_dir = pkg_dir
        self.lumen_dir = lumen_dir
        self.runtime_root = lumen_dir / "modules"
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.connectors = connectors
        self.memory = memory
        self.brain = brain
        self._loaded: dict[str, LoadedModuleRuntime] = {}

    async def sync(self) -> None:
        modules_dir = self.pkg_dir / "modules"
        installed_names: set[str] = set()

        if modules_dir.exists():
            for module_dir in modules_dir.iterdir():
                if not module_dir.is_dir() or module_dir.name.startswith("_"):
                    continue
                manifest_path, manifest = load_module_manifest(module_dir)
                if manifest_path is None:
                    continue
                name = str(manifest.get("name") or module_dir.name)
                installed_names.add(name)
                if name not in self._loaded:
                    await self._activate(name, module_dir)

        for name in list(self._loaded):
            if name not in installed_names:
                await self.unload(name)

    async def unload(self, name: str) -> None:
        loaded = self._loaded.pop(name, None)
        if loaded is None:
            return

        module = loaded.module
        try:
            if hasattr(module, "deactivate"):
                result = module.deactivate(loaded.context, loaded.state)
                if inspect.isawaitable(result):
                    await result
        finally:
            loaded.context.unregister_registered_tools()

    async def close(self) -> None:
        for name in list(self._loaded):
            await self.unload(name)

    async def _activate(self, name: str, module_dir: Path) -> None:
        module = _load_runtime_module(module_dir, name)
        if module is None or not hasattr(module, "activate"):
            return

        context = _build_context(
            name=name,
            module_dir=module_dir,
            runtime_root=self.runtime_root,
            config=self.config,
            connectors=self.connectors,
            memory=self.memory,
            lumen_dir=self.lumen_dir,
            brain=self.brain,
            inbox=getattr(self.brain, "inbox", None) if self.brain else None,
        )
        context.ensure_runtime_dir()

        try:
            result = module.activate(context)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            context.unregister_registered_tools()
            return

        self._loaded[name] = LoadedModuleRuntime(
            module=module, context=context, state=result
        )

        # Gateway modules: detect from manifest and wire to inbox automatically.
        # The module only needs to implement send() and push incoming messages
        # to context.inbox. The framework handles adapter registration and
        # inbox consumer routing.
        gateway_cfg = _gateway_config(context.manifest)
        send_fn = getattr(result, "send", None) or getattr(module, "send", None)
        has_send = send_fn is not None and callable(send_fn)

        if gateway_cfg and context.inbox is not None:
            channel_id = gateway_cfg["channel"]
            if has_send:
                context.inbox.register_adapter(channel_id, send_fn)
            _start_gateway_inbox_watcher(name, channel_id, context)
        elif has_send and context.inbox is not None:
            # Module has send() and inbox but no gateway declaration —
            # derive channel from module name and auto-register.
            channel_id = _derive_channel_id(name, context.manifest)
            logger.warning(
                "Module %s has send() and inbox access but no x-lumen.gateway "
                "in manifest. Auto-registering adapter for channel '%s'. "
                "Add gateway.channel to the manifest to suppress this warning.",
                name,
                channel_id,
            )
            context.inbox.register_adapter(channel_id, send_fn)


def _gateway_config(manifest: dict[str, Any] | None) -> dict[str, str] | None:
    """Extract gateway declaration from a module manifest, if present."""
    if not isinstance(manifest, dict):
        return None
    x_lumen = manifest.get("x-lumen") or manifest.get("x_lumen") or {}
    if not isinstance(x_lumen, dict):
        return None
    gateway = x_lumen.get("gateway")
    if not isinstance(gateway, dict) or not gateway.get("channel"):
        return None
    return {"channel": str(gateway["channel"])}


def _derive_channel_id(module_name: str, manifest: dict | None) -> str:
    """Derive a channel ID from the module name when no gateway is declared.

    Tries the manifest display_name first, then extracts from the module name
    (e.g. "x-lumen-comunicacion-telegram" → "telegram").
    """
    if isinstance(manifest, dict):
        display = manifest.get("display_name", "")
        if display:
            return display.lower().strip().replace(" ", "-")
    parts = module_name.split("-")
    return parts[-1] if parts else module_name


def _start_gateway_inbox_watcher(
    module_name: str, channel_id: str, context: ModuleRuntimeContext
) -> None:
    """Watch the module's inbox.jsonl and push new lines to the unified inbox.

    Gateway modules write incoming messages to their runtime_dir/inbox.jsonl
    as JSONL — one JSON object per line. This watcher tails that file and
    pushes each new entry to the unified Inbox, where the consumer routes
    it through brain.think().

    This is a bridge pattern: modules stay simple (write to file), the
    framework handles the routing. When modules are updated to push to
    context.inbox directly, this watcher becomes unnecessary.
    """
    inbox = context.inbox
    if inbox is None:
        logger.warning("No inbox for gateway module %s — incoming messages will not be routed", module_name)
        return

    from lumen.core.inbox import IncomingMessage

    inbox_path = context.runtime_dir / "inbox.jsonl"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    if not inbox_path.exists():
        inbox_path.touch()

    async def _watch():
        offset = inbox_path.stat().st_size
        logger.info("Inbox watcher started for %s, initial offset=%d", module_name, offset)
        while True:
            await asyncio.sleep(1)
            try:
                new_size = inbox_path.stat().st_size
            except OSError:
                break
            if new_size <= offset:
                continue
            logger.info("New inbox data for %s: offset=%d new_size=%d", module_name, offset, new_size)
        while True:
            await asyncio.sleep(1)
            try:
                new_size = inbox_path.stat().st_size
            except OSError:
                break
            if new_size <= offset:
                continue
            _logger.info("New data detected: offset=%d new_size=%d", offset, new_size)
            with inbox_path.open("r", encoding="utf-8") as f:
                f.seek(offset)
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sender_id = str(entry.get("chat_id") or entry.get("sender_id") or "")
                    text = str(entry.get("text") or "")
                    if sender_id and text:
                        await inbox.push(
                            IncomingMessage(
                                channel=channel_id,
                                sender_id=sender_id,
                                text=text,
                            )
                        )
                offset = f.tell()

    import asyncio

    asyncio.create_task(_watch(), name=f"gateway-watcher-{module_name}")
