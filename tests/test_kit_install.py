"""Tests for kit artifact installation.

Kit layout:
  kit.yaml
  personality.yaml
  modules/
  skills/
  flows/
"""

import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from fastapi.testclient import TestClient

from lumen.channels import web
from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry
from lumen.core.installer import Installer
from lumen.core.registry import Registry


def _make_installer(tmp_dir: Path, config=None) -> Installer:
    pkg_dir = tmp_dir / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "modules").mkdir()
    return Installer(
        pkg_dir=pkg_dir,
        connectors=ConnectorRegistry(),
        memory=None,
        catalog=Catalog(),
        lumen_dir=tmp_dir / "lumen",
        config=config or {"model": "test-model", "language": "es"},
    )


def _write_module(parent: Path, name: str, tags=None):
    mod_dir = parent / name
    mod_dir.mkdir(parents=True, exist_ok=True)
    (mod_dir / "module.yaml").write_text(
        yaml.dump(
            {
                "name": name,
                "display_name": name.replace("-", " ").title(),
                "description": f"Module {name}",
                "version": "1.0.0",
                "tags": tags or ["x-lumen"],
            },
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    (mod_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name}\n---\n# {name}\n", encoding="utf-8"
    )
    return mod_dir


def _write_kit(root: Path, name: str = "barber-kit") -> Path:
    kit_dir = root / name
    (kit_dir / "modules").mkdir(parents=True, exist_ok=True)
    (kit_dir / "skills").mkdir(parents=True, exist_ok=True)
    (kit_dir / "flows").mkdir(parents=True, exist_ok=True)
    (kit_dir / "kit.yaml").write_text(
        yaml.dump(
            {
                "name": name,
                "display_name": "Barber Kit",
                "description": "Full barbershop kit",
                "personality": "personality.yaml",
                "skills": ["skills/barber-ops.md"],
                "flows": ["flows/intake.yaml"],
            },
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    (kit_dir / "personality.yaml").write_text(
        yaml.dump(
            {
                "identity": {"name": "Barber", "role": "Barbershop assistant"},
                "ui": {"tag": "barber-ui", "surfaces": ["briefing"]},
            },
            default_flow_style=False,
        ),
        encoding="utf-8",
    )
    (kit_dir / "skills" / "barber-ops.md").write_text(
        "---\nname: barber-ops\ndescription: Barber operations\n---\nUse barber operations.",
        encoding="utf-8",
    )
    (kit_dir / "flows" / "intake.yaml").write_text(
        yaml.dump({"intent": "intake", "triggers": ["book"], "slots": {}}),
        encoding="utf-8",
    )
    _write_module(kit_dir / "modules", "calendar-sync")
    _write_module(kit_dir / "modules", "customer-memory")
    return kit_dir


class KitInstallCoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.installer = _make_installer(self.root)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_install_kit_from_local_path(self):
        kit_dir = _write_kit(self.root)
        result = self.installer.install_kit_from_local_path(kit_dir)

        self.assertEqual(result["status"], "installed")
        self.assertEqual(result["name"], "barber-kit")
        self.assertEqual(result["active_personality"], "barber-kit")
        self.assertEqual(sorted(result["installed_modules"]), ["calendar-sync", "customer-memory"])

        kit_module_dir = self.installer.installed_dir / "barber-kit"
        self.assertTrue((kit_module_dir / "module.yaml").exists())
        self.assertTrue((kit_module_dir / "personality.yaml").exists())
        self.assertTrue((kit_module_dir / "skills" / "barber-ops.md").exists())
        self.assertTrue((kit_module_dir / "flows" / "intake.yaml").exists())
        self.assertEqual(self.installer.config.get("active_personality"), "barber-kit")

    def test_install_kit_invalid_without_kit_yaml(self):
        empty = self.root / "empty-kit"
        empty.mkdir()
        result = self.installer.install_kit_from_local_path(empty)
        self.assertEqual(result["status"], "error")

    def test_install_from_zip_detects_kit_yaml(self):
        kit_dir = _write_kit(self.root)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as zf:
            for path in kit_dir.rglob("*"):
                if path.is_file():
                    zf.writestr(f"{kit_dir.name}/{path.relative_to(kit_dir).as_posix()}", path.read_bytes())
        result = self.installer.install_from_zip(data.getvalue())
        self.assertEqual(result["status"], "installed")
        self.assertEqual(result["name"], "barber-kit")


class KitInstallCLITests(unittest.TestCase):
    def test_kit_command_exists(self):
        from typer.testing import CliRunner
        from lumen.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["kit", "--help"])
        self.assertEqual(result.exit_code, 0)
        self.assertIn("install", result.output)

    @patch("lumen.core.installer.Installer.install_kit_from_local_path")
    def test_module_install_autodetects_kit_path(self, mock_install_kit):
        from typer.testing import CliRunner
        from lumen.cli.main import app

        mock_install_kit.return_value = {"status": "installed", "name": "barber-kit"}

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            _write_kit(root)
            lumen_dir = root / "instance"
            lumen_dir.mkdir(parents=True, exist_ok=True)
            (lumen_dir / "config.yaml").write_text(
                yaml.dump({"model": "test-model", "language": "es"}),
                encoding="utf-8",
            )

            runner = CliRunner()
            result = runner.invoke(
                app,
                ["module", "install", str(root / "barber-kit"), "--data-dir", str(lumen_dir)],
            )

            self.assertEqual(result.exit_code, 0)
            mock_install_kit.assert_called_once()


class KitInstallWebTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.original_brain = web._brain
        self.original_config = web._config
        self.original_locale = web._locale
        self.original_lumen_dir = web.LUMEN_DIR
        self.original_config_path = web.CONFIG_PATH
        self.original_sync = web.sync_runtime_modules
        self.original_refresh = web.refresh_runtime_registry
        self.original_reload = web.reload_runtime_personality_surface

        web.LUMEN_DIR = self.root / "lumen"
        web.CONFIG_PATH = web.LUMEN_DIR / "config.yaml"
        web._config = {"model": "test-model", "language": "es"}
        web._locale = {}

        brain = MagicMock()
        brain.connectors = ConnectorRegistry()
        brain.memory = MagicMock()
        brain.catalog = Catalog()
        brain.registry = Registry()
        brain.flows = []
        web._brain = brain

        async def _noop_sync(*args, **kwargs):
            return None

        web.sync_runtime_modules = _noop_sync
        web.refresh_runtime_registry = MagicMock()
        web.reload_runtime_personality_surface = MagicMock()
        self.client = TestClient(web.app)

    def tearDown(self):
        self.temp_dir.cleanup()
        web._brain = self.original_brain
        web._config = self.original_config
        web._locale = self.original_locale
        web.LUMEN_DIR = self.original_lumen_dir
        web.CONFIG_PATH = self.original_config_path
        web.sync_runtime_modules = self.original_sync
        web.refresh_runtime_registry = self.original_refresh
        web.reload_runtime_personality_surface = self.original_reload

    def test_api_modules_upload_accepts_kit_zip(self):
        kit_dir = _write_kit(self.root)
        data = io.BytesIO()
        with zipfile.ZipFile(data, "w") as zf:
            for path in kit_dir.rglob("*"):
                if path.is_file():
                    zf.writestr(f"{kit_dir.name}/{path.relative_to(kit_dir).as_posix()}", path.read_bytes())

        response = self.client.post("/api/modules/upload", content=data.getvalue())
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "installed")
        self.assertEqual(payload["name"], "barber-kit")


if __name__ == "__main__":
    unittest.main()
