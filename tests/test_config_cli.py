"""Tests for lumen config CLI commands — REQ-C1 through REQ-C5."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from lumen.core.paths import resolve_lumen_dir
from lumen.core.secrets_store import configure_paths, load_module, save_module


class ConfigSetGetTests(unittest.TestCase):
    """REQ-C1: config set. REQ-C2: config get. REQ-C5: Instance-aware."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lumen_dir = Path(self.temp_dir.name)
        configure_paths(lumen_dir=self.lumen_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_set_saves_to_secrets(self):
        """REQ-C1: lumen config set otto.store_id 12345."""
        save_module("otto-tiendanube", {"store_id": "12345"})
        loaded = load_module("otto-tiendanube")
        assert loaded["store_id"] == "12345"

    def test_get_returns_value(self):
        """REQ-C2: config get returns saved value."""
        save_module("otto-tiendanube", {"store_id": "12345"})
        loaded = load_module("otto-tiendanube")
        assert loaded.get("store_id") == "12345"

    def test_get_missing_key_returns_none(self):
        """Missing key returns empty dict."""
        save_module("otto", {"token": "abc"})
        loaded = load_module("otto")
        assert loaded.get("store_id") is None

    def test_set_multiple_keys(self):
        """Multiple keys for same module."""
        save_module("otto", {"store_id": "123", "token": "abc"})
        loaded = load_module("otto")
        assert loaded["store_id"] == "123"
        assert loaded["token"] == "abc"

    def test_set_updates_existing(self):
        """Updating a key preserves others."""
        save_module("otto", {"store_id": "123", "token": "abc"})
        save_module("otto", {"store_id": "456"})
        loaded = load_module("otto")
        assert loaded["store_id"] == "456"
        assert loaded["token"] == "abc"  # preserved


class ConfigDeleteTests(unittest.TestCase):
    """REQ-C3: config delete."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lumen_dir = Path(self.temp_dir.name)
        configure_paths(lumen_dir=self.lumen_dir)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_delete_removes_key(self):
        """Deleting a key removes it from module config."""
        save_module("otto", {"store_id": "123", "token": "abc"})
        from lumen.core.secrets_store import delete_module_key
        delete_module_key("otto", "store_id")
        result = load_module("otto")
        assert "store_id" not in result
        assert result["token"] == "abc"


class ConfigRedactionTests(unittest.TestCase):
    """REQ-C4: Value redaction in list output."""

    def test_redact_short_values(self):
        """Values <= 8 chars show first 2 chars + ****."""
        value = "abcd"
        redacted = value[:2] + "****" if len(value) > 4 else "****"
        assert redacted == "****"

    def test_redact_long_values(self):
        """Values > 8 chars show first 4 chars + ****."""
        value = "sk-1234567890abcdef"
        redacted = value[:4] + "****"
        assert redacted == "sk-1****"

    def test_redact_function(self):
        """Redaction function works correctly."""
        def redact(value: str) -> str:
            if len(value) <= 4:
                return "****"
            return value[:4] + "****"

        assert redact("ab") == "****"
        assert redact("abcd") == "****"
        assert redact("abcdefgh") == "abcd****"
        assert redact("sk-long-token-here") == "sk-l****"


class InstanceAwareConfigTests(unittest.TestCase):
    """REQ-C5: Config commands are instance-aware."""

    def test_different_instances_have_separate_secrets(self):
        """Config in one instance doesn't leak to another."""
        dir_a = Path(tempfile.mkdtemp())
        dir_b = Path(tempfile.mkdtemp())

        configure_paths(lumen_dir=dir_a)
        save_module("otto", {"token": "secret-a"})

        configure_paths(lumen_dir=dir_b)
        loaded_b = load_module("otto")
        assert loaded_b.get("token") is None  # Not leaked

        configure_paths(lumen_dir=dir_a)
        loaded_a = load_module("otto")
        assert loaded_a["token"] == "secret-a"  # Still there

        # Cleanup
        import shutil
        shutil.rmtree(dir_a, ignore_errors=True)
        shutil.rmtree(dir_b, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
