from __future__ import annotations

import asyncio
import json
from pathlib import Path
from time import time
from urllib import error as urllib_error
from urllib import request as urllib_request

import yaml


API_ROOT = "https://discord.com/api/v10"
MODULE_NAME = "x-lumen-comunicacion-discord"


def install(context):
    context.ensure_runtime_dir()

    config_path = context.runtime_dir / "config.yaml"
    if not config_path.exists():
        config_path.write_text(
            yaml.dump(
                {
                    "bot_token_env": "DISCORD_BOT_TOKEN",
                    "channel_id_env": "DISCORD_CHANNEL_ID",
                    "poll_interval_seconds": 2,
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
                "last_message_id": None,
            }
        )


def uninstall(context):
    # Discord bots don't need explicit webhook cleanup like Telegram.
    # The bot stops receiving events once the poll loop stops.
    pass


class DiscordRuntime:
    def __init__(self, context):
        self.context = context
        self._poll_task: asyncio.Task | None = None
        self._stopping = False

    async def start(self):
        state = self.context.read_runtime_state()
        token = self._bot_token()
        if not token:
            state.update(
                {
                    "module": MODULE_NAME,
                    "status": "degraded",
                    "polling": False,
                    "error": "Missing DISCORD_BOT_TOKEN",
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

        state = self.context.read_runtime_state()
        state.update(
            {
                "module": MODULE_NAME,
                "status": "stopped",
                "polling": False,
                "updated_at": time(),
            }
        )
        self.context.write_runtime_state(state)

    async def send(self, recipient_id: str, message: str) -> None:
        """ChannelAdapter protocol — route inbox response back to Discord."""
        token = self._bot_token()
        if not token:
            return
        channel_id = str(recipient_id or self._channel_id() or "").strip()
        if not channel_id:
            return
        try:
            await asyncio.to_thread(
                _discord_api,
                token,
                f"/channels/{channel_id}/messages",
                {"content": message},
            )
        except Exception:
            pass

    async def send_message(self, text: str, channel_id: str | None = None):
        token = self._bot_token()
        if not token:
            return {
                "status": "error",
                "error": "Missing DISCORD_BOT_TOKEN",
            }

        resolved_channel_id = str(
            channel_id or self._channel_id() or ""
        ).strip()
        if not resolved_channel_id:
            return {
                "status": "error",
                "error": "Missing Discord channel_id",
            }

        result = await asyncio.to_thread(
            _discord_api,
            token,
            f"/channels/{resolved_channel_id}/messages",
            {"content": text},
        )
        return {
            "status": "ok",
            "channel_id": resolved_channel_id,
            "message_id": result.get("id"),
        }

    async def _poll_loop(self):
        while not self._stopping:
            try:
                token = self._bot_token()
                channel_id = self._channel_id()
                if not token or not channel_id:
                    await asyncio.sleep(2)
                    continue

                state = self.context.read_runtime_state()
                last_message_id = state.get("last_message_id")

                params = {"limit": 50}
                if last_message_id is not None:
                    params["after"] = str(last_message_id)

                messages = await asyncio.to_thread(
                    _discord_api_get,
                    token,
                    f"/channels/{channel_id}/messages",
                    params,
                    25,
                )

                # Discord returns newest-first, reverse to process in order
                messages.reverse()
                for message in messages:
                    await self._handle_message(message)
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
                await asyncio.sleep(2)

    async def _handle_message(self, message: dict):
        message_id = message.get("id")
        author = message.get("author") or {}
        author_id = author.get("id")
        # Skip messages from the bot itself
        bot_user_id = self._get_bot_user_id()
        if bot_user_id and str(author_id) == str(bot_user_id):
            # Still update the last_message_id so we don't reprocess
            state = self.context.read_runtime_state()
            state.update(
                {
                    "module": MODULE_NAME,
                    "status": "running",
                    "polling": True,
                    "last_message_id": message_id,
                    "updated_at": time(),
                }
            )
            self.context.write_runtime_state(state)
            return

        channel_id = message.get("channel_id")
        text = message.get("content") or ""

        state = self.context.read_runtime_state()
        state.update(
            {
                "module": MODULE_NAME,
                "status": "running",
                "polling": True,
                "last_message_id": message_id,
                "last_channel_id": channel_id,
                "last_message_preview": text[:120],
                "updated_at": time(),
            }
        )
        self.context.write_runtime_state(state)

        if channel_id is None or not text:
            return

        # The framework handles inbox routing. The module just needs
        # send() to be callable — ModuleRuntimeManager wires the rest.
        inbox_path = self.context.runtime_dir / "inbox.jsonl"
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with inbox_path.open("a", encoding="utf-8") as inbox_file:
            inbox_file.write(
                json.dumps(
                    {
                        "message_id": message_id,
                        "channel_id": channel_id,
                        "author_id": str(author_id),
                        "author_username": author.get("username", ""),
                        "text": text,
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
                text,
                category="discord_message",
                metadata={
                    "channel_id": str(channel_id),
                    "author_id": str(author_id),
                    "module": MODULE_NAME,
                },
            )

    def _bot_token(self) -> str | None:
        return self.context.resolve_setting("bot_token", "DISCORD_BOT_TOKEN")

    def _channel_id(self) -> str | None:
        return self.context.resolve_setting("channel_id", "DISCORD_CHANNEL_ID")

    def _get_bot_user_id(self) -> str | None:
        """Return the bot's own user ID from runtime state, if cached."""
        state = self.context.read_runtime_state()
        return state.get("bot_user_id")


async def activate(context):
    runtime = DiscordRuntime(context)

    # Fetch bot's own user ID on activation so we can skip own messages
    token = runtime._bot_token()
    if token:
        try:
            user_info = await asyncio.to_thread(
                _discord_api_get, token, "/users/@me", {}, 10
            )
            state = context.read_runtime_state()
            state["bot_user_id"] = user_info.get("id")
            context.write_runtime_state(state)
        except Exception:
            pass  # Non-fatal; we just won't skip own messages

    context.register_tool(
        "message.send_discord",
        "Send a Discord message using the installed Discord communication module.",
        {
            "type": "object",
            "properties": {
                "channel_id": {
                    "type": "string",
                    "description": "Discord channel ID. Optional if DISCORD_CHANNEL_ID is configured.",
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


def _discord_api(
    token: str,
    endpoint: str,
    payload: dict | None = None,
    timeout: int = 15,
) -> dict:
    url = f"{API_ROOT}{endpoint}"
    data = None
    headers = {"Authorization": f"Bot {token}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib_request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Discord API error: {exc.code} {body}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Discord API unavailable: {exc.reason}") from exc

    return body


def _discord_api_get(
    token: str,
    endpoint: str,
    params: dict | None = None,
    timeout: int = 15,
) -> list | dict:
    url = f"{API_ROOT}{endpoint}"
    if params:
        query = "&".join(
            f"{k}={v}" for k, v in params.items()
        )
        url = f"{url}?{query}"

    headers = {"Authorization": f"Bot {token}"}
    req = urllib_request.Request(url, headers=headers, method="GET")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"Discord API error: {exc.code} {body}") from exc
    except urllib_error.URLError as exc:
        raise RuntimeError(f"Discord API unavailable: {exc.reason}") from exc

    return body
