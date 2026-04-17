import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

import yaml

from lumen.core.connectors import ConnectorRegistry
from lumen.core.discovery import discover_all
from lumen.core.installer import Installer
from lumen.core.registry import CapabilityKind, Registry


def _write_yaml(path: Path, payload: dict):
    path.write_text(yaml.dump(payload, sort_keys=False), encoding="utf-8")


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buffer.getvalue()


class ModuleManifestResolutionTests(unittest.TestCase):
    def test_module_yaml_is_preferred_over_manifest_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp)
            module_dir = pkg_dir / "modules" / "demo-module"
            module_dir.mkdir(parents=True)
            (module_dir / "SKILL.md").write_text("# Demo skill\n", encoding="utf-8")

            _write_yaml(
                module_dir / "module.yaml",
                {
                    "name": "module-native",
                    "display_name": "Native Module",
                    "description": "Preferred manifest",
                    "version": "1.1.0",
                },
            )
            _write_yaml(
                module_dir / "manifest.yaml",
                {
                    "name": "module-legacy",
                    "display_name": "Legacy Module",
                    "description": "Fallback manifest",
                    "version": "0.9.0",
                },
            )

            installer = Installer(
                pkg_dir=pkg_dir,
                connectors=ConnectorRegistry(),
                memory=None,
            )
            installed = installer.list_installed()

            registry = Registry()
            discover_all(
                registry=registry,
                pkg_dir=pkg_dir,
                connectors=ConnectorRegistry(),
                active_channels=[],
            )

        self.assertEqual(installed[0]["name"], "module-native")
        self.assertEqual(installed[0]["version"], "1.1.0")

        discovered = registry.get(CapabilityKind.MODULE, "module-native")
        self.assertIsNotNone(discovered)
        self.assertEqual(Path(discovered.metadata["manifest_path"]).name, "module.yaml")
        self.assertIsNone(registry.get(CapabilityKind.MODULE, "module-legacy"))

    def test_manifest_yaml_remains_supported_as_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp)
            module_dir = pkg_dir / "modules" / "legacy-module"
            module_dir.mkdir(parents=True)
            (module_dir / "SKILL.md").write_text("# Legacy skill\n", encoding="utf-8")

            _write_yaml(
                module_dir / "manifest.yaml",
                {
                    "name": "legacy-module",
                    "display_name": "Legacy Module",
                    "description": "Fallback manifest path",
                    "version": "0.8.0",
                },
            )

            installer = Installer(
                pkg_dir=pkg_dir,
                connectors=ConnectorRegistry(),
                memory=None,
            )

            registry = Registry()
            discover_all(
                registry=registry,
                pkg_dir=pkg_dir,
                connectors=ConnectorRegistry(),
                active_channels=[],
            )
            is_installed = installer.is_installed("legacy-module")
            installed = installer.list_installed()

        self.assertTrue(is_installed)
        self.assertEqual(installed[0]["name"], "legacy-module")

        discovered = registry.get(CapabilityKind.MODULE, "legacy-module")
        self.assertIsNotNone(discovered)
        self.assertEqual(
            Path(discovered.metadata["manifest_path"]).name, "manifest.yaml"
        )

    def test_zip_install_supports_module_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp)
            installer = Installer(
                pkg_dir=pkg_dir,
                connectors=ConnectorRegistry(),
                memory=None,
            )

            result = installer.install_from_zip(
                _zip_bytes(
                    {
                        "demo-zip/module.yaml": yaml.dump(
                            {
                                "name": "zip-module",
                                "display_name": "ZIP Module",
                                "description": "Installed from module.yaml",
                                "version": "1.0.0",
                            },
                            sort_keys=False,
                        ),
                        "demo-zip/SKILL.md": "# ZIP Module\n",
                    }
                )
            )

            manifest_path = pkg_dir / "modules" / "zip-module" / "module.yaml"

            self.assertEqual(result["status"], "installed")
            self.assertTrue(manifest_path.exists())

    def test_zip_install_prefers_module_yaml_over_manifest_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp)
            installer = Installer(
                pkg_dir=pkg_dir,
                connectors=ConnectorRegistry(),
                memory=None,
            )

            result = installer.install_from_zip(
                _zip_bytes(
                    {
                        "dual-manifest/module.yaml": yaml.dump(
                            {
                                "name": "preferred-module",
                                "display_name": "Preferred Module",
                                "description": "Chosen from module.yaml",
                            },
                            sort_keys=False,
                        ),
                        "dual-manifest/manifest.yaml": yaml.dump(
                            {
                                "name": "fallback-module",
                                "display_name": "Fallback Module",
                                "description": "Should not win",
                            },
                            sort_keys=False,
                        ),
                    }
                )
            )

            installed_dir = pkg_dir / "modules" / "preferred-module"

            self.assertEqual(result["status"], "installed")
            self.assertEqual(result["name"], "preferred-module")
            self.assertTrue((installed_dir / "module.yaml").exists())
            self.assertTrue((installed_dir / "manifest.yaml").exists())

    def test_zip_install_falls_back_to_manifest_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp)
            installer = Installer(
                pkg_dir=pkg_dir,
                connectors=ConnectorRegistry(),
                memory=None,
            )

            result = installer.install_from_zip(
                _zip_bytes(
                    {
                        "legacy/manifest.yaml": yaml.dump(
                            {
                                "name": "legacy-zip-module",
                                "display_name": "Legacy ZIP Module",
                                "description": "Installed from manifest.yaml",
                            },
                            sort_keys=False,
                        ),
                        "legacy/SKILL.md": "# Legacy ZIP Module\n",
                    }
                )
            )

            manifest_path = pkg_dir / "modules" / "legacy-zip-module" / "manifest.yaml"

            self.assertEqual(result["status"], "installed")
            self.assertEqual(result["name"], "legacy-zip-module")
            self.assertTrue(manifest_path.exists())


if __name__ == "__main__":
    unittest.main()
