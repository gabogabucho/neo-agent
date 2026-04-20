"""Session — per-conversation state management."""

import time
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
    flow_prompted: bool = False
    pending_setup_offer: dict[str, Any] | None = None
    last_seen: float = field(default_factory=time.time)

    def touch(self):
        self.last_seen = time.time()

    def add_message(self, role: str, content: str):
        self.history.append({"role": role, "content": content})

    def start_flow(self, flow: dict):
        self.active_flow = flow
        self.slots = {}
        self.flow_prompted = False
        self.pending_setup_offer = None

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
        self.flow_prompted = False
        self.pending_setup_offer = None


class SessionManager:
    """Manages active sessions across all channels."""

    def __init__(self, idle_timeout_seconds: float = 300):
        self._sessions: dict[str, Session] = {}
        self.idle_timeout_seconds = idle_timeout_seconds

    def prune_stale(self):
        now = time.time()
        stale_ids = [
            session_id
            for session_id, session in self._sessions.items()
            if now - session.last_seen > self.idle_timeout_seconds
        ]
        for session_id in stale_ids:
            self.remove(session_id)

    def get_or_create(self, session_id: str | None = None) -> Session:
        self.prune_stale()
        if session_id and session_id in self._sessions:
            session = self._sessions[session_id]
            session.touch()
            return session
        session = Session(session_id=session_id or str(uuid.uuid4()))
        session.touch()
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        self.prune_stale()
        return self._sessions.get(session_id)

    def touch(self, session_id: str) -> Session | None:
        self.prune_stale()
        session = self._sessions.get(session_id)
        if session:
            session.touch()
        return session

    def remove(self, session_id: str):
        self._sessions.pop(session_id, None)
