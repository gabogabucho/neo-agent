import sys
import tempfile
import unittest
from pathlib import Path

from lumen.core.registry import CapabilityKind
from lumen.core.runtime import bootstrap_runtime


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


if __name__ == "__main__":
    unittest.main()
