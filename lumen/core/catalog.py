"""Module Catalog — what Lumen can recommend when it detects a gap.

When Lumen can't do something, it doesn't just say "I can't."
It checks the catalog, finds modules that fill the gap, and suggests
installing them. Like WordPress: "You need this plugin. Install it?"
"""

from __future__ import annotations

from pathlib import Path

import yaml

from lumen.core.cerebelo import compatibility_for_catalog_entry


class Catalog:
    """Available modules that can be installed to extend Lumen."""

    def __init__(self, catalog_path: Path | None = None):
        if catalog_path is None:
            catalog_path = Path(__file__).parent.parent / "catalog" / "index.yaml"
        self._modules: list[dict] = []
        if catalog_path.exists():
            with open(catalog_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
                self._modules = data.get("modules", [])

    @property
    def modules(self) -> list[dict]:
        return self._modules

    def search(self, query: str, *, registry=None, connectors=None) -> list[dict]:
        """Search catalog by name, description, tags, or fills_gaps."""
        query_lower = query.lower()
        results = []
        for mod in self._modules:
            score = 0
            # Check fills_gaps (highest priority)
            for gap in mod.get("fills_gaps", []):
                if gap in query_lower:
                    score += 10
            # Check name and description
            if query_lower in mod.get("name", "").lower():
                score += 5
            if query_lower in mod.get("description", "").lower():
                score += 3
            # Check tags
            for tag in mod.get("tags", []):
                if tag in query_lower or query_lower in tag:
                    score += 2
            if score > 0:
                results.append(
                    (
                        score,
                        self._decorate(mod, registry=registry, connectors=connectors),
                    )
                )
        results.sort(key=lambda x: x[0], reverse=True)
        return [mod for _, mod in results]

    def find_for_gap(
        self, gap_description: str, *, registry=None, connectors=None
    ) -> list[dict]:
        """Find modules that could fill a specific capability gap.

        This is the key method: Lumen detects it can't do something,
        describes the gap, and this method finds relevant modules.
        """
        return self.search(gap_description, registry=registry, connectors=connectors)

    def get(self, name: str, *, registry=None, connectors=None) -> dict | None:
        """Get a module by name."""
        for mod in self._modules:
            if mod["name"] == name:
                return self._decorate(mod, registry=registry, connectors=connectors)
        return None

    def list_all(self, *, registry=None, connectors=None) -> list[dict]:
        """List all available modules."""
        return [
            self._decorate(
                {
                    "name": m["name"],
                    "display_name": m.get("display_name", m["name"]),
                    "description": m.get("description", ""),
                    "version": m.get("version", "0.0.0"),
                    "price": m.get("price", "free"),
                    "min_capability": m.get("min_capability", "tier-1"),
                    "tags": m.get("tags", []),
                    "provides": m.get("provides", []),
                    "requires": m.get("requires", {}),
                    "fills_gaps": m.get("fills_gaps", []),
                },
                registry=registry,
                connectors=connectors,
            )
            for m in self._modules
        ]

    def as_context(
        self, installed_names: set[str] | None = None, *, registry=None, connectors=None
    ) -> str:
        """Format catalog for the LLM prompt.

        Filters out already-installed modules so the LLM doesn't
        recommend installing something Lumen already has.
        """
        installed = installed_names or set()
        available = [
            self._decorate(m, registry=registry, connectors=connectors)
            for m in self._modules
            if m["name"] not in installed
        ]

        if not available:
            return ""

        lines = [
            "## Module Catalog (what I can recommend to install)",
            "",
            "When I cannot fulfill a request, I should check if a module "
            "in this catalog could help, and suggest installing it.",
            "",
        ]
        for mod in available:
            gaps = ", ".join(mod.get("fills_gaps", [])[:4])
            compat = mod.get("compatibility") or {}
            compat_hint = compat.get("status", "unknown")
            lines.append(
                f"- **{mod.get('display_name', mod['name'])}** "
                f"({mod['name']}): {mod.get('description', '')} "
                f"[fills: {gaps}; compatibility: {compat_hint}]"
            )
        return "\n".join(lines)

    def _decorate(self, module: dict, *, registry=None, connectors=None) -> dict:
        item = dict(module)
        if registry is not None and connectors is not None:
            item["compatibility"] = compatibility_for_catalog_entry(
                item,
                registry,
                connectors,
            )
        return item
