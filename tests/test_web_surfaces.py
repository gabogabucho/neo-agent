import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

from lumen.channels import web
from lumen.core.catalog import Catalog
from lumen.core.connectors import Connector, ConnectorRegistry
from lumen.core.discovery import discover_all
from lumen.core.marketplace import Marketplace
from lumen.core.registry import Capability, CapabilityKind, CapabilityStatus, Registry
from lumen.core.session import SessionManager


async def _noop(**_kwargs):
    return {"ok": True}


class MemoryStub:
    def __init__(self, messages=None, *, should_fail=False):
        self.messages = list(messages or [])
        self.should_fail = should_fail
        self.calls = []

    async def load_conversation(self, session_id: str, limit: int = 50):
        self.calls.append((session_id, limit))
        if self.should_fail:
            raise RuntimeError("memory unavailable")
        return list(self.messages)


class BrainStub:
    def __init__(self, *, registry=None, connectors=None, flows=None, memory=None):
        self.registry = registry or Registry()
        self.connectors = connectors or ConnectorRegistry()
        self.flows = list(flows or [])
        self.memory = memory or MemoryStub()
        self.last_think = None
        self.think_calls = 0

    async def think(self, user_text, session):
        self.think_calls += 1
        self.last_think = {
            "user_text": user_text,
            "session_id": session.session_id,
            "history": list(session.history),
        }
        return {"message": f"Echo: {user_text}"}


class StubMarketplace(Marketplace):
    def __init__(self, *args, payloads=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.payloads = payloads or {}

    def _fetch_json(self, url: str):
        return self.payloads[url]


class WebSurfaceTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_lumen_dir = web.LUMEN_DIR
        self.original_config_path = web.CONFIG_PATH
        self.original_access_mode = web._access_mode
        self.original_brain = web._brain
        self.original_config = web._config
        self.original_locale = web._locale
        self.original_sessions = dict(web.session_manager._sessions)
        self.original_idle_timeout = web.session_manager.idle_timeout_seconds
        web.LUMEN_DIR = Path(self.temp_dir.name)
        web.CONFIG_PATH = web.LUMEN_DIR / "config.yaml"
        web.configure_access_mode("run")
        web._brain = None
        web._config = {}
        web._locale = {}
        web.session_manager._sessions.clear()
        web.session_manager.idle_timeout_seconds = 300
        self.client = TestClient(web.app)

    def tearDown(self):
        os.environ.pop("LUMEN_TEST_SETTINGS_API_KEY", None)
        self.temp_dir.cleanup()
        web.LUMEN_DIR = self.original_lumen_dir
        web.CONFIG_PATH = self.original_config_path
        web._access_mode = self.original_access_mode
        web._brain = self.original_brain
        web._config = self.original_config
        web._locale = self.original_locale
        web.session_manager._sessions.clear()
        web.session_manager._sessions.update(self.original_sessions)
        web.session_manager.idle_timeout_seconds = self.original_idle_timeout

    def test_api_status_reports_truthful_payload_shape_and_counts(self):
        connectors = ConnectorRegistry()
        task = Connector("task", "Tasks", ["create"])
        task.register_handler("create", _noop)
        connectors.register(task)
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.CHANNEL,
                name="web",
                description="Web dashboard",
                status=CapabilityStatus.READY,
            )
        )
        registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name="faq",
                description="Answers FAQs",
                status=CapabilityStatus.READY,
                provides=["faq.answer"],
                min_capability="tier-1",
            )
        )
        registry.register(
            Capability(
                kind=CapabilityKind.MCP,
                name="docs",
                description="Docs MCP",
                status=CapabilityStatus.ERROR,
                provides=["docs.search"],
                min_capability="tier-2",
            )
        )
        web._brain = BrainStub(
            registry=registry,
            connectors=connectors,
            flows=[
                {
                    "intent": "book_demo",
                    "triggers": ["book", "demo"],
                    "slots": {"email": {"required": True}, "date": {"required": True}},
                }
            ],
        )
        web._config = {"model": "demo-model", "language": "es"}

        response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "active")
        self.assertEqual(payload["model"], "demo-model")
        self.assertEqual(payload["language"], "es")
        self.assertEqual(len(payload["capabilities"]), 3)
        self.assertEqual(payload["ready"], 2)
        self.assertEqual(payload["gaps"], 1)
        self.assertEqual(payload["summary"]["channel"]["ready"], 1)
        self.assertEqual(payload["summary"]["skill"]["ready"], 1)
        self.assertEqual(payload["summary"]["mcp"]["error"], 1)
        self.assertEqual(payload["awareness"], {"pending": 0, "counts": {}, "effects": {}, "events": []})
        self.assertEqual(payload["flows"][0]["intent"], "book_demo")
        self.assertEqual(payload["flows"][0]["slots"], ["email", "date"])
        self.assertEqual(payload["capabilities"][2]["min_capability"], "tier-2")
        self.assertIn("consciousness", payload["capabilities"][0])

    def test_api_status_defaults_to_not_configured_when_brain_missing(self):
        response = self.client.get("/api/status")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "status": "not_configured",
                "version": "0.1.0",
                "model": "not configured",
                "language": "en",
                "capabilities": [],
                "summary": {},
                "awareness": {"pending": 0, "counts": {}, "effects": {}, "events": []},
                "flows": [],
                "ready": 0,
                "gaps": 0,
            },
        )

    def test_api_history_returns_persisted_messages(self):
        web._brain = BrainStub(
            memory=MemoryStub(
                [
                    {"role": "user", "content": "hola"},
                    {"role": "assistant", "content": "buenas"},
                ]
            )
        )

        response = self.client.get("/api/history/session-123")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["messages"],
            [
                {"role": "user", "content": "hola"},
                {"role": "assistant", "content": "buenas"},
            ],
        )
        self.assertEqual(web._brain.memory.calls, [("session-123", 50)])

    def test_websocket_hydrates_session_sends_response_and_cleans_up(self):
        web._brain = BrainStub(
            memory=MemoryStub(
                [
                    {"role": "user", "content": "previous question"},
                    {"role": "assistant", "content": "previous answer"},
                ]
            )
        )

        with self.client.websocket_connect("/ws/session-abc") as websocket:
            websocket.send_text(json.dumps({"content": "new question"}))

            typing_on = json.loads(websocket.receive_text())
            assistant = json.loads(websocket.receive_text())
            typing_off = json.loads(websocket.receive_text())

            self.assertEqual(typing_on, {"type": "typing", "status": True})
            self.assertEqual(assistant["type"], "message")
            self.assertEqual(assistant["role"], "assistant")
            self.assertEqual(assistant["content"], "Echo: new question")
            self.assertEqual(typing_off, {"type": "typing", "status": False})
            self.assertEqual(
                web._brain.last_think["history"],
                [
                    {"role": "user", "content": "previous question"},
                    {"role": "assistant", "content": "previous answer"},
                ],
            )
            self.assertIsNotNone(web.session_manager.get("session-abc"))

        self.assertEqual(web._brain.memory.calls, [("session-abc", 50)])
        self.assertIsNone(web.session_manager.get("session-abc"))

    def test_websocket_ping_updates_last_seen_and_skips_brain_and_history(self):
        web._brain = BrainStub()

        with self.client.websocket_connect("/ws/session-ping") as websocket:
            session = web.session_manager.get("session-ping")
            self.assertIsNotNone(session)
            initial_last_seen = session.last_seen

            time.sleep(0.02)
            websocket.send_text(json.dumps({"type": "ping"}))

            pong = json.loads(websocket.receive_text())
            self.assertEqual(pong, {"type": "pong"})

            updated_session = web.session_manager.get("session-ping")
            self.assertGreater(updated_session.last_seen, initial_last_seen)
            self.assertEqual(updated_session.history, [])
            self.assertEqual(web._brain.think_calls, 0)
            self.assertIsNone(web._brain.last_think)

    def test_serve_mode_requires_setup_token_before_showing_setup(self):
        web.configure_access_mode("serve")
        token = web.ensure_server_bootstrap(host="0.0.0.0", port=3000)

        gated = self.client.get("/setup")
        self.assertEqual(gated.status_code, 200)
        self.assertIn("One-time setup token", gated.text)

        invalid = self.client.post("/api/setup/token", json={"token": "wrong-token"})
        self.assertEqual(invalid.status_code, 401)

        unlocked = self.client.post("/api/setup/token", json={"token": token})
        self.assertEqual(unlocked.status_code, 200)
        self.assertEqual(unlocked.json()["status"], "ok")

        setup_page = self.client.get("/setup")
        self.assertEqual(setup_page.status_code, 200)
        self.assertIn("Contraseña o PIN del owner", setup_page.text)

    def test_serve_mode_requires_owner_login_for_api_and_websocket(self):
        web.configure_access_mode("serve")
        config = {
            "model": "demo-model",
            "language": "en",
            "server_mode": True,
            "server_secret": "test-server-secret",
            "owner_secret_hash": web._hash_secret("2468"),
        }
        web.CONFIG_PATH.write_text(yaml.dump(config), encoding="utf-8")
        web._config = dict(config)
        web._brain = BrainStub()

        denied = self.client.get("/api/status")
        self.assertEqual(denied.status_code, 401)

        with self.assertRaises(Exception):
            with self.client.websocket_connect("/ws/protected-session"):
                pass

        bad_login = self.client.post("/api/login", json={"secret": "wrong"})
        self.assertEqual(bad_login.status_code, 401)

        login = self.client.post("/api/login", json={"secret": "2468"})
        self.assertEqual(login.status_code, 200)
        self.assertEqual(login.json()["status"], "ok")

        allowed = self.client.get("/api/status")
        self.assertEqual(allowed.status_code, 200)
        self.assertEqual(allowed.json()["status"], "active")

        with self.client.websocket_connect("/ws/protected-session") as websocket:
            websocket.send_text(json.dumps({"type": "ping"}))
            pong = json.loads(websocket.receive_text())
            self.assertEqual(pong, {"type": "pong"})

        logout = self.client.post("/api/logout")
        self.assertEqual(logout.status_code, 200)
        denied_again = self.client.get("/api/status")
        self.assertEqual(denied_again.status_code, 401)

    def test_api_settings_merges_and_refreshes_runtime_config(self):
        config = {
            "provider": "DeepSeek",
            "model": "deepseek/deepseek-chat",
            "language": "en",
            "api_key_env": "DEEPSEEK_API_KEY",
            "api_key": "old-key",
            "mcp": {"servers": {"local": {"command": "node"}}},
        }
        web.CONFIG_PATH.write_text(yaml.dump(config), encoding="utf-8")
        web._config = dict(config)
        web._brain = type(
            "BrainStub",
            (),
            {
                "model": config["model"],
                "marketplace": type("MarketplaceStub", (), {"config": dict(config)})(),
                "module_manager": None,
            },
        )()

        with patch.object(web, "refresh_runtime_registry") as refresh_runtime_registry:
            response = self.client.post(
                "/api/settings",
                json={
                    "provider": "OpenAI",
                    "model": "gpt-4o-mini",
                    "api_key_env": "LUMEN_TEST_SETTINGS_API_KEY",
                    "api_key": "new-key",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")

        saved = yaml.safe_load(web.CONFIG_PATH.read_text(encoding="utf-8"))
        self.assertEqual(saved["provider"], "OpenAI")
        self.assertEqual(saved["model"], "gpt-4o-mini")
        self.assertEqual(saved["api_key_env"], "LUMEN_TEST_SETTINGS_API_KEY")
        self.assertEqual(saved["api_key"], "new-key")
        self.assertEqual(saved["mcp"], {"servers": {"local": {"command": "node"}}})
        self.assertEqual(web._config["provider"], "OpenAI")
        self.assertEqual(web._brain.model, "gpt-4o-mini")
        self.assertEqual(web._brain.marketplace.config["provider"], "OpenAI")
        self.assertEqual(os.environ["LUMEN_TEST_SETTINGS_API_KEY"], "new-key")
        refresh_runtime_registry.assert_called_once()

    def test_api_settings_requires_owner_auth_in_serve_mode(self):
        web.configure_access_mode("serve")
        config = {
            "provider": "DeepSeek",
            "model": "deepseek/deepseek-chat",
            "language": "en",
            "server_mode": True,
            "server_secret": "test-server-secret",
            "owner_secret_hash": web._hash_secret("2468"),
        }
        web.CONFIG_PATH.write_text(yaml.dump(config), encoding="utf-8")
        web._config = dict(config)
        web._brain = type(
            "BrainStub",
            (),
            {"model": config["model"], "marketplace": None, "module_manager": None},
        )()

        denied = self.client.post(
            "/api/settings",
            json={"provider": "OpenAI", "model": "gpt-4o-mini"},
        )
        self.assertEqual(denied.status_code, 401)

        login = self.client.post("/api/login", json={"secret": "2468"})
        self.assertEqual(login.status_code, 200)

        with patch.object(web, "refresh_runtime_registry"):
            allowed = self.client.post(
                "/api/settings",
                json={"provider": "OpenAI", "model": "gpt-4o-mini"},
            )

        self.assertEqual(allowed.status_code, 200)

    def test_openrouter_oauth_routes_require_setup_or_owner_auth_in_serve_mode(self):
        web.configure_access_mode("serve")

        token = web.ensure_server_bootstrap(host="0.0.0.0", port=3000)
        denied_setup = self.client.get(
            "/oauth/openrouter/start",
            params={"model": "deepseek/deepseek-chat:free"},
        )
        self.assertEqual(denied_setup.status_code, 401)

        unlocked = self.client.post("/api/setup/token", json={"token": token})
        self.assertEqual(unlocked.status_code, 200)

        allowed_setup = self.client.get(
            "/oauth/openrouter/start",
            params={"model": "deepseek/deepseek-chat:free"},
            follow_redirects=False,
        )
        self.assertEqual(allowed_setup.status_code, 307)

        config = {
            "provider": "OpenRouter",
            "model": "deepseek/deepseek-chat:free",
            "language": "en",
            "server_mode": True,
            "server_secret": "test-server-secret",
            "owner_secret_hash": web._hash_secret("2468"),
        }
        web.CONFIG_PATH.write_text(yaml.dump(config), encoding="utf-8")
        web._config = dict(config)
        web._oauth_state_store["state-protected"] = {
            "code_verifier": "verifier-protected",
            "model": "deepseek/deepseek-chat:free",
            "language": "en",
            "port": 3000,
            "redirect_to": "/dashboard",
            "expires_at": 9999999999,
        }

        denied_owner = self.client.get(
            "/oauth/openrouter/callback",
            params={"code": "code-protected", "state": "state-protected"},
        )
        self.assertEqual(denied_owner.status_code, 401)

        login = self.client.post("/api/login", json={"secret": "2468"})
        self.assertEqual(login.status_code, 200)

        web._oauth_state_store["state-protected-2"] = {
            "code_verifier": "verifier-protected-2",
            "model": "deepseek/deepseek-chat:free",
            "language": "en",
            "port": 3000,
            "redirect_to": "/dashboard",
            "expires_at": 9999999999,
        }

        with (
            patch.object(web, "_exchange_openrouter_code", return_value="or-key-protected"),
            patch.object(web, "_init_brain_from_config", _noop),
        ):
            allowed_owner = self.client.get(
                "/oauth/openrouter/callback",
                params={"code": "code-protected", "state": "state-protected-2"},
                follow_redirects=False,
            )

        self.assertEqual(allowed_owner.status_code, 307)


class SessionManagerTests(unittest.TestCase):
    def test_get_or_create_sets_last_seen_and_reuses_session(self):
        manager = SessionManager(idle_timeout_seconds=60)

        session = manager.get_or_create("session-1")
        first_seen = session.last_seen

        time.sleep(0.02)
        same_session = manager.get_or_create("session-1")

        self.assertIs(session, same_session)
        self.assertGreater(same_session.last_seen, first_seen)

    def test_prune_stale_removes_idle_sessions_during_normal_activity(self):
        manager = SessionManager(idle_timeout_seconds=0.01)
        stale = manager.get_or_create("stale-session")

        time.sleep(0.02)
        active = manager.get_or_create("active-session")

        self.assertIsNone(manager.get("stale-session"))
        self.assertIs(manager.get("active-session"), active)
        self.assertNotEqual(stale.session_id, active.session_id)


class CapabilityPropagationTests(unittest.TestCase):
    def test_discovery_catalog_and_marketplace_preserve_min_capability(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp)
            (pkg_dir / "modules" / "tiered-module").mkdir(parents=True)
            (pkg_dir / "modules" / "tiered-module" / "module.yaml").write_text(
                yaml.dump(
                    {
                        "name": "tiered-module",
                        "display_name": "Tiered Module",
                        "description": "Installed runtime module",
                        "version": "1.0.0",
                        "min_capability": "tier-2",
                        "tags": ["personality"],
                    }
                ),
                encoding="utf-8",
            )
            (pkg_dir / "modules" / "tiered-module" / "SKILL.md").write_text(
                "---\nname: tiered-module\ndescription: runtime skill\n---\n",
                encoding="utf-8",
            )
            catalog_path = pkg_dir / "index.yaml"
            catalog_path.write_text(
                yaml.dump(
                    {
                        "modules": [
                            {
                                "name": "catalog-tiered",
                                "display_name": "Catalog Tiered",
                                "description": "Catalog entry",
                                "min_capability": "tier-2",
                                "path": "kits/catalog-tiered",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            connectors = ConnectorRegistry()
            task = Connector("task", "Tasks", ["create"])
            task.register_handler("create", _noop)
            connectors.register(task)
            registry = discover_all(
                Registry(), pkg_dir, connectors, active_channels=["web"]
            )
            catalog = Catalog(catalog_path)
            marketplace = StubMarketplace(
                catalog=catalog,
                registry=registry,
                connectors=connectors,
                config={
                    "marketplace": {
                        "feeds": [
                            {
                                "name": "OpenClaw",
                                "url": "https://example.test/feed.json",
                            }
                        ]
                    }
                },
                payloads={
                    "https://example.test/feed.json": {
                        "skills": [
                            {
                                "name": "remote-planner",
                                "description": "Remote planning skill",
                                "connectors_required": ["task"],
                                "min_capability": "tier-3",
                            }
                        ],
                        "mcps": [
                            {
                                "name": "remote-docs",
                                "description": "Remote docs mcp",
                                "tools": ["docs.search"],
                                "min_capability": "tier-2",
                            }
                        ],
                    }
                },
            )

            catalog_items = catalog.list_all()
            snapshot = marketplace.snapshot()

        self.assertEqual(
            registry.get(CapabilityKind.MODULE, "tiered-module").metadata[
                "min_capability"
            ],
            "tier-2",
        )
        self.assertEqual(catalog_items[0]["min_capability"], "tier-2")
        self.assertEqual(snapshot["skills"]["available"][0]["min_capability"], "tier-3")
        self.assertEqual(
            snapshot["modules"]["available"][0]["min_capability"], "tier-2"
        )
        self.assertEqual(
            snapshot["kits"]["installed"][0]["min_capability"],
            "tier-2",
        )


if __name__ == "__main__":
    unittest.main()
