import unittest

from lumen.core.cerebelo import (
    COMPAT_BLOCKED,
    COMPAT_INSTALLABLE,
    COMPAT_PARTIAL,
    COMPAT_READY,
    annotate_registry,
    build_runtime_surface,
    calculate_compatibility,
    match_declared_tools,
    normalize_module_manifest,
    normalize_openclaw_metadata,
)
from lumen.core.connectors import Connector, ConnectorRegistry
from lumen.core.registry import Capability, CapabilityKind, CapabilityStatus, Registry


async def _noop(**_kwargs):
    return {"ok": True}


class CerebeloTests(unittest.TestCase):
    def test_normalize_module_manifest_handles_schema_drift(self):
        artifact = normalize_module_manifest(
            {
                "name": "peluqueria",
                "description": "Salon assistant",
                "skills_required": ["whatsapp-responder"],
                "channels_supported": ["whatsapp", "web"],
                "requires": {"connectors": ["message"]},
                "provides": ["calendar.create"],
            }
        )

        self.assertEqual(artifact.requires["skills"], ["whatsapp-responder"])
        self.assertEqual(artifact.requires["channels"], ["whatsapp", "web"])
        self.assertEqual(artifact.requires["connectors"], ["message"])
        self.assertEqual(artifact.provides, ["calendar.create"])
        self.assertEqual(artifact.tool_refs, [])

    def test_openclaw_metadata_normalizes_required_tools(self):
        artifact = normalize_openclaw_metadata(
            {
                "name": "openclaw-demo",
                "description": "demo",
                "required_tools": ["task.create"],
                "connectors_required": ["task"],
            }
        )

        self.assertEqual(artifact.requires["tools"], ["task.create"])
        self.assertEqual(artifact.requires["connectors"], ["task"])
        self.assertEqual(artifact.tool_refs, ["task.create"])

    def test_tool_mapper_matches_connector_and_mcp_runtime_tools(self):
        connectors = ConnectorRegistry()
        task = Connector("task", "Tasks", ["create", "list"])
        task.register_handler("create", _noop)
        connectors.register(task)
        connectors.register_tool(
            "mcp__demo__ping",
            "Ping tool",
            {"type": "object", "properties": {}},
            _noop,
            {"kind": "mcp", "server_id": "demo", "remote_tool_name": "ping"},
        )

        runtime_surface = build_runtime_surface(connectors)
        matches = match_declared_tools(
            ["task.create", "task__list", "mcp__demo__ping", "missing__tool"],
            runtime_surface,
        )

        self.assertEqual(matches[0]["status"], COMPAT_READY)
        self.assertEqual(matches[0]["resolved"], "task__create")
        self.assertEqual(matches[1]["status"], COMPAT_PARTIAL)
        self.assertEqual(matches[2]["status"], COMPAT_READY)
        self.assertEqual(matches[3]["status"], COMPAT_BLOCKED)

    def test_catalog_module_compatibility_is_installable_when_runtime_is_ready(self):
        connectors = ConnectorRegistry()
        task = Connector("task", "Tasks", ["create"])
        task.register_handler("create", _noop)
        memory = Connector("memory", "Memory", ["read"])
        memory.register_handler("read", _noop)
        connectors.register(task)
        connectors.register(memory)

        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.CHANNEL,
                name="web",
                description="Web",
                status=CapabilityStatus.READY,
            )
        )

        artifact = normalize_module_manifest(
            {
                "name": "scheduler",
                "description": "Scheduler",
                "requires": {"connectors": ["task", "memory"], "channels": ["web"]},
                "provides": ["task__create with due_date"],
            },
            installed=False,
            source_type="catalog_entry",
        )

        compatibility = calculate_compatibility(
            artifact,
            build_runtime_surface(connectors, registry),
        )
        self.assertEqual(compatibility["status"], COMPAT_INSTALLABLE)

    def test_registry_annotation_adds_cerebelo_compatibility(self):
        connectors = ConnectorRegistry()
        message = Connector("message", "Message", ["send"])
        message.register_handler("send", _noop)
        connectors.register(message)

        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name="notify",
                description="Notify users",
                status=CapabilityStatus.READY,
                provides=["message.send"],
                requires={"connectors": ["message"]},
            )
        )

        annotate_registry(registry, connectors)
        annotated = registry.get(CapabilityKind.SKILL, "notify")

        self.assertIsNotNone(annotated)
        self.assertEqual(annotated.metadata["cerebelo"]["status"], COMPAT_READY)


if __name__ == "__main__":
    unittest.main()
