from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from time import sleep, time
from urllib import error as urllib_error
from urllib import request as urllib_request

import yaml


MODULE_NAME = "x-lumen-comunicacion-whatsapp"
BRIDGE_SOURCE_DIR = Path(__file__).parent
DEFAULT_BRIDGE_PORT = 3100
POLL_INTERVAL = 2
HEALTH_TIMEOUT = 30  # seconds to wait for bridge to become ready


# ---------------------------------------------------------------------------
# install / uninstall
# ---------------------------------------------------------------------------

def install(context):
    context.ensure_runtime_dir()

    # Copy bridge files (bridge.js, allowlist.js, package.json) to runtime dir
    _copy_bridge_files(context.runtime_dir)

    config_path = context.runtime_dir / "config.yaml"
    if not config_path.exists():
        config_path.write_text(
            yaml.dump(
                {
                    "bridge_port_env": "WHATSAPP_BRIDGE_PORT",
                    "mode_env": "WHATSAPP_MODE",
                    "allowed_users_env": "WHATSAPP_ALLOWED_USERS",
                    "poll_interval_seconds": POLL_INTERVAL,
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    if not (context.runtime_dir / "runtime.json").exists():
        context.write_runtime_state(
            {
                "module": MODULE_NAME,
                "status": "installed",
                "polling": False,
                "bridge_pid": None,
            }
        )


def uninstall(context):
    # The deactivate path handles process cleanup.  On uninstall we also
    # remove the copied bridge files — the catalog copy is the source of truth.
    pass


def _copy_bridge_files(runtime_dir: Path):
    """Copy Node.js bridge sources into the runtime directory."""
    for filename in ("bridge.js", "allowlist.js", "package.json"):
        src = BRIDGE_SOURCE_DIR / filename
        dst = runtime_dir / filename
        if src.exists() and not dst.exists():
            shutil.copy2(src, dst)


# ---------------------------------------------------------------------------
# WhatsAppRuntime
# ---------------------------------------------------------------------------

class WhatsAppRuntime:
    def __init__(self, context):
        self.context = context
        self._poll_task: asyncio.Task | None = None
        self._bridge_proc: subprocess.Popen | None = None
        self._stopping = False

    # -- lifecycle -----------------------------------------------------------

    async def start(self):
        state = self.context.read_runtime_state()

        node_bin = _find_node()
        if node_bin is None:
            state.update(
                {
                    "module": MODULE_NAME,
                    "status": "degraded",
                    "polling": False,
                    "error": "Node.js not found. Install Node.js (v18+) to use the WhatsApp bridge.",
                    "updated_at": time(),
                }
            )
            self.context.write_runtime_state(state)
            return

        # Ensure bridge files are present in runtime dir
        _copy_bridge_files(self.context.runtime_dir)

        # npm install if node_modules missing
        node_modules = self.context.runtime_dir / "node_modules"
        if not node_modules.exists():
            npm_bin = _find_npm()
            if npm_bin is None:
                state.update(
                    {
                        "module": MODULE_NAME,
                        "status": "degraded",
                        "polling": False,
                        "error": "npm not found. Install Node.js (v18+) to use the WhatsApp bridge.",
                        "updated_at": time(),
                    }
                )
                self.context.write_runtime_state(state)
                return

            state.update(
                {
                    "module": MODULE_NAME,
                    "status": "installing",
                    "polling": False,
                    "error": None,
                    "updated_at": time(),
                }
            )
            self.context.write_runtime_state(state)

            await asyncio.to_thread(
                _run_npm_install, npm_bin, self.context.runtime_dir
            )

        # Kill orphaned bridge processes on the configured port
        port = self._bridge_port()
        await asyncio.to_thread(_kill_orphans_on_port, port)

        # Start the bridge subprocess
        bridge_log = self.context.runtime_dir / "bridge.log"
        env = _build_bridge_env(self.context, port)

        try:
            log_fh = open(bridge_log, "a", encoding="utf-8")
            self._bridge_proc = subprocess.Popen(
                [node_bin, "bridge.js", "--port", str(port)],
                cwd=str(self.context.runtime_dir),
                stdout=log_fh,
                stderr=log_fh,
                env=env,
            )
        except Exception as exc:
            state.update(
                {
                    "module": MODULE_NAME,
                    "status": "degraded",
                    "polling": False,
                    "error": f"Failed to start bridge: {exc}",
                    "updated_at": time(),
                }
            )
            self.context.write_runtime_state(state)
            return

        # Wait for bridge to become healthy
        ready = await asyncio.to_thread(_wait_for_health, port, HEALTH_TIMEOUT)
        if not ready:
            state.update(
                {
                    "module": MODULE_NAME,
                    "status": "degraded",
                    "polling": False,
                    "error": "Bridge did not become healthy in time. Check bridge.log.",
                    "bridge_pid": self._bridge_proc.pid if self._bridge_proc else None,
                    "updated_at": time(),
                }
            )
            self.context.write_runtime_state(state)
            return

        state.update(
            {
                "module": MODULE_NAME,
                "status": "running",
                "polling": True,
                "error": None,
                "bridge_pid": self._bridge_proc.pid if self._bridge_proc else None,
                "updated_at": time(),
            }
        )
        self.context.write_runtime_state(state)

        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"{MODULE_NAME}-poll"
        )

    async def stop(self):
        self._stopping = True
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        if self._bridge_proc is not None:
            try:
                self._bridge_proc.terminate()
                self._bridge_proc.wait(timeout=5)
            except Exception:
                try:
                    self._bridge_proc.kill()
                except Exception:
                    pass
            self._bridge_proc = None

        state = self.context.read_runtime_state()
        state.update(
            {
                "module": MODULE_NAME,
                "status": "stopped",
                "polling": False,
                "bridge_pid": None,
                "updated_at": time(),
            }
        )
        self.context.write_runtime_state(state)

    # -- send methods --------------------------------------------------------

    async def send(self, recipient_id: str, message: str) -> None:
        """ChannelAdapter protocol -- route inbox response back to WhatsApp."""
        chat_id = str(recipient_id or "").strip()
        if not chat_id:
            return
        try:
            await asyncio.to_thread(
                _bridge_post,
                self._bridge_port(),
                "/send",
                {"chatId": chat_id, "message": message},
            )
        except Exception:
            pass

    async def send_message(self, text: str, chat_id: str | None = None) -> dict:
        """Tool-registered method: send a WhatsApp message."""
        resolved_chat_id = str(chat_id or "").strip()
        if not resolved_chat_id:
            return {
                "status": "error",
                "error": "Missing WhatsApp chat_id (phone number or JID).",
            }

        try:
            result = await asyncio.to_thread(
                _bridge_post,
                self._bridge_port(),
                "/send",
                {"chatId": resolved_chat_id, "message": text},
            )
            return {
                "status": "ok",
                "chat_id": resolved_chat_id,
                "message_id": result.get("messageId"),
            }
        except Exception as exc:
            return {
                "status": "error",
                "error": str(exc),
            }

    # -- poll loop -----------------------------------------------------------

    async def _poll_loop(self):
        while not self._stopping:
            try:
                messages = await asyncio.to_thread(
                    _bridge_get, self._bridge_port(), "/messages"
                )
                for msg in messages:
                    await self._handle_message(msg)
                await asyncio.sleep(POLL_INTERVAL)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state = self.context.read_runtime_state()
                state.update(
                    {
                        "module": MODULE_NAME,
                        "status": "degraded",
                        "polling": False,
                        "error": str(exc),
                        "updated_at": time(),
                    }
                )
                self.context.write_runtime_state(state)
                await asyncio.sleep(POLL_INTERVAL)

    async def _handle_message(self, msg: dict):
        chat_id = msg.get("chatId", "")
        body = msg.get("body", "")
        sender = msg.get("senderId", "")
        timestamp = msg.get("timestamp")

        state = self.context.read_runtime_state()
        state.update(
            {
                "module": MODULE_NAME,
                "status": "running",
                "polling": True,
                "last_chat_id": chat_id,
                "last_sender": sender,
                "last_message_preview": body[:120],
                "updated_at": time(),
            }
        )
        self.context.write_runtime_state(state)

        if not chat_id or not body:
            return

        # Write to inbox.jsonl -- the framework watches this file and
        # bridges entries to the unified Inbox automatically.
        inbox_path = self.context.runtime_dir / "inbox.jsonl"
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with inbox_path.open("a", encoding="utf-8") as inbox_file:
            inbox_file.write(
                json.dumps(
                    {
                        "chat_id": chat_id,
                        "text": body,
                        "received_at": time(),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )

        if (
            self.context.memory is not None
            and getattr(self.context.memory, "_db", None) is not None
        ):
            await self.context.memory.remember(
                body,
                category="whatsapp_message",
                metadata={"chat_id": str(chat_id), "module": MODULE_NAME},
            )

    # -- helpers -------------------------------------------------------------

    def _bridge_port(self) -> int:
        raw = self.context.resolve_setting(
            "bridge_port", "WHATSAPP_BRIDGE_PORT"
        )
        if raw:
            try:
                return int(raw)
            except (ValueError, TypeError):
                pass
        return DEFAULT_BRIDGE_PORT


# ---------------------------------------------------------------------------
# activate / deactivate
# ---------------------------------------------------------------------------

async def activate(context):
    runtime = WhatsAppRuntime(context)
    context.register_tool(
        "message.send_whatsapp",
        "Send a WhatsApp message using the installed WhatsApp communication module.",
        {
            "type": "object",
            "properties": {
                "chat_id": {
                    "type": "string",
                    "description": "WhatsApp chat ID (phone number with country code or JID like 5491112345678@s.whatsapp.net).",
                },
                "text": {
                    "type": "string",
                    "description": "Plain-text message to send.",
                },
            },
            "required": ["text"],
        },
        runtime.send_message,
        metadata={"kind": "module", "module": MODULE_NAME},
    )
    await runtime.start()
    return runtime


async def deactivate(context, runtime):
    if runtime is not None:
        await runtime.stop()


# ---------------------------------------------------------------------------
# HTTP helpers (urllib, no external deps)
# ---------------------------------------------------------------------------

def _bridge_url(port: int, path: str) -> str:
    return f"http://127.0.0.1:{port}{path}"


def _bridge_get(port: int, path: str, timeout: int = 10) -> list:
    """GET from the bridge and return parsed JSON (expects a list)."""
    url = _bridge_url(port, path)
    req = urllib_request.Request(url, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        raise RuntimeError(f"Bridge HTTP error: {exc.code}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Bridge unreachable: {exc.reason}") from exc


def _bridge_post(port: int, path: str, payload: dict, timeout: int = 15) -> dict:
    """POST to the bridge and return parsed JSON."""
    url = _bridge_url(port, path)
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    req = urllib_request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Bridge HTTP error: {exc.code} {body}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Bridge unreachable: {exc.reason}") from exc


# ---------------------------------------------------------------------------
# Process management helpers
# ---------------------------------------------------------------------------

def _find_node() -> str | None:
    """Find the Node.js binary."""
    for candidate in ("node", "node.exe"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _find_npm() -> str | None:
    """Find the npm binary."""
    for candidate in ("npm", "npm.cmd"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _run_npm_install(npm_bin: str, cwd: Path) -> None:
    """Run npm install synchronously."""
    subprocess.run(
        [npm_bin, "install", "--production"],
        cwd=str(cwd),
        capture_output=True,
        timeout=120,
    )


def _build_bridge_env(context, port: int) -> dict:
    """Build the environment dict for the bridge subprocess."""
    env = os.environ.copy()
    env["WHATSAPP_BRIDGE_PORT"] = str(port)

    mode = context.resolve_setting("mode", "WHATSAPP_MODE")
    if mode:
        env["WHATSAPP_MODE"] = str(mode)

    allowed = context.resolve_setting("allowed_users", "WHATSAPP_ALLOWED_USERS")
    if allowed:
        env["WHATSAPP_ALLOWED_USERS"] = str(allowed)

    return env


def _wait_for_health(port: int, timeout: int = 30) -> bool:
    """Block until the bridge /health endpoint responds, or timeout."""
    deadline = time() + timeout
    while time() < deadline:
        try:
            url = _bridge_url(port, "/health")
            req = urllib_request.Request(url, method="GET")
            with urllib_request.urlopen(req, timeout=3) as response:
                data = json.loads(response.read().decode("utf-8"))
                if data.get("status") in ("connected", "disconnected"):
                    # "disconnected" is fine -- it means the bridge is up,
                    # waiting for QR scan or reconnection.
                    return True
        except Exception:
            pass
        sleep(1)
    return False


def _kill_orphans_on_port(port: int) -> None:
    """Kill any process already listening on the given port."""
    if sys.platform == "win32":
        _kill_orphans_win32(port)
    else:
        _kill_orphans_unix(port)


def _kill_orphans_win32(port: int) -> None:
    """Windows: use netstat + taskkill."""
    try:
        result = subprocess.run(
            ["netstat", "-ano", "-p", "TCP"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in result.stdout.splitlines():
            # Lines look like:  TCP    127.0.0.1:3100    0.0.0.0:0    LISTENING    12345
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}"):
                try:
                    pid = int(parts[-1])
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True,
                        timeout=5,
                    )
                except (ValueError, subprocess.TimeoutExpired):
                    pass
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass


def _kill_orphans_unix(port: int) -> None:
    """Unix: use lsof or fuser."""
    try:
        subprocess.run(
            ["fuser", "-k", f"{port}/tcp"],
            capture_output=True,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
