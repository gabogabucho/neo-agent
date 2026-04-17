"""Module Installer — install = Lumen knows, uninstall = Lumen forgets.

No restart. No config editing. No noise.
Install a module → discovery re-runs → Lumen is aware.
Uninstall → gone from consciousness, as if it never existed.
"""

from __future__ import annotations

import io
import shutil
import zipfile
from pathlib import Path

import yaml

from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry
from lumen.core.memory import Memory
from lumen.core.module_manifest import (
    find_module_manifest_in_zip,
    load_module_manifest,
    resolve_module_manifest_path,
    zip_manifest_root_prefix,
)


# Where installed modules live
INSTALLED_DIR = Path(__file__).parent.parent / "modules"


class Installer:
    """Installs and uninstalls modules on disk."""

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
            manifest_path, manifest = load_module_manifest(module_dir)
            if manifest_path is not None:
                installed.append(
                    {
                        "name": manifest.get("name", module_dir.name),
                        "display_name": manifest.get("display_name", module_dir.name),
                        "description": manifest.get("description", ""),
                        "version": manifest.get("version", "0.0.0"),
                        "path": str(module_dir),
                    }
                )
        return installed

    def is_installed(self, name: str) -> bool:
        """Check if a module is installed."""
        module_dir = self.installed_dir / name
        return (
            module_dir.exists() and resolve_module_manifest_path(module_dir) is not None
        )

    def install_from_catalog(self, name: str) -> dict:
        """Install a module from the catalog.

        If the module has real files in catalog/modules/{name}/, copy them.
        Otherwise, generate manifest + SKILL.md from catalog metadata.
        """
        if self.is_installed(name):
            return {"status": "already_installed", "name": name}

        module_info = self.catalog.get(name)
        if not module_info:
            return {"status": "not_found", "name": name}

        module_dir = self.installed_dir / name
        module_dir.mkdir(parents=True, exist_ok=True)

        # Check if real module files exist in catalog/modules/
        catalog_module_dir = self.pkg_dir / "catalog" / "modules" / name
        if catalog_module_dir.exists():
            # Copy real module files
            for src_file in catalog_module_dir.rglob("*"):
                if src_file.is_file():
                    relative = src_file.relative_to(catalog_module_dir)
                    dest = module_dir / relative
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src_file, dest)
        else:
            # Generate from catalog metadata
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
            with open(module_dir / "module.yaml", "w", encoding="utf-8") as f:
                yaml.dump(manifest, f, default_flow_style=False)

            skill_content = self._generate_skill_md(module_info)
            (module_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

        return {
            "status": "installed",
            "name": name,
            "display_name": module_info.get("display_name", name),
            "description": module_info.get("description", ""),
        }

    def install_from_zip(self, zip_data: bytes) -> dict:
        """Install a module from a ZIP file (WordPress-style upload)."""
        try:
            with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
                # Find the manifest to get the module name
                manifest_path = find_module_manifest_in_zip(zf.namelist())

                if not manifest_path:
                    return {
                        "status": "error",
                        "error": "No module.yaml or manifest.yaml found in ZIP",
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
                root_prefix = zip_manifest_root_prefix(manifest_path)
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
                    "display_name": manifest.get("display_name", module_name),
                    "description": manifest.get("description", ""),
                }

        except Exception as e:
            return {"status": "error", "error": str(e)}

    def uninstall(self, name: str) -> dict:
        """Uninstall a module. Lumen forgets."""
        module_dir = self.installed_dir / name
        if not module_dir.exists():
            return {"status": "not_installed", "name": name}

        # Don't allow uninstalling the template
        if name.startswith("_"):
            return {"status": "error", "error": "Cannot uninstall templates"}

        shutil.rmtree(module_dir)

        return {"status": "uninstalled", "name": name}

    def _generate_skill_md(self, module_info: dict) -> str:
        """Generate a default SKILL.md for a catalog module."""
        name = module_info["name"]
        description = module_info.get("description", "")
        provides = module_info.get("provides", [])
        fills = module_info.get("fills_gaps", [])

        lines = [
            "---",
            f"name: {name}",
            f'description: "{description}"',
            f"min_capability: {module_info.get('min_capability', 'tier-2')}",
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
                "Use this module when the user asks about: " + ", ".join(fills[:5])
            )

        return "\n".join(lines)
