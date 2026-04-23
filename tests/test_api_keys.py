"""Tests for API Key Management feature — REQ-AK1 through REQ-AK6.

F11: lumen api-key generate/revoke/list per instance, SHA-256 hashing.
Keys stored in api_keys.yaml, bearer auth checks against them.
"""

import hashlib
import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

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


class APIKeysCoreTests(unittest.TestCase):
    """Tests for lumen.core.api_keys module — REQ-AK1 through REQ-AK5."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.keys_path = Path(self.temp_dir.name) / "api_keys.yaml"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_module_exists(self):
        """lumen.core.api_keys module is importable."""
        from lumen.core.api_keys import generate_api_key, list_api_keys, revoke_api_key, verify_api_key
        assert callable(generate_api_key)
        assert callable(list_api_keys)
        assert callable(revoke_api_key)
        assert callable(verify_api_key)

    # --- REQ-AK1: Generate hashed API key ---

    def test_generate_returns_plain_key_and_stores_hash(self):
        """generate_api_key returns plain key, stores only hash."""
        from lumen.core.api_keys import generate_api_key
        result = generate_api_key(label="test-app", keys_path=self.keys_path)
        assert "key" in result
        assert "prefix" in result
        assert result["label"] == "test-app"
        # Key should be reasonably long
        assert len(result["key"]) >= 32

    def test_generate_stores_sha256_hash(self):
        """Stored record contains SHA-256 hash, not plaintext."""
        from lumen.core.api_keys import generate_api_key
        import yaml
        result = generate_api_key(label="test-app", keys_path=self.keys_path)
        data = yaml.safe_load(self.keys_path.read_text())
        keys = data.get("keys", [])
        assert len(keys) == 1
        stored = keys[0]
        # Hash should match
        expected_hash = hashlib.sha256(result["key"].encode()).hexdigest()
        assert stored["key_hash"] == expected_hash
        # Plaintext key should NOT be stored
        assert result["key"] not in str(stored)

    # --- REQ-AK4: Storage format ---

    def test_generate_stores_metadata(self):
        """Stored record has label, key_hash, prefix, created_at."""
        from lumen.core.api_keys import generate_api_key
        import yaml
        result = generate_api_key(label="my app", keys_path=self.keys_path)
        data = yaml.safe_load(self.keys_path.read_text())
        keys = data.get("keys", [])
        assert len(keys) == 1
        stored = keys[0]
        assert "label" in stored
        assert "key_hash" in stored
        assert "prefix" in stored
        assert "created_at" in stored
        assert stored["label"] == "my app"

    # --- REQ-AK5: Verify key against hash ---

    def test_verify_accepts_valid_key(self):
        """verify_api_key returns True for a valid key."""
        from lumen.core.api_keys import generate_api_key, verify_api_key
        result = generate_api_key(label="test", keys_path=self.keys_path)
        assert verify_api_key(result["key"], keys_path=self.keys_path) is True

    def test_verify_rejects_invalid_key(self):
        """verify_api_key returns False for wrong key."""
        from lumen.core.api_keys import generate_api_key, verify_api_key
        generate_api_key(label="test", keys_path=self.keys_path)
        assert verify_api_key("wrong-key", keys_path=self.keys_path) is False

    def test_verify_returns_false_for_missing_file(self):
        """verify_api_key returns False when no keys file exists."""
        from lumen.core.api_keys import verify_api_key
        missing_path = Path(self.temp_dir.name) / "nonexistent.yaml"
        assert verify_api_key("any-key", keys_path=missing_path) is False

    # --- REQ-AK2: List keys ---

    def test_list_returns_key_info_without_hash(self):
        """list_api_keys shows labels and prefixes, not full keys or hashes."""
        from lumen.core.api_keys import generate_api_key, list_api_keys
        generate_api_key(label="app1", keys_path=self.keys_path)
        generate_api_key(label="app2", keys_path=self.keys_path)
        keys = list_api_keys(keys_path=self.keys_path)
        assert len(keys) == 2
        for key_info in keys:
            assert "label" in key_info
            assert "prefix" in key_info
            assert "created_at" in key_info
            assert "key_hash" not in key_info  # hash should not be exposed
            assert "key" not in key_info  # plaintext not exposed

    # --- REQ-AK3: Revoke key ---

    def test_revoke_removes_key_by_prefix(self):
        """revoke_api_key removes key matching the prefix."""
        from lumen.core.api_keys import generate_api_key, revoke_api_key, list_api_keys
        r1 = generate_api_key(label="app1", keys_path=self.keys_path)
        r2 = generate_api_key(label="app2", keys_path=self.keys_path)
        revoke_api_key(r1["prefix"], keys_path=self.keys_path)
        remaining = list_api_keys(keys_path=self.keys_path)
        assert len(remaining) == 1
        assert remaining[0]["label"] == "app2"

    def test_revoke_nonexistent_prefix_is_noop(self):
        """Revoking a prefix that doesn't exist is safe (no error)."""
        from lumen.core.api_keys import generate_api_key, revoke_api_key, list_api_keys
        generate_api_key(label="app1", keys_path=self.keys_path)
        revoke_api_key("nonexist", keys_path=self.keys_path)
        assert len(list_api_keys(keys_path=self.keys_path)) == 1

    # --- REQ-AK6: Key shown once ---

    def test_key_only_returned_at_generation(self):
        """Key is returned by generate_api_key but not stored in plaintext."""
        from lumen.core.api_keys import generate_api_key
        import yaml
        result = generate_api_key(label="once", keys_path=self.keys_path)
        file_content = self.keys_path.read_text()
        # The actual key should not appear in the file
        assert result["key"] not in file_content


class APIKeysBearerAuthTests(unittest.TestCase):
    """Tests for bearer auth checking api_keys.yaml — REQ-AK5."""

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
        self.brain_stub.memory = MagicMock()

    def tearDown(self):
        self.temp_dir.cleanup()
        web.LUMEN_DIR = self.original_lumen_dir
        web.CONFIG_PATH = self.original_config_path
        web._brain = self.original_brain
        web._config = self.original_config
        web._locale = self.original_locale
        web.session_manager._sessions.clear()
        os.environ.pop("LUMEN_API_KEY", None)

    def test_bearer_auth_accepts_api_key_from_file(self):
        """Bearer auth accepts a key generated by api-keys generate."""
        from lumen.core.api_keys import generate_api_key
        keys_path = web.LUMEN_DIR / "api_keys.yaml"
        result = generate_api_key(label="test", keys_path=keys_path)
        generated_key = result["key"]

        web._brain = self.brain_stub
        web._config = {"model": "test"}

        response = self.client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": f"Bearer {generated_key}"},
        )
        assert response.status_code == 200

    def test_bearer_auth_still_accepts_env_key(self):
        """Bearer auth still accepts LUMEN_API_KEY env var."""
        os.environ["LUMEN_API_KEY"] = "env-key-123"
        web._brain = self.brain_stub
        web._config = {"model": "test"}

        response = self.client.post(
            "/api/chat",
            json={"message": "hello"},
            headers={"Authorization": "Bearer env-key-123"},
        )
        assert response.status_code == 200


class APIKeyCLICommandTests(unittest.TestCase):
    """Tests for lumen api-key CLI commands — REQ-AK1 through REQ-AK3."""

    def test_api_key_subgroup_exists(self):
        """'api-key' CLI subgroup exists."""
        from typer.testing import CliRunner
        from lumen.cli.main import app
        runner = CliRunner()
        result = runner.invoke(app, ["api-key", "--help"])
        assert "generate" in result.output, f"'generate' should appear in api-key help, got: {result.output}"
        assert "revoke" in result.output
        assert "list" in result.output

    def test_api_key_generate_requires_label(self):
        """'lumen api-key generate' without --label shows error."""
        from typer.testing import CliRunner
        from lumen.cli.main import app
        runner = CliRunner()
        result = runner.invoke(app, ["api-key", "generate"])
        # Should show error about missing label
        assert result.exit_code != 0 or "label" in result.output.lower() or "required" in result.output.lower()

    def test_api_key_revoke_requires_prefix(self):
        """'lumen api-key revoke' without prefix shows error."""
        from typer.testing import CliRunner
        from lumen.cli.main import app
        runner = CliRunner()
        result = runner.invoke(app, ["api-key", "revoke"])
        assert result.exit_code != 0 or "prefix" in result.output.lower() or "required" in result.output.lower()


if __name__ == "__main__":
    unittest.main()
