"""Regression test for real runtime skill interpolation from instance secrets store."""

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from lumen.core.connectors import ConnectorRegistry
from lumen.core.discovery import discover_all
from lumen.core.registry import CapabilityKind, Registry
from lumen.core.runtime import bootstrap_runtime
from lumen.core.secrets_store import configure_paths, save_module


class SkillInterpolationRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.pkg_dir = self.root / "pkg"
        self.lumen_dir = self.root / "instance"
        (self.pkg_dir / "modules" / "otto-tiendanube" / "skills").mkdir(parents=True, exist_ok=True)
        (self.pkg_dir / "skills").mkdir(parents=True, exist_ok=True)
        (self.pkg_dir / "connectors").mkdir(parents=True, exist_ok=True)
        (self.pkg_dir / "locales" / "es").mkdir(parents=True, exist_ok=True)

        (self.pkg_dir / "locales" / "es" / "personality.yaml").write_text(
            yaml.dump({"identity": {"name": "Lumen ES"}}), encoding="utf-8"
        )

        (self.pkg_dir / "modules" / "otto-tiendanube" / "module.yaml").write_text(
            yaml.dump(
                {
                    "name": "otto-tiendanube",
                    "description": "TiendaNube ops",
                    "tags": ["x-lumen"],
                    "skills": ["skills/tiendanube-ops.md"],
                },
                default_flow_style=False,
            ),
            encoding="utf-8",
        )
        (self.pkg_dir / "modules" / "otto-tiendanube" / "skills" / "tiendanube-ops.md").write_text(
            "---\nname: tiendanube-ops\ndescription: TiendaNube ops\n---\npython3 {SCRIPTS_DIR}/tn_get_product_by_sku.py --instance {INSTANCE_ID} --sku 45 --pretty",
            encoding="utf-8",
        )

        configure_paths(lumen_dir=self.lumen_dir)
        save_module(
            "otto-tiendanube",
            {
                "SCRIPTS_DIR": "/srv/otto/shared/capabilities/tiendanube",
                "INSTANCE_ID": "otto-003",
            },
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_bootstrap_runtime_hydrates_instance_secrets_for_skill_interpolation(self):
        runtime = __import__("asyncio").run(
            bootstrap_runtime(
                {"model": "test-model", "language": "es"},
                pkg_dir=self.pkg_dir,
                lumen_dir=self.lumen_dir,
                active_channels=["web"],
            )
        )

        result = runtime.brain._read_skill(json.dumps({"skill_name": "otto-tiendanube/tiendanube-ops"}))
        self.assertIn("/srv/otto/shared/capabilities/tiendanube", result["content"])
        self.assertIn("otto-003", result["content"])
        self.assertNotIn("{SCRIPTS_DIR}", result["content"])
        self.assertNotIn("{INSTANCE_ID}", result["content"])
