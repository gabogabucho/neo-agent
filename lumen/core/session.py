"""Session — per-conversation state management."""

import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Session:
    """Per-conversation state. Tracks history, active flows, and filled slots.

    Each channel connection (WebSocket, WhatsApp chat, etc.) gets its own session.
    """

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    history: list[dict] = field(default_factory=list)
    active_flow: dict | None = None
    slots: dict[str, Any] = field(default_factory=dict)

    def add_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})

    def start_flow(self, flow: dict):
        self.active_flow = flow
        self.slots = {}

    def fill_slot(self, name: str, value: Any):
        self.slots[name] = value

    def get_pending_slots(self) -> list[dict]:
        """Return required slots that haven't been filled yet."""
        if not self.active_flow:
            return []
        all_slots = self.active_flow.get("slots", {})
        return [
            {"name": name, **config}
            for name, config in all_slots.items()
            if name not in self.slots and config.get("required", False)
        ]

    def complete_flow(self):
        self.active_flow = None
        self.slots = {}


class SessionManager:
    """Manages active sessions across all channels."""

    def __init__(self):
        self._sessions: dict[str, Session] = {}

    def get_or_create(self, session_id: str | None = None) -> Session:
        if session_id and session_id in self._sessions:
            return self._sessions[session_id]
        session = Session(session_id=session_id or str(uuid.uuid4()))
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str):
        self._sessions.pop(session_id, None)
