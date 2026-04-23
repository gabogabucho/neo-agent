"""Tests for GET /health endpoint — REQ-H1, H2, H3."""

import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from lumen.channels import web
from lumen.core.connectors import ConnectorRegistry
from lumen.core.registry import Capability, CapabilityKind, CapabilityStatus, Registry


class BrainStub:
    def __init__(self, *, registry=None):
        self.registry = registry or Registry()
        self.connectors = ConnectorRegistry()
        self.flows = []
        self.memory = None
        self.last_think = None


class HealthCheckTests(unittest.TestCase):
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
        self.client = TestClient(web.app)

    def tearDown(self):
        self.temp_dir.cleanup()
        web.LUMEN_DIR = self.original_lumen_dir
        web.CONFIG_PATH = self.original_config_path
        web._brain = self.original_brain
        web._config = self.original_config
        web._locale = self.original_locale

    # --- REQ-H1: Health response with ok + version ---

    def test_health_returns_ok_false_when_no_brain(self):
        """Brain not initialized → ok=false."""
        web._brain = None
        response = self.client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is False
        assert "version" in data

    def test_health_returns_ok_true_when_brain_initialized(self):
        """Brain initialized → ok=true."""
        web._brain = BrainStub()
        response = self.client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["ok"] is True
        assert "version" in data

    def test_health_includes_version_string(self):
        """Version matches package __version__."""
        from lumen import __version__

        response = self.client.get("/health")
        data = response.json()
        assert data["version"] == __version__

    # --- REQ-H2: Module count ---

    def test_health_includes_modules_ready_count(self):
        """modules_ready counts capabilities with kind=module and status=ready."""
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="mod-a",
                description="test",
                status=CapabilityStatus.READY,
            )
        )
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="mod-b",
                description="test",
                status=CapabilityStatus.READY,
            )
        )
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="mod-c",
                description="test",
                status=CapabilityStatus.AVAILABLE,
            )
        )
        web._brain = BrainStub(registry=registry)
        response = self.client.get("/health")
        data = response.json()
        assert data["modules_ready"] == 2

    def test_health_modules_ready_zero_when_no_brain(self):
        """No brain → modules_ready=0."""
        web._brain = None
        response = self.client.get("/health")
        data = response.json()
        assert data["modules_ready"] == 0

    # --- REQ-H3: No auth required ---

    def test_health_no_auth_required(self):
        """Health endpoint works without any cookies or tokens."""
        response = self.client.get("/health")
        assert response.status_code == 200

    def test_health_no_cookie_required(self):
        """Works even without owner cookie."""
        web._brain = BrainStub()
        # Explicitly no cookies
        response = self.client.get("/health", cookies={})
        assert response.status_code == 200
        assert response.json()["ok"] is True


if __name__ == "__main__":
    unittest.main()
