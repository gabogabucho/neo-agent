"""Consciousness — WHO Lumen is. Immutable. The soul.

This is Lumen's BIOS. It defines identity and nature — things that NEVER
change regardless of what skills, connectors, or modules are installed.

Consciousness does NOT know what capabilities Lumen has. That's the Body.
Consciousness does NOT decide what to do. That's the Brain.

Consciousness only knows: "I am Lumen. I am modular. I can grow."
"""

from pathlib import Path

import yaml


class Consciousness:
    """Lumen's immutable identity. Cannot be changed by modules or plugins.

    Analogy: A human always knows they are human, that they can learn,
    that they can use tools. They don't need to check their hands to
    know they COULD hold something. That's consciousness.

    What they actually have in their hands — that's the Body.
    """

    def __init__(self, config_path: Path | None = None):
        if config_path is None:
            config_path = Path(__file__).parent / "consciousness.yaml"
        with open(config_path, encoding="utf-8") as f:
            self._config = yaml.safe_load(f)

    @property
    def identity(self) -> dict:
        return self._config["identity"]

    @property
    def nature(self) -> list[str]:
        return self._config["nature"]

    @property
    def name(self) -> str:
        return self.identity["name"]

    def as_context(self) -> str:
        """Format consciousness for the LLM. Only identity and nature."""
        lines = [
            "## Consciousness (who I am — this never changes)",
            "",
            f"I am {self.identity['name']}, a {self.identity['type']}.",
            "",
            "My nature:",
        ]
        for trait in self.nature:
            lines.append(f"- {trait}")
        return "\n".join(lines)
