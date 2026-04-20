import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from lumen.core.connectors import ConnectorRegistry
from lumen.core.installer import Installer
from lumen.core.catalog import Catalog
from lumen.core.registry import CapabilityKind
from lumen.core.runtime import bootstrap_runtime, refresh_runtime_registry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = PROJECT_ROOT / "lumen"
FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


class MCPRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_bootstrap_uses_locale_personality_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = await bootstrap_runtime(
                {"language": "en", "model": "deepseek/deepseek-chat"},
                pkg_dir=self._make_runtime_pkg(Path(tmp)),
                lumen_dir=Path(tmp) / "runtime",
                active_channels=["web"],
            )

            try:
                self.assertEqual(
                    runtime.brain.personality.current()["identity"]["name"],
                    "Locale Lumen",
                )
                self.assertEqual(
                    [flow["intent"] for flow in runtime.brain.flows],
                    ["locale-default"],
                )
            finally:
                await runtime.brain.memory.close()

    async def test_bootstrap_loads_pending_module_setup_flows(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = self._make_runtime_pkg(Path(tmp))
            module_dir = pkg_dir / "modules" / "pending-module"
            module_dir.mkdir(parents=True)
            (module_dir / "module.yaml").write_text(
                yaml.dump(
                    {
                        "name": "pending-module",
                        "display_name": "Pending Module",
                        "description": "Needs setup",
                        "x-lumen": {
                            "runtime": {
                                "env": [
                                    {"name": "DEMO_TOKEN", "secret": True},
                                ]
                            }
                        },
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            runtime = await bootstrap_runtime(
                {"language": "en", "model": "deepseek/deepseek-chat"},
                pkg_dir=pkg_dir,
                lumen_dir=Path(tmp) / "runtime",
                active_channels=["web"],
            )

            try:
                self.assertEqual(
                    [flow["intent"] for flow in runtime.brain.flows],
                    ["locale-default", "module-setup-pending-module"],
                )
            finally:
                await runtime.brain.memory.close()

    async def test_bootstrap_loads_pending_mcp_setup_flows(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = await bootstrap_runtime(
                {
                    "language": "en",
                    "model": "deepseek/deepseek-chat",
                    "mcp": {
                        "servers": {
                            "github": {
                                "command": sys.executable,
                                "args": [str(FAKE_SERVER)],
                                "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
                            }
                        }
                    },
                },
                pkg_dir=self._make_runtime_pkg(Path(tmp)),
                lumen_dir=Path(tmp) / "runtime",
                active_channels=["web"],
            )

            try:
                assert [flow["intent"] for flow in runtime.brain.flows] == [
                    "locale-default",
                    "artifact-setup-mcp-github",
                ]
                assert runtime.brain.registry.get(CapabilityKind.MCP, "github").metadata[
                    "pending_setup"
                ]["env_specs"][0]["name"] == "GITHUB_PERSONAL_ACCESS_TOKEN"
            finally:
                await runtime.brain.memory.close()

    async def test_bootstrap_uses_active_personality_module_when_valid(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = self._make_runtime_pkg(Path(tmp))
            module_dir = pkg_dir / "modules" / "demo-personality"
            (module_dir / "flows").mkdir(parents=True)
            (module_dir / "module.yaml").write_text(
                yaml.dump(
                    {
                        "name": "demo-personality",
                        "display_name": "Demo Personality",
                        "description": "Overrides locale personality",
                        "tags": ["x-lumen", "personality"],
                        "personality": "personality.yaml",
                        "onboarding_flow": "flows/onboarding.yaml",
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (module_dir / "personality.yaml").write_text(
                yaml.dump(
                    {
                        "identity": {
                            "name": "Module Persona",
                            "role": "Module Assistant",
                        }
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (module_dir / "flows" / "onboarding.yaml").write_text(
                yaml.dump(
                    {
                        "intent": "module-onboarding",
                        "triggers": ["start setup"],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            runtime = await bootstrap_runtime(
                {
                    "language": "en",
                    "model": "deepseek/deepseek-chat",
                    "active_personality": "demo-personality",
                },
                pkg_dir=pkg_dir,
                lumen_dir=Path(tmp) / "runtime",
                active_channels=["web"],
            )

            try:
                self.assertEqual(
                    runtime.brain.personality.current()["identity"]["name"],
                    "Module Persona",
                )
                self.assertEqual(
                    [flow["intent"] for flow in runtime.brain.flows],
                    ["locale-default", "module-onboarding"],
                )
            finally:
                await runtime.brain.memory.close()

    async def test_bootstrap_falls_back_when_active_personality_is_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = self._make_runtime_pkg(Path(tmp))
            module_dir = pkg_dir / "modules" / "not-a-personality"
            module_dir.mkdir(parents=True)
            (module_dir / "module.yaml").write_text(
                yaml.dump(
                    {
                        "name": "not-a-personality",
                        "display_name": "Tool Module",
                        "description": "Missing personality tag",
                        "tags": ["tools"],
                        "personality": "personality.yaml",
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (module_dir / "personality.yaml").write_text(
                yaml.dump(
                    {"identity": {"name": "Should Not Load", "role": "Ignored"}},
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            runtime = await bootstrap_runtime(
                {
                    "language": "en",
                    "model": "deepseek/deepseek-chat",
                    "active_personality": "not-a-personality",
                },
                pkg_dir=pkg_dir,
                lumen_dir=Path(tmp) / "runtime",
                active_channels=["web"],
            )

            try:
                self.assertEqual(
                    runtime.brain.personality.current()["identity"]["name"],
                    "Locale Lumen",
                )
                self.assertEqual(
                    [flow["intent"] for flow in runtime.brain.flows],
                    ["locale-default"],
                )
            finally:
                await runtime.brain.memory.close()

    async def test_catalog_install_uses_entry_path_and_preserves_catalog_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            pkg_dir = temp_root / "pkg"

            catalog_dir = pkg_dir / "catalog"
            (catalog_dir / "kits" / "demo-personality").mkdir(parents=True)
            (catalog_dir / "kits" / "demo-personality" / "module.yaml").write_text(
                yaml.dump(
                    {
                        "name": "demo-personality",
                        "display_name": "Demo Personality",
                        "description": "Catalog path aware install",
                        "version": "2.0.0",
                        "tags": ["x-lumen", "personality"],
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )
            (catalog_dir / "kits" / "demo-personality" / "SKILL.md").write_text(
                "# Demo Personality\n",
                encoding="utf-8",
            )
            (catalog_dir / "index.yaml").write_text(
                yaml.dump(
                    {
                        "modules": [
                            {
                                "name": "demo-personality",
                                "display_name": "Demo Personality",
                                "description": "Catalog path aware install",
                                "version": "2.0.0",
                                "tags": ["x-lumen", "personality"],
                                "path": "kits/demo-personality",
                            }
                        ]
                    },
                    sort_keys=False,
                ),
                encoding="utf-8",
            )

            catalog = Catalog(catalog_dir / "index.yaml")
            installer = Installer(
                pkg_dir,
                ConnectorRegistry(),
                memory=None,
                catalog=catalog,
            )

            install_result = installer.install_from_catalog("demo-personality")
            installed = installer.list_installed()
            installed_manifest_exists = (
                pkg_dir / "modules" / "demo-personality" / "module.yaml"
            ).exists()

        self.assertEqual(install_result["status"], "installed")
        self.assertTrue(installed_manifest_exists)
        self.assertEqual(catalog.list_all()[0]["path"], "kits/demo-personality")
        self.assertEqual(
            catalog.get("demo-personality")["path"], "kits/demo-personality"
        )
        self.assertEqual(catalog.list_all()[0]["tags"], ["x-lumen", "personality"])
        self.assertEqual(installed[0]["tags"], ["x-lumen", "personality"])

    async def test_bootstrap_registers_stdio_mcp_tools(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = await bootstrap_runtime(
                {
                    "language": "en",
                    "model": "deepseek/deepseek-chat",
                    "mcp": {
                        "servers": {
                            "demo": {
                                "transport": "stdio",
                                "command": sys.executable,
                                "args": [str(FAKE_SERVER)],
                                "description": "Demo MCP server",
                            }
                        }
                    },
                },
                pkg_dir=PKG_DIR,
                lumen_dir=Path(temp_dir),
                active_channels=["web"],
            )

            await runtime.brain.memory.init()
            try:
                tool_names = [
                    tool["function"]["name"]
                    for tool in runtime.brain.connectors.as_tools()
                ]
                self.assertIn("mcp__demo__ping", tool_names)

                mcp_cap = runtime.brain.registry.get(CapabilityKind.MCP, "demo")
                self.assertIsNotNone(mcp_cap)
                self.assertEqual(mcp_cap.status.value, "ready")
                self.assertEqual(mcp_cap.metadata.get("tools"), ["ping"])

                result = await runtime.brain.connectors.execute_tool(
                    "mcp__demo__ping",
                    {"message": "hello"},
                )
                self.assertEqual(result["server"], "demo")
                self.assertEqual(result["tool"], "ping")
                self.assertEqual(result["text"], "ping:hello")
            finally:
                if runtime.brain.mcp_manager:
                    await runtime.brain.mcp_manager.close()
                await runtime.brain.memory.close()

    async def test_runtime_refresh_preserves_mcp_truth_and_syncs_marketplace(self):
        with tempfile.TemporaryDirectory() as tmp:
            temp_root = Path(tmp)
            pkg_dir = temp_root / "pkg"
            lumen_dir = temp_root / "runtime"

            (pkg_dir / "locales" / "en").mkdir(parents=True)
            (pkg_dir / "catalog").mkdir(parents=True)
            (pkg_dir / "locales" / "en" / "personality.yaml").write_text(
                yaml.dump({"identity": {"name": "Lumen", "role": "Assistant"}}),
                encoding="utf-8",
            )
            (pkg_dir / "catalog" / "index.yaml").write_text(
                yaml.dump(
                    {
                        "modules": [
                            {
                                "name": "demo-kit",
                                "display_name": "Demo Kit",
                                "description": "Demo catalog module",
                                "version": "1.0.0",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            runtime = await bootstrap_runtime(
                {
                    "language": "en",
                    "model": "deepseek/deepseek-chat",
                    "mcp": {
                        "servers": {
                            "demo": {
                                "transport": "stdio",
                                "command": sys.executable,
                                "args": [str(FAKE_SERVER)],
                                "description": "Demo MCP server",
                            }
                        }
                    },
                },
                pkg_dir=pkg_dir,
                lumen_dir=lumen_dir,
                active_channels=["web"],
            )

            try:
                runtime.brain.catalog = Catalog(pkg_dir / "catalog" / "index.yaml")
                runtime.brain.marketplace.catalog = runtime.brain.catalog

                installer = Installer(
                    pkg_dir,
                    runtime.brain.connectors,
                    runtime.brain.memory,
                    runtime.brain.catalog,
                )
                install_result = installer.install_from_catalog("demo-kit")
                self.assertEqual(install_result["status"], "installed")

                refresh_runtime_registry(
                    runtime.brain,
                    pkg_dir=pkg_dir,
                    active_channels=["web"],
                )

                mcp_cap = runtime.brain.registry.get(CapabilityKind.MCP, "demo")
                self.assertIsNotNone(mcp_cap)
                self.assertEqual(mcp_cap.status.value, "ready")

                installed_names = {
                    item["name"] for item in runtime.brain.marketplace.kits_installed()
                } | {
                    item["name"] for item in runtime.brain.marketplace.modules_installed()
                }
                self.assertIn("demo-kit", installed_names)
                refresh_summary = runtime.brain.capability_awareness.peek_summary()
                self.assertGreaterEqual(refresh_summary["pending"], 2)
                self.assertIn("capability_discovered", refresh_summary["counts"])
                self.assertIn("capability_connected", refresh_summary["counts"])
            finally:
                if runtime.brain.mcp_manager:
                    await runtime.brain.mcp_manager.close()
                await runtime.brain.memory.close()

    async def test_bootstrap_returns_initial_integration_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = await bootstrap_runtime(
                {"language": "en", "model": "deepseek/deepseek-chat"},
                pkg_dir=self._make_runtime_pkg(Path(tmp)),
                lumen_dir=Path(tmp) / "runtime",
                active_channels=["web"],
            )

            try:
                self.assertIsNotNone(runtime.integration_summary)
                self.assertGreaterEqual(runtime.integration_summary["pending"], 1)
                self.assertIn("capability_discovered", runtime.integration_summary["counts"])
            finally:
                await runtime.brain.memory.close()


if __name__ == "__main__":
    unittest.main()


def _write_yaml(path: Path, payload: dict):
    path.write_text(yaml.dump(payload, sort_keys=False), encoding="utf-8")


def _make_runtime_pkg(temp_root: Path) -> Path:
    pkg_dir = temp_root / "pkg"
    (pkg_dir / "locales" / "en" / "flows").mkdir(parents=True)
    (pkg_dir / "catalog").mkdir(parents=True)
    _write_yaml(
        pkg_dir / "locales" / "en" / "personality.yaml",
        {"identity": {"name": "Locale Lumen", "role": "Assistant"}},
    )
    _write_yaml(
        pkg_dir / "locales" / "en" / "flows" / "default.yaml",
        {"intent": "locale-default", "triggers": ["hello"]},
    )
    _write_yaml(pkg_dir / "catalog" / "index.yaml", {"modules": []})
    return pkg_dir


MCPRuntimeTests._make_runtime_pkg = staticmethod(_make_runtime_pkg)
