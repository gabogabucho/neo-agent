"""Tests for Remote Registry feature — REQ-RR1 through REQ-RR4.

F10: lumen install github:owner/repo + URL format.
Installer looks for module.yaml or SKILL.md in repo root.
Graceful error if repo doesn't contain a valid module.
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from zipfile import ZipFile

from lumen.core.catalog import Catalog
from lumen.core.connectors import ConnectorRegistry
from lumen.core.installer import Installer
from lumen.core.memory import Memory


def _make_installer(tmp_dir: Path) -> Installer:
    """Create an Installer with a temp installed_dir."""
    pkg_dir = tmp_dir / "pkg"
    pkg_dir.mkdir()
    (pkg_dir / "modules").mkdir()
    return Installer(
        pkg_dir=pkg_dir,
        connectors=ConnectorRegistry(),
        memory=MagicMock(spec=Memory),
        catalog=Catalog(),
        lumen_dir=tmp_dir / "lumen",
        config={},
    )


class GitHubInstallUnitTests(unittest.TestCase):
    """Tests for Installer.install_from_github_ref — REQ-RR1 through REQ-RR4."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.installer = _make_installer(Path(self.temp_dir.name))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_install_from_github_ref_method_exists(self):
        """Installer has install_from_github_ref method."""
        assert hasattr(self.installer, "install_from_github_ref"), \
            "Installer should have install_from_github_ref method"

    # --- REQ-RR1: github:owner/repo installs from GitHub repo ---

    @patch("urllib.request.urlopen")
    def test_install_from_github_ref_downloads_repo_zip(self, mock_urlopen):
        """install_from_github_ref downloads the repo zip from GitHub."""
        import io
        zip_buf = io.BytesIO()
        with ZipFile(zip_buf, "w") as zf:
            zf.writestr("test-module/module.yaml", "name: test-module\nversion: 0.1.0\n")
        zip_bytes = zip_buf.getvalue()

        mock_response = MagicMock()
        mock_response.read.return_value = zip_bytes
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = self.installer.install_from_github_ref("owner", "test-repo")
        assert result["status"] == "installed", f"Expected installed, got: {result}"
        assert result["name"] == "test-module"

    # --- REQ-RR3: Looks for module.yaml or SKILL.md ---

    @patch("urllib.request.urlopen")
    def test_installs_from_repo_with_module_yaml(self, mock_urlopen):
        """Repo with module.yaml in root installs successfully."""
        import io
        zip_buf = io.BytesIO()
        with ZipFile(zip_buf, "w") as zf:
            zf.writestr("repo-main/module.yaml", "name: my-mod\nversion: 1.0.0\ndescription: Test\n")
        zip_bytes = zip_buf.getvalue()

        mock_response = MagicMock()
        mock_response.read.return_value = zip_bytes
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = self.installer.install_from_github_ref("owner", "my-mod-repo")
        assert result["status"] == "installed"
        assert result["name"] == "my-mod"

    @patch("urllib.request.urlopen")
    def test_installs_from_repo_with_skill_md(self, mock_urlopen):
        """Repo with SKILL.md (no module.yaml) installs with generated manifest."""
        import io
        zip_buf = io.BytesIO()
        with ZipFile(zip_buf, "w") as zf:
            zf.writestr("repo-main/SKILL.md", "# My Skill\n\nDoes things.")
        zip_bytes = zip_buf.getvalue()

        mock_response = MagicMock()
        mock_response.read.return_value = zip_bytes
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = self.installer.install_from_github_ref("owner", "my-skill")
        assert result["status"] == "installed"
        assert result["name"] == "my-skill"

    # --- REQ-RR4: Graceful error if no valid module ---

    @patch("urllib.request.urlopen")
    def test_returns_error_for_invalid_repo(self, mock_urlopen):
        """Repo without module.yaml or SKILL.md returns error."""
        import io
        zip_buf = io.BytesIO()
        with ZipFile(zip_buf, "w") as zf:
            zf.writestr("repo-main/README.md", "# Just a readme\n")
        zip_bytes = zip_buf.getvalue()

        mock_response = MagicMock()
        mock_response.read.return_value = zip_bytes
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = self.installer.install_from_github_ref("owner", "bad-repo")
        assert result["status"] == "error"
        assert "module.yaml" in result["error"].lower() or "skill.md" in result["error"].lower() or "valid" in result["error"].lower()

    @patch("urllib.request.urlopen")
    def test_returns_error_on_network_failure(self, mock_urlopen):
        """Network error returns graceful error."""
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("Connection refused")

        result = self.installer.install_from_github_ref("owner", "broken-repo")
        assert result["status"] == "error"
        assert "module.yaml" in result["error"].lower() or "skill.md" in result["error"].lower() or "valid" in result["error"].lower()

    @patch("urllib.request.urlopen")
    def test_returns_error_on_network_failure(self, mock_urlopen):
        """Network error returns graceful error."""
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("Connection refused")

        result = self.installer.install_from_github_ref("owner", "broken-repo")
        assert result["status"] == "error"

    # --- REQ-RR2: URL format ---

    def test_parse_github_url(self):
        """Helper parses https://github.com/owner/repo format."""
        # The CLI should parse URLs and extract owner/repo
        # Test the parse helper directly
        from lumen.cli.main import _parse_github_ref
        owner, repo = _parse_github_ref("https://github.com/acme/my-module")
        assert owner == "acme"
        assert repo == "my-module"

    def test_parse_github_shorthand(self):
        """Helper parses github:owner/repo format."""
        from lumen.cli.main import _parse_github_ref
        owner, repo = _parse_github_ref("github:acme/my-module")
        assert owner == "acme"
        assert repo == "my-module"

    def test_parse_github_shorthand_with_git_suffix(self):
        """Helper handles .git suffix in repo name."""
        from lumen.cli.main import _parse_github_ref
        owner, repo = _parse_github_ref("github:acme/my-module.git")
        assert owner == "acme"
        assert repo == "my-module"

    def test_parse_invalid_ref_returns_none(self):
        """Helper returns None for invalid refs."""
        from lumen.cli.main import _parse_github_ref
        result = _parse_github_ref("not-a-valid-ref")
        assert result is None or result == (None, None)


class RemoteInstallCLIIntegrationTests(unittest.TestCase):
    """Tests for 'lumen module install' CLI command."""

    def test_module_subgroup_exists(self):
        """'module' CLI subgroup exists."""
        from typer.testing import CliRunner
        from lumen.cli.main import app
        runner = CliRunner()
        result = runner.invoke(app, ["module", "--help"])
        # Should not error — subgroup exists
        assert "install" in result.output, f"'install' should appear in module help, got: {result.output}"

    @patch("lumen.cli.main._load_persisted_config")
    def test_module_install_requires_ref(self, mock_config):
        """'lumen module install' without a ref shows error."""
        mock_config.return_value = {"model": "test"}
        from typer.testing import CliRunner
        from lumen.cli.main import app
        runner = CliRunner()
        result = runner.invoke(app, ["module", "install"])
        # Should show error or help about missing argument
        assert result.exit_code != 0 or "usage" in result.output.lower() or "required" in result.output.lower()


if __name__ == "__main__":
    unittest.main()
