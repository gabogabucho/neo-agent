import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import yaml
from fastapi.testclient import TestClient

from lumen.channels import web


async def _noop_init_brain():
    return True


class OpenRouterOAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.original_lumen_dir = web.LUMEN_DIR
        self.original_config_path = web.CONFIG_PATH
        self.original_brain = web._brain
        self.original_locale = web._locale
        self.original_config = web._config
        web.LUMEN_DIR = self.tmp_path
        web.CONFIG_PATH = self.tmp_path / "config.yaml"
        web._brain = None
        web._locale = {}
        web._config = {}
        web._oauth_state_store.clear()
        self.client = TestClient(web.app)

    def tearDown(self):
        web.LUMEN_DIR = self.original_lumen_dir
        web.CONFIG_PATH = self.original_config_path
        web._brain = self.original_brain
        web._locale = self.original_locale
        web._config = self.original_config
        web._oauth_state_store.clear()
        self.tmp.cleanup()

    def test_openrouter_start_redirects_with_pkce_and_stores_state(self):
        response = self.client.get(
            "/oauth/openrouter/start",
            params={
                "entry_path": "negocio",
                "language": "es",
                "model": "deepseek/deepseek-chat:free",
                "port": 4312,
            },
            follow_redirects=False,
        )

        self.assertEqual(response.status_code, 307)
        location = response.headers["location"]
        parsed = urlparse(location)
        query = parse_qs(parsed.query)

        self.assertEqual(
            f"{parsed.scheme}://{parsed.netloc}{parsed.path}", web.OPENROUTER_AUTH_URL
        )
        self.assertEqual(query["code_challenge_method"][0], "S256")
        self.assertTrue(query["code_challenge"][0])
        self.assertTrue(query["callback_url"][0].endswith("/oauth/openrouter/callback"))

        state = query["state"][0]
        stored = web._oauth_state_store[state]
        self.assertEqual(stored["entry_path"], "negocio")
        self.assertEqual(stored["language"], "es")
        self.assertEqual(stored["model"], "deepseek/deepseek-chat:free")
        self.assertEqual(stored["port"], 4312)
        self.assertTrue(stored["code_verifier"])

    def test_openrouter_callback_merge_saves_config(self):
        web.CONFIG_PATH.write_text(
            yaml.dump(
                {
                    "language": "en",
                    "port": 9999,
                    "mcp": {"servers": {"x": {}}},
                }
            ),
            encoding="utf-8",
        )
        web._oauth_state_store["state-123"] = {
            "code_verifier": "verifier-123",
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "entry_path": "uso_personal",
            "language": "es",
            "port": 3000,
            "expires_at": 9999999999,
        }

        with (
            patch.object(web, "_exchange_openrouter_code", return_value="or-key-123"),
            patch.object(
                web,
                "_init_brain_from_config",
                _noop_init_brain,
            ),
        ):
            response = self.client.get(
                "/oauth/openrouter/callback",
                params={"code": "code-123", "state": "state-123"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 307)
        self.assertEqual(response.headers["location"], "/")
        config = yaml.safe_load(web.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(config["entry_path"], "uso_personal")
        self.assertEqual(config["language"], "es")
        self.assertEqual(config["port"], 3000)
        self.assertEqual(config["model"], "meta-llama/llama-3.3-70b-instruct:free")
        self.assertEqual(config["api_key"], "or-key-123")
        self.assertEqual(config["api_key_env"], "OPENROUTER_API_KEY")
        self.assertEqual(config["mcp"], {"servers": {"x": {}}})
        self.assertNotIn("state-123", web._oauth_state_store)

    def test_api_setup_merge_preserves_unrelated_config(self):
        web.CONFIG_PATH.write_text(
            yaml.dump(
                {"mcp": {"servers": {"local": {"command": "node"}}}, "language": "en"}
            ),
            encoding="utf-8",
        )

        with patch.object(web, "_init_brain_from_config", _noop_init_brain):
            response = self.client.post(
                "/api/setup",
                json={
                    "entry_path": "desde_cero",
                    "language": "es",
                    "model": "deepseek/deepseek-chat",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "api_key": "sk-test",
                    "port": 3000,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        config = yaml.safe_load(web.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(config["entry_path"], "desde_cero")
        self.assertEqual(config["language"], "es")
        self.assertEqual(config["model"], "deepseek/deepseek-chat")
        self.assertEqual(config["api_key_env"], "DEEPSEEK_API_KEY")
        self.assertEqual(config["api_key"], "sk-test")
        self.assertEqual(config["mcp"], {"servers": {"local": {"command": "node"}}})

    def test_merge_save_config_only_writes_active_personality_when_installed(self):
        personality_dir = web.PKG_DIR / "modules" / "test-web-personality"
        personality_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = personality_dir / "module.yaml"
        created_files = [manifest_path]
        created_dirs = [personality_dir]
        manifest_path.write_text(
            yaml.dump(
                {
                    "name": "test-web-personality",
                    "tags": ["x-lumen", "personality"],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )

        try:
            merged = web._merge_save_config(
                {
                    "entry_path": "uso_personal",
                    "active_personality": "test-web-personality",
                }
            )
            self.assertEqual(merged["entry_path"], "uso_personal")
            self.assertEqual(merged["active_personality"], "test-web-personality")

            merged = web._merge_save_config(
                {
                    "entry_path": "invalid-path",
                    "active_personality": "missing-personality",
                }
            )
            self.assertEqual(merged["entry_path"], "uso_personal")
            self.assertEqual(merged["active_personality"], "test-web-personality")
        finally:
            for file_path in created_files:
                if file_path.exists():
                    file_path.unlink()
            for directory in created_dirs:
                if directory.exists():
                    directory.rmdir()


if __name__ == "__main__":
    unittest.main()
