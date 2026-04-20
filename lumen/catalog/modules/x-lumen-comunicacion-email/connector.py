from __future__ import annotations

import asyncio
import email
import email.policy
import imaplib
import json
import smtplib
from email.mime.text import MIMEText
from pathlib import Path
from time import time

import yaml


MODULE_NAME = "x-lumen-comunicacion-email"

# Senders that are clearly automated — skip them.
_BLOCKED_SENDERS = frozenset({
    "noreply", "no-reply", "donotreply", "do-not-reply",
    "mailer-daemon", "postmaster", "noreply.",
})


def install(context):
    context.ensure_runtime_dir()

    config_path = context.runtime_dir / "config.yaml"
    if not config_path.exists():
        config_path.write_text(
            yaml.dump(
                {
                    "email_address_env": "EMAIL_ADDRESS",
                    "email_password_env": "EMAIL_PASSWORD",
                    "imap_host_env": "EMAIL_IMAP_HOST",
                    "smtp_host_env": "EMAIL_SMTP_HOST",
                    "imap_port_env": "EMAIL_IMAP_PORT",
                    "smtp_port_env": "EMAIL_SMTP_PORT",
                    "poll_interval_seconds_env": "EMAIL_POLL_INTERVAL",
                    "presets": {
                        "gmail": {
                            "imap_host": "imap.gmail.com",
                            "smtp_host": "smtp.gmail.com",
                            "imap_port": 993,
                            "smtp_port": 587,
                        },
                        "outlook": {
                            "imap_host": "outlook.office365.com",
                            "smtp_host": "smtp.office365.com",
                            "imap_port": 993,
                            "smtp_port": 587,
                        },
                        "yahoo": {
                            "imap_host": "imap.mail.yahoo.com",
                            "smtp_host": "smtp.mail.yahoo.com",
                            "imap_port": 993,
                            "smtp_port": 587,
                        },
                    },
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
                "last_uid": None,
            }
        )


def uninstall(context):
    # Nothing external to clean up for IMAP/SMTP — just stop polling.
    pass


class EmailRuntime:
    def __init__(self, context):
        self.context = context
        self._poll_task: asyncio.Task | None = None
        self._stopping = False

    async def start(self):
        state = self.context.read_runtime_state()
        address = self._email_address()
        password = self._email_password()
        imap_host = self._imap_host()

        if not address or not password or not imap_host:
            state.update(
                {
                    "module": MODULE_NAME,
                    "status": "degraded",
                    "polling": False,
                    "error": "Missing EMAIL_ADDRESS, EMAIL_PASSWORD, or EMAIL_IMAP_HOST",
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

    async def send(self, recipient_id: str, text: str):
        """Send an email via SMTP.

        Called by the framework's inbox router to deliver responses.
        ``recipient_id`` is the sender's email address.
        """
        address = self._email_address()
        password = self._email_password()
        smtp_host = self._smtp_host()

        if not address or not password or not smtp_host:
            return {
                "status": "error",
                "error": "Missing EMAIL_ADDRESS, EMAIL_PASSWORD, or EMAIL_SMTP_HOST",
            }

        try:
            result = await asyncio.to_thread(
                _send_smtp,
                smtp_host,
                self._smtp_port(),
                address,
                password,
                recipient_id,
                text,
            )
            return result
        except Exception as exc:
            return {
                "status": "error",
                "error": str(exc),
            }

    async def send_message(self, text: str, recipient: str | None = None):
        """Tool-callable method for sending emails from the Lumen brain."""
        resolved_recipient = str(
            recipient or self._default_recipient() or ""
        ).strip()
        if not resolved_recipient:
            return {
                "status": "error",
                "error": "Missing email recipient",
            }

        return await self.send(resolved_recipient, text)

    async def _poll_loop(self):
        while not self._stopping:
            try:
                address = self._email_address()
                password = self._email_password()
                imap_host = self._imap_host()
                if not address or not password or not imap_host:
                    await asyncio.sleep(self._poll_interval())
                    continue

                state = self.context.read_runtime_state()
                last_uid = state.get("last_uid")

                messages = await asyncio.to_thread(
                    _poll_imap,
                    imap_host,
                    self._imap_port(),
                    address,
                    password,
                    last_uid,
                )

                for msg in messages:
                    await self._handle_message(msg)

                await asyncio.sleep(self._poll_interval())
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
                await asyncio.sleep(self._poll_interval())

    async def _handle_message(self, msg: dict):
        uid = msg.get("uid")
        sender = msg.get("sender") or ""
        subject = msg.get("subject") or ""
        text = msg.get("text") or ""

        state = self.context.read_runtime_state()
        state.update(
            {
                "module": MODULE_NAME,
                "status": "running",
                "polling": True,
                "last_uid": uid,
                "last_sender": sender,
                "last_subject": subject,
                "last_message_preview": text[:120],
                "updated_at": time(),
            }
        )
        self.context.write_runtime_state(state)

        if not sender or not text:
            return

        # The framework handles inbox routing. The module just needs
        # send() to be callable — ModuleRuntimeManager wires the rest.
        inbox_path = self.context.runtime_dir / "inbox.jsonl"
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with inbox_path.open("a", encoding="utf-8") as inbox_file:
            inbox_file.write(
                json.dumps(
                    {
                        "uid": uid,
                        "chat_id": sender,
                        "text": text,
                        "subject": subject,
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
                category="email_message",
                metadata={"sender": sender, "module": MODULE_NAME},
            )

    # -- Settings helpers --------------------------------------------------

    def _email_address(self) -> str | None:
        return self.context.resolve_setting("email_address", "EMAIL_ADDRESS")

    def _email_password(self) -> str | None:
        return self.context.resolve_setting("email_password", "EMAIL_PASSWORD")

    def _imap_host(self) -> str | None:
        return self.context.resolve_setting("imap_host", "EMAIL_IMAP_HOST")

    def _smtp_host(self) -> str | None:
        return self.context.resolve_setting("smtp_host", "EMAIL_SMTP_HOST")

    def _imap_port(self) -> int:
        raw = self.context.resolve_setting("imap_port", "EMAIL_IMAP_PORT")
        if raw:
            try:
                return int(raw)
            except (ValueError, TypeError):
                pass
        return 993

    def _smtp_port(self) -> int:
        raw = self.context.resolve_setting("smtp_port", "EMAIL_SMTP_PORT")
        if raw:
            try:
                return int(raw)
            except (ValueError, TypeError):
                pass
        return 587

    def _poll_interval(self) -> int:
        raw = self.context.resolve_setting(
            "poll_interval_seconds", "EMAIL_POLL_INTERVAL"
        )
        if raw:
            try:
                return int(raw)
            except (ValueError, TypeError):
                pass
        return 15

    def _default_recipient(self) -> str | None:
        return self.context.resolve_setting(
            "default_recipient", "EMAIL_DEFAULT_RECIPIENT"
        )


async def activate(context):
    runtime = EmailRuntime(context)
    context.register_tool(
        "message.send_email",
        "Send an email using the installed Email communication module.",
        {
            "type": "object",
            "properties": {
                "recipient": {
                    "type": "string",
                    "description": "Recipient email address. Optional if EMAIL_DEFAULT_RECIPIENT is configured.",
                },
                "text": {
                    "type": "string",
                    "description": "Plain-text email body to send.",
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
# IMAP / SMTP helpers (pure stdlib, blocking — run via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _is_blocked_sender(sender: str) -> bool:
    """Return True if the sender looks like an automated/noreply address."""
    lower = sender.lower()
    local_part = lower.split("@", 1)[0] if "@" in lower else lower
    return any(blocked in local_part for blocked in _BLOCKED_SENDERS)


def _extract_text_from_email(msg: email.message.EmailMessage) -> str:
    """Extract plain-text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition") or "")
            if content_type == "text/plain" and "attachment" not in content_disposition:
                try:
                    payload = part.get_content()
                    if isinstance(payload, str):
                        return payload.strip()
                except Exception:
                    # Fallback: decode manually
                    raw = part.get_payload(decode=True)
                    if raw:
                        charset = part.get_content_charset() or "utf-8"
                        return raw.decode(charset, errors="replace").strip()
        # No text/plain part found — skip HTML-only for simplicity.
        return ""
    else:
        if msg.get_content_type() == "text/plain":
            try:
                payload = msg.get_content()
                if isinstance(payload, str):
                    return payload.strip()
            except Exception:
                raw = msg.get_payload(decode=True)
                if raw:
                    charset = msg.get_content_charset() or "utf-8"
                    return raw.decode(charset, errors="replace").strip()
        return ""


def _poll_imap(
    host: str,
    port: int,
    username: str,
    password: str,
    last_uid: str | None,
) -> list[dict]:
    """Poll IMAP for new unseen messages since ``last_uid``.

    Returns a list of dicts with keys: uid, sender, subject, text.
    """
    results: list[dict] = []

    try:
        imap = imaplib.IMAP4_SSL(host, port)
    except Exception as exc:
        raise RuntimeError(f"IMAP connection failed: {exc}") from exc

    try:
        imap.login(username, password)
        imap.select("INBOX", readonly=True)

        # Search for all UNSEEN messages.
        status, data = imap.uid("search", None, "UNSEEN")
        if status != "OK":
            return results

        uid_list = data[0].split() if data and data[0] else []
        if not uid_list:
            return results

        for uid_bytes in uid_list:
            uid = uid_bytes.decode("utf-8", errors="replace")

            # Skip messages we already processed.
            if last_uid is not None and uid <= str(last_uid):
                continue

            fetch_status, fetch_data = imap.uid("fetch", uid_bytes, "(BODY.PEEK[])")
            if fetch_status != "OK" or not fetch_data:
                continue

            # fetch_data is a list of tuples: (response_part, message_bytes)
            raw_message = None
            for item in fetch_data:
                if isinstance(item, tuple):
                    raw_message = item[1]
                    break

            if raw_message is None:
                continue

            try:
                parsed = email.message_from_bytes(
                    raw_message, policy=email.policy.default
                )
            except Exception:
                continue

            sender = parsed.get("From", "")
            # Extract just the email address from "Name <email>" format.
            if "<" in sender and ">" in sender:
                sender = sender[sender.index("<") + 1 : sender.index(">")]

            # Skip automated senders.
            if _is_blocked_sender(sender):
                continue

            subject = parsed.get("Subject", "")
            if isinstance(subject, email.header.Header):
                subject = str(subject)

            text = _extract_text_from_email(parsed)

            results.append(
                {
                    "uid": uid,
                    "sender": sender,
                    "subject": subject,
                    "text": text,
                }
            )
    finally:
        try:
            imap.logout()
        except Exception:
            pass

    return results


def _send_smtp(
    host: str,
    port: int,
    username: str,
    password: str,
    recipient: str,
    text: str,
) -> dict:
    """Send a plain-text email via SMTP (STARTTLS)."""
    msg = MIMEText(text, "plain", "utf-8")
    msg["From"] = username
    msg["To"] = recipient
    msg["Subject"] = "Re: Lumen"

    try:
        server = smtplib.SMTP(host, port, timeout=15)
    except Exception as exc:
        raise RuntimeError(f"SMTP connection failed: {exc}") from exc

    try:
        server.ehlo()
        server.starttls()
        server.ehlo()
        server.login(username, password)
        server.sendmail(username, [recipient], msg.as_string())
    except smtplib.SMTPAuthenticationError as exc:
        raise RuntimeError(f"SMTP auth failed: {exc}") from exc
    except smtplib.SMTPException as exc:
        raise RuntimeError(f"SMTP error: {exc}") from exc
    finally:
        try:
            server.quit()
        except Exception:
            pass

    return {
        "status": "ok",
        "recipient": recipient,
    }
