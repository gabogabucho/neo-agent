"""Tests for CLI Twin Wizard — onboarding in terminal.

Wizard triggers when no config.yaml exists in the instance dir.
Saves config and returns it for the caller to bootstrap runtime.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml


class CLIWizardTests(unittest.TestCase):
    """Tests for _run_cli_wizard function."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.lumen_dir = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_wizard_function_exists(self):
        """_run_cli_wizard is importable from cli.main."""
        from lumen.cli.main import _run_cli_wizard
        assert callable(_run_cli_wizard)

    @patch("lumen.cli.main.Prompt.ask")
    def test_wizard_saves_config_yaml(self, mock_ask):
        """Wizard saves config.yaml with model, language, api_key."""
        from lumen.cli.main import _run_cli_wizard

        # Simulate: DeepSeek (1), API key, Spanish, port
        mock_ask.side_effect = ["1", "sk-test-123", "es"]

        _run_cli_wizard(lumen_dir=self.lumen_dir)

        config_path = self.lumen_dir / "config.yaml"
        assert config_path.exists(), "config.yaml should be created"

        config = yaml.safe_load(config_path.read_text())
        assert config["model"] == "deepseek/deepseek-chat"
        assert config["language"] == "es"
        assert config["api_key"] == "sk-test-123"

    @patch("lumen.cli.main.Prompt.ask")
    def test_wizard_ollama_no_api_key(self, mock_ask):
        """Wizard with Ollama doesn't ask for API key."""
        from lumen.cli.main import _run_cli_wizard

        mock_ask.side_effect = ["4", "en"]

        _run_cli_wizard(lumen_dir=self.lumen_dir)

        config_path = self.lumen_dir / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        assert "api_key" not in config or config.get("api_key") is None
        assert config["model"] == "ollama/llama3"

    @patch("lumen.cli.main.Prompt.ask")
    def test_wizard_returns_config(self, mock_ask):
        """Wizard returns the config dict for caller to use."""
        from lumen.cli.main import _run_cli_wizard

        mock_ask.side_effect = ["1", "sk-test", "en"]

        result = _run_cli_wizard(lumen_dir=self.lumen_dir)

        assert isinstance(result, dict)
        assert "model" in result
        assert "language" in result

    @patch("lumen.cli.main.Prompt.ask")
    def test_wizard_creates_lumen_dir(self, mock_ask):
        """Wizard creates lumen_dir if it doesn't exist."""
        from lumen.cli.main import _run_cli_wizard

        new_dir = self.lumen_dir / "instances" / "test"
        mock_ask.side_effect = ["1", "sk-test", "es"]

        _run_cli_wizard(lumen_dir=new_dir)

        assert new_dir.exists()
        assert (new_dir / "config.yaml").exists()

    @patch("lumen.cli.main.Prompt.ask")
    def test_wizard_openrouter_option(self, mock_ask):
        """Wizard offers OpenRouter as a provider option."""
        from lumen.cli.main import _run_cli_wizard

        # Option 5 = OpenRouter (no API key needed, uses OAuth)
        mock_ask.side_effect = ["5", "en"]

        _run_cli_wizard(lumen_dir=self.lumen_dir)

        config_path = self.lumen_dir / "config.yaml"
        config = yaml.safe_load(config_path.read_text())
        assert "openrouter" in config.get("model", "").lower()


class CLIWizardIntegrationTests(unittest.TestCase):
    """Tests for wizard integration with run/server commands."""

    def test_run_command_calls_wizard_when_no_config(self):
        """lumen run triggers wizard when no config.yaml exists."""
        from typer.testing import CliRunner
        from lumen.cli.main import app

        runner = CliRunner()
        # With a temp dir that has no config
        with tempfile.TemporaryDirectory() as tmp:
            # This will try to run wizard but Prompt.ask will fail in CI
            # Just verify it doesn't crash with "No such option"
            result = runner.invoke(app, ["run", "--data-dir", tmp])
            # Should not error with unknown option
            assert "No such option" not in (result.output or "")

    def test_run_no_wizard_flag_exists(self):
        """lumen run --no-wizard flag exists."""
        from typer.testing import CliRunner
        from lumen.cli.main import app

        runner = CliRunner()
        result = runner.invoke(app, ["run", "--help"])
        assert "no-wizard" in result.output.lower() or "no_wizard" in result.output.lower()


if __name__ == "__main__":
    unittest.main()
