"""Tests for chat-driven module onboarding helpers."""

from __future__ import annotations

import os

import pytest

from lumen.core.module_setup import (
    EnvSpec,
    build_setup_flow,
    missing_env_specs,
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
                }
            ]
        )
        assert specs == [
            EnvSpec(
                name="TELEGRAM_BOT_TOKEN",
                label="Token del bot",
                hint="Pedíselo a @BotFather",
                secret=True,
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

        chat_slot = flow["slots"]["TELEGRAM_CHAT_ID"]
        assert chat_slot["ask"] == "Chat ID"  # no hint → no trailing newline
        assert chat_slot["secret"] is False

    def test_empty_specs_produces_empty_slots(self):
        flow = build_setup_flow("foo", [])
        assert flow["slots"] == {}
        assert flow["on_complete"] == "save_module_env:foo"
