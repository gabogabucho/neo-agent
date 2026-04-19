import tempfile
import unittest
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from lumen.channels import web
from lumen.core.catalog import Catalog
from lumen.core.connectors import Connector, ConnectorRegistry
from lumen.core.marketplace import Marketplace
from lumen.core.registry import Capability, CapabilityKind, CapabilityStatus, Registry


async def _noop(**_kwargs):
    return {"ok": True}


class StubMarketplace(Marketplace):
    def __init__(self, *args, payloads=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.payloads = payloads or {}

    def _fetch_json(self, url: str):
        return self.payloads[url]


class MarketplaceTests(unittest.TestCase):
    def setUp(self):
        self.connectors = ConnectorRegistry()
        task = Connector("task", "Tasks", ["create"])
        task.register_handler("create", _noop)
        memory = Connector("memory", "Memory", ["read"])
        memory.register_handler("read", _noop)
        self.connectors.register(task)
        self.connectors.register(memory)

        self.registry = Registry()
        self.registry.register(
            Capability(
                kind=CapabilityKind.CHANNEL,
                name="web",
                description="Web dashboard",
                status=CapabilityStatus.READY,
            )
        )
        self.registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name="notify",
                description="Notify users",
                status=CapabilityStatus.READY,
                provides=["task.create"],
                requires={"connectors": ["task"]},
                metadata={
                    "cerebelo": {
                        "status": "ready",
                        "reasons": [],
                        "warnings": [],
                    }
                },
            )
        )

    def test_marketplace_snapshot_merges_runtime_catalog_and_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "index.yaml"
            catalog_path.write_text(
                yaml.dump(
                    {
                        "modules": [
                            {
                                "name": "scheduler",
                                "display_name": "Scheduler",
                                "description": "Schedules reminders.",
                                "version": "1.0.0",
                                "path": "kits/scheduler",
                                "price": "free",
                                "min_capability": "tier-2",
                                "requires": {
                                    "connectors": ["task", "memory"],
                                    "channels": ["web"],
                                },
                                "tags": ["productivity"],
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = Catalog(catalog_path)
            marketplace = StubMarketplace(
                catalog=catalog,
                registry=self.registry,
                connectors=self.connectors,
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
                                "name": "notify",
                                "description": "Remote copy of notify",
                                "connectors_required": ["task"],
                            },
                            {
                                "name": "planner",
                                "description": "Plan tasks",
                                "connectors_required": ["task", "memory"],
                            },
                        ],
                        "mcps": [
                            {
                                "name": "docs-mcp",
                                "description": "Documentation MCP",
                                "tools": ["docs.search"],
                            }
                        ],
                    }
                },
            )

            snapshot = marketplace.snapshot()

        self.assertEqual(snapshot["skills"]["counts"]["installed"], 1)
        self.assertEqual(snapshot["skills"]["counts"]["available"], 1)
        self.assertEqual(snapshot["skills"]["items"][0]["name"], "notify")
        self.assertEqual(
            snapshot["skills"]["items"][0]["interoperability"]["level"], "adapted"
        )
        self.assertEqual(
            snapshot["skills"]["items"][0]["interoperability"]["label"], "Adapted"
        )
        self.assertEqual(
            snapshot["skills"]["items"][0]["compatibility"]["badge"]["emoji"], "🟢"
        )
        self.assertEqual(snapshot["skills"]["available"][0]["name"], "planner")
        self.assertEqual(
            snapshot["skills"]["available"][0]["compatibility"]["status"], "installable"
        )
        self.assertEqual(
            snapshot["modules"]["available"][0]["interoperability"]["level"], "opaque"
        )
        self.assertEqual(snapshot["modules"]["available"][0]["name"], "docs-mcp")
        self.assertTrue(snapshot["modules"]["available"][0]["actions"]["read_only"])
        self.assertEqual(snapshot["kits"]["available"][0]["name"], "scheduler")
        self.assertEqual(
            snapshot["kits"]["available"][0]["interoperability"]["level"], "native"
        )
        self.assertEqual(snapshot["kits"]["available"][0]["path"], "kits/scheduler")
        self.assertTrue(snapshot["kits"]["available"][0]["actions"]["can_install"])
        self.assertEqual(
            snapshot["kits"]["available"][0]["compatibility"]["status"],
            "installable",
        )

    def test_marketplace_api_returns_aggregated_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "index.yaml"
            catalog_path.write_text(yaml.dump({"modules": []}), encoding="utf-8")
            catalog = Catalog(catalog_path)
            marketplace = StubMarketplace(
                catalog=catalog,
                registry=self.registry,
                connectors=self.connectors,
                config={},
            )

            original_brain = web._brain
            web._brain = type(
                "BrainStub",
                (),
                {
                    "marketplace": marketplace,
                    "registry": self.registry,
                    "catalog": catalog,
                    "connectors": self.connectors,
                    "memory": None,
                    "flows": [],
                },
            )()
            try:
                client = TestClient(web.app)
                response = client.get("/api/marketplace")
            finally:
                web._brain = original_brain

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("skills", payload)
        self.assertIn("kits", payload)
        self.assertIn("modules", payload)
        self.assertEqual(payload["skills"]["items"][0]["name"], "notify")

    def test_marketplace_surfaces_advisory_model_tier_warnings_without_blocking(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "index.yaml"
            catalog_path.write_text(
                yaml.dump(
                    {
                        "modules": [
                            {
                                "name": "deep-research",
                                "display_name": "Deep Research",
                                "description": "Tiered module.",
                                "min_capability": "tier-3",
                                "path": "kits/deep-research",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = Catalog(catalog_path)
            marketplace = StubMarketplace(
                catalog=catalog,
                registry=self.registry,
                connectors=self.connectors,
                config={"model": "deepseek/deepseek-chat"},
            )

            snapshot = marketplace.snapshot()

        module_card = snapshot["kits"]["available"][0]
        self.assertEqual(module_card["compatibility"]["status"], "installable")
        self.assertIn(
            "recommends tier-3",
            module_card["compatibility"]["warnings"][0],
        )

    def test_kits_personalities_sort_before_generic_kits(self):
        with tempfile.TemporaryDirectory() as tmp:
            catalog_path = Path(tmp) / "index.yaml"
            catalog_path.write_text(
                yaml.dump(
                    {
                        "modules": [
                            {
                                "name": "zeta-generic",
                                "display_name": "A Generic Module",
                                "description": "Generic module.",
                                "path": "kits/zeta-generic",
                                "tags": ["productivity"],
                            },
                            {
                                "name": "alpha-personality",
                                "display_name": "Z Personality Pack",
                                "description": "Personality module.",
                                "path": "kits/alpha-personality",
                                "tags": ["personality", "x-lumen"],
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            catalog = Catalog(catalog_path)
            marketplace = Marketplace(
                catalog=catalog,
                registry=self.registry,
                connectors=self.connectors,
                config={},
            )

            snapshot = marketplace.snapshot()

        self.assertEqual(snapshot["tabs"][0]["key"], "modules")
        self.assertEqual(snapshot["tabs"][1]["key"], "kits")
        self.assertEqual(snapshot["tabs"][1]["label"], "Kits")
        self.assertEqual(
            [item["name"] for item in snapshot["kits"]["available"]],
            ["alpha-personality", "zeta-generic"],
        )
        self.assertEqual(
            [item["name"] for item in marketplace.kits_catalog()],
            ["alpha-personality", "zeta-generic"],
        )
        self.assertIn("kits", snapshot)
        self.assertIn("available", snapshot["kits"])
        self.assertIn("installed", snapshot["kits"])

    def test_dashboard_template_defaults_marketplace_to_modules(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "lumen"
            / "channels"
            / "templates"
            / "dashboard.html"
        ).read_text(encoding="utf-8")

        self.assertIn("let currentMarketplaceTab = 'modules';", template)
        self.assertIn('id="tab-modules"', template)
        self.assertLess(
            template.index('id="tab-modules"'),
            template.index('id="tab-kits"'),
        )
        self.assertLess(
            template.index('id="tab-kits"'),
            template.index('id="tab-skills"'),
        )
        self.assertIn('id="settings-form"', template)
        self.assertIn('id="openrouter-connect-button"', template)
        self.assertIn("redirect_to=%2Fdashboard", template)
        self.assertIn("fetch('/api/settings'", template)
        self.assertIn("function renderInteroperabilityBadge(item)", template)
        self.assertIn("function renderInteroperabilityMeta(item)", template)
        self.assertIn("Adopción", template)

    def test_dashboard_renders_configured_active_personality(self):
        original_brain = web._brain
        original_config = web._config
        original_locale = web._locale
        original_has_config = web._has_config
        original_init_brain = web._init_brain_from_config

        web._config = {
            "language": "en",
            "model": "demo-model",
            "active_personality": "demo-personality",
        }
        web._locale = {}
        web._brain = type(
            "BrainStub",
            (),
            {
                "connectors": type("ConnectorsStub", (), {"list": lambda self: []})(),
                "flows": [],
                "registry": type(
                    "RegistryStub",
                    (),
                    {"list_by_kind": lambda self, kind: []},
                )(),
                "personality": type(
                    "PersonalityStub",
                    (),
                    {"current": lambda self: {"identity": {"name": "Runtime Lumen"}}},
                )(),
            },
        )()

        async def _noop_init():
            return True

        web._has_config = lambda: True
        web._init_brain_from_config = _noop_init

        try:
            client = TestClient(web.app)
            response = client.get("/dashboard")
        finally:
            web._brain = original_brain
            web._config = original_config
            web._locale = original_locale
            web._has_config = original_has_config
            web._init_brain_from_config = original_init_brain

        self.assertEqual(response.status_code, 200)
        self.assertIn("Demo Personality", response.text)
        self.assertIn("Conectar OpenRouter", response.text)

    def test_dashboard_falls_back_to_runtime_personality_name_when_no_active_module(
        self,
    ):
        original_brain = web._brain
        original_config = web._config
        original_locale = web._locale
        original_has_config = web._has_config
        original_init_brain = web._init_brain_from_config

        web._config = {
            "language": "en",
            "model": "demo-model",
        }
        web._locale = {}
        web._brain = type(
            "BrainStub",
            (),
            {
                "connectors": type("ConnectorsStub", (), {"list": lambda self: []})(),
                "flows": [],
                "registry": type(
                    "RegistryStub",
                    (),
                    {"list_by_kind": lambda self, kind: []},
                )(),
                "personality": type(
                    "PersonalityStub",
                    (),
                    {"current": lambda self: {"identity": {"name": "Locale Lumen"}}},
                )(),
            },
        )()

        async def _noop_init():
            return True

        web._has_config = lambda: True
        web._init_brain_from_config = _noop_init

        try:
            client = TestClient(web.app)
            response = client.get("/dashboard")
        finally:
            web._brain = original_brain
            web._config = original_config
            web._locale = original_locale
            web._has_config = original_has_config
            web._init_brain_from_config = original_init_brain

        self.assertEqual(response.status_code, 200)
        self.assertIn("Locale Lumen", response.text)
        self.assertIn("id=\"openrouter-connect-button\"", response.text)


if __name__ == "__main__":
    unittest.main()
