"""Module Installer — install = Neo knows, uninstall = Neo forgets.

No restart. No config editing. No noise.
Install a module → discovery re-runs → Neo is aware.
Uninstall → gone from consciousness, as if it never existed.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import yaml

from neo.core.catalog import Catalog
from neo.core.connectors import ConnectorRegistry
from neo.core.discovery import discover_all
from neo.core.handlers import register_builtin_handlers
from neo.core.memory import Memory
from neo.core.registry import Registry


# Where installed modules live
INSTALLED_DIR = Path(__file__).parent.parent / "modules"


class Installer:
    """Installs and uninstalls modules. Re-runs discovery after each operation."""

    def __init__(
        self,
        pkg_dir: Path,
        connectors: ConnectorRegistry,
        memory: Memory,
        catalog: Catalog | None = None,
    ):
        self.pkg_dir = pkg_dir
        self.connectors = connectors
        self.memory = memory
        self.catalog = catalog or Catalog()
        self.installed_dir = pkg_dir / "modules"
        self.installed_dir.mkdir(parents=True, exist_ok=True)

    def list_installed(self) -> list[dict]:
        """List all installed modules."""
        installed = []
        for module_dir in self.installed_dir.iterdir():
            if not module_dir.is_dir() or module_dir.name.startswith("_"):
                continue
            manifest_path = module_dir / "manifest.yaml"
            if manifest_path.exists():
                with open(manifest_path, encoding="utf-8") as f:
                    manifest = yaml.safe_load(f) or {}
                installed.append(
                    {
                        "name": manifest.get("name", module_dir.name),
                        "display_name": manifest.get(
                            "display_name", module_dir.name
                        ),
                        "description": manifest.get("description", ""),
                        "version": manifest.get("version", "0.0.0"),
                        "path": str(module_dir),
                    }
                )
        return installed

    def is_installed(self, name: str) -> bool:
        """Check if a module is installed."""
        module_dir = self.installed_dir / name
        return module_dir.exists() and (module_dir / "manifest.yaml").exists()

    def install_from_catalog(self, name: str) -> dict:
        """Install a module from the catalog.

        For now, creates the module directory with manifest and skill files.
        Future: download from remote registry.
        """
        if self.is_installed(name):
            return {"status": "already_installed", "name": name}

        # Get module info from catalog
        module_info = self.catalog.get(name)
        if not module_info:
            return {"status": "not_found", "name": name}

        # Create module directory
        module_dir = self.installed_dir / name
        module_dir.mkdir(parents=True, exist_ok=True)

        # Write manifest
        manifest = {
            "name": module_info["name"],
            "display_name": module_info.get("display_name", name),
            "description": module_info.get("description", ""),
            "version": module_info.get("version", "1.0.0"),
            "author": module_info.get("author", ""),
            "price": module_info.get("price", "free"),
            "min_capability": module_info.get("min_capability", "tier-1"),
            "provides": module_info.get("provides", []),
            "tags": module_info.get("tags", []),
        }
        with open(module_dir / "manifest.yaml", "w", encoding="utf-8") as f:
            yaml.dump(manifest, f, default_flow_style=False)

        # Write a default SKILL.md for the module
        skill_content = self._generate_skill_md(module_info)
        (module_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

        return {
            "status": "installed",
            "name": name,
            "display_name": manifest["display_name"],
            "description": manifest["description"],
        }

    def install_from_zip(self, zip_data: bytes) -> dict:
        """Install a module from a ZIP file (WordPress-style upload)."""
        try:
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                # Find the manifest to get the module name
                manifest_path = None
                for name in zf.namelist():
                    if name.endswith("manifest.yaml"):
                        manifest_path = name
                        break

                if not manifest_path:
                    return {
                        "status": "error",
                        "error": "No manifest.yaml found in ZIP",
                    }

                # Read manifest
                manifest = yaml.safe_load(zf.read(manifest_path))
                module_name = manifest.get("name", "unknown")

                if self.is_installed(module_name):
                    return {
                        "status": "already_installed",
                        "name": module_name,
                    }

                # Extract to modules directory
                # Determine the root dir inside the zip
                root_prefix = manifest_path.rsplit("manifest.yaml", 1)[0]
                module_dir = self.installed_dir / module_name
                module_dir.mkdir(parents=True, exist_ok=True)

                for zip_entry in zf.namelist():
                    if not zip_entry.startswith(root_prefix):
                        continue
                    relative = zip_entry[len(root_prefix) :]
                    if not relative:
                        continue
                    target = module_dir / relative
                    if zip_entry.endswith("/"):
                        target.mkdir(parents=True, exist_ok=True)
                    else:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(zf.read(zip_entry))

                return {
                    "status": "installed",
                    "name": module_name,
                    "display_name": manifest.get(
                        "display_name", module_name
                    ),
                    "description": manifest.get("description", ""),
                }

        except Exception as e:
            return {"status": "error", "error": str(e)}

    def uninstall(self, name: str) -> dict:
        """Uninstall a module. Neo forgets."""
        module_dir = self.installed_dir / name
        if not module_dir.exists():
            return {"status": "not_installed", "name": name}

        # Don't allow uninstalling the template
        if name.startswith("_"):
            return {"status": "error", "error": "Cannot uninstall templates"}

        shutil.rmtree(module_dir)

        return {"status": "uninstalled", "name": name}

    def rediscover(self) -> Registry:
        """Re-run discovery after install/uninstall. Neo becomes aware."""
        registry = Registry()
        discover_all(
            registry=registry,
            pkg_dir=self.pkg_dir,
            connectors=self.connectors,
            active_channels=["web"],
        )
        return registry

    def _generate_skill_md(self, module_info: dict) -> str:
        """Generate a default SKILL.md for a catalog module."""
        name = module_info["name"]
        description = module_info.get("description", "")
        provides = module_info.get("provides", [])
        fills = module_info.get("fills_gaps", [])

        lines = [
            "---",
            f'name: {name}',
            f'description: "{description}"',
            f'min_capability: {module_info.get("min_capability", "tier-2")}',
        ]
        if module_info.get("requires"):
            lines.append("requires:")
            for key, val in module_info["requires"].items():
                lines.append(f"  {key}: {val}")
        lines.append("---")
        lines.append(f"# {module_info.get('display_name', name)}")
        lines.append("")
        lines.append(description)
        lines.append("")

        if provides:
            lines.append("## Capabilities")
            for p in provides:
                lines.append(f"- {p}")
            lines.append("")

        if fills:
            lines.append("## When to use")
            lines.append(
                "Use this module when the user asks about: "
                + ", ".join(fills[:5])
            )

        return "\n".join(lines)
