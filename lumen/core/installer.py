"""Module Installer — install = Lumen knows, uninstall = Lumen forgets.

No restart. No config editing. No noise.
Install a module → discovery re-runs → Lumen is aware.
Uninstall → gone from consciousness, as if it never existed.
"""

from __future__ import annotations

import io
import subprocess
import shutil
import zipfile
from pathlib import Path

import yaml

from lumen.core.marketplace import humanize_module_name

from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry
from lumen.core.memory import Memory
from lumen.core.module_manifest import (
    find_module_manifest_in_zip,
    load_module_manifest,
    resolve_module_manifest_path,
    zip_manifest_root_prefix,
)
from lumen.core.module_runtime import (
    run_module_install_hook,
    run_module_uninstall_hook,
)
from lumen.core.module_setup import pending_setup_for_manifest


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
        lumen_dir: Path | None = None,
        config: dict | None = None,
    ):
        self.pkg_dir = pkg_dir
        self.connectors = connectors
        self.memory = memory
        self.catalog = catalog or Catalog()
        self.lumen_dir = lumen_dir or (Path.home() / ".lumen")
        self.installed_dir = pkg_dir / "modules"
        self.installed_dir.mkdir(parents=True, exist_ok=True)
        self.config = config if config is not None else {}

    def _detect_pending_setup(self, module_name: str) -> dict | None:
        """Inspect the installed module and return setup info if env vars are missing."""
        module_dir = self.installed_dir / module_name
        if not module_dir.exists():
            return None
        _, manifest = load_module_manifest(module_dir)
        return pending_setup_for_manifest(
            module_name,
            manifest,
            self.config,
            module_dir=module_dir,
        )

    def list_installed(self) -> list[dict]:
        """List all installed modules."""
        installed = []
        for module_dir in self.installed_dir.iterdir():
            if not module_dir.is_dir() or module_dir.name.startswith("_"):
                continue
            manifest_path, manifest = load_module_manifest(module_dir)
            if manifest_path is not None:
                module_name = manifest.get("name", module_dir.name)
                item = {
                    "name": module_name,
                    "display_name": humanize_module_name(
                        module_name,
                        manifest.get("display_name"),
                    ),
                    "description": manifest.get("description", ""),
                    "version": manifest.get("version", "0.0.0"),
                    "tags": manifest.get("tags", []),
                    "path": str(module_dir),
                }
                pending = pending_setup_for_manifest(
                    module_name,
                    manifest,
                    self.config,
                    module_dir=module_dir,
                )
                if pending:
                    item["pending_setup"] = pending
                installed.append(item)
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
            result = {"status": "already_installed", "name": name}
            pending = self._detect_pending_setup(name)
            if pending:
                result["pending_setup"] = pending
            return result

        module_info = self.catalog.get(name)
        if not module_info:
            return {"status": "not_found", "name": name}

        module_dir = self.installed_dir / name
        module_dir.mkdir(parents=True, exist_ok=True)

        catalog_module_dir = self._resolve_catalog_module_dir(module_info)
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

        run_module_install_hook(
            name=name,
            module_dir=module_dir,
            runtime_root=self.lumen_dir / "modules",
            config=self.config,
            lumen_dir=self.lumen_dir,
        )

        result = {
            "status": "installed",
            "name": name,
            "display_name": module_info.get("display_name", name),
            "description": module_info.get("description", ""),
        }
        pending = self._detect_pending_setup(name)
        if pending:
            result["pending_setup"] = pending
        return result

    def install_marketplace_item(self, item: dict) -> dict:
        """Install a marketplace card from a remote source."""
        if not isinstance(item, dict) or not item.get("name"):
            return {"status": "error", "error": "Invalid marketplace item"}

        source_type = str(
            item.get("source_type")
            or ((item.get("sources") or [{}])[0].get("type") or "")
        ).strip()

        if source_type == "clawhub":
            return self._install_from_clawhub(item)

        if source_type == "mcp-registry":
            return self._install_from_mcp_registry(item)

        if source_type == "skills-sh":
            return self._install_from_skills_sh(item)

        return {
            "status": "error",
            "error": f"Unsupported install source: {source_type or 'unknown'}",
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
                    result = {
                        "status": "already_installed",
                        "name": module_name,
                    }
                    pending = self._detect_pending_setup(module_name)
                    if pending:
                        result["pending_setup"] = pending
                    return result

                warnings = self._external_module_warnings(module_name)

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

                run_module_install_hook(
                    name=module_name,
                    module_dir=module_dir,
                    runtime_root=self.lumen_dir / "modules",
                    config=self.config,
                    lumen_dir=self.lumen_dir,
                )

                result = {
                    "status": "installed",
                    "name": module_name,
                    "display_name": humanize_module_name(
                        module_name, manifest.get("display_name")
                    ),
                    "description": manifest.get("description", ""),
                    "warnings": warnings,
                }
                pending = self._detect_pending_setup(module_name)
                if pending:
                    result["pending_setup"] = pending
                return result

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

        run_module_uninstall_hook(
            name=name,
            module_dir=module_dir,
            runtime_root=self.lumen_dir / "modules",
            lumen_dir=self.lumen_dir,
        )

        shutil.rmtree(module_dir)

        # Purge secrets — module is gone, secrets are useless
        try:
            from lumen.core.secrets_store import delete_module
            delete_module(name)
        except Exception:
            pass

        # Clean in-memory secrets
        if isinstance(self.config, dict):
            secrets = self.config.get("secrets")
            if isinstance(secrets, dict):
                secrets.pop(name, None)

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

    def _resolve_catalog_module_dir(self, module_info: dict) -> Path:
        """Resolve the source directory for a catalog module safely."""
        catalog_root = (self.pkg_dir / "catalog").resolve()
        relative_path = module_info.get("path") or f"modules/{module_info['name']}"
        candidate = (catalog_root / relative_path).resolve()

        if candidate != catalog_root and catalog_root not in candidate.parents:
            return catalog_root / "__invalid__"
        return candidate

    def _external_module_warnings(self, module_name: str) -> list[str]:
        warnings: list[str] = []
        if module_name and not module_name.startswith("x-lumen-"):
            warnings.append(
                "External module does not follow the recommended 'x-lumen-*' naming convention"
            )
        return warnings

    def _install_from_clawhub(self, item: dict) -> dict:
        slug = str(item.get("name") or "").strip()
        if not slug:
            return {"status": "error", "error": "Missing ClawHub slug"}

        npx = shutil.which("npx")
        if not npx:
            return {
                "status": "error",
                "error": "Install Node.js to use ClawHub skills: https://nodejs.org",
            }

        before = {
            path.name
            for path in self.installed_dir.iterdir()
            if path.is_dir() and not path.name.startswith("_")
        }
        result = subprocess.run(
            [npx, "clawhub@latest", "install", slug, "--dir", str(self.installed_dir)],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            return {
                "status": "error",
                "error": detail or f"ClawHub install failed for {slug}",
            }

        module_dir = self._detect_new_module_dir(before, slug)
        if module_dir is None:
            return {
                "status": "error",
                "error": "ClawHub install completed but no valid module was added to Lumen.",
            }

        manifest_path, manifest = load_module_manifest(module_dir)
        if manifest_path is None:
            return {
                "status": "error",
                "error": "ClawHub install completed but no module manifest was found.",
            }

        module_name = manifest.get("name", module_dir.name)
        run_module_install_hook(
            name=module_name,
            module_dir=module_dir,
            runtime_root=self.lumen_dir / "modules",
            config=self.config,
            lumen_dir=self.lumen_dir,
        )
        result = {
            "status": "installed",
            "name": module_name,
            "display_name": humanize_module_name(
                module_name, manifest.get("display_name")
            ),
            "description": manifest.get("description", item.get("description", "")),
        }
        pending = self._detect_pending_setup(module_name)
        if pending:
            result["pending_setup"] = pending
        return result

    def _install_from_mcp_registry(self, item: dict) -> dict:
        remote_transport = item.get("remote_transport") or {}
        transport_type = str(remote_transport.get("type") or "stdio").strip().lower()
        if transport_type and transport_type != "stdio":
            return {
                "status": "error",
                "error": f"Remote MCP transports ({transport_type}) are not installable yet.",
            }

        name = str(item.get("name") or "").strip()
        module_info = self.catalog.get(name)
        if module_info:
            return self.install_from_catalog(name)

        return {
            "status": "error",
            "error": "This MCP Registry entry has no local stdio install path yet.",
        }

    def _install_from_skills_sh(self, item: dict) -> dict:
        """Install a skill from skills.sh via npx CLI or GitHub fallback."""
        name = str(item.get("name") or "").strip()
        owner = str(item.get("owner") or "").strip()
        repo = str(item.get("repo") or "").strip()
        skill_name = str(item.get("skill_name") or name.split("/")[-1] if "/" in name else name).strip()

        if not name:
            return {"status": "error", "error": "Missing skill name"}

        # Try npx first
        npx = shutil.which("npx")
        if npx:
            result = subprocess.run(
                [npx, "skills", "add", f"{owner}/{repo}", "--skill", skill_name, "-g", "-y"],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            if result.returncode != 0:
                # Fallback to GitHub
                return self._install_from_github(owner, repo, skill_name)

            # Detect installed module
            module_dir = self._detect_new_module_dir(set(), skill_name)
            if module_dir is None:
                return self._install_from_github(owner, repo, skill_name)

            run_module_install_hook(
                name=skill_name,
                module_dir=module_dir,
                runtime_root=self.lumen_dir / "modules",
                config=self.config,
                lumen_dir=self.lumen_dir,
            )
            return {
                "status": "installed",
                "name": skill_name,
                "display_name": item.get("display_name", skill_name),
                "description": item.get("description", ""),
            }

        # No npx — use GitHub fallback
        return self._install_from_github(owner, repo, skill_name)

    def _install_from_github(self, owner: str, repo: str, skill_name: str) -> dict:
        """Fetch SKILL.md directly from GitHub raw URL."""
        if not owner or not repo:
            return {"status": "error", "error": "Missing owner/repo for GitHub fetch"}

        from urllib.request import urlopen
        from urllib.error import URLError

        # Try common paths for SKILL.md
        for path in [f"{skill_name}/SKILL.md", "SKILL.md"]:
            url = f"https://raw.githubusercontent.com/{owner}/{repo}/main/{path}"
            try:
                with urlopen(url, timeout=10) as response:
                    content = response.read().decode("utf-8")
                # Save to skills directory
                skill_dir = self.installed_dir / skill_name
                skill_dir.mkdir(parents=True, exist_ok=True)
                (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")

                # Generate a minimal manifest so Lumen discovers it
                manifest = {
                    "name": skill_name,
                    "display_name": skill_name.replace("-", " ").replace("_", " ").title(),
                    "description": f"Skill from skills.sh ({owner}/{repo})",
                    "version": "0.1.0",
                    "tags": ["skill", "skills-sh"],
                    "provides": [],
                }
                import yaml
                (skill_dir / "module.yaml").write_text(
                    yaml.dump(manifest, default_flow_style=False),
                    encoding="utf-8",
                )

                run_module_install_hook(
                    name=skill_name,
                    module_dir=skill_dir,
                    runtime_root=self.lumen_dir / "modules",
                    config=self.config,
                    lumen_dir=self.lumen_dir,
                )
                return {
                    "status": "installed",
                    "name": skill_name,
                    "display_name": manifest["display_name"],
                    "description": manifest["description"],
                }
            except Exception:
                continue

        return {
            "status": "error",
            "error": f"Could not fetch SKILL.md from github.com/{owner}/{repo}",
        }

    def install_from_github_ref(self, owner: str, repo: str) -> dict:
        """Install a module from a GitHub repo by downloading the zip archive.

        Looks for module.yaml or SKILL.md in the repo root.
        Returns {"status": "installed", "name": ...} or {"status": "error", ...}.
        """
        from urllib.request import urlopen
        from urllib.error import URLError

        if not owner or not repo:
            return {"status": "error", "error": "Missing owner/repo for GitHub install"}

        # Strip .git suffix if present
        if repo.endswith(".git"):
            repo = repo[:-4]

        zip_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/main.zip"

        try:
            with urlopen(zip_url, timeout=30) as response:
                zip_bytes = response.read()
        except URLError:
            # Try 'master' branch as fallback
            zip_url = f"https://github.com/{owner}/{repo}/archive/refs/heads/master.zip"
            try:
                with urlopen(zip_url, timeout=30) as response:
                    zip_bytes = response.read()
            except Exception as e:
                return {"status": "error", "error": f"Could not fetch repo from github.com/{owner}/{repo}: {e}"}
        except Exception as e:
            return {"status": "error", "error": f"Could not fetch repo from github.com/{owner}/{repo}: {e}"}

        # Extract and find module.yaml or SKILL.md
        try:
            zip_file = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except Exception as e:
            return {"status": "error", "error": f"Invalid zip from github.com/{owner}/{repo}: {e}"}

        # GitHub zips have a root prefix like "repo-main/"
        namelist = zip_file.namelist()
        root_prefix = ""
        if namelist:
            first = namelist[0]
            slash_idx = first.find("/")
            if slash_idx > 0:
                root_prefix = first[:slash_idx + 1]

        # Look for module.yaml in root
        module_yaml_path = None
        skill_md_path = None
        for name in namelist:
            rel = name[len(root_prefix):] if root_prefix and name.startswith(root_prefix) else name
            if rel == "module.yaml":
                module_yaml_path = name
            elif rel == "SKILL.md":
                skill_md_path = name

        if not module_yaml_path and not skill_md_path:
            return {
                "status": "error",
                "error": f"No module.yaml or SKILL.md found in github.com/{owner}/{repo}",
            }

        # Determine module name
        module_name = repo
        if module_yaml_path:
            manifest_content = zip_file.read(module_yaml_path).decode("utf-8")
            manifest_data = yaml.safe_load(manifest_content) or {}
            module_name = str(manifest_data.get("name") or repo)

        # Install to modules directory
        module_dir = self.installed_dir / module_name
        module_dir.mkdir(parents=True, exist_ok=True)

        # Extract relevant files
        for name in namelist:
            rel = name[len(root_prefix):] if root_prefix and name.startswith(root_prefix) else name
            if not rel or rel.endswith("/"):
                continue
            # Only extract files from the root of the repo (no deep nesting)
            parts = rel.split("/")
            if len(parts) <= 2:  # root or one level deep
                target = module_dir / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zip_file.read(name))

        # If only SKILL.md found, generate a minimal module.yaml
        if not module_yaml_path and skill_md_path:
            display_name = module_name.replace("-", " ").replace("_", " ").title()
            manifest = {
                "name": module_name,
                "display_name": display_name,
                "description": f"Skill from github.com/{owner}/{repo}",
                "version": "0.1.0",
                "tags": ["skill", "github"],
                "provides": [],
            }
            (module_dir / "module.yaml").write_text(
                yaml.dump(manifest, default_flow_style=False),
                encoding="utf-8",
            )

        zip_file.close()

        # Run install hook
        run_module_install_hook(
            name=module_name,
            module_dir=module_dir,
            runtime_root=self.lumen_dir / "modules",
            config=self.config,
            lumen_dir=self.lumen_dir,
        )

        display_name = module_name.replace("-", " ").replace("_", " ").title()
        return {
            "status": "installed",
            "name": module_name,
            "display_name": display_name,
            "description": f"Installed from github.com/{owner}/{repo}",
        }

    def _detect_new_module_dir(self, before: set[str], slug: str) -> Path | None:
        direct = self.installed_dir / slug
        if direct.exists() and resolve_module_manifest_path(direct) is not None:
            return direct

        candidates = [
            path
            for path in self.installed_dir.iterdir()
            if path.is_dir()
            and not path.name.startswith("_")
            and path.name not in before
            and resolve_module_manifest_path(path) is not None
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None
