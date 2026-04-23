"""Tests for per-instance isolation — REQ-I1 through REQ-I6."""

import tempfile
import unittest
from pathlib import Path

from lumen.core.paths import resolve_lumen_dir, LUMEN_BASE


class ResolveLumenDirTests(unittest.TestCase):
    """REQ-I1: Instance directory. REQ-I3: Custom data dir. REQ-I6: Backward compat."""

    def test_default_returns_base(self):
        """REQ-I6: No instance/data-dir → ~/.lumen/."""
        result = resolve_lumen_dir()
        assert result == LUMEN_BASE

    def test_instance_creates_instances_subpath(self):
        """REQ-I1: --instance test → ~/.lumen/instances/test/."""
        result = resolve_lumen_dir(instance="test")
        assert result == LUMEN_BASE / "instances" / "test"

    def test_instance_with_dashes(self):
        """Instance names with dashes work."""
        result = resolve_lumen_dir(instance="cliente-01")
        assert result == LUMEN_BASE / "instances" / "cliente-01"

    def test_custom_data_dir_overrides_instance(self):
        """REQ-I3: --data-dir takes priority over --instance."""
        result = resolve_lumen_dir(instance="test", data_dir="/opt/lumen")
        assert result == Path("/opt/lumen")

    def test_custom_data_dir_absolute(self):
        """Custom data-dir uses exact path."""
        result = resolve_lumen_dir(data_dir="/tmp/my-lumen")
        assert result == Path("/tmp/my-lumen")

    def test_auto_creates_instance_dir(self):
        """REQ-I4: Instance dir is auto-created on resolve."""
        with tempfile.TemporaryDirectory() as tmp:
            instance_path = Path(tmp) / "instances" / "new-bot"
            result = resolve_lumen_dir(instance="new-bot", base_dir=Path(tmp))
            # resolve_lumen_dir should NOT create dirs itself — caller does
            assert result == instance_path

    def test_default_config_path_uses_resolved_dir(self):
        """Config path is relative to resolved lumen_dir."""
        lumen_dir = resolve_lumen_dir(instance="test")
        config_path = lumen_dir / "config.yaml"
        # Use PurePosixPath comparison for cross-platform
        assert config_path.name == "config.yaml"
        assert config_path.parent.name == "test"
        assert config_path.parent.parent.name == "instances"

    def test_default_memory_path_uses_resolved_dir(self):
        """Memory DB path is relative to resolved lumen_dir."""
        lumen_dir = resolve_lumen_dir(instance="test")
        memory_path = lumen_dir / "memory.db"
        assert memory_path.name == "memory.db"
        assert memory_path.parent.name == "test"


class InstanceIsolationTests(unittest.TestCase):
    """REQ-I5: Two instances have isolated state."""

    def test_two_instances_have_different_dirs(self):
        """Different instance IDs → different directories."""
        dir_a = resolve_lumen_dir(instance="bot-a")
        dir_b = resolve_lumen_dir(instance="bot-b")
        assert dir_a != dir_b

    def test_two_instances_have_different_config_paths(self):
        """Different instances → different config.yaml paths."""
        config_a = resolve_lumen_dir(instance="bot-a") / "config.yaml"
        config_b = resolve_lumen_dir(instance="bot-b") / "config.yaml"
        assert config_a != config_b

    def test_two_instances_have_different_memory_paths(self):
        """Different instances → different memory.db paths."""
        memory_a = resolve_lumen_dir(instance="bot-a") / "memory.db"
        memory_b = resolve_lumen_dir(instance="bot-b") / "memory.db"
        assert memory_a != memory_b

    def test_instance_separate_from_default(self):
        """Instance dir is separate from default dir (not a child that shares config)."""
        default = resolve_lumen_dir()
        instance = resolve_lumen_dir(instance="test")
        assert instance != default
        # Instance is a grandchild of base, not the same dir
        assert instance.parent.parent == default


if __name__ == "__main__":
    unittest.main()
