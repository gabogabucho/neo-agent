"""Tests for on_configure lifecycle hook — REQ-L1 through REQ-L4."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from lumen.core.module_runtime import (
    ModuleRuntimeContext,
    run_module_configure_hook,
    _load_runtime_module,
)


class OnConfigureHookTests(unittest.TestCase):
    """REQ-L1: Hook called on config save. REQ-L2: Optional hook."""

    def test_configure_called_when_defined(self):
        """REQ-L1: Module with configure() gets it called."""
        hook_calls = []

        # We'll mock _load_runtime_module to return a module with configure()
        mock_module = MagicMock()
        mock_module.configure = MagicMock(side_effect=lambda ctx: hook_calls.append(ctx))
        
        with patch("lumen.core.module_runtime._load_runtime_module", return_value=mock_module):
            run_module_configure_hook(
                name="test-module",
                module_dir=Path("/fake/dir"),
                runtime_root=Path("/fake/runtime"),
                config={"test": True},
            )

        assert len(hook_calls) == 1
        assert isinstance(hook_calls[0], ModuleRuntimeContext)
        assert hook_calls[0].name == "test-module"

    def test_no_error_when_configure_not_defined(self):
        """REQ-L2: Module without configure() doesn't cause error."""
        mock_module = MagicMock(spec=[])  # No attributes = no configure

        with patch("lumen.core.module_runtime._load_runtime_module", return_value=mock_module):
            # Should not raise
            run_module_configure_hook(
                name="simple-tool",
                module_dir=Path("/fake/dir"),
                runtime_root=Path("/fake/runtime"),
            )

    def test_no_error_when_module_is_none(self):
        """REQ-L2: Module without connector.py doesn't cause error."""
        with patch("lumen.core.module_runtime._load_runtime_module", return_value=None):
            run_module_configure_hook(
                name="no-connector",
                module_dir=Path("/fake/dir"),
                runtime_root=Path("/fake/runtime"),
            )


class OnConfigureErrorHandlingTests(unittest.TestCase):
    """REQ-L4: Error in configure() is logged but doesn't fail save."""

    def test_configure_exception_does_not_propagate(self):
        """REQ-L4: configure() raising should be caught."""
        mock_module = MagicMock()
        mock_module.configure = MagicMock(side_effect=RuntimeError("Test error"))

        with patch("lumen.core.module_runtime._load_runtime_module", return_value=mock_module):
            # Should NOT raise
            run_module_configure_hook(
                name="failing-module",
                module_dir=Path("/fake/dir"),
                runtime_root=Path("/fake/runtime"),
            )

    def test_configure_receives_config_in_context(self):
        """REQ-L3: Context has config, connectors, memory, lumen_dir."""
        hook_calls = []

        mock_module = MagicMock()
        mock_module.configure = MagicMock(side_effect=lambda ctx: hook_calls.append(ctx))

        with patch("lumen.core.module_runtime._load_runtime_module", return_value=mock_module):
            run_module_configure_hook(
                name="test-mod",
                module_dir=Path("/fake/dir"),
                runtime_root=Path("/fake/runtime"),
                config={"key": "value"},
                lumen_dir=Path("/fake/lumen"),
            )

        ctx = hook_calls[0]
        assert ctx.config == {"key": "value"}
        assert ctx.lumen_dir == Path("/fake/lumen")


if __name__ == "__main__":
    unittest.main()
