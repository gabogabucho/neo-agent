"""Tests for Bug #6 — _config loses api section, REST auth fails.

The config.yaml has api.rest_key but after bootstrap_runtime the _config
may not have it. _validate_bearer_token must read CONFIG_PATH directly.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import yaml
from fastapi.testclient import TestClient

from lumen.channels import web
from lumen.core.connectors import ConnectorRegistry
from lumen.core.registry import Registry


class BrainStub:
    def __init__(self):
        self.registry = Registry()
        self.connectors = ConnectorRegistry()
        self.flows = []
        self.memory = MagicMock()
        self.think_calls = []
        self.confirmation_gate = MagicMock()

    async def think(self, message, session):
        self.think_calls.append({"message": message, "session_id": session.session_id})
        return {"message": f"Reply: {message}"}


class Bug6RestKeyAuthTests(unittest.TestCase):
    """Bug #6: REST auth works even when _config doesn't have api section."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lumen_dir = Path(self.temp_dir.name)
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

    def _setup_with_config_yaml(self, config_yaml: str, _config_in_memory: dict | None = None):
        """Helper: write config.yaml and configure web with optional stripped _config."""
        config_path = self.lumen_dir / "config.yaml"
        config_path.write_text(config_yaml, encoding="utf-8")

        brain_stub = BrainStub()
        web.configure(brain_stub, {}, _config_in_memory or {}, lumen_dir=self.lumen_dir)
        return TestClient(web.app)

    def test_rest_key_in_config_yaml_works_when_config_has_api(self):
        """REST auth works when _config has api section (happy path)."""
        config_yaml = yaml.dump({
            "model": "deepseek/deepseek-chat",
            "api_key": "test",
            "language": "es",
            "api": {"rest_key": "my-secret-key"},
        })
        full_config = yaml.safe_load(config_yaml)
        client = self._setup_with_config_yaml(config_yaml, full_config)

        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer my-secret-key"},
        )
        assert response.status_code == 200

    def test_rest_key_in_config_yaml_works_when_config_lacks_api(self):
        """REST auth works even when _config does NOT have api section (Bug #6)."""
        config_yaml = yaml.dump({
            "model": "deepseek/deepseek-chat",
            "api_key": "test",
            "language": "es",
            "api": {"rest_key": "my-secret-key"},
        })
        # Simulate bootstrap_runtime stripping api section
        stripped_config = {
            "model": "deepseek/deepseek-chat",
            "api_key": "test",
            "language": "es",
            # NO api section!
        }
        client = self._setup_with_config_yaml(config_yaml, stripped_config)

        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer my-secret-key"},
        )
        assert response.status_code == 200

    def test_rest_key_from_yaml_rejects_wrong_key(self):
        """Wrong key is rejected even when reading from CONFIG_PATH."""
        config_yaml = yaml.dump({
            "model": "deepseek/deepseek-chat",
            "api_key": "test",
            "language": "es",
            "api": {"rest_key": "my-secret-key"},
        })
        stripped_config = {"model": "deepseek/deepseek-chat"}
        client = self._setup_with_config_yaml(config_yaml, stripped_config)

        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert response.status_code == 401

    def test_rest_key_works_with_instance(self):
        """REST auth works with --instance (CONFIG_PATH points to instance dir)."""
        instance_dir = self.lumen_dir / "instances" / "test"
        instance_dir.mkdir(parents=True)
        config_path = instance_dir / "config.yaml"
        config_path.write_text(yaml.dump({
            "model": "deepseek/deepseek-chat",
            "api_key": "test",
            "language": "es",
            "api": {"rest_key": "instance-key-123"},
        }), encoding="utf-8")

        brain_stub = BrainStub()
        web.configure(brain_stub, {}, {"model": "deepseek/deepseek-chat"}, lumen_dir=instance_dir)
        client = TestClient(web.app)

        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer instance-key-123"},
        )
        assert response.status_code == 200

    def test_no_config_yaml_still_checks_env_var(self):
        """Without config.yaml, env var LUMEN_API_KEY still works."""
        client = self._setup_with_config_yaml("", {})
        os.environ["LUMEN_API_KEY"] = "env-key"

        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer env-key"},
        )
        assert response.status_code == 200

    def test_api_keys_yaml_works_alongside_rest_key(self):
        """API keys from api_keys.yaml work alongside rest_key in config."""
        from lumen.core.api_keys import generate_api_key

        config_yaml = yaml.dump({
            "model": "deepseek/deepseek-chat",
            "api_key": "test",
            "language": "es",
            "api": {"rest_key": "config-key"},
        })
        keys_path = self.lumen_dir / "api_keys.yaml"
        result = generate_api_key(label="test-app", keys_path=keys_path)

        client = self._setup_with_config_yaml(config_yaml, {"model": "deepseek/deepseek-chat"})

        # Generated key should work
        response = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": f"Bearer {result['key']}"},
        )
        assert response.status_code == 200

        # Config rest_key should also work
        response2 = client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer config-key"},
        )
        assert response2.status_code == 200


if __name__ == "__main__":
    unittest.main()
