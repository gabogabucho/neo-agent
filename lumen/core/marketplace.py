"""Marketplace read model for dashboard/API consumption.

Keeps marketplace logic on the server side by merging runtime truth from the
registry, the local installable module catalog, and optional remote read-only
feeds.
"""

from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from lumen.core.catalog import Catalog
from lumen.core.cerebellum import (
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
from lumen.core.interoperability import (
    INTEROP_ADAPTED,
    INTEROP_NATIVE,
    INTEROP_OPAQUE,
    classify_capability_interoperability,
    classify_interoperability,
)
from lumen.core.registry import Capability, CapabilityKind, Registry


COMPAT_BADGES = {
    COMPAT_READY: {"emoji": "🟢", "label": "Ready"},
    COMPAT_INSTALLABLE: {"emoji": "🟡", "label": "Installable"},
    COMPAT_PARTIAL: {"emoji": "🟠", "label": "Partial"},
    COMPAT_BLOCKED: {"emoji": "🔴", "label": "Blocked"},
}


def humanize_module_name(name: str, display_name: str | None = None) -> str:
    """Return a human-friendly label for a module name.

    Strips the conventional `x-lumen-` namespace prefix when no explicit
    `display_name` is provided, so UIs never surface raw machine names like
    `x-lumen-comunicacion-telegram`.
    """
    if display_name:
        return display_name
    if not name:
        return name
    cleaned = name
    if cleaned.startswith("x-lumen-"):
        cleaned = cleaned[len("x-lumen-") :]
    return cleaned.replace("-", " ").replace("_", " ").strip().title() or name


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
        runtime_surface = build_runtime_surface(
            self.connectors,
            self.registry,
            model=self.config.get("model"),
        )
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
        kits = self._build_product_section(
            runtime_surface,
            product_kind="kit",
            remote_items=[],
        )
        modules = self._build_product_section(
            runtime_surface,
            product_kind="module",
            remote_items=remote["mcps"],
        )

        return {
            "generated_at": int(time.time()),
            "feeds": remote["feeds"],
            "tabs": [
                {
                    "key": "modules",
                    "label": "Modules",
                    "count": len(modules["items"]),
                },
                {
                    "key": "kits",
                    "label": "Kits",
                    "count": len(kits["items"]),
                },
                {"key": "skills", "label": "Skills", "count": len(skills)},
            ],
            "modules": {
                **self._section_payload(
                    "modules",
                    "Modules",
                    modules["items"],
                    read_only=False,
                    installed_label="Installed modules",
                    available_label="Available modules",
                ),
                "installed": modules["installed"],
                "available": modules["available"],
                "upload_enabled": True,
            },
            "kits": {
                **self._section_payload(
                    "kits",
                    "Kits",
                    kits["items"],
                    read_only=False,
                    installed_label="Installed kits",
                    available_label="Available kits",
                ),
                "installed": kits["installed"],
                "available": kits["available"],
                "upload_enabled": False,
            },
            "skills": self._section_payload(
                "skills",
                "Skills",
                skills,
                read_only=True,
                installed_label="Already in Body",
                available_label="Discoverable",
            ),
        }

    def kits_catalog(self) -> list[dict[str, Any]]:
        return self.snapshot()["kits"]["available"]

    def kits_installed(self) -> list[dict[str, Any]]:
        return self.snapshot()["kits"]["installed"]

    def modules_catalog(self) -> list[dict[str, Any]]:
        return self.snapshot()["modules"]["available"]

    def modules_installed(self) -> list[dict[str, Any]]:
        return self.snapshot()["modules"]["installed"]

    def sync_registry(self, registry: Registry):
        """Update runtime registry truth and invalidate cached projections."""
        self.registry = registry
        self._remote_cache = None
        self._remote_cache_at = 0.0

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

    def _build_product_section(
        self,
        runtime_surface: dict[str, Any],
        *,
        product_kind: str,
        remote_items: list[dict[str, Any]],
    ) -> dict[str, list[dict[str, Any]]]:
        catalog_cards = [
            self._catalog_kit_card(entry)
            for entry in self.catalog.list_all(
                registry=self.registry,
                connectors=self.connectors,
                model=self.config.get("model"),
            )
        ]
        runtime_cards = [
            self._runtime_card(
                cap, category=product_kind, runtime_surface=runtime_surface
            )
            for kind in (
                [CapabilityKind.MODULE]
                if product_kind == "kit"
                else [CapabilityKind.MODULE, CapabilityKind.MCP]
            )
            for cap in self.registry.list_by_kind(kind)
        ]
        items = self._merge_cards(remote_items, catalog_cards, runtime_cards)
        items = [item for item in items if item.get("kind") == product_kind]
        return {
            "items": items,
            "installed": [item for item in items if item.get("installed")],
            "available": [item for item in items if not item.get("installed")],
        }

    def _catalog_kit_card(self, entry: dict[str, Any]) -> dict[str, Any]:
        compatibility = self._with_badge(entry.get("compatibility") or {})
        installed = self.registry.get(CapabilityKind.MODULE, entry["name"]) is not None
        path = str(entry.get("path") or "")
        is_personality_kit = self._product_kind_for_entry(entry) == "kit"
        source_label = "Kits Lumen" if is_personality_kit else "Lumen Modules"
        kind = "kit" if is_personality_kit else "module"
        return {
            "id": f"{kind}:{entry['name']}",
            "name": entry["name"],
            "display_name": humanize_module_name(
                entry["name"], entry.get("display_name")
            ),
            "description": entry.get("description", ""),
            "kind": kind,
            "category": kind,
            "installed": installed,
            "runtime": False,
            "status": "installed" if installed else "catalog",
            "version": entry.get("version", "0.0.0"),
            "path": entry.get("path"),
            "price": entry.get("price", "free"),
            "min_capability": entry.get("min_capability", "tier-1"),
            "tags": entry.get("tags", []),
            "provides": entry.get("provides", []),
            "requires": entry.get("requires", {}),
            "compatibility": compatibility,
            "interoperability": classify_interoperability(
                source_type="catalog_entry",
                metadata=entry,
                can_install=not installed,
            ),
            "sources": [{"type": kind, "label": source_label}],
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
        artifact = normalize_capability(capability)
        if artifact is not None:
            compatibility = calculate_compatibility(artifact, runtime_surface)
        compatibility = self._with_badge(compatibility)

        kind = self._product_kind_for_capability(capability)
        display_name = humanize_module_name(
            capability.name, capability.metadata.get("display_name")
        )
        source_label = (
            "Installed kit"
            if kind == "kit"
            else "Installed module"
            if capability.kind == CapabilityKind.MODULE
            else "MCP module"
        )
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
            "path": capability.metadata.get("path"),
            "price": capability.metadata.get("price", "free"),
            "min_capability": capability.metadata.get(
                "min_capability", capability.min_capability
            ),
            "tags": capability.metadata.get("tags", []),
            "provides": list(capability.provides),
            "requires": dict(capability.requires),
            "compatibility": compatibility,
            "interoperability": capability.metadata.get("interoperability")
            or classify_capability_interoperability(capability),
            "sources": [{"type": kind, "label": source_label}],
            "actions": {
                "read_only": capability.kind
                not in {CapabilityKind.MODULE, CapabilityKind.MCP},
                "can_install": False,
                "can_uninstall": capability.kind == CapabilityKind.MODULE,
            },
            "metadata": capability.metadata,
        }

    def _product_kind_for_entry(self, entry: dict[str, Any]) -> str:
        tags = set(entry.get("tags") or [])
        path = str(entry.get("path") or "")
        x_lumen = entry.get("x-lumen") or entry.get("x_lumen") or {}
        if x_lumen.get("product_kind") == "kit":
            return "kit"
        if path.startswith("kits/") or "personality" in tags:
            return "kit"
        return "module"

    def _product_kind_for_capability(self, capability: Capability) -> str:
        if capability.kind == CapabilityKind.MCP:
            return "module"
        tags = set(capability.metadata.get("tags") or [])
        path = str(capability.metadata.get("path") or "")
        x_lumen = capability.metadata.get("x_lumen") or {}
        if x_lumen.get("product_kind") == "kit":
            return "kit"
        if "personality" in tags or "catalog/kits/" in path:
            return "kit"
        return "module"

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

        # Append community defaults unless explicitly disabled. User/env config
        # always wins — defaults only fill in what isn't already there.
        if os.getenv("LUMEN_MARKETPLACE_DISABLE_DEFAULTS", "").lower() not in (
            "1",
            "true",
            "yes",
        ):
            existing_urls = {f["url"] for f in feeds}
            for default in DEFAULT_FEEDS:
                if default["url"] not in existing_urls:
                    feeds.append(dict(default))
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

        # Format detection: dispatch by payload shape.
        # - Native Lumen feeds: {skills: [...], mcps: [...]} (or legacy {items})
        # - ClawHub API v1:     {results: [{slug, displayName, summary, ...}]}
        # - MCP Registry v0:    {servers: [{server: {name, description, remotes, ...}}]}
        if "results" in payload and "skills" not in payload and "mcps" not in payload:
            return self._parse_clawhub_payload(
                payload, source_name, source_type, runtime_surface
            )
        if "servers" in payload and "skills" not in payload and "mcps" not in payload:
            return self._parse_mcp_registry_payload(
                payload, source_name, source_type, runtime_surface
            )

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

    def _parse_clawhub_payload(
        self,
        payload: dict[str, Any],
        source_name: str,
        source_type: str,
        runtime_surface: dict[str, Any],
    ):
        """ClawHub /api/v1/search → Lumen skills cards.

        ClawHub items look like:
          {slug, displayName, summary, score, version, updatedAt}
        We pre-map to the native skill shape and reuse _remote_skill_card.
        """
        entries: list[tuple[str, dict[str, Any]]] = []
        for item in payload.get("results", []):
            raw = _clawhub_item_to_skill_raw(item)
            if not raw:
                continue
            card = self._remote_skill_card(
                raw, runtime_surface, source_name, source_type
            )
            if card:
                entries.append(("skills", card))
        return entries

    def _parse_mcp_registry_payload(
        self,
        payload: dict[str, Any],
        source_name: str,
        source_type: str,
        runtime_surface: dict[str, Any],
    ):
        """Anthropic MCP Registry /v0/servers → Lumen mcps cards.

        Items look like:
          {server: {name, title, description, version, remotes[], repository}, _meta: {...}}
        Remote transports (sse, streamable-http) are listed but flagged as
        unsupported by the current Lumen MCP runtime (stdio only).
        """
        entries: list[tuple[str, dict[str, Any]]] = []
        for item in payload.get("servers", []):
            raw = _mcp_registry_item_to_mcp_raw(item)
            if not raw:
                continue
            card = self._remote_mcp_card(raw, runtime_surface, source_name, source_type)
            if card:
                entries.append(("mcps", card))
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
            can_install=True,
            read_only=False,
            extra={
                "install_spec": raw.get("install"),
                "source_url": raw.get("source_url"),
                "source_type": source_type,
                "interoperability": classify_interoperability(
                    source_type=source_type,
                    metadata=raw,
                    can_install=True,
                    install_spec=raw.get("install"),
                ),
            },
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
            metadata={
                "remote_source": source_name,
                "min_capability": raw.get("min_capability", "tier-1"),
            },
        )
        compatibility = self._remote_compatibility(artifact, runtime_surface)
        remote_transport = raw.get("remote_transport") or {}
        transport_type = str(remote_transport.get("type") or "stdio").strip().lower()
        can_install = source_type == "mcp-registry" and transport_type in {"", "stdio"}
        if transport_type and transport_type != "stdio":
            compatibility = self._with_badge(
                {
                    **compatibility,
                    "status": COMPAT_BLOCKED,
                    "reasons": list(compatibility.get("reasons", []))
                    + [f"requires remote MCP transport support ({transport_type})"],
                    "warnings": compatibility.get("warnings", []),
                }
            )
        return self._artifact_card(
            artifact,
            compatibility=compatibility,
            category="modules",
            source_name=source_name,
            display_name=raw.get("display_name") or raw.get("title"),
            tags=raw.get("tags", []),
            version=raw.get("version", "remote"),
            can_install=can_install,
            read_only=not can_install,
            extra={
                "remote_transport": raw.get("remote_transport"),
                "source_url": raw.get("source_url"),
                "source_type": source_type,
                "interoperability": classify_interoperability(
                    source_type=source_type,
                    metadata=raw,
                    can_install=can_install,
                    remote_transport=raw.get("remote_transport"),
                ),
            },
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
        can_install: bool = False,
        read_only: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        kind = "module" if artifact.kind == "mcp" else artifact.kind
        card = {
            "id": f"{kind}:{artifact.name}",
            "name": artifact.name,
            "display_name": display_name
            or artifact.metadata.get("display_name")
            or artifact.name,
            "description": artifact.description,
            "kind": kind,
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
                "read_only": read_only,
                "can_install": can_install,
                "can_uninstall": False,
            },
        }
        if extra:
            card.update(extra)
        return card

    def _merge_cards(
        self,
        *card_groups: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for cards in card_groups:
            for card in cards:
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
                    "display_name",
                ):
                    value = card.get(field)
                    if value not in (None, "", [], {}):
                        combined[field] = value
                combined["interoperability"] = _prefer_interoperability(
                    existing.get("interoperability"), card.get("interoperability")
                )
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
            key=self._sort_key,
        )

    def _sort_key(self, item: dict[str, Any]) -> tuple[bool, bool, str]:
        return (
            not item.get("installed", False),
            not self._is_personality_first(item),
            item.get("display_name", item["name"]).lower(),
        )

    def _is_personality_first(self, item: dict[str, Any]) -> bool:
        if item.get("kind") != "kit":
            return False
        return "personality" in {
            str(tag).strip().lower() for tag in item.get("tags", []) if str(tag).strip()
        }

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
        if "modelcontextprotocol" in lowered or "mcp-registry" in lowered:
            return "MCP Registry"
        return "Remote Feed"

    def _source_type_for(self, source_name: str) -> str:
        lowered = source_name.lower()
        if "clawhub" in lowered:
            return "clawhub"
        if "openclaw" in lowered:
            return "openclaw"
        if "mcp registry" in lowered or "mcp-registry" in lowered:
            return "mcp-registry"
        return "remote"


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)]


# Default community feeds shipped with Lumen. Merged into user config unless
# LUMEN_MARKETPLACE_DISABLE_DEFAULTS is set.
DEFAULT_FEEDS: list[dict[str, str]] = [
    {
        "name": "MCP Registry",
        "url": "https://registry.modelcontextprotocol.io/v0/servers?limit=100",
    },
    {
        "name": "ClawHub",
        "url": "https://clawhub.ai/api/v1/search?q=skill&limit=50",
    },
]


def _clawhub_item_to_skill_raw(item: Any) -> dict[str, Any] | None:
    """Map a ClawHub /api/v1/search result into the native skill raw shape.

    Input:  {slug, displayName, summary, score, version, updatedAt}
    Output: dict compatible with _remote_skill_card / normalize_openclaw_metadata.
    """
    if not isinstance(item, dict):
        return None
    slug = str(item.get("slug") or "").strip()
    if not slug:
        return None
    version = item.get("version")
    return {
        "name": slug,
        "display_name": item.get("displayName") or slug,
        "description": item.get("summary") or "",
        "version": str(version) if version else "latest",
        "tags": ["clawhub", "skill"],
        "install": {
            "method": "npx",
            "target": f"clawhub@latest install {slug}",
        },
        "source_url": f"https://clawhub.ai/skills/{slug}",
        "provides": [],
        "requires": {},
    }


def _mcp_registry_item_to_mcp_raw(item: Any) -> dict[str, Any] | None:
    """Map an Anthropic MCP Registry entry into the native MCP raw shape.

    Input:  {server: {name, title, description, version, remotes, repository}, _meta: {...}}
    Output: dict compatible with _remote_mcp_card.

    Remote transports (sse, streamable-http) are preserved in metadata so the
    install bridge can later wire them as remote MCPs. Until the Lumen MCP
    runtime supports non-stdio transports, cards are still listable but will
    be flagged in metadata.remote_transport.
    """
    if not isinstance(item, dict):
        return None
    server = item.get("server")
    if not isinstance(server, dict):
        return None
    raw_name = str(server.get("name") or "").strip()
    if not raw_name:
        return None

    # MCP Registry names often use slashes: "ac.inference.sh/mcp".
    # Keep original in display_name; sanitize for Lumen internal name.
    safe_name = raw_name.replace("/", "-").replace(" ", "-")

    remotes = server.get("remotes") or []
    primary_remote: dict[str, Any] | None = None
    if isinstance(remotes, list) and remotes:
        first = remotes[0]
        if isinstance(first, dict):
            primary_remote = first

    repository = server.get("repository")
    repo_url = ""
    if isinstance(repository, dict):
        repo_url = str(repository.get("url") or "")

    return {
        "name": safe_name,
        "display_name": server.get("title") or raw_name,
        "description": str(server.get("description") or ""),
        "version": str(server.get("version") or "latest"),
        "tags": ["mcp-registry", "mcp"],
        "provides": [],
        "requires": {},
        "source_url": repo_url,
        "remote_transport": (
            {
                "type": primary_remote.get("type"),
                "url": primary_remote.get("url"),
            }
            if primary_remote
            else None
        ),
        "original_name": raw_name,
    }


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


def _prefer_interoperability(
    existing: dict[str, Any] | None, incoming: dict[str, Any] | None
) -> dict[str, Any] | None:
    if incoming in (None, {}):
        return existing
    if existing in (None, {}):
        return incoming

    rank = {
        INTEROP_NATIVE: 0,
        INTEROP_ADAPTED: 1,
        INTEROP_OPAQUE: 2,
    }
    existing_rank = rank.get(str(existing.get("level") or "").strip().lower(), -1)
    incoming_rank = rank.get(str(incoming.get("level") or "").strip().lower(), -1)
    return incoming if incoming_rank > existing_rank else existing
