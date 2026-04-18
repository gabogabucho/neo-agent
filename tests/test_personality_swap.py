import json
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

if __name__ == "__main__":
    unittest.main()
