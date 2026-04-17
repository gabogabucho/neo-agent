"""Marketplace read model for dashboard/API consumption.

Keeps marketplace logic on the server side by merging runtime truth from the
registry, local Kits Lumen catalog entries, and optional remote read-only feeds.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from lumen.core.catalog import Catalog
from lumen.core.cerebelo import (
    COMPAT_BLOCKED,
    COMPAT_INSTALLABLE,
    COMPAT_PARTIAL,
    COMPAT_READY,
    NormalizedArtifact,
    build_runtime_surface,
    calculate_compatibility,
    normalize_capability,
    normalize_openclaw_metadata,
    normalize_requires,
)
from lumen.core.connectors import ConnectorRegistry
from lumen.core.registry import Capability, CapabilityKind, Registry


COMPAT_BADGES = {
    COMPAT_READY: {"emoji": "🟢", "label": "Ready"},
    COMPAT_INSTALLABLE: {"emoji": "🟡", "label": "Installable"},
    COMPAT_PARTIAL: {"emoji": "🟠", "label": "Partial"},
    COMPAT_BLOCKED: {"emoji": "🔴", "label": "Blocked"},
}


class Marketplace:
    """Aggregates marketplace data for the web dashboard."""

    def __init__(
        self,
        catalog: Catalog,
        registry: Registry,
        connectors: ConnectorRegistry,
        config: dict | None = None,
        *,
        remote_timeout: float = 4.0,
        cache_ttl_seconds: int = 300,
    ):
        self.catalog = catalog
        self.registry = registry
        self.connectors = connectors
        self.config = config or {}
        self.remote_timeout = remote_timeout
        self.cache_ttl_seconds = cache_ttl_seconds
        self._remote_cache: dict[str, Any] | None = None
        self._remote_cache_at = 0.0

    def snapshot(self) -> dict[str, Any]:
        runtime_surface = build_runtime_surface(self.connectors, self.registry)
        remote = self._load_remote(runtime_surface)

        skills = self._merge_cards(
            remote["skills"],
            [
                self._runtime_card(
                    cap, category="skills", runtime_surface=runtime_surface
                )
                for cap in self.registry.list_by_kind(CapabilityKind.SKILL)
            ],
        )
        mcps = self._merge_cards(
            remote["mcps"],
            [
                self._runtime_card(
                    cap, category="mcps", runtime_surface=runtime_surface
                )
                for cap in self.registry.list_by_kind(CapabilityKind.MCP)
            ],
        )
        kits = self._build_kits(runtime_surface)

        return {
            "generated_at": int(time.time()),
            "feeds": remote["feeds"],
            "tabs": [
                {"key": "skills", "label": "Skills", "count": len(skills)},
                {"key": "mcps", "label": "MCPs", "count": len(mcps)},
                {
                    "key": "kits_lumen",
                    "label": "Kits Lumen",
                    "count": len(kits["items"]),
                },
            ],
            "skills": self._section_payload(
                "skills",
                "Skills",
                skills,
                read_only=True,
                installed_label="Already in Body",
                available_label="Discoverable",
            ),
            "mcps": self._section_payload(
                "mcps",
                "MCPs",
                mcps,
                read_only=True,
                installed_label="Connected in Body",
                available_label="Discoverable",
            ),
            "kits_lumen": {
                **self._section_payload(
                    "kits_lumen",
                    "Kits Lumen",
                    kits["items"],
                    read_only=False,
                    installed_label="Installed in Body",
                    available_label="Available to install",
                ),
                "installed": kits["installed"],
                "available": kits["available"],
                "upload_enabled": True,
            },
        }

    def kits_catalog(self) -> list[dict[str, Any]]:
        return self.snapshot()["kits_lumen"]["available"]

    def kits_installed(self) -> list[dict[str, Any]]:
        return self.snapshot()["kits_lumen"]["installed"]

    def _section_payload(
        self,
        key: str,
        title: str,
        items: list[dict[str, Any]],
        *,
        read_only: bool,
        installed_label: str,
        available_label: str,
    ) -> dict[str, Any]:
        installed = [item for item in items if item.get("installed")]
        available = [item for item in items if not item.get("installed")]
        return {
            "key": key,
            "title": title,
            "read_only": read_only,
            "items": items,
            "installed": installed,
            "available": available,
            "installed_label": installed_label,
            "available_label": available_label,
            "counts": {
                "total": len(items),
                "installed": len(installed),
                "available": len(available),
            },
        }

    def _build_kits(
        self, runtime_surface: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        catalog_cards = [
            self._catalog_kit_card(entry)
            for entry in self.catalog.list_all(
                registry=self.registry,
                connectors=self.connectors,
            )
        ]
        runtime_cards = [
            self._runtime_card(
                cap, category="kits_lumen", runtime_surface=runtime_surface
            )
            for cap in self.registry.list_by_kind(CapabilityKind.MODULE)
        ]
        items = self._merge_cards(catalog_cards, runtime_cards)
        return {
            "items": items,
            "installed": [item for item in items if item.get("installed")],
            "available": [item for item in items if not item.get("installed")],
        }

    def _catalog_kit_card(self, entry: dict[str, Any]) -> dict[str, Any]:
        compatibility = self._with_badge(entry.get("compatibility") or {})
        installed = self.registry.get(CapabilityKind.MODULE, entry["name"]) is not None
        return {
            "id": f"kit:{entry['name']}",
            "name": entry["name"],
            "display_name": entry.get("display_name", entry["name"]),
            "description": entry.get("description", ""),
            "kind": "kit",
            "category": "kits_lumen",
            "installed": installed,
            "runtime": False,
            "status": "installed" if installed else "catalog",
            "version": entry.get("version", "0.0.0"),
            "price": entry.get("price", "free"),
            "min_capability": entry.get("min_capability", "tier-1"),
            "tags": entry.get("tags", []),
            "provides": entry.get("provides", []),
            "requires": entry.get("requires", {}),
            "compatibility": compatibility,
            "sources": [{"type": "kits_lumen", "label": "Kits Lumen"}],
            "actions": {
                "read_only": False,
                "can_install": not installed,
                "can_uninstall": installed,
            },
        }

    def _runtime_card(
        self,
        capability: Capability,
        *,
        category: str,
        runtime_surface: dict[str, Any],
    ) -> dict[str, Any]:
        compatibility = capability.metadata.get("cerebelo") or {}
        if not compatibility:
            artifact = normalize_capability(capability)
            if artifact is not None:
                compatibility = calculate_compatibility(artifact, runtime_surface)
        compatibility = self._with_badge(compatibility)

        kind = (
            "kit" if capability.kind == CapabilityKind.MODULE else capability.kind.value
        )
        display_name = capability.metadata.get("display_name") or capability.name
        return {
            "id": f"{kind}:{capability.name}",
            "name": capability.name,
            "display_name": display_name,
            "description": capability.description,
            "kind": kind,
            "category": category,
            "installed": True,
            "runtime": True,
            "status": capability.status.value,
            "version": capability.metadata.get("version", "0.0.0"),
            "price": capability.metadata.get("price", "free"),
            "min_capability": capability.metadata.get(
                "min_capability", capability.min_capability
            ),
            "tags": capability.metadata.get("tags", []),
            "provides": list(capability.provides),
            "requires": dict(capability.requires),
            "compatibility": compatibility,
            "sources": [{"type": "body", "label": "Body"}],
            "actions": {
                "read_only": capability.kind != CapabilityKind.MODULE,
                "can_install": False,
                "can_uninstall": capability.kind == CapabilityKind.MODULE,
            },
            "metadata": capability.metadata,
        }

    def _load_remote(self, runtime_surface: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        if (
            self._remote_cache is not None
            and now - self._remote_cache_at < self.cache_ttl_seconds
        ):
            return self._remote_cache

        result = {"skills": [], "mcps": [], "feeds": []}
        for feed in self._feed_configs():
            feed_meta = {
                "name": feed["name"],
                "url": feed["url"],
                "status": "ok",
                "items": 0,
            }
            try:
                payload = self._fetch_json(feed["url"])
                for bucket, item in self._parse_remote_payload(
                    payload, feed, runtime_surface
                ):
                    result[bucket].append(item)
                    feed_meta["items"] += 1
            except Exception as exc:  # pragma: no cover - defensive path
                feed_meta["status"] = "error"
                feed_meta["error"] = str(exc)
            result["feeds"].append(feed_meta)

        self._remote_cache = result
        self._remote_cache_at = now
        return result

    def _feed_configs(self) -> list[dict[str, str]]:
        feeds: list[dict[str, str]] = []
        configured = self.config.get("marketplace", {}).get("feeds", [])
        for entry in configured:
            if isinstance(entry, str):
                feeds.append({"name": self._infer_feed_name(entry), "url": entry})
            elif isinstance(entry, dict) and entry.get("url"):
                feeds.append(
                    {
                        "name": str(
                            entry.get("name") or self._infer_feed_name(entry["url"])
                        ),
                        "url": str(entry["url"]),
                    }
                )

        env_feeds = os.getenv("LUMEN_MARKETPLACE_FEEDS", "")
        for url in [value.strip() for value in env_feeds.split(",") if value.strip()]:
            feeds.append({"name": self._infer_feed_name(url), "url": url})
        return feeds

    def _fetch_json(self, url: str) -> Any:
        try:
            with urlopen(url, timeout=self.remote_timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except URLError as exc:  # pragma: no cover - depends on network
            raise RuntimeError(
                f"cannot read remote marketplace feed '{url}': {exc}"
            ) from exc

    def _parse_remote_payload(
        self,
        payload: Any,
        feed: dict[str, str],
        runtime_surface: dict[str, Any],
    ):
        source_name = feed["name"]
        source_type = self._source_type_for(source_name)

        if isinstance(payload, list):
            payload = {"items": payload}
        if not isinstance(payload, dict):
            return []

        entries: list[tuple[str, dict[str, Any]]] = []
        for item in payload.get("skills", []):
            card = self._remote_skill_card(
                item, runtime_surface, source_name, source_type
            )
            if card:
                entries.append(("skills", card))
        for item in payload.get("mcps", []):
            card = self._remote_mcp_card(
                item, runtime_surface, source_name, source_type
            )
            if card:
                entries.append(("mcps", card))
        for item in payload.get("items", []):
            kind = str(item.get("kind") or item.get("type") or "skill").lower()
            if kind == "mcp":
                card = self._remote_mcp_card(
                    item, runtime_surface, source_name, source_type
                )
                if card:
                    entries.append(("mcps", card))
            else:
                card = self._remote_skill_card(
                    item, runtime_surface, source_name, source_type
                )
                if card:
                    entries.append(("skills", card))
        return entries

    def _remote_skill_card(
        self,
        raw: dict[str, Any],
        runtime_surface: dict[str, Any],
        source_name: str,
        source_type: str,
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or not raw.get("name"):
            return None
        artifact = normalize_openclaw_metadata(raw)
        artifact.source_type = source_type
        artifact.installed = False
        compatibility = self._remote_compatibility(artifact, runtime_surface)
        return self._artifact_card(
            artifact,
            compatibility=compatibility,
            category="skills",
            source_name=source_name,
            display_name=raw.get("display_name") or raw.get("title"),
            tags=raw.get("tags", []),
            version=raw.get("version", "remote"),
        )

    def _remote_mcp_card(
        self,
        raw: dict[str, Any],
        runtime_surface: dict[str, Any],
        source_name: str,
        source_type: str,
    ) -> dict[str, Any] | None:
        if not isinstance(raw, dict) or not raw.get("name"):
            return None

        provides = _string_list(
            raw.get("provides") or raw.get("tools") or raw.get("capabilities")
        )
        artifact = NormalizedArtifact(
            name=str(raw["name"]),
            kind="mcp",
            source_type=source_type,
            description=str(raw.get("description", "")),
            provides=provides,
            requires=normalize_requires(raw),
            tool_refs=_string_list(
                raw.get("tool_refs") or raw.get("required_tools") or raw.get("tools")
            ),
            installed=False,
            metadata={"remote_source": source_name},
        )
        compatibility = self._remote_compatibility(artifact, runtime_surface)
        return self._artifact_card(
            artifact,
            compatibility=compatibility,
            category="mcps",
            source_name=source_name,
            display_name=raw.get("display_name") or raw.get("title"),
            tags=raw.get("tags", []),
            version=raw.get("version", "remote"),
        )

    def _artifact_card(
        self,
        artifact: NormalizedArtifact,
        *,
        compatibility: dict[str, Any],
        category: str,
        source_name: str,
        display_name: str | None = None,
        tags: list[str] | None = None,
        version: str | None = None,
    ) -> dict[str, Any]:
        return {
            "id": f"{artifact.kind}:{artifact.name}",
            "name": artifact.name,
            "display_name": display_name
            or artifact.metadata.get("display_name")
            or artifact.name,
            "description": artifact.description,
            "kind": artifact.kind,
            "category": category,
            "installed": False,
            "runtime": False,
            "status": "remote",
            "version": version or "remote",
            "price": "read-only",
            "min_capability": artifact.metadata.get("min_capability", "tier-1"),
            "tags": tags or [],
            "provides": list(artifact.provides),
            "requires": dict(artifact.requires),
            "compatibility": compatibility,
            "sources": [{"type": artifact.source_type, "label": source_name}],
            "actions": {
                "read_only": True,
                "can_install": False,
                "can_uninstall": False,
            },
        }

    def _merge_cards(
        self,
        incoming: list[dict[str, Any]],
        runtime_cards: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {
            self._merge_key(card): dict(card) for card in incoming
        }
        for card in runtime_cards:
            key = self._merge_key(card)
            existing = merged.get(key)
            if not existing:
                merged[key] = dict(card)
                continue

            combined = dict(existing)
            for field in (
                "description",
                "version",
                "min_capability",
                "price",
                "tags",
                "provides",
                "requires",
            ):
                value = card.get(field)
                if value not in (None, "", [], {}):
                    combined[field] = value
            combined["installed"] = card.get("installed", False) or existing.get(
                "installed", False
            )
            combined["runtime"] = card.get("runtime", False) or existing.get(
                "runtime", False
            )
            combined["status"] = card.get("status") or existing.get("status")
            combined["compatibility"] = card.get("compatibility") or existing.get(
                "compatibility"
            )
            combined["actions"] = card.get("actions") or existing.get("actions")
            combined["sources"] = _dedupe_sources(
                existing.get("sources", []) + card.get("sources", [])
            )
            merged[key] = combined

        return sorted(
            merged.values(),
            key=lambda item: (
                not item.get("installed", False),
                item.get("display_name", item["name"]).lower(),
            ),
        )

    def _merge_key(self, card: dict[str, Any]) -> str:
        return f"{card.get('category')}::{card.get('name', '').lower()}"

    def _with_badge(self, compatibility: dict[str, Any]) -> dict[str, Any]:
        status = compatibility.get("status", COMPAT_BLOCKED)
        badge = COMPAT_BADGES.get(status, COMPAT_BADGES[COMPAT_BLOCKED])
        return {
            **compatibility,
            "status": status,
            "badge": {
                "emoji": badge["emoji"],
                "label": badge["label"],
                "status": status,
            },
        }

    def _remote_compatibility(
        self,
        artifact: NormalizedArtifact,
        runtime_surface: dict[str, Any],
    ) -> dict[str, Any]:
        compatibility = calculate_compatibility(artifact, runtime_surface)
        if compatibility.get("status") == COMPAT_READY:
            compatibility = {**compatibility, "status": COMPAT_INSTALLABLE}
        return self._with_badge(compatibility)

    def _infer_feed_name(self, url: str) -> str:
        lowered = url.lower()
        if "clawhub" in lowered:
            return "ClawHub"
        if "openclaw" in lowered:
            return "OpenClaw"
        return "Remote Feed"

    def _source_type_for(self, source_name: str) -> str:
        lowered = source_name.lower()
        if "clawhub" in lowered:
            return "clawhub"
        if "openclaw" in lowered:
            return "openclaw"
        return "remote"


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


def _dedupe_sources(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        key = (str(value.get("type", "")), str(value.get("label", "")))
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result
