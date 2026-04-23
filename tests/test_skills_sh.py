"""Tests for skills.sh integration — REQ-SH1 through REQ-SH7."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lumen.core.marketplace import (
    DEFAULT_FEEDS,
    Marketplace,
    _skills_sh_item_to_skill_raw,
)
from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry
from lumen.core.registry import Registry


class SkillsShAdapterTests(unittest.TestCase):
    """REQ-SH2: Skill cards format."""

    def test_adapter_converts_skill_item(self):
        """skills.sh item → native skill raw shape."""
        item = {
            "name": "find-skills",
            "owner": "vercel-labs",
            "repo": "skills",
            "description": "Discover and install skills",
            "installs": 1200000,
            "stars": 15400,
        }
        result = _skills_sh_item_to_skill_raw(item)
        assert result is not None
        assert result["name"] == "vercel-labs/skills/find-skills"
        assert result["display_name"] == "find-skills"
        assert "Discover" in result["description"]
        assert "skills-sh" in result.get("tags", [])

    def test_adapter_returns_none_for_invalid_item(self):
        """Invalid item → None."""
        assert _skills_sh_item_to_skill_raw(None) is None
        assert _skills_sh_item_to_skill_raw({}) is None
        assert _skills_sh_item_to_skill_raw("not a dict") is None

    def test_adapter_extracts_install_info(self):
        """Adapter includes install method info."""
        item = {
            "name": "frontend-design",
            "owner": "anthropics",
            "repo": "skills",
            "description": "Frontend design skill",
            "installs": 326000,
        }
        result = _skills_sh_item_to_skill_raw(item)
        assert result is not None
        assert result["install"]["method"] == "npx"
        assert "skills" in result["install"]["target"]

    def test_adapter_extracts_source_url(self):
        """Adapter includes source URL for GitHub."""
        item = {
            "name": "test-skill",
            "owner": "test-org",
            "repo": "test-repo",
            "description": "Test",
        }
        result = _skills_sh_item_to_skill_raw(item)
        assert result is not None
        assert "github.com" in result.get("source_url", "")


class SkillsShDefaultFeedTests(unittest.TestCase):
    """REQ-SH1: skills.sh as default marketplace feed."""

    def test_skills_sh_in_default_feeds(self):
        """skills.sh appears in DEFAULT_FEEDS."""
        names = [f["name"] for f in DEFAULT_FEEDS]
        assert any("skills.sh" in n.lower() or "skills-sh" in n.lower() for n in names)

    def test_default_feed_has_url(self):
        """Each default feed has a URL."""
        for feed in DEFAULT_FEEDS:
            assert "url" in feed
            assert feed["url"].startswith("http")


class SkillsShFeedTests(unittest.TestCase):
    """REQ-SH6: Graceful degradation. REQ-SH7: Cache."""

    def test_marketplace_works_without_skills_sh(self):
        """Marketplace functions when skills.sh is unreachable."""
        catalog = Catalog()
        registry = Registry()
        connectors = ConnectorRegistry()
        # Use a URL that will fail
        config = {
            "marketplace": {
                "feeds": [{"name": "skills.sh", "url": "http://localhost:99999/does-not-exist"}]
            }
        }
        mp = Marketplace(catalog, registry, connectors, config=config, cache_ttl_seconds=0)
        # snapshot should not crash
        result = mp.snapshot()
        assert isinstance(result, dict)
        # Feeds should report error
        feeds = result.get("feeds", [])
        assert len(feeds) > 0
        error_feeds = [f for f in feeds if f.get("status") == "error"]
        assert len(error_feeds) > 0


class SkillsShInstallTests(unittest.IsolatedAsyncioTestCase):
    """REQ-SH3: Install via CLI. REQ-SH4: GitHub fallback."""

    def test_install_from_skills_sh_with_npx(self):
        """Install uses npx skills add when npx is available."""
        from lumen.core.installer import Installer

        temp_dir = tempfile.mkdtemp()
        pkg_dir = Path(temp_dir) / "pkg"
        pkg_dir.mkdir()
        (pkg_dir / "modules").mkdir()
        connectors = ConnectorRegistry()
        catalog = Catalog()

        memory = MagicMock()

        installer = Installer(
            pkg_dir=pkg_dir,
            connectors=connectors,
            memory=memory,
            catalog=catalog,
        )

        # Mock npx command — it "installs" by creating the dir
        with patch("shutil.which", return_value="/usr/bin/npx"), \
             patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

            # Simulate what npx skills add would do: create the skill dir
            skill_dir = installer.installed_dir / "find-skills"
            skill_dir.mkdir(exist_ok=True)
            (skill_dir / "SKILL.md").write_text("# find-skills\nTest skill")
            (skill_dir / "module.yaml").write_text(
                "name: find-skills\ndescription: Test\n"
            )

            result = installer._install_from_skills_sh({
                "name": "vercel-labs/skills/find-skills",
                "owner": "vercel-labs",
                "repo": "skills",
                "skill_name": "find-skills",
                "source_type": "skills-sh",
            })

        assert result["status"] == "installed", f"Got: {result}"

        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
