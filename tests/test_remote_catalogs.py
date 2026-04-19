"""Remote marketplace catalog adapters.

Covers ClawHub (/api/v1/search) and Anthropic MCP Registry (/v0/servers):
  - shape detection in _parse_remote_payload
  - per-item pre-mappers produce canonical raw shapes
  - end-to-end Marketplace.snapshot merges remote items
  - default feeds are injected unless disabled
  - native {skills, mcps} shape still wins (back-compat)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry
from lumen.core.marketplace import (
    DEFAULT_FEEDS,
    Marketplace,
    _clawhub_item_to_skill_raw,
    _mcp_registry_item_to_mcp_raw,
)
from lumen.core.registry import Registry


FIXTURES = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, encoding="utf-8") as fh:
        return json.load(fh)


def _build_marketplace() -> Marketplace:
    return Marketplace(
        catalog=Catalog(),
        registry=Registry(),
        connectors=ConnectorRegistry(),
        config={},
    )


# --- Pre-mappers ---------------------------------------------------------


def test_clawhub_pre_mapper_produces_canonical_shape():
    payload = _load_fixture("clawhub_search.json")
    item = payload["results"][0]

    raw = _clawhub_item_to_skill_raw(item)

    assert raw is not None
    assert raw["name"] == "email-daily-summary"
    assert raw["description"]
    assert raw["install"]["method"] == "npx"
    assert "clawhub@latest install email-daily-summary" in raw["install"]["target"]
    assert "clawhub" in raw["tags"]
    assert raw["source_url"].endswith("/email-daily-summary")


def test_clawhub_pre_mapper_rejects_bad_input():
    assert _clawhub_item_to_skill_raw(None) is None
    assert _clawhub_item_to_skill_raw({}) is None
    assert _clawhub_item_to_skill_raw({"slug": ""}) is None
    assert _clawhub_item_to_skill_raw("not-a-dict") is None


def test_mcp_registry_pre_mapper_produces_canonical_shape():
    payload = _load_fixture("mcp_registry.json")
    item = payload["servers"][0]

    raw = _mcp_registry_item_to_mcp_raw(item)

    assert raw is not None
    # Slash sanitized for internal name
    assert raw["name"] == "ac.inference.sh-mcp"
    assert raw["original_name"] == "ac.inference.sh/mcp"
    assert raw["display_name"] == "inference.sh"
    assert raw["description"]
    assert "mcp-registry" in raw["tags"]
    # Remote transport preserved for future install-bridge wiring
    assert raw["remote_transport"] is not None
    assert raw["remote_transport"]["type"] == "streamable-http"


def test_mcp_registry_pre_mapper_rejects_bad_input():
    assert _mcp_registry_item_to_mcp_raw(None) is None
    assert _mcp_registry_item_to_mcp_raw({}) is None
    assert _mcp_registry_item_to_mcp_raw({"server": {}}) is None
    assert _mcp_registry_item_to_mcp_raw({"server": {"name": ""}}) is None


# --- End-to-end through Marketplace.snapshot -----------------------------


def test_snapshot_integrates_clawhub_feed():
    market = _build_marketplace()
    clawhub_payload = _load_fixture("clawhub_search.json")

    with patch.object(market, "_feed_configs", return_value=[
        {"name": "ClawHub", "url": "https://clawhub.ai/api/v1/search?q=email&limit=3"}
    ]):
        with patch.object(market, "_fetch_json", return_value=clawhub_payload):
            snapshot = market.snapshot()

    slugs = {item["name"] for item in snapshot["skills"]["items"]}
    assert "email-daily-summary" in slugs
    assert "porteden-email" in slugs

    # Feed metadata is surfaced so the UI can render sources
    feeds = snapshot["feeds"]
    assert any(f["name"] == "ClawHub" and f["items"] == 3 for f in feeds)


def test_snapshot_integrates_mcp_registry_feed():
    market = _build_marketplace()
    mcp_payload = _load_fixture("mcp_registry.json")

    with patch.object(market, "_feed_configs", return_value=[
        {
            "name": "MCP Registry",
            "url": "https://registry.modelcontextprotocol.io/v0/servers?limit=3",
        }
    ]):
        with patch.object(market, "_fetch_json", return_value=mcp_payload):
            snapshot = market.snapshot()

    mcp_names = {item["name"] for item in snapshot["modules"]["items"]}
    # The registry fixture has 3 versions of the same server — dedup by safe name
    assert "ac.inference.sh-mcp" in mcp_names


def test_mcp_registry_remote_transport_is_marked_opaque_when_not_stdio():
    market = _build_marketplace()
    mcp_payload = _load_fixture("mcp_registry.json")

    with patch.object(market, "_feed_configs", return_value=[
        {
            "name": "MCP Registry",
            "url": "https://registry.modelcontextprotocol.io/v0/servers?limit=3",
        }
    ]):
        with patch.object(market, "_fetch_json", return_value=mcp_payload):
            snapshot = market.snapshot()

    card = next(item for item in snapshot["modules"]["items"] if item["name"] == "ac.inference.sh-mcp")
    assert card["interoperability"]["level"] == "opaque"
    assert card["interoperability"]["install_path"] == "manual"


def test_native_shape_still_works_for_back_compat():
    """A feed publishing the original {skills, mcps} shape must keep working."""
    market = _build_marketplace()
    native_payload = {
        "skills": [
            {
                "name": "legacy-skill",
                "description": "old-format skill",
                "provides": ["search"],
            }
        ],
        "mcps": [
            {
                "name": "legacy-mcp",
                "description": "old-format mcp",
            }
        ],
    }

    with patch.object(market, "_feed_configs", return_value=[
        {"name": "Legacy", "url": "https://example.com/feed.json"}
    ]):
        with patch.object(market, "_fetch_json", return_value=native_payload):
            snapshot = market.snapshot()

    skill_names = {item["name"] for item in snapshot["skills"]["items"]}
    mcp_names = {item["name"] for item in snapshot["modules"]["items"]}
    assert "legacy-skill" in skill_names
    assert "legacy-mcp" in mcp_names


# --- Default feeds -------------------------------------------------------


def test_default_feeds_injected_when_user_has_none():
    market = _build_marketplace()
    os.environ.pop("LUMEN_MARKETPLACE_DISABLE_DEFAULTS", None)
    os.environ.pop("LUMEN_MARKETPLACE_FEEDS", None)

    feeds = market._feed_configs()

    urls = {f["url"] for f in feeds}
    for default in DEFAULT_FEEDS:
        assert default["url"] in urls


def test_default_feeds_can_be_disabled_via_env():
    market = _build_marketplace()
    os.environ["LUMEN_MARKETPLACE_DISABLE_DEFAULTS"] = "1"
    try:
        feeds = market._feed_configs()
        assert feeds == []
    finally:
        del os.environ["LUMEN_MARKETPLACE_DISABLE_DEFAULTS"]


def test_user_configured_feed_is_not_duplicated_by_defaults():
    market = Marketplace(
        catalog=Catalog(),
        registry=Registry(),
        connectors=ConnectorRegistry(),
        config={
            "marketplace": {
                "feeds": [DEFAULT_FEEDS[0]],  # user pins the same URL
            }
        },
    )
    os.environ.pop("LUMEN_MARKETPLACE_DISABLE_DEFAULTS", None)

    feeds = market._feed_configs()

    matching = [f for f in feeds if f["url"] == DEFAULT_FEEDS[0]["url"]]
    assert len(matching) == 1


# --- Infer helpers -------------------------------------------------------


def test_infer_feed_name_recognizes_mcp_registry():
    market = _build_marketplace()
    assert (
        market._infer_feed_name("https://registry.modelcontextprotocol.io/v0/servers")
        == "MCP Registry"
    )


def test_source_type_recognizes_mcp_registry():
    market = _build_marketplace()
    assert market._source_type_for("MCP Registry") == "mcp-registry"
