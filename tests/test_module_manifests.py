import tempfile
import unittest
import zipfile
from io import BytesIO
from pathlib import Path

import yaml

from lumen.core.connectors import ConnectorRegistry
from lumen.core.discovery import discover_all
from lumen.core.installer import Installer
from lumen.core.registry import CapabilityKind, CapabilityStatus, Registry


def _write_yaml(path: Path, payload: dict):
    path.write_text(yaml.dump(payload, sort_keys=False), encoding="utf-8")


def _zip_bytes(entries: dict[str, str]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for name, content in entries.items():
            zf.writestr(name, content)
    return buffer.getvalue()


class ModuleManifestResolutionTests(unittest.TestCase):
    def setUp(self):
        from lumen.core import secrets_store
        self._orig_lumen_dir = secrets_store.LUMEN_DIR
        self._orig_secrets_path = secrets_store.SECRETS_PATH
        secrets_store.configure_paths(lumen_dir=Path(tempfile.mkdtemp()))

    def tearDown(self):
        from lumen.core import secrets_store
        import shutil
        shutil.rmtree(str(secrets_store.LUMEN_DIR), ignore_errors=True)
        secrets_store.LUMEN_DIR = self._orig_lumen_dir
        secrets_store.SECRETS_PATH = self._orig_secrets_path

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
        self.assertEqual(discovered.metadata["interoperability"]["level"], "native")
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
        self.assertEqual(discovered.metadata["interoperability"]["level"], "adapted")

    def test_installed_module_discovery_preserves_path_and_tags_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp)
            module_dir = pkg_dir / "modules" / "metadata-module"
            module_dir.mkdir(parents=True)
            (module_dir / "SKILL.md").write_text("# Metadata skill\n", encoding="utf-8")

            _write_yaml(
                module_dir / "module.yaml",
                {
                    "name": "metadata-module",
                    "display_name": "Metadata Module",
                    "description": "Preserves tags and path",
                    "version": "1.2.3",
                    "tags": ["personality", "developer"],
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
            installed = installer.list_installed()

        self.assertEqual(installed[0]["tags"], ["personality", "developer"])
        self.assertEqual(installed[0]["path"], str(module_dir))

        discovered = registry.get(CapabilityKind.MODULE, "metadata-module")
        self.assertIsNotNone(discovered)
        self.assertEqual(discovered.metadata["tags"], ["personality", "developer"])
        self.assertEqual(discovered.metadata["path"], str(module_dir))

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

    def test_zip_install_warns_for_external_non_lumen_name(self):
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
                        "external/module.yaml": yaml.dump(
                            {
                                "name": "external-module",
                                "display_name": "External Module",
                                "description": "Installed from outside the catalog",
                            },
                            sort_keys=False,
                        ),
                        "external/SKILL.md": "# External Module\n",
                    }
                )
            )

        self.assertEqual(result["status"], "installed")
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("x-lumen-*", result["warnings"][0])

    def test_list_installed_and_install_result_include_pending_setup(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp)
            installer = Installer(
                pkg_dir=pkg_dir,
                connectors=ConnectorRegistry(),
                memory=None,
                config={},
            )

            result = installer.install_from_zip(
                _zip_bytes(
                    {
                        "pending/module.yaml": yaml.dump(
                            {
                                "name": "pending-module",
                                "display_name": "Pending Module",
                                "description": "Needs setup first",
                                "x-lumen": {
                                    "runtime": {
                                        "env": [
                                            {"name": "DEMO_TOKEN", "secret": True},
                                            "DEMO_CHAT_ID",
                                        ]
                                    }
                                },
                            },
                            sort_keys=False,
                        ),
                        "pending/SKILL.md": "# Pending Module\n",
                    }
                )
            )

            installed = installer.list_installed()

        self.assertEqual(result["status"], "installed")
        self.assertIn("pending_setup", result)
        self.assertEqual(
            [spec["name"] for spec in result["pending_setup"]["env_specs"]],
            ["DEMO_TOKEN", "DEMO_CHAT_ID"],
        )
        self.assertEqual(installed[0]["name"], "pending-module")
        self.assertIn("pending_setup", installed[0])

    def test_pending_setup_respects_saved_config_and_discovery_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg_dir = Path(tmp)
            module_dir = pkg_dir / "modules" / "pending-module"
            module_dir.mkdir(parents=True)
            (module_dir / "SKILL.md").write_text("# Pending skill\n", encoding="utf-8")
            _write_yaml(
                module_dir / "module.yaml",
                {
                    "name": "pending-module",
                    "display_name": "Pending Module",
                    "description": "Needs setup first",
                    "x-lumen": {
                        "runtime": {
                            "env": [
                                {"name": "DEMO_TOKEN", "secret": True},
                                "DEMO_CHAT_ID",
                            ]
                        }
                    },
                },
            )

            without_config = Installer(
                pkg_dir=pkg_dir,
                connectors=ConnectorRegistry(),
                memory=None,
                config={},
            )
            with_config = Installer(
                pkg_dir=pkg_dir,
                connectors=ConnectorRegistry(),
                memory=None,
                config={
                    "secrets": {
                        "pending-module": {
                            "DEMO_TOKEN": "token",
                            "DEMO_CHAT_ID": "chat-1",
                        }
                    }
                },
            )

            registry = Registry()
            discover_all(
                registry=registry,
                pkg_dir=pkg_dir,
                connectors=ConnectorRegistry(),
                active_channels=[],
                config=with_config.config,
            )

            installed_without = without_config.list_installed()
            installed_with = with_config.list_installed()
            discovered = registry.get(CapabilityKind.MODULE, "pending-module")

        self.assertIn("pending_setup", installed_without[0])
        self.assertNotIn("pending_setup", installed_with[0])
        self.assertIsNotNone(discovered)
        self.assertEqual(discovered.status, CapabilityStatus.READY)
        self.assertIsNone(discovered.metadata.get("pending_setup"))


if __name__ == "__main__":
    unittest.main()
