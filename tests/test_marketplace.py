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
            snapshot["skills"]["items"][0]["compatibility"]["badge"]["emoji"], "🟢"
        )
        self.assertEqual(snapshot["skills"]["available"][0]["name"], "planner")
        self.assertEqual(
            snapshot["skills"]["available"][0]["compatibility"]["status"], "installable"
        )
        self.assertEqual(snapshot["mcps"]["available"][0]["name"], "docs-mcp")
        self.assertTrue(snapshot["mcps"]["available"][0]["actions"]["read_only"])
        self.assertEqual(snapshot["kits_lumen"]["available"][0]["name"], "scheduler")
        self.assertTrue(
            snapshot["kits_lumen"]["available"][0]["actions"]["can_install"]
        )
        self.assertEqual(
            snapshot["kits_lumen"]["available"][0]["compatibility"]["status"],
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
        self.assertIn("kits_lumen", payload)
        self.assertEqual(payload["skills"]["items"][0]["name"], "notify")


if __name__ == "__main__":
    unittest.main()
