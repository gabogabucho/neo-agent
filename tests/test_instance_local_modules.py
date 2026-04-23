"""Tests for instance-local module installation/runtime discovery."""

import tempfile
import unittest
from pathlib import Path

import yaml

from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry
from lumen.core.discovery import discover_all
from lumen.core.installer import Installer
from lumen.core.registry import CapabilityKind, Registry
from lumen.core.runtime import _resolve_active_personality_module


class InstanceLocalModulesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.pkg_dir = self.root / "pkg"
        self.lumen_dir = self.root / "instance"
        (self.pkg_dir / "modules").mkdir(parents=True)
        (self.pkg_dir / "skills").mkdir(parents=True)
        (self.pkg_dir / "connectors").mkdir(parents=True)
        self.installer = Installer(
            pkg_dir=self.pkg_dir,
            connectors=ConnectorRegistry(),
            memory=None,
            catalog=Catalog(),
            lumen_dir=self.lumen_dir,
            config={"model": "test-model", "language": "es"},
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_local_module(self, name: str, *, tags=None, with_personality=False) -> Path:
        src = self.root / "src" / name
        src.mkdir(parents=True, exist_ok=True)
        manifest = {
            "name": name,
            "display_name": name.replace("-", " ").title(),
            "description": f"Module {name}",
            "version": "1.0.0",
            "tags": tags or ["x-lumen"],
        }
        if with_personality:
            manifest["personality"] = "personality.yaml"
        (src / "module.yaml").write_text(yaml.dump(manifest, default_flow_style=False), encoding="utf-8")
        (src / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n", encoding="utf-8")
        if with_personality:
            (src / "personality.yaml").write_text(yaml.dump({"identity": {"name": name}}), encoding="utf-8")
        return src

    def test_installer_writes_to_lumen_dir_modules(self):
        src = self._write_local_module("instance-only")
        result = self.installer.install_from_local_path(src)
        self.assertEqual(result["status"], "installed")
        self.assertTrue((self.lumen_dir / "modules" / "instance-only" / "module.yaml").exists())
        self.assertFalse((self.pkg_dir / "modules" / "instance-only").exists())

    def test_discovery_reads_instance_modules(self):
        src = self._write_local_module("instance-skill")
        self.installer.install_from_local_path(src)

        registry = Registry()
        discover_all(
            registry,
            self.pkg_dir,
            ConnectorRegistry(),
            active_channels=["web"],
            config={"model": "test-model"},
            lumen_dir=self.lumen_dir,
        )

        self.assertIsNotNone(registry.get(CapabilityKind.MODULE, "instance-skill"))
        self.assertIsNotNone(registry.get(CapabilityKind.SKILL, "instance-skill"))

    def test_active_personality_resolves_from_instance_modules(self):
        src = self._write_local_module("barber-kit", tags=["x-lumen", "personality"], with_personality=True)
        self.installer.install_from_local_path(src)
        config = {"active_personality": "barber-kit"}

        active = _resolve_active_personality_module(config, self.pkg_dir, lumen_dir=self.lumen_dir)
        self.assertIsNotNone(active)
        self.assertEqual(active["manifest"]["name"], "barber-kit")
