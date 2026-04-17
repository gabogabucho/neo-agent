import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from lumen.core.installer import Installer
from lumen.core.catalog import Catalog
from lumen.core.registry import CapabilityKind
from lumen.core.runtime import bootstrap_runtime, refresh_runtime_registry


PROJECT_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = PROJECT_ROOT / "lumen"
FAKE_SERVER = Path(__file__).parent / "fixtures" / "fake_mcp_server.py"


class MCPRuntimeTests(unittest.IsolatedAsyncioTestCase):
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

                installed_kits = runtime.brain.marketplace.kits_installed()
                self.assertEqual(
                    [item["name"] for item in installed_kits], ["demo-kit"]
                )
            finally:
                if runtime.brain.mcp_manager:
                    await runtime.brain.mcp_manager.close()
                await runtime.brain.memory.close()


if __name__ == "__main__":
    unittest.main()
