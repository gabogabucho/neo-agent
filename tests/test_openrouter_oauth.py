import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import yaml
from fastapi.testclient import TestClient

from lumen.channels import web
from lumen.core.runtime import bootstrap_runtime


async def _noop_init_brain():
    return True


class OpenRouterOAuthTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        self.original_lumen_dir = web.LUMEN_DIR
        self.original_config_path = web.CONFIG_PATH
        self.original_pkg_dir = web.PKG_DIR
        self.original_brain = web._brain
        self.original_locale = web._locale
        self.original_config = web._config
        web.LUMEN_DIR = self.tmp_path
        web.CONFIG_PATH = self.tmp_path / "config.yaml"
        web.PKG_DIR = self.tmp_path / "pkg"
        web._brain = None
        web._locale = {}
        web._config = {}
        web._oauth_state_store.clear()
        self.client = TestClient(web.app)

    def tearDown(self):
        web.LUMEN_DIR = self.original_lumen_dir
        web.CONFIG_PATH = self.original_config_path
        web.PKG_DIR = self.original_pkg_dir
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
                    "tags": ["x-lumen", "personality", "personal"],
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

    def test_setup_personalities_filters_by_entry_path(self):
        self._write_catalog_personality(
            "x-lumen-personal",
            display_name="Personal",
            description="Personal assistant",
            tags=["x-lumen", "personality", "personal"],
        )
        self._write_catalog_personality(
            "x-lumen-negocio",
            display_name="Negocio",
            description="Business assistant",
            tags=["x-lumen", "personality", "negocio"],
        )
        self._write_personality_module(
            web.PKG_DIR / "modules" / "x-lumen-personal",
            module_name="x-lumen-personal",
            persona_name="Installed Personal",
            flow_intent="installed-personal",
            tags=["x-lumen", "personality", "personal"],
        )

        personal = self.client.get(
            "/api/setup/personalities", params={"entry_path": "uso_personal"}
        )
        negocio = self.client.get(
            "/api/setup/personalities", params={"entry_path": "negocio"}
        )
        desde_cero = self.client.get(
            "/api/setup/personalities", params={"entry_path": "desde_cero"}
        )

        self.assertEqual(personal.status_code, 200)
        self.assertEqual(negocio.status_code, 200)
        self.assertEqual(desde_cero.status_code, 200)
        self.assertEqual(
            personal.json()["modules"],
            [
                {
                    "name": "x-lumen-personal",
                    "display_name": "Personal",
                    "description": "Personal assistant",
                    "tags": ["x-lumen", "personality", "personal"],
                    "installed": True,
                }
            ],
        )
        self.assertEqual(
            negocio.json()["modules"],
            [
                {
                    "name": "x-lumen-negocio",
                    "display_name": "Negocio",
                    "description": "Business assistant",
                    "tags": ["x-lumen", "personality", "negocio"],
                    "installed": False,
                }
            ],
        )
        self.assertEqual(desde_cero.json()["modules"], [])

    def test_api_setup_installs_catalog_personality_before_saving(self):
        self._write_catalog_personality(
            "x-lumen-personal",
            display_name="Personal",
            description="Personal assistant",
            tags=["x-lumen", "personality", "personal"],
        )

        with patch.object(web, "_init_brain_from_config", _noop_init_brain):
            response = self.client.post(
                "/api/setup",
                json={
                    "entry_path": "uso_personal",
                    "active_personality": "x-lumen-personal",
                    "language": "es",
                    "model": "deepseek/deepseek-chat",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "api_key": "sk-test",
                    "port": 3000,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        saved = yaml.safe_load(web.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["active_personality"], "x-lumen-personal")
        self.assertTrue(
            (web.PKG_DIR / "modules" / "x-lumen-personal" / "module.yaml").exists()
        )

    def test_api_setup_rejects_wrong_personality_for_entry_path(self):
        self._write_catalog_personality(
            "x-lumen-negocio",
            display_name="Negocio",
            description="Business assistant",
            tags=["x-lumen", "personality", "negocio"],
        )

        with patch.object(web, "_init_brain_from_config", _noop_init_brain):
            response = self.client.post(
                "/api/setup",
                json={
                    "entry_path": "uso_personal",
                    "active_personality": "x-lumen-negocio",
                    "language": "es",
                    "model": "deepseek/deepseek-chat",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "api_key": "sk-test",
                    "port": 3000,
                },
            )

        self.assertEqual(response.status_code, 200)
        saved = yaml.safe_load(web.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["entry_path"], "uso_personal")
        self.assertNotIn("active_personality", saved)
        self.assertFalse((web.PKG_DIR / "modules" / "x-lumen-negocio").exists())

    def test_api_setup_from_scratch_clears_active_personality(self):
        self._write_personality_module(
            web.PKG_DIR / "modules" / "x-lumen-personal",
            module_name="x-lumen-personal",
            persona_name="Installed Personal",
            flow_intent="installed-personal",
            tags=["x-lumen", "personality", "personal"],
        )

        with patch.object(web, "_init_brain_from_config", _noop_init_brain):
            response = self.client.post(
                "/api/setup",
                json={
                    "entry_path": "desde_cero",
                    "active_personality": "x-lumen-personal",
                    "language": "es",
                    "model": "deepseek/deepseek-chat",
                    "api_key_env": "DEEPSEEK_API_KEY",
                    "api_key": "sk-test",
                    "port": 3000,
                },
            )

        self.assertEqual(response.status_code, 200)
        saved = yaml.safe_load(web.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["entry_path"], "desde_cero")
        self.assertNotIn("active_personality", saved)

    def test_openrouter_callback_installs_catalog_personality_before_save(self):
        self._write_catalog_personality(
            "x-lumen-negocio",
            display_name="Negocio",
            description="Business assistant",
            tags=["x-lumen", "personality", "negocio"],
        )
        web._oauth_state_store["state-456"] = {
            "code_verifier": "verifier-456",
            "model": "meta-llama/llama-3.3-70b-instruct:free",
            "entry_path": "negocio",
            "active_personality": "x-lumen-negocio",
            "language": "es",
            "port": 3000,
            "expires_at": 9999999999,
        }

        with (
            patch.object(web, "_exchange_openrouter_code", return_value="or-key-456"),
            patch.object(web, "_init_brain_from_config", _noop_init_brain),
        ):
            response = self.client.get(
                "/oauth/openrouter/callback",
                params={"code": "code-456", "state": "state-456"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 307)
        saved = yaml.safe_load(web.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["active_personality"], "x-lumen-negocio")
        self.assertTrue(
            (web.PKG_DIR / "modules" / "x-lumen-negocio" / "module.yaml").exists()
        )

    def test_uninstall_active_personality_clears_config_and_falls_back_runtime(self):
        pkg_dir = self._make_runtime_pkg()
        self._write_personality_module(
            pkg_dir / "modules" / "demo-personality",
            module_name="demo-personality",
            persona_name="Module Persona",
            flow_intent="module-onboarding",
        )

        config = {
            "language": "en",
            "model": "deepseek/deepseek-chat",
            "api_key": "sk-test",
            "api_key_env": "OPENROUTER_API_KEY",
            "active_personality": "demo-personality",
            "mcp": {"servers": {"demo": {"command": "node"}}},
        }
        web.CONFIG_PATH.write_text(yaml.dump(config, sort_keys=False), encoding="utf-8")

        runtime = asyncio.run(
            bootstrap_runtime(
                dict(config),
                pkg_dir=pkg_dir,
                lumen_dir=self.tmp_path / "runtime",
                active_channels=["web"],
            )
        )

        try:
            web.configure(runtime.brain, runtime.locale, dict(config))
            original_memory = web._brain.memory
            original_connectors = web._brain.connectors

            response = self.client.delete("/api/modules/uninstall/demo-personality")

            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()["status"], "uninstalled")

            saved = yaml.safe_load(web.CONFIG_PATH.read_text(encoding="utf-8"))
            self.assertNotIn("active_personality", saved)
            self.assertEqual(saved["model"], "deepseek/deepseek-chat")
            self.assertEqual(saved["api_key"], "sk-test")
            self.assertEqual(saved["api_key_env"], "OPENROUTER_API_KEY")
            self.assertEqual(saved["mcp"], {"servers": {"demo": {"command": "node"}}})

            self.assertNotIn("active_personality", web._config)
            self.assertEqual(
                web._brain.personality.current()["identity"]["name"],
                "Locale Lumen",
            )
            self.assertEqual(
                [flow["intent"] for flow in web._brain.flows],
                ["locale-default"],
            )
            self.assertIs(web._brain.memory, original_memory)
            self.assertIs(web._brain.connectors, original_connectors)
        finally:
            if runtime.brain.mcp_manager:
                asyncio.run(runtime.brain.mcp_manager.close())
            asyncio.run(runtime.brain.memory.close())

    def test_uninstalling_non_active_modules_keeps_active_personality(self):
        pkg_dir = self._make_runtime_pkg()
        self._write_personality_module(
            pkg_dir / "modules" / "demo-personality",
            module_name="demo-personality",
            persona_name="Module Persona",
            flow_intent="module-onboarding",
        )
        self._write_personality_module(
            pkg_dir / "modules" / "other-personality",
            module_name="other-personality",
            persona_name="Other Persona",
            flow_intent="other-onboarding",
        )
        self._write_plain_module(pkg_dir / "modules" / "tool-module", "tool-module")

        config = {
            "language": "en",
            "model": "deepseek/deepseek-chat",
            "active_personality": "demo-personality",
        }
        web.CONFIG_PATH.write_text(yaml.dump(config, sort_keys=False), encoding="utf-8")

        runtime = asyncio.run(
            bootstrap_runtime(
                dict(config),
                pkg_dir=pkg_dir,
                lumen_dir=self.tmp_path / "runtime",
                active_channels=["web"],
            )
        )

        try:
            web.configure(runtime.brain, runtime.locale, dict(config))

            inactive_response = self.client.delete(
                "/api/modules/uninstall/other-personality"
            )
            normal_response = self.client.delete("/api/modules/uninstall/tool-module")

            self.assertEqual(inactive_response.json()["status"], "uninstalled")
            self.assertEqual(normal_response.json()["status"], "uninstalled")

            saved = yaml.safe_load(web.CONFIG_PATH.read_text(encoding="utf-8"))
            self.assertEqual(saved["active_personality"], "demo-personality")
            self.assertEqual(web._config["active_personality"], "demo-personality")
            self.assertEqual(
                web._brain.personality.current()["identity"]["name"],
                "Module Persona",
            )
            self.assertEqual(
                [flow["intent"] for flow in web._brain.flows],
                ["locale-default", "module-onboarding"],
            )
        finally:
            if runtime.brain.mcp_manager:
                asyncio.run(runtime.brain.mcp_manager.close())
            asyncio.run(runtime.brain.memory.close())

    def _make_runtime_pkg(self) -> Path:
        pkg_dir = web.PKG_DIR
        (pkg_dir / "locales" / "en" / "flows").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "catalog").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "modules").mkdir(parents=True, exist_ok=True)
        (pkg_dir / "locales" / "en" / "personality.yaml").write_text(
            yaml.dump(
                {"identity": {"name": "Locale Lumen", "role": "Assistant"}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (pkg_dir / "locales" / "en" / "flows" / "default.yaml").write_text(
            yaml.dump(
                {"intent": "locale-default", "triggers": ["hello"]},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (pkg_dir / "catalog" / "index.yaml").write_text(
            yaml.dump({"modules": []}, sort_keys=False),
            encoding="utf-8",
        )
        return pkg_dir

    def _write_personality_module(
        self,
        module_dir: Path,
        *,
        module_name: str,
        persona_name: str,
        flow_intent: str,
        tags: list[str] | None = None,
    ):
        (module_dir / "flows").mkdir(parents=True, exist_ok=True)
        (module_dir / "module.yaml").write_text(
            yaml.dump(
                {
                    "name": module_name,
                    "display_name": persona_name,
                    "tags": tags or ["x-lumen", "personality"],
                    "personality": "personality.yaml",
                    "onboarding_flow": "flows/onboarding.yaml",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (module_dir / "personality.yaml").write_text(
            yaml.dump(
                {"identity": {"name": persona_name, "role": "Module Assistant"}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (module_dir / "flows" / "onboarding.yaml").write_text(
            yaml.dump(
                {"intent": flow_intent, "triggers": ["start"]},
                sort_keys=False,
            ),
            encoding="utf-8",
        )

    def _write_catalog_personality(
        self,
        module_name: str,
        *,
        display_name: str,
        description: str,
        tags: list[str],
    ):
        catalog_dir = web.PKG_DIR / "catalog"
        catalog_dir.mkdir(parents=True, exist_ok=True)
        kit_dir = catalog_dir / "kits" / module_name
        kit_dir.mkdir(parents=True, exist_ok=True)
        (kit_dir / "module.yaml").write_text(
            yaml.dump(
                {
                    "name": module_name,
                    "display_name": display_name,
                    "description": description,
                    "version": "1.0.0",
                    "tags": tags,
                    "personality": "personality.yaml",
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        (kit_dir / "personality.yaml").write_text(
            yaml.dump(
                {"identity": {"name": display_name, "role": "Assistant"}},
                sort_keys=False,
            ),
            encoding="utf-8",
        )
        index_path = catalog_dir / "index.yaml"
        current = (
            yaml.safe_load(index_path.read_text(encoding="utf-8"))
            if index_path.exists()
            else {"modules": []}
        ) or {"modules": []}
        modules = [
            entry
            for entry in current.get("modules", [])
            if entry.get("name") != module_name
        ]
        modules.append(
            {
                "name": module_name,
                "display_name": display_name,
                "description": description,
                "version": "1.0.0",
                "tags": tags,
                "path": f"kits/{module_name}",
            }
        )
        (catalog_dir / "index.yaml").write_text(
            yaml.dump({"modules": modules}, sort_keys=False),
            encoding="utf-8",
        )

    def _write_plain_module(self, module_dir: Path, module_name: str):
        module_dir.mkdir(parents=True, exist_ok=True)
        (module_dir / "module.yaml").write_text(
            yaml.dump(
                {
                    "name": module_name,
                    "display_name": "Tool Module",
                    "tags": ["tools"],
                },
                sort_keys=False,
            ),
            encoding="utf-8",
        )


if __name__ == "__main__":
    unittest.main()
