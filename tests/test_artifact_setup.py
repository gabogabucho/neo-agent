"""Tests for the unified artifact setup contract (Phases 1-4)."""

from __future__ import annotations

import pytest

from pathlib import Path

from lumen.core.artifact_setup import (
    ArtifactSetupContract,
    KIND_EXTERNAL,
    KIND_MANUAL,
    KIND_MCP,
    KIND_NATIVE,
    build_flow_from_contract,
    collect_pending_artifact_setup_flows,
    contract_from_external,
    contract_from_mcp_server,
    contract_from_native_manifest,
    contract_from_opaque_manifest,
    load_mcp_overlay,
    parse_artifact_action,
)


class TestArtifactSetupContract:
    def test_rejects_unknown_kind(self):
        with pytest.raises(ValueError):
            ArtifactSetupContract(
                kind="weird",
                artifact_id="x",
                display_name="X",
            )

    def test_rejects_empty_artifact_id(self):
        with pytest.raises(ValueError):
            ArtifactSetupContract(
                kind=KIND_NATIVE,
                artifact_id="",
                display_name="",
            )

    def test_action_strings(self):
        contract = ArtifactSetupContract(
            kind=KIND_MCP,
            artifact_id="github",
            display_name="GitHub",
        )
        assert contract.action_string == "save_artifact_env:mcp:github"
        assert contract.legacy_action_string == "save_module_env:github"

    def test_manual_only_heuristic(self):
        manual = ArtifactSetupContract(
            kind=KIND_MANUAL,
            artifact_id="foo",
            display_name="Foo",
            manual_instructions="Open foo.com and paste key in ~/.foo",
        )
        assert manual.is_manual_only()
        assert not manual.has_pending_values()

    def test_native_with_pending_is_not_manual_only(self):
        from lumen.core.module_setup import EnvSpec

        contract = ArtifactSetupContract(
            kind=KIND_NATIVE,
            artifact_id="mod",
            display_name="Mod",
            specs=[EnvSpec(name="X", label="X", hint="", secret=False)],
        )
        assert contract.has_pending_values()
        assert not contract.is_manual_only()


class TestContractFromNativeManifest:
    def test_no_env_declared_returns_none(self):
        assert contract_from_native_manifest("mod", {"name": "mod"}) is None

    def test_returns_contract_with_specs(self):
        manifest = {
            "name": "mod",
            "display_name": "Mod",
            "x-lumen": {
                "runtime": {
                    "env": [
                        {"name": "TOKEN", "label": "Token", "secret": True},
                        {"name": "CHAT_ID", "label": "Chat", "secret": False},
                    ]
                }
            },
        }
        contract = contract_from_native_manifest("mod", manifest)
        assert contract is not None
        assert contract.kind == KIND_NATIVE
        assert contract.artifact_id == "mod"
        assert contract.display_name == "Mod"
        assert [spec.name for spec in contract.specs] == ["TOKEN", "CHAT_ID"]
        assert contract.sink == {"type": "native_secrets", "module_name": "mod"}

    def test_filters_out_already_satisfied_specs(self):
        manifest = {
            "name": "mod",
            "x-lumen": {
                "runtime": {
                    "env": [
                        {"name": "TOKEN", "label": "Token", "secret": True},
                        {"name": "CHAT_ID", "label": "Chat", "secret": False},
                    ]
                }
            },
        }
        config = {"secrets": {"mod": {"TOKEN": "abc"}}}
        contract = contract_from_native_manifest("mod", manifest, config)
        assert contract is not None
        assert [spec.name for spec in contract.specs] == ["CHAT_ID"]

    def test_returns_contract_with_empty_specs_when_all_satisfied(self):
        manifest = {
            "name": "mod",
            "x-lumen": {
                "runtime": {
                    "env": [{"name": "TOKEN", "label": "Token", "secret": True}]
                }
            },
        }
        config = {"secrets": {"mod": {"TOKEN": "abc"}}}
        contract = contract_from_native_manifest("mod", manifest, config)
        assert contract is not None
        assert contract.specs == []
        assert not contract.has_pending_values()


class TestContractFromMcpServer:
    def test_no_config_returns_none(self):
        assert contract_from_mcp_server("srv", None) is None
        assert contract_from_mcp_server("", {"env": {"X": ""}}) is None

    def test_no_env_declared_returns_none(self):
        assert contract_from_mcp_server("srv", {"command": "foo"}) is None

    def test_overlay_beats_everything(self):
        overlay = {
            "display_name": "Foo",
            "env": [
                {"name": "FOO_TOKEN", "label": "Foo token", "secret": True},
            ],
        }
        server_config = {
            "command": "foo",
            "env": {"FOO_TOKEN": "", "OTHER": "ignored"},
            "x-lumen-env": [{"name": "IGNORED", "label": "x"}],
        }
        contract = contract_from_mcp_server("foo", server_config, overlay=overlay)
        assert contract is not None
        assert contract.kind == KIND_MCP
        assert contract.artifact_id == "foo"
        assert contract.display_name == "Foo"
        assert [spec.name for spec in contract.specs] == ["FOO_TOKEN"]
        assert contract.sink == {"type": "mcp_server_env", "server_id": "foo"}
        assert contract.action_string == "save_artifact_env:mcp:foo"

    def test_falls_back_to_x_lumen_env(self):
        server_config = {
            "env": {"API_KEY": ""},
            "x-lumen-env": [
                {"name": "API_KEY", "label": "API key", "secret": True},
            ],
        }
        contract = contract_from_mcp_server("srv", server_config)
        assert contract is not None
        assert [spec.name for spec in contract.specs] == ["API_KEY"]

    def test_falls_back_to_env_keys_when_no_annotations(self):
        server_config = {"env": {"SOME_KEY": "", "SOME_TOKEN": ""}}
        contract = contract_from_mcp_server("srv", server_config)
        assert contract is not None
        names = sorted(spec.name for spec in contract.specs)
        assert names == ["SOME_KEY", "SOME_TOKEN"]
        # Default secret inference by name substring
        token_spec = next(s for s in contract.specs if s.name == "SOME_TOKEN")
        assert token_spec.secret is True

    def test_filters_out_already_populated_env(self):
        overlay = {"env": [{"name": "A"}, {"name": "B"}]}
        server_config = {"env": {"A": "already-set", "B": ""}}
        contract = contract_from_mcp_server("srv", server_config, overlay=overlay)
        assert contract is not None
        assert [spec.name for spec in contract.specs] == ["B"]

    def test_all_satisfied_returns_empty_specs(self):
        overlay = {"env": [{"name": "A"}]}
        server_config = {"env": {"A": "ok"}}
        contract = contract_from_mcp_server("srv", server_config, overlay=overlay)
        assert contract is not None
        assert contract.specs == []
        assert not contract.has_pending_values()


class TestLoadMcpOverlay:
    def test_missing_pkg_dir_returns_none(self):
        assert load_mcp_overlay("foo", None) is None
        assert load_mcp_overlay("", Path(".")) is None

    def test_loads_shipped_github_overlay(self):
        pkg_dir = Path(__file__).resolve().parent.parent / "lumen"
        overlay = load_mcp_overlay("github", pkg_dir)
        assert overlay is not None
        assert overlay["display_name"] == "GitHub"
        env = overlay["env"]
        assert env[0]["name"] == "GITHUB_PERSONAL_ACCESS_TOKEN"
        assert env[0]["secret"] is True

    def test_loads_shipped_anthropic_overlay(self):
        pkg_dir = Path(__file__).resolve().parent.parent / "lumen"
        overlay = load_mcp_overlay("anthropic", pkg_dir)
        assert overlay is not None
        assert overlay["env"][0]["name"] == "ANTHROPIC_API_KEY"

    def test_missing_file_returns_none(self):
        pkg_dir = Path(__file__).resolve().parent.parent / "lumen"
        assert load_mcp_overlay("definitely-not-a-real-server", pkg_dir) is None

    def test_loads_shipped_openai_overlay(self):
        pkg_dir = Path(__file__).resolve().parent.parent / "lumen"
        overlay = load_mcp_overlay("openai", pkg_dir)
        assert overlay is not None
        assert overlay["display_name"] == "OpenAI"
        assert overlay["env"][0]["name"] == "OPENAI_API_KEY"
        assert overlay["env"][0]["secret"] is True

    def test_loads_shipped_filesystem_overlay(self):
        pkg_dir = Path(__file__).resolve().parent.parent / "lumen"
        overlay = load_mcp_overlay("filesystem", pkg_dir)
        assert overlay is not None
        assert overlay["display_name"] == "Filesystem"
        assert overlay["env"][0]["name"] == "ALLOWED_DIRS"
        assert overlay["env"][0]["secret"] is False

    def test_loads_shipped_slack_overlay(self):
        pkg_dir = Path(__file__).resolve().parent.parent / "lumen"
        overlay = load_mcp_overlay("slack", pkg_dir)
        assert overlay is not None
        assert overlay["display_name"] == "Slack"
        assert overlay["env"][0]["name"] == "SLACK_BOT_TOKEN"
        assert overlay["env"][0]["secret"] is True


class TestParseArtifactAction:
    def test_new_format(self):
        assert parse_artifact_action("save_artifact_env:mcp:github") == (
            "mcp",
            "github",
        )
        assert parse_artifact_action("save_artifact_env:native:telegram") == (
            "native",
            "telegram",
        )
        assert parse_artifact_action("save_artifact_env:manual:foo") == (
            "manual",
            "foo",
        )

    def test_legacy_format_maps_to_native(self):
        assert parse_artifact_action("save_module_env:telegram") == (
            "native",
            "telegram",
        )

    def test_handles_artifact_id_with_colons_is_rejected(self):
        # split limit=1, so the id can contain colons in theory — but we
        # want strict parsing; anything beyond the first ``kind:id`` pair
        # is taken as part of the id. Document current behavior.
        assert parse_artifact_action("save_artifact_env:mcp:a:b") == ("mcp", "a:b")

    def test_unknown_prefix_returns_none(self):
        assert parse_artifact_action("random_action") is None
        assert parse_artifact_action("") is None
        assert parse_artifact_action("save_artifact_env:") is None
        assert parse_artifact_action("save_artifact_env:foo:") is None
        assert parse_artifact_action("save_artifact_env:unknown:x") is None


class TestArtifactSetupFlows:
    def test_build_flow_from_contract_uses_generic_action_for_mcp(self):
        contract = ArtifactSetupContract(
            kind=KIND_MCP,
            artifact_id="github",
            display_name="GitHub",
            specs=contract_from_mcp_server(
                "github",
                {"env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""}},
                overlay={
                    "display_name": "GitHub",
                    "env": [{"name": "GITHUB_PERSONAL_ACCESS_TOKEN", "secret": True}],
                },
            ).specs,
        )

        flow = build_flow_from_contract(contract)

        assert flow is not None
        assert flow["intent"] == "artifact-setup-mcp-github"
        assert flow["on_complete"] == "save_artifact_env:mcp:github"
        assert flow["display_name"] == "GitHub"
        assert flow["kind"] == "mcp"
        assert "setup:github" in flow["triggers"]
        assert "setup:mcp:github" in flow["triggers"]
        assert "GitHub" in flow["first_message"]

    def test_collect_pending_artifact_setup_flows_includes_mcp(self, tmp_path: Path):
        from lumen.core import secrets_store
        orig_lumen_dir = secrets_store.LUMEN_DIR
        orig_secrets_path = secrets_store.SECRETS_PATH
        secrets_store.configure_paths(lumen_dir=tmp_path)
        try:
            modules_dir = tmp_path / "modules" / "pending-module"
            modules_dir.mkdir(parents=True)
            (modules_dir / "module.yaml").write_text(
                """
name: pending-module
x-lumen:
  runtime:
    env:
      - name: DEMO_TOKEN
""".strip(),
                encoding="utf-8",
            )

            config = {
                "mcp": {
                    "servers": {
                        "github": {
                            "command": "npx",
                            "args": ["-y", "@modelcontextprotocol/server-github"],
                            "env": {"GITHUB_PERSONAL_ACCESS_TOKEN": ""},
                        }
                    }
                }
            }

            flows = collect_pending_artifact_setup_flows(tmp_path, config)

            assert [flow["intent"] for flow in flows] == [
                "module-setup-pending-module",
                "artifact-setup-mcp-github",
            ]
        finally:
            secrets_store.LUMEN_DIR = orig_lumen_dir
            secrets_store.SECRETS_PATH = orig_secrets_path


class TestContractFromOpaqueManifest:
    def test_no_manifest_returns_none(self):
        assert contract_from_opaque_manifest("mod", None) is None

    def test_no_x_lumen_returns_none(self):
        assert contract_from_opaque_manifest("mod", {"name": "mod"}) is None

    def test_no_manual_setup_returns_none(self):
        manifest = {"x-lumen": {"runtime": {"env": [{"name": "X"}]}}}
        assert contract_from_opaque_manifest("mod", manifest) is None

    def test_manual_setup_with_steps(self):
        manifest = {
            "name": "my-mod",
            "display_name": "My Module",
            "x-lumen": {
                "runtime": {
                    "manual_setup": {
                        "title": "Configure My Module",
                        "steps": [
                            "Go to mymod.com and create an API key",
                            "Paste the key in ~/.mymod/config",
                        ],
                        "doc_url": "https://mymod.com/docs",
                    }
                }
            },
        }
        contract = contract_from_opaque_manifest("my-mod", manifest)
        assert contract is not None
        assert contract.kind == KIND_MANUAL
        assert contract.artifact_id == "my-mod"
        assert contract.display_name == "My Module"
        assert contract.is_manual_only()
        assert not contract.has_pending_values()
        assert "Configure My Module" in contract.manual_instructions
        assert "Go to mymod.com" in contract.manual_instructions
        assert "https://mymod.com/docs" in contract.manual_instructions
        assert contract.action_string == "save_artifact_env:manual:my-mod"

    def test_manual_setup_without_doc_url(self):
        manifest = {
            "name": "mod",
            "x-lumen": {
                "runtime": {
                    "manual_setup": {
                        "steps": ["Do thing A", "Do thing B"],
                    }
                }
            },
        }
        contract = contract_from_opaque_manifest("mod", manifest)
        assert contract is not None
        assert "Do thing A" in contract.manual_instructions
        assert "Documentación" not in contract.manual_instructions

    def test_manual_setup_empty_steps_returns_contract_with_title_only(self):
        manifest = {
            "name": "mod",
            "x-lumen": {
                "runtime": {
                    "manual_setup": {
                        "title": "Setup required",
                    }
                }
            },
        }
        contract = contract_from_opaque_manifest("mod", manifest)
        assert contract is not None
        assert "Setup required" in contract.manual_instructions


class TestContractFromExternal:
    def test_no_artifact_id_returns_none(self):
        assert contract_from_external("") is None
        assert contract_from_external(None) is None

    def test_no_instructions_no_url_returns_none(self):
        assert contract_from_external("clawhub-tool") is None

    def test_with_instructions_only(self):
        contract = contract_from_external(
            "clawhub-tool",
            instructions="Run npx clawhub-tool and follow prompts.",
        )
        assert contract is not None
        assert contract.kind == KIND_EXTERNAL
        assert contract.artifact_id == "clawhub-tool"
        assert contract.is_manual_only()
        assert "npx clawhub-tool" in contract.manual_instructions

    def test_with_doc_url_only(self):
        contract = contract_from_external(
            "clawhub-tool",
            doc_url="https://clawhub.ai/tools/clawhub-tool",
        )
        assert contract is not None
        assert "clawhub.ai" in contract.manual_instructions

    def test_with_both(self):
        contract = contract_from_external(
            "clawhub-tool",
            display_name="ClawHub Tool",
            instructions="Run setup wizard.",
            doc_url="https://clawhub.ai/tools/clawhub-tool",
        )
        assert contract is not None
        assert contract.display_name == "ClawHub Tool"
        assert "Run setup wizard." in contract.manual_instructions
        assert "clawhub.ai" in contract.manual_instructions
        assert contract.action_string == "save_artifact_env:external:clawhub-tool"


class TestManualSetupInCollectFlows:
    def test_manual_module_produces_flow(self, tmp_path: Path):
        from lumen.core import secrets_store
        orig_lumen_dir = secrets_store.LUMEN_DIR
        orig_secrets_path = secrets_store.SECRETS_PATH
        secrets_store.configure_paths(lumen_dir=tmp_path)
        try:
            modules_dir = tmp_path / "modules" / "manual-mod"
            modules_dir.mkdir(parents=True)
            (modules_dir / "module.yaml").write_text(
                """
name: manual-mod
display_name: Manual Module
x-lumen:
  runtime:
    manual_setup:
      title: Set up Manual Module
      steps:
        - Visit example.com
        - Get API key
      doc_url: https://example.com/docs
""".strip(),
                encoding="utf-8",
            )

            flows = collect_pending_artifact_setup_flows(tmp_path, {})

            assert len(flows) == 1
            flow = flows[0]
            assert flow["kind"] == KIND_MANUAL
            assert flow["intent"] == "artifact-setup-manual-manual-mod"
            assert "Visit example.com" in flow.get("manual_instructions", "")
            assert "setup:manual-mod" in flow["triggers"]
        finally:
            secrets_store.LUMEN_DIR = orig_lumen_dir
            secrets_store.SECRETS_PATH = orig_secrets_path

    def test_env_module_takes_priority_over_manual(self, tmp_path: Path):
        from lumen.core import secrets_store
        orig_lumen_dir = secrets_store.LUMEN_DIR
        orig_secrets_path = secrets_store.SECRETS_PATH
        secrets_store.configure_paths(lumen_dir=tmp_path)
        try:
            modules_dir = tmp_path / "modules" / "hybrid-mod"
            modules_dir.mkdir(parents=True)
            (modules_dir / "module.yaml").write_text(
                """
name: hybrid-mod
x-lumen:
  runtime:
    env:
      - name: API_KEY
    manual_setup:
      title: Fallback instructions
      steps:
        - Do it manually
""".strip(),
                encoding="utf-8",
            )

            flows = collect_pending_artifact_setup_flows(tmp_path, {})

            # Env specs take priority — should get a native flow, not manual
            assert len(flows) == 1
            assert flows[0]["intent"] == "module-setup-hybrid-mod"
        finally:
            secrets_store.LUMEN_DIR = orig_lumen_dir
            secrets_store.SECRETS_PATH = orig_secrets_path
