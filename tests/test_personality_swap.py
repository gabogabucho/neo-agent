import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

from fastapi.testclient import TestClient

from lumen.channels import web
from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry

class PersonalitySwapTests(unittest.TestCase):
    def setUp(self):
        self.original_config = web._config
        self.original_brain = web._brain
        self.original_merge_save = web._merge_save_config
        self.original_refresh = web.refresh_runtime_registry
        self.original_reload = web.reload_runtime_personality_surface
        self.original_manifest = web._installed_personality_manifest

        # Initial clean state
        web._config = {
            "language": "es",
            "model": "deepseek/deepseek-chat",
            "api_key": "dummy-key"
        }
        
        self.brain_mock = MagicMock()
        self.brain_mock.connectors = ConnectorRegistry()
        self.brain_mock.memory = MagicMock()
        self.brain_mock.catalog = Catalog()
        web._brain = self.brain_mock
        
        self.client = TestClient(web.app)
        
        # We will mock the config save to just update our in-memory _config dict
        def fake_merge_save(updates, removals=None):
            web._config.update(updates)
            if removals:
                for r in removals:
                    web._config.pop(r, None)
            return web._config
        
        web._merge_save_config = fake_merge_save
        web.refresh_runtime_registry = MagicMock()
        web.reload_runtime_personality_surface = MagicMock()

    def tearDown(self):
        web._config = self.original_config
        web._brain = self.original_brain
        web._merge_save_config = self.original_merge_save
        web.refresh_runtime_registry = self.original_refresh
        web.reload_runtime_personality_surface = self.original_reload
        web._installed_personality_manifest = self.original_manifest

    @patch("lumen.core.installer.Installer")
    def test_install_personality_sets_active(self, MockInstaller):
        # Mock the installer to succeed
        installer_instance = MockInstaller.return_value
        installer_instance.install_from_catalog.return_value = {"status": "installed"}

        # Mock the manifest resolution so that the web handler thinks it's a personality module
        web._installed_personality_manifest = lambda name: {"tags": ["personality"]} if name == "personality-a" else None

        response = self.client.post("/api/modules/install/personality-a")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "installed")
        self.assertEqual(web._config["active_personality"], "personality-a")
        
        # Verify provider config + memory untouched
        self.assertEqual(web._config["api_key"], "dummy-key")
        self.assertEqual(web._config["model"], "deepseek/deepseek-chat")
        
        # Verify side effects
        web.refresh_runtime_registry.assert_called_once()
        web.reload_runtime_personality_surface.assert_called_once()

    @patch("lumen.core.installer.Installer")
    def test_install_personality_clean_swap(self, MockInstaller):
        # Setup: Personality A is currently active
        web._config["active_personality"] = "personality-a"
        
        installer_instance = MockInstaller.return_value
        installer_instance.install_from_catalog.return_value = {"status": "installed"}

        # Mock the manifest resolution for personality B
        web._installed_personality_manifest = lambda name: {"tags": ["personality"]} if name == "personality-b" else None

        response = self.client.post("/api/modules/install/personality-b")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(web._config["active_personality"], "personality-b")
        
        # Verify provider config + memory untouched
        self.assertEqual(web._config["api_key"], "dummy-key")
        
        web.refresh_runtime_registry.assert_called_once()
        web.reload_runtime_personality_surface.assert_called_once()

    @patch("lumen.core.installer.Installer")
    def test_uninstall_active_personality_fallbacks_to_default(self, MockInstaller):
        # Setup: Personality B is currently active
        web._config["active_personality"] = "personality-b"
        
        installer_instance = MockInstaller.return_value
        installer_instance.uninstall.return_value = {"status": "uninstalled"}

        response = self.client.delete("/api/modules/uninstall/personality-b")

        self.assertEqual(response.status_code, 200)
        
        # Verify active_personality is removed from config (fallback to default)
        self.assertNotIn("active_personality", web._config)
        
        # Verify provider config + memory untouched
        self.assertEqual(web._config["api_key"], "dummy-key")
        self.assertEqual(web._config["model"], "deepseek/deepseek-chat")

        web.refresh_runtime_registry.assert_called_once()
        web.reload_runtime_personality_surface.assert_called_once()

    @patch("lumen.core.installer.Installer")
    def test_uninstall_inactive_personality_leaves_active_untouched(self, MockInstaller):
        # Setup: Personality B is currently active
        web._config["active_personality"] = "personality-b"
        
        installer_instance = MockInstaller.return_value
        installer_instance.uninstall.return_value = {"status": "uninstalled"}

        response = self.client.delete("/api/modules/uninstall/personality-a")

        self.assertEqual(response.status_code, 200)
        
        # Verify active_personality is still B
        self.assertEqual(web._config["active_personality"], "personality-b")
        
        # Verify we refresh registry but do NOT reload personality surface
        web.refresh_runtime_registry.assert_called_once()
        web.reload_runtime_personality_surface.assert_not_called()

class PersonalitySwapDiskTests(unittest.TestCase):
    """Validate that install/uninstall of NON-active personalities never rewrites
    provider config (api_key, model, language) on disk."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp_path = Path(self._tmpdir.name)
        self._patches = [
            patch.object(web, "LUMEN_DIR", tmp_path),
            patch.object(web, "CONFIG_PATH", tmp_path / "config.yaml"),
        ]
        for p in self._patches:
            p.start()

        self.original_brain = web._brain
        self.original_config = web._config
        self.original_refresh = web.refresh_runtime_registry
        self.original_reload = web.reload_runtime_personality_surface
        self.original_manifest = web._installed_personality_manifest
        self.original_is_installed = web._is_installed_personality_module

        web._config = {
            "language": "es",
            "provider": "deepseek",
            "model": "deepseek/deepseek-chat",
            "api_key": "sk-secret-do-not-touch",
            "active_personality": "personality-a",
        }
        web.CONFIG_PATH.write_text(
            __import__("yaml").dump(web._config, default_flow_style=False),
            encoding="utf-8",
        )

        self.brain_mock = MagicMock()
        self.brain_mock.connectors = ConnectorRegistry()
        self.brain_mock.memory = MagicMock()
        self.brain_mock.catalog = Catalog()
        web._brain = self.brain_mock

        web.refresh_runtime_registry = MagicMock()
        web.reload_runtime_personality_surface = MagicMock()
        web._installed_personality_manifest = lambda name: (
            {"tags": ["personality"]} if name in {"personality-a", "personality-b"} else None
        )
        web._is_installed_personality_module = lambda name: name in {
            "personality-a",
            "personality-b",
        }

        self.client = TestClient(web.app)

    def tearDown(self):
        web._brain = self.original_brain
        web._config = self.original_config
        web.refresh_runtime_registry = self.original_refresh
        web.reload_runtime_personality_surface = self.original_reload
        web._installed_personality_manifest = self.original_manifest
        web._is_installed_personality_module = self.original_is_installed
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()

    def _config_hash(self) -> str:
        return hashlib.sha256(web.CONFIG_PATH.read_bytes()).hexdigest()

    def _provider_snapshot(self) -> dict:
        loaded = web._load_config()
        return {k: loaded.get(k) for k in ("language", "provider", "model", "api_key")}

    @patch("lumen.core.installer.Installer")
    def test_install_inactive_personality_does_not_rewrite_provider_on_disk(self, MockInstaller):
        installer_instance = MockInstaller.return_value
        installer_instance.install_from_catalog.return_value = {"status": "installed"}

        provider_before = self._provider_snapshot()
        active_before = web._load_config().get("active_personality")

        self.client.post("/api/modules/install/personality-b")

        provider_after = self._provider_snapshot()
        self.assertEqual(provider_before, provider_after,
                         "provider config (api_key/model/language) must not change during personality swap")
        # Active personality DOES change here — that's expected; we only assert provider untouched.
        self.assertEqual(active_before, "personality-a")

    @patch("lumen.core.installer.Installer")
    def test_uninstall_inactive_personality_leaves_disk_byte_identical(self, MockInstaller):
        installer_instance = MockInstaller.return_value
        installer_instance.uninstall.return_value = {"status": "uninstalled"}

        hash_before = self._config_hash()

        # personality-a is active; uninstall personality-b (inactive)
        self.client.delete("/api/modules/uninstall/personality-b")

        hash_after = self._config_hash()
        self.assertEqual(hash_before, hash_after,
                         "config.yaml on disk must be byte-identical after uninstalling a NON-active personality")


if __name__ == "__main__":
    unittest.main()
