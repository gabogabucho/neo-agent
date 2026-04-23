"""Tests for module config interpolation in neo__read_skill."""

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from lumen.core.brain import Brain
from lumen.core.connectors import ConnectorRegistry
from lumen.core.memory import Memory
from lumen.core.personality import Personality
from lumen.core.registry import Capability, CapabilityKind, CapabilityStatus, Registry


class SkillInterpolationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        personality_path = self.root / "personality.yaml"
        personality_path.write_text(yaml.dump({"identity": {"name": "Lumen"}}), encoding="utf-8")
        self.registry = Registry()
        self.brain = Brain(
            consciousness={"name": "Lumen"},
            personality=Personality(personality_path),
            registry=self.registry,
            connectors=ConnectorRegistry(),
            memory=Memory(str(self.root / "memory.db")),
            config={
                "instance_id": "otto-003",
                "secrets": {
                    "tiendanube-kit": {
                        "SCRIPTS_DIR": "/srv/otto/shared/capabilities/tiendanube",
                        "INSTANCE_ID": "otto-003",
                    }
                },
            },
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_read_skill_interpolates_module_placeholders(self):
        skill_path = self.root / "skill.md"
        skill_path.write_text(
            "---\nname: tiendanube-ops\ndescription: ops\n---\nUse {SCRIPTS_DIR} for scripts. Instance={INSTANCE_ID}",
            encoding="utf-8",
        )
        self.registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name="tiendanube-ops",
                description="ops",
                status=CapabilityStatus.READY,
                metadata={"path": str(skill_path), "module_name": "tiendanube-kit"},
            )
        )
        result = self.brain._read_skill(json.dumps({"skill_name": "tiendanube-ops"}))
        self.assertEqual(result["skill"], "tiendanube-ops")
        self.assertIn("/srv/otto/shared/capabilities/tiendanube", result["content"])
        self.assertIn("otto-003", result["content"])
        self.assertNotIn("{SCRIPTS_DIR}", result["content"])

    def test_read_skill_keeps_unknown_placeholders(self):
        skill_path = self.root / "skill2.md"
        skill_path.write_text(
            "---\nname: demo\ndescription: demo\n---\nUnknown {DOES_NOT_EXIST}",
            encoding="utf-8",
        )
        self.registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name="demo",
                description="demo",
                status=CapabilityStatus.READY,
                metadata={"path": str(skill_path), "module_name": "tiendanube-kit"},
            )
        )
        result = self.brain._read_skill(json.dumps({"skill_name": "demo"}))
        self.assertIn("{DOES_NOT_EXIST}", result["content"])

    def test_read_skill_accepts_module_alias(self):
        skill_path = self.root / "skill3.md"
        skill_path.write_text(
            "---\nname: tiendanube-ops\ndescription: ops\n---\nAlias works",
            encoding="utf-8",
        )
        self.registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name="tiendanube-ops",
                description="ops",
                status=CapabilityStatus.READY,
                metadata={
                    "path": str(skill_path),
                    "module_name": "tiendanube-kit",
                    "aliases": ["tiendanube-kit/tiendanube-ops"],
                },
            )
        )
        result = self.brain._read_skill(json.dumps({"skill_name": "tiendanube-kit/tiendanube-ops"}))
        self.assertIn("Alias works", result["content"])
