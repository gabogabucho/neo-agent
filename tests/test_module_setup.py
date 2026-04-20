"""Tests for chat-driven module onboarding helpers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lumen.core.module_setup import (
    EnvSpec,
    build_setup_flow,
    env_specs_from_manifest,
    merge_module_setup_config,
    missing_env_specs,
    normalize_module_setup_values,
    pending_setup_for_manifest,
    parse_env_specs,
)


class TestParseEnvSpecs:
    def test_none_returns_empty(self):
        assert parse_env_specs(None) == []

    def test_empty_list(self):
        assert parse_env_specs([]) == []

    def test_legacy_strings(self):
        specs = parse_env_specs(["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"])
        assert len(specs) == 2
        assert specs[0].name == "TELEGRAM_BOT_TOKEN"
        assert specs[0].label == "Telegram bot token"
        assert specs[0].hint == ""
        assert specs[0].secret is True  # TOKEN → secret by default
        assert specs[1].secret is False  # CHAT_ID → not secret

    def test_rich_objects_honor_all_fields(self):
        specs = parse_env_specs(
            [
                {
                    "name": "TELEGRAM_BOT_TOKEN",
                    "label": "Token del bot",
                    "hint": "Pedíselo a @BotFather",
                    "secret": True,
                    "expected_type": "token",
                    "pattern": r"\d+:\w+",
                    "examples": ["123:ABC"],
                    "format_guidance": "Pegá solo el token",
                }
            ]
        )
        assert specs == [
            EnvSpec(
                name="TELEGRAM_BOT_TOKEN",
                label="Token del bot",
                hint="Pedíselo a @BotFather",
                secret=True,
                expected_type="token",
                pattern=r"\d+:\w+",
                examples=["123:ABC"],
                format_guidance="Pegá solo el token",
            )
        ]

    def test_rich_objects_fill_missing_fields(self):
        specs = parse_env_specs([{"name": "FOO_API_KEY"}])
        assert specs[0].name == "FOO_API_KEY"
        assert specs[0].label == "Foo api key"
        assert specs[0].secret is True

    def test_mixed_inputs(self):
        specs = parse_env_specs(
            ["SIMPLE_VAR", {"name": "OTHER", "label": "Other"}]
        )
        assert [s.name for s in specs] == ["SIMPLE_VAR", "OTHER"]

    def test_skips_blank_names(self):
        specs = parse_env_specs(["", "  ", {"name": ""}, {"label": "no name"}])
        assert specs == []


class TestMissingEnvSpecs:
    def _specs(self):
        return [
            EnvSpec("FOO", "Foo", "", False),
            EnvSpec("BAR", "Bar", "", True),
        ]

    def test_all_missing_when_nothing_set(self, monkeypatch):
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAR", raising=False)
        assert missing_env_specs(self._specs(), {}) == self._specs()

    def test_env_var_satisfies(self, monkeypatch):
        monkeypatch.setenv("FOO", "present")
        monkeypatch.delenv("BAR", raising=False)
        missing = missing_env_specs(self._specs(), {})
        assert [s.name for s in missing] == ["BAR"]

    def test_config_secrets_satisfy(self, monkeypatch):
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAR", raising=False)
        config = {"secrets": {"some-module": {"FOO": "x"}}}
        missing = missing_env_specs(self._specs(), config)
        assert [s.name for s in missing] == ["BAR"]

    def test_top_level_config_satisfies(self, monkeypatch):
        monkeypatch.delenv("FOO", raising=False)
        monkeypatch.delenv("BAR", raising=False)
        missing = missing_env_specs(self._specs(), {"FOO": "x"})
        assert [s.name for s in missing] == ["BAR"]


class TestBuildSetupFlow:
    def test_requires_module_name(self):
        with pytest.raises(ValueError):
            build_setup_flow("", [])

    def test_produces_slot_per_spec(self):
        flow = build_setup_flow(
            "x-lumen-comunicacion-telegram",
            [
                EnvSpec(
                    "TELEGRAM_BOT_TOKEN",
                    "Token del bot",
                    "Pedíselo a @BotFather",
                    True,
                    "token",
                    r"\d+:\w+",
                    ["123456:ABC"],
                    "Pegá solo el token",
                ),
                EnvSpec(
                    "TELEGRAM_CHAT_ID",
                    "Chat ID",
                    "",
                    False,
                ),
            ],
        )
        assert flow["intent"] == "module-setup-x-lumen-comunicacion-telegram"
        assert flow["triggers"] == ["setup:x-lumen-comunicacion-telegram"]
        assert (
            flow["on_complete"]
            == "save_module_env:x-lumen-comunicacion-telegram"
        )
        assert set(flow["slots"].keys()) == {
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
        }

        token_slot = flow["slots"]["TELEGRAM_BOT_TOKEN"]
        assert token_slot["required"] is True
        assert token_slot["secret"] is True
        assert "Pedíselo a @BotFather" in token_slot["ask"]
        assert "Token del bot" in token_slot["ask"]
        assert "valor crudo únicamente" in token_slot["ask"]
        assert "Formato:" in token_slot["ask"]
        assert "123456:ABC" in token_slot["ask"]

        chat_slot = flow["slots"]["TELEGRAM_CHAT_ID"]
        assert chat_slot["ask"].startswith("Chat ID")
        assert chat_slot["secret"] is False

    def test_empty_specs_produces_empty_slots(self):
        flow = build_setup_flow("foo", [])
        assert flow["slots"] == {}
        assert flow["on_complete"] == "save_module_env:foo"


class TestManifestHelpers:
    def test_env_specs_from_manifest_supports_legacy_string_list(self):
        manifest = {
            "x-lumen": {
                "runtime": {
                    "env": ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"],
                }
            }
        }

        specs = env_specs_from_manifest(manifest)

        assert [spec.name for spec in specs] == [
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_CHAT_ID",
        ]
        assert specs[0].secret is True
        assert specs[1].secret is False

    def test_pending_setup_for_manifest_only_returns_missing_specs(self, monkeypatch):
        monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
        monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
        manifest = {
            "x-lumen": {
                "runtime": {
                    "env": [
                        {"name": "TELEGRAM_BOT_TOKEN", "secret": True},
                        {"name": "TELEGRAM_CHAT_ID", "secret": False},
                    ]
                }
            }
        }

        pending = pending_setup_for_manifest(
            "x-lumen-comunicacion-telegram",
            manifest,
            {"secrets": {"x-lumen-comunicacion-telegram": {"TELEGRAM_CHAT_ID": "123"}}},
        )

        assert pending is not None
        assert pending["module"] == "x-lumen-comunicacion-telegram"
        assert [spec["name"] for spec in pending["env_specs"]] == ["TELEGRAM_BOT_TOKEN"]
        assert pending["flow"]["on_complete"] == "save_module_env:x-lumen-comunicacion-telegram"

    def test_pending_setup_skips_opaque_modules(self, monkeypatch):
        monkeypatch.delenv("DEMO_TOKEN", raising=False)
        manifest = {
            "x-lumen": {
                "runtime": {"env": [{"name": "DEMO_TOKEN", "secret": True}]},
                "interoperability": {"level": "opaque"},
            }
        }

        pending = pending_setup_for_manifest("opaque-module", manifest, {})

        assert pending is None

    def test_merge_module_setup_config_stores_only_declared_values(self):
        manifest = {
            "x-lumen": {
                "runtime": {
                    "env": [
                        {"name": "TELEGRAM_BOT_TOKEN", "secret": True},
                        {"name": "TELEGRAM_CHAT_ID", "secret": False},
                    ]
                }
            }
        }

        merged = merge_module_setup_config(
            {
                "model": "demo",
                "secrets": {"other-module": {"OTHER_TOKEN": "keep-me"}},
            },
            "x-lumen-comunicacion-telegram",
            {
                "TELEGRAM_BOT_TOKEN": "  token-123  ",
                "TELEGRAM_CHAT_ID": "456",
                "IGNORED": "nope",
            },
            manifest=manifest,
        )

        assert merged["model"] == "demo"
        assert merged["secrets"]["other-module"]["OTHER_TOKEN"] == "keep-me"
        assert merged["secrets"]["x-lumen-comunicacion-telegram"] == {
            "TELEGRAM_BOT_TOKEN": "token-123",
            "TELEGRAM_CHAT_ID": "456",
        }

    def test_normalize_module_setup_values_extracts_pattern_match_from_phrase(self):
        manifest = {
            "x-lumen": {
                "runtime": {
                    "env": [
                        {
                            "name": "DEMO_TOKEN",
                            "pattern": r"token-[A-Z0-9]+",
                            "examples": ["token-ABC123"],
                            "format_guidance": "Pegá solo el token",
                        }
                    ]
                }
            }
        }

        result = normalize_module_setup_values(
            {"DEMO_TOKEN": "Te dejo el token: token-ABC123"},
            module_name="demo-module",
            manifest=manifest,
        )

        assert result["values"] == {"DEMO_TOKEN": "token-ABC123"}
        assert result["errors"] == {}

    def test_normalize_module_setup_values_rejects_invalid_pattern(self):
        manifest = {
            "x-lumen": {
                "runtime": {
                    "env": [
                        {
                            "name": "DEMO_TOKEN",
                            "label": "Demo token",
                            "pattern": r"token-[A-Z0-9]+",
                            "examples": ["token-ABC123"],
                            "format_guidance": "Pegá solo el token",
                        }
                    ]
                }
            }
        }

        result = normalize_module_setup_values(
            {"DEMO_TOKEN": "Te dejo el token: hola"},
            module_name="demo-module",
            manifest=manifest,
        )

        assert result["values"] == {}
        assert "DEMO_TOKEN" in result["errors"]

    def test_pending_setup_reports_readiness_failure_until_smoke_passes(self, tmp_path: Path):
        module_dir = tmp_path / "demo-module"
        module_dir.mkdir()
        (module_dir / "connector.py").write_text(
            "def check_setup_readiness(context):\n"
            "    token = context.resolve_setting('demo_token', 'DEMO_TOKEN')\n"
            "    if token != 'token-READY':\n"
            "        return {'ok': False, 'reason': 'Smoke check failed'}\n"
            "    return {'ok': True}\n",
            encoding="utf-8",
        )
        manifest = {
            "name": "demo-module",
            "x-lumen": {
                "runtime": {
                    "env": [
                        {
                            "name": "DEMO_TOKEN",
                            "pattern": r"token-[A-Z]+",
                        }
                    ]
                }
            },
        }

        blocked = pending_setup_for_manifest(
            "demo-module",
            manifest,
            {"secrets": {"demo-module": {"DEMO_TOKEN": "token-BAD"}}},
            module_dir=module_dir,
        )
        ready = pending_setup_for_manifest(
            "demo-module",
            manifest,
            {"secrets": {"demo-module": {"DEMO_TOKEN": "token-READY"}}},
            module_dir=module_dir,
        )

        assert blocked is not None
        assert blocked["env_specs"] == []
        assert blocked["readiness"]["reason"] == "Smoke check failed"
        assert ready is None
