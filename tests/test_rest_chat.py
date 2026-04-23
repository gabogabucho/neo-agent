"""Tests for POST /api/chat endpoint — REQ-R1 through REQ-R6."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from lumen.channels import web
from lumen.core.connectors import ConnectorRegistry
from lumen.core.registry import Registry
from lumen.core.session import SessionManager


class BrainStub:
    def __init__(self):
        self.registry = Registry()
        self.connectors = ConnectorRegistry()
        self.flows = []
        self.memory = None
        self.think_calls = []

    async def think(self, message, session):
        self.think_calls.append({"message": message, "session_id": session.session_id})
        return {"message": f"Reply: {message}"}


class MemoryStub:
    async def save_conversation_turn(self, session_id, role, content):
        pass

    async def load_conversation(self, session_id, limit=50):
        return []


class RESTChatTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_lumen_dir = web.LUMEN_DIR
        self.original_config_path = web.CONFIG_PATH
        self.original_brain = web._brain
        self.original_config = web._config
        self.original_locale = web._locale
        web.LUMEN_DIR = Path(self.temp_dir.name)
        web.CONFIG_PATH = web.LUMEN_DIR / "config.yaml"
        web._brain = None
        web._config = {}
        web._locale = {}
        web.session_manager._sessions.clear()
        self.client = TestClient(web.app)
        self.brain_stub = BrainStub()
        self.brain_stub.memory = MemoryStub()

    def tearDown(self):
        self.temp_dir.cleanup()
        web.LUMEN_DIR = self.original_lumen_dir
        web.CONFIG_PATH = self.original_config_path
        web._brain = self.original_brain
        web._config = self.original_config
        web._locale = self.original_locale
        web.session_manager._sessions.clear()
        os.environ.pop("LUMEN_API_KEY", None)

    # --- REQ-R3: Bearer Token Auth ---

    def test_chat_rejects_no_auth(self):
        """No Authorization header → 401."""
        response = self.client.post("/api/chat", json={"message": "hello"})
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"

    def test_chat_rejects_wrong_key(self):
        """Wrong Bearer token → 401."""
        os.environ["LUMEN_API_KEY"] = "correct-key"
        response = self.client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert response.status_code == 401

    def test_chat_accepts_valid_bearer(self):
        """Valid Bearer token → 200."""
        os.environ["LUMEN_API_KEY"] = "test-key-123"
        web._brain = self.brain_stub
        response = self.client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer test-key-123"},
        )
        assert response.status_code == 200

    def test_chat_accepts_key_from_config(self):
        """API key from config.api.rest_key also works."""
        os.environ.pop("LUMEN_API_KEY", None)
        web._config = {"api": {"rest_key": "config-key-456"}}
        web._brain = self.brain_stub
        response = self.client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer config-key-456"},
        )
        assert response.status_code == 200

    # --- REQ-R1: Chat Request ---

    def test_chat_returns_response_and_session_id(self):
        """POST /api/chat → {response, session_id}."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        web._brain = self.brain_stub
        response = self.client.post(
            "/api/chat",
            json={"message": "hola"},
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "response" in data
        assert "session_id" in data
        assert len(data["session_id"]) > 0

    # --- REQ-R2: Auto Session Management ---

    def test_chat_creates_session_when_missing(self):
        """No session_id → new session created."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        web._brain = self.brain_stub
        response = self.client.post(
            "/api/chat",
            json={"message": "hola"},
            headers={"Authorization": "Bearer test-key"},
        )
        data = response.json()
        session_id = data["session_id"]
        assert session_id is not None
        assert len(session_id) > 0

    def test_chat_reuses_existing_session(self):
        """Same session_id → same session."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        web._brain = self.brain_stub
        # First request — get session_id
        r1 = self.client.post(
            "/api/chat",
            json={"message": "first"},
            headers={"Authorization": "Bearer test-key"},
        )
        session_id = r1.json()["session_id"]
        # Second request — reuse session_id
        r2 = self.client.post(
            "/api/chat",
            json={"message": "second", "session_id": session_id},
            headers={"Authorization": "Bearer test-key"},
        )
        assert r2.json()["session_id"] == session_id

    # --- REQ-R6: Input Validation ---

    def test_chat_rejects_empty_message(self):
        """Empty message → 400."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        web._brain = self.brain_stub
        response = self.client.post(
            "/api/chat",
            json={"message": ""},
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 400
        assert "message" in response.json()["error"].lower()

    def test_chat_rejects_missing_message(self):
        """No message field → 400."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        web._brain = self.brain_stub
        response = self.client.post(
            "/api/chat",
            json={},
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 400

    # --- REQ-R5: WebSocket Compatibility ---

    def test_websocket_still_works(self):
        """WebSocket /ws/{session_id} route still exists after REST endpoint added."""
        # WebSocket routes only accept WS upgrades, not regular HTTP.
        # Verify route is registered by checking app routes.
        ws_routes = [r.path for r in web.app.routes if hasattr(r, "path") and "/ws/" in r.path]
        assert len(ws_routes) > 0, "WebSocket route /ws/{session_id} should still be registered"


if __name__ == "__main__":
    unittest.main()
