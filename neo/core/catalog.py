"""Module Catalog — what Neo can recommend when it detects a gap.

When Neo can't do something, it doesn't just say "I can't."
It checks the catalog, finds modules that fill the gap, and suggests
installing them. Like WordPress: "You need this plugin. Install it?"
"""

from __future__ import annotations

from pathlib import Path

import yaml


class Catalog:
    """Available modules that can be installed to extend Neo."""

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

    def search(self, query: str) -> list[dict]:
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
                results.append((score, mod))
        results.sort(key=lambda x: x[0], reverse=True)
        return [mod for _, mod in results]

    def find_for_gap(self, gap_description: str) -> list[dict]:
        """Find modules that could fill a specific capability gap.

        This is the key method: Neo detects it can't do something,
        describes the gap, and this method finds relevant modules.
        """
        return self.search(gap_description)

    def get(self, name: str) -> dict | None:
        """Get a module by name."""
        for mod in self._modules:
            if mod["name"] == name:
                return mod
        return None

    def list_all(self) -> list[dict]:
        """List all available modules."""
        return [
            {
                "name": m["name"],
                "display_name": m.get("display_name", m["name"]),
                "description": m.get("description", ""),
                "version": m.get("version", "0.0.0"),
                "price": m.get("price", "free"),
                "min_capability": m.get("min_capability", "tier-1"),
                "tags": m.get("tags", []),
            }
            for m in self._modules
        ]

    def as_context(self) -> str:
        """Format catalog for the LLM prompt.

        The LLM uses this to recommend modules when it can't fulfill a request.
        """
        if not self._modules:
            return ""

        lines = [
            "## Module Catalog (what I can recommend to install)",
            "",
            "When I cannot fulfill a request, I should check if a module "
            "in this catalog could help, and suggest installing it.",
            "",
        ]
        for mod in self._modules:
            gaps = ", ".join(mod.get("fills_gaps", [])[:4])
            lines.append(
                f"- **{mod.get('display_name', mod['name'])}** "
                f"({mod['name']}): {mod.get('description', '')} "
                f"[fills: {gaps}]"
            )
        return "\n".join(lines)
