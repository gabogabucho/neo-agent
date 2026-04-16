"""Personality — who Lumen is in this context. Loaded from YAML, swappable by modules."""

from pathlib import Path
from typing import Any

import yaml


class Personality:
    """Defines Lumen's identity, tone, rules, and domain knowledge for a context.

    Loaded from a YAML file. Modules can replace the personality to transform
    Lumen into a different assistant (e.g. barbershop, restaurant, support).
    """

    def __init__(self, path: Path | str):
        with open(path, encoding="utf-8") as f:
            self._config = yaml.safe_load(f) or {}

    @property
    def identity(self) -> dict:
        return self._config.get("identity", {})

    @property
    def tone(self) -> dict:
        return self._config.get("tone", {})

    @property
    def rules(self) -> list[str]:
        return self._config.get("rules", [])

    @property
    def knowledge(self) -> dict:
        return self._config.get("knowledge", {})

    def current(self) -> dict:
        return self._config

    def as_context(self) -> str:
        """Format personality for LLM system prompt."""
        identity = self.identity
        lines = [
            f"Your name is {identity.get('name', 'Lumen')}.",
            f"Your role: {identity.get('role', 'AI Assistant')}.",
        ]

        if identity.get("description"):
            lines.append(identity["description"])

        if self.tone:
            lines.append(f"\nTone: {self.tone.get('style', 'friendly, direct')}")

        if self.rules:
            lines.append("\nRules you MUST follow:")
            for rule in self.rules:
                lines.append(f"- {rule}")

        if self.knowledge:
            lines.append("\nDomain knowledge:")
            for key, value in self.knowledge.items():
                lines.append(self._format_knowledge(key, value))

        return "\n".join(lines)

    def _format_knowledge(self, key: str, value: Any, indent: int = 2) -> str:
        prefix = " " * indent
        if isinstance(value, list):
            items = "\n".join(f"{prefix}  - {item}" for item in value)
            return f"{prefix}{key}:\n{items}"
        if isinstance(value, dict):
            items = "\n".join(
                self._format_knowledge(k, v, indent + 2) for k, v in value.items()
            )
            return f"{prefix}{key}:\n{items}"
        return f"{prefix}{key}: {value}"
