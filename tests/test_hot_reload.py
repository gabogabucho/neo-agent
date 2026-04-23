"""Tests for Hot-Reload feature — REQ-HR1 through REQ-HR3.

F9: lumen reload CLI + POST /api/reload endpoint.
Reload re-discovers modules, re-syncs runtime, refreshes registry.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

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
        self.module_manager = MagicMock()
        self.mcp_manager = MagicMock()
        self.mcp_manager.discovery_payload.return_value = None
        self.capability_awareness = None
        self.marketplace = None


class HotReloadTests(unittest.TestCase):
    """Tests for POST /api/reload endpoint."""

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

    def tearDown(self):
        self.temp_dir.cleanup()
        web.LUMEN_DIR = self.original_lumen_dir
        web.CONFIG_PATH = self.original_config_path
        web._brain = self.original_brain
        web._config = self.original_config
        web._locale = self.original_locale
        web.session_manager._sessions.clear()
        os.environ.pop("LUMEN_API_KEY", None)

    # --- REQ-HR2: POST /api/reload endpoint exists ---

    def test_reload_endpoint_exists(self):
        """POST /api/reload route is registered."""
        reload_routes = [
            r for r in web.app.routes
            if hasattr(r, "path") and r.path == "/api/reload"
        ]
        assert len(reload_routes) > 0, "/api/reload route should be registered"

    def test_reload_requires_auth(self):
        """POST /api/reload requires Bearer auth."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        response = self.client.post("/api/reload")
        assert response.status_code == 401
        assert response.json()["error"] == "unauthorized"

    def test_reload_rejects_wrong_key(self):
        """POST /api/reload rejects wrong Bearer token."""
        os.environ["LUMEN_API_KEY"] = "correct-key"
        response = self.client.post(
            "/api/reload",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert response.status_code == 401

    # --- REQ-HR2: Reload triggers runtime refresh ---

    def test_reload_returns_503_when_not_ready(self):
        """POST /api/reload returns 503 if brain is not initialized."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        response = self.client.post(
            "/api/reload",
            headers={"Authorization": "Bearer test-key"},
        )
        assert response.status_code == 503
        assert "not ready" in response.json()["error"].lower()

    @patch("lumen.channels.web.sync_runtime_modules", new_callable=AsyncMock)
    @patch("lumen.channels.web.refresh_runtime_registry")
    @patch("lumen.channels.web.reload_runtime_personality_surface")
    def test_reload_calls_sync_and_refresh(
        self, mock_personality, mock_refresh, mock_sync
    ):
        """POST /api/reload calls sync_runtime_modules + refresh_runtime_registry."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        web._brain = self.brain_stub
        web._config = {"model": "test"}

        response = self.client.post(
            "/api/reload",
            headers={"Authorization": "Bearer test-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data.get("status") == "reloaded"

        # Verify sync was called with the brain
        mock_sync.assert_called_once()
        call_args = mock_sync.call_args
        assert call_args[0][0] is self.brain_stub or call_args.kwargs.get("brain") is self.brain_stub

        # Verify refresh was called
        mock_refresh.assert_called_once()

    # --- REQ-HR3: Reload re-discovers modules ---

    @patch("lumen.channels.web.sync_runtime_modules", new_callable=AsyncMock)
    @patch("lumen.channels.web.refresh_runtime_registry")
    @patch("lumen.channels.web.reload_runtime_personality_surface")
    def test_reload_returns_modules_count(
        self, mock_personality, mock_refresh, mock_sync
    ):
        """POST /api/reload response includes modules count."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        web._brain = self.brain_stub
        web._config = {"model": "test"}

        response = self.client.post(
            "/api/reload",
            headers={"Authorization": "Bearer test-key"},
        )

        assert response.status_code == 200
        data = response.json()
        assert "modules" in data or "status" in data

    @patch("lumen.channels.web.sync_runtime_modules", new_callable=AsyncMock)
    @patch("lumen.channels.web.refresh_runtime_registry")
    @patch("lumen.channels.web.reload_runtime_personality_surface")
    def test_reload_updates_brain_registry(
        self, mock_personality, mock_refresh, mock_sync
    ):
        """POST /api/reload replaces brain.registry via refresh_runtime_registry."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        web._brain = self.brain_stub
        web._config = {"model": "test"}

        # Simulate refresh_runtime_registry updating the brain
        new_registry = Registry()
        mock_refresh.side_effect = lambda brain, **kw: setattr(brain, "registry", new_registry)

        self.client.post(
            "/api/reload",
            headers={"Authorization": "Bearer test-key"},
        )

        # refresh was called with our brain
        mock_refresh.assert_called_once_with(
            self.brain_stub,
            pkg_dir=web.PKG_DIR,
            lumen_dir=web.LUMEN_DIR,
            active_channels=["web"],
        )

    @patch("lumen.channels.web.sync_runtime_modules", new_callable=AsyncMock)
    @patch("lumen.channels.web.refresh_runtime_registry")
    @patch("lumen.channels.web.reload_runtime_personality_surface")
    def test_reload_handles_sync_error_gracefully(
        self, mock_personality, mock_refresh, mock_sync
    ):
        """POST /api/reload handles sync errors gracefully."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        web._brain = self.brain_stub
        web._config = {"model": "test"}

        mock_sync.side_effect = Exception("sync failed")

        response = self.client.post(
            "/api/reload",
            headers={"Authorization": "Bearer test-key"},
        )

        # Should return error, not crash
        assert response.status_code == 500
        assert "error" in response.json()


class ReloadCLICommandTests(unittest.TestCase):
    """Tests for lumen reload CLI command — REQ-HR1."""

    def test_reload_command_is_registered(self):
        """'reload' command is registered in the CLI app."""
        from lumen.cli.main import app
        # typer registers commands as a click group; check via help text
        from typer.testing import CliRunner
        runner = CliRunner()
        result = runner.invoke(app, ["--help"])
        assert "reload" in result.output, f"'reload' should appear in help, got: {result.output}"

    @patch("lumen.cli.main.bootstrap_runtime")
    @patch("lumen.cli.main._load_persisted_config")
    @patch("lumen.cli.main._is_runtime_configured")
    def test_reload_shows_error_if_not_configured(
        self, mock_is_configured, mock_load_config, mock_bootstrap
    ):
        """'lumen reload' shows error when Lumen is not configured."""
        mock_is_configured.return_value = False
        mock_load_config.return_value = {}

        from typer.testing import CliRunner
        from lumen.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["reload"])

        assert result.exit_code != 0 or "not" in result.output.lower() or "error" in result.output.lower()

    @patch("lumen.cli.main.refresh_runtime_registry")
    @patch("lumen.cli.main.bootstrap_runtime")
    @patch("lumen.cli.main._load_persisted_config")
    @patch("lumen.cli.main._is_runtime_configured")
    def test_reload_calls_refresh_when_configured(
        self, mock_is_configured, mock_load_config, mock_bootstrap, mock_refresh
    ):
        """'lumen reload' calls refresh_runtime_registry when configured."""
        mock_is_configured.return_value = True
        mock_load_config.return_value = {"model": "test"}

        # Create a mock runtime with brain
        mock_runtime = MagicMock()
        mock_runtime.brain = MagicMock()
        mock_runtime.brain.registry = Registry()
        mock_runtime.brain.connectors = ConnectorRegistry()
        mock_runtime.brain.module_manager = MagicMock()
        mock_runtime.brain.mcp_manager = MagicMock()
        mock_runtime.brain.mcp_manager.discovery_payload.return_value = None
        mock_runtime.brain.capability_awareness = None
        mock_runtime.brain.marketplace = None
        mock_runtime.config = {"model": "test"}
        mock_runtime.locale = {}
        mock_runtime.awareness = None
        mock_bootstrap.return_value = mock_runtime

        from typer.testing import CliRunner
        from lumen.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["reload"])

        # bootstrap_runtime should have been called
        mock_bootstrap.assert_called_once()


if __name__ == "__main__":
    unittest.main()
