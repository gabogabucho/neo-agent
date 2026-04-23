"""Tests for VPS-reported bugs — Bugs #1 through #5.

Bug #1: lumen status --instance
Bug #2: reload crash list_all()
Bug #3: API keys instance path mismatch
Bug #4: web.py hardcoded LUMEN_DIR (root cause of #3)
Bug #5: README serve vs server (verified manually)
"""

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
        self.memory = MagicMock()
        self.think_calls = []

    async def think(self, message, session):
        self.think_calls.append({"message": message, "session_id": session.session_id})
        return {"message": f"Reply: {message}"}


# ── Bug #4: web.py uses instance-aware LUMEN_DIR ────────────────────────────


class Bug4WebInstanceIsolationTests(unittest.TestCase):
    """Bug #4: configure() sets LUMEN_DIR and CONFIG_PATH from instance."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_lumen_dir = web.LUMEN_DIR
        self.original_config_path = web.CONFIG_PATH
        self.original_brain = web._brain
        self.original_config = web._config
        self.original_locale = web._locale
        web._brain = None
        web._config = {}
        web._locale = {}
        web.session_manager._sessions.clear()

    def tearDown(self):
        self.temp_dir.cleanup()
        web.LUMEN_DIR = self.original_lumen_dir
        web.CONFIG_PATH = self.original_config_path
        web._brain = self.original_brain
        web._config = self.original_config
        web._locale = self.original_locale
        web.session_manager._sessions.clear()

    def test_configure_sets_lumen_dir(self):
        """configure() with lumen_dir updates global LUMEN_DIR."""
        instance_dir = Path(self.temp_dir.name) / "instances" / "test"
        instance_dir.mkdir(parents=True)
        brain_stub = BrainStub()

        web.configure(brain_stub, {}, {}, lumen_dir=instance_dir)

        assert web.LUMEN_DIR == instance_dir
        assert web.CONFIG_PATH == instance_dir / "config.yaml"

    def test_configure_without_lumen_dir_keeps_default(self):
        """configure() without lumen_dir keeps default ~/.lumen/."""
        original_dir = web.LUMEN_DIR
        brain_stub = BrainStub()

        web.configure(brain_stub, {}, {})

        assert web.LUMEN_DIR == original_dir

    def test_api_reload_uses_instance_lumen_dir(self):
        """POST /api/reload uses instance-aware LUMEN_DIR for sync."""
        os.environ["LUMEN_API_KEY"] = "test-key"
        instance_dir = Path(self.temp_dir.name) / "instances" / "test"
        instance_dir.mkdir(parents=True)

        web.configure(BrainStub(), {}, {"model": "test"}, lumen_dir=instance_dir)
        client = TestClient(web.app)

        assert web.LUMEN_DIR == instance_dir

        with patch("lumen.channels.web.sync_runtime_modules", new_callable=AsyncMock), \
             patch("lumen.channels.web.refresh_runtime_registry"), \
             patch("lumen.channels.web.reload_runtime_personality_surface"):
            response = client.post(
                "/api/reload",
                headers={"Authorization": "Bearer test-key"},
            )
            assert response.status_code == 200

        os.environ.pop("LUMEN_API_KEY", None)


# ── Bug #3: API keys with instance path ─────────────────────────────────────


class Bug3APIKeyInstancePathTests(unittest.TestCase):
    """Bug #3: API keys generated with --instance must validate in web.py."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_lumen_dir = web.LUMEN_DIR
        self.original_config_path = web.CONFIG_PATH
        self.original_brain = web._brain
        self.original_config = web._config
        self.original_locale = web._locale
        web._brain = None
        web._config = {}
        web._locale = {}
        web.session_manager._sessions.clear()

    def tearDown(self):
        self.temp_dir.cleanup()
        web.LUMEN_DIR = self.original_lumen_dir
        web.CONFIG_PATH = self.original_config_path
        web._brain = self.original_brain
        web._config = self.original_config
        web._locale = self.original_locale
        web.session_manager._sessions.clear()
        os.environ.pop("LUMEN_API_KEY", None)

    def test_api_key_generated_for_instance_validates(self):
        """Key generated with --instance validates via web auth."""
        from lumen.core.api_keys import generate_api_key, verify_api_key

        instance_dir = Path(self.temp_dir.name) / "instances" / "otto"
        instance_dir.mkdir(parents=True)
        keys_path = instance_dir / "api_keys.yaml"

        # Generate key for instance
        result = generate_api_key(label="otto-app", keys_path=keys_path)
        generated_key = result["key"]

        # Configure web with same instance dir
        brain_stub = BrainStub()
        web.configure(brain_stub, {}, {"model": "test"}, lumen_dir=instance_dir)
        client = TestClient(web.app)

        # Bearer auth should accept the instance key
        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": f"Bearer {generated_key}"},
        )
        assert response.status_code == 200

    def test_api_key_from_different_instance_rejected(self):
        """Key from instance A is rejected when web runs with instance B."""
        from lumen.core.api_keys import generate_api_key

        dir_a = Path(self.temp_dir.name) / "instances" / "a"
        dir_a.mkdir(parents=True)
        dir_b = Path(self.temp_dir.name) / "instances" / "b"
        dir_b.mkdir(parents=True)

        # Generate key for instance A
        result = generate_api_key(label="app-a", keys_path=dir_a / "api_keys.yaml")
        key_a = result["key"]

        # Configure web with instance B
        brain_stub = BrainStub()
        web.configure(brain_stub, {}, {"model": "test"}, lumen_dir=dir_b)
        client = TestClient(web.app)

        # Key from A should be rejected on B
        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": f"Bearer {key_a}"},
        )
        assert response.status_code == 401


# ── Bug #2: reload crash ────────────────────────────────────────────────────


class Bug2ReloadCrashTests(unittest.TestCase):
    """Bug #2: registry.list_all() should be registry.all()."""

    def test_registry_has_all_method(self):
        """Registry has .all() method, not .list_all()."""
        reg = Registry()
        assert hasattr(reg, "all"), "Registry should have .all() method"
        assert not hasattr(reg, "list_all"), "Registry should NOT have .list_all()"


# ── Bug #1: status --instance ────────────────────────────────────────────────


class Bug1StatusInstanceTests(unittest.TestCase):
    """Bug #1: lumen status accepts --instance flag."""

    def test_status_command_accepts_instance(self):
        """'lumen status --instance foo' does not error on unknown option."""
        from typer.testing import CliRunner
        from lumen.cli.main import app

        runner = CliRunner()
        # This should NOT fail with "No such option: --instance"
        result = runner.invoke(app, ["status", "--instance", "nonexist"])
        # It will fail because no config, but NOT because of unknown option
        assert "No such option" not in result.output
        assert result.exit_code != 0  # Expected: no config found

    def test_status_command_accepts_data_dir(self):
        """'lumen status --data-dir /tmp' does not error on unknown option."""
        from typer.testing import CliRunner
        from lumen.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["status", "--data-dir", "/tmp/nonexist"])
        assert "No such option" not in result.output


if __name__ == "__main__":
    unittest.main()
