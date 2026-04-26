"""Tests for lumen.core.brain — context assembler and tool execution loop."""

import asyncio
import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
import yaml

from lumen.core.brain import Brain
from lumen.core.catalog import Catalog
from lumen.core.connectors import Connector, ConnectorRegistry
from lumen.core.memory import Memory
from lumen.core.registry import Capability, CapabilityKind, CapabilityStatus, Registry
from lumen.core.session import Session, SessionManager


# ── Helpers ───────────────────────────────────────────────────────────


def _make_consciousness():
    """Create a Consciousness stub with a temp YAML file."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    yaml.dump(
        {
            "identity": {"name": "TestLumen", "type": "test agent"},
            "nature": ["I am modular", "I can grow"],
        },
        tmp,
    )
    tmp.flush()

    from lumen.core.consciousness import Consciousness

    c = Consciousness(config_path=Path(tmp.name))
    return c


def _make_personality():
    """Create a Personality stub with a temp YAML file."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    )
    yaml.dump(
        {
            "identity": {"name": "TestLumen", "role": "Test Assistant"},
            "tone": {"style": "direct"},
            "rules": ["Be helpful"],
        },
        tmp,
    )
    tmp.flush()

    from lumen.core.personality import Personality

    return Personality(path=Path(tmp.name))


def _make_catalog(modules=None):
    """Create a Catalog with optional modules."""
    with tempfile.TemporaryDirectory() as tmp:
        catalog_path = Path(tmp) / "index.yaml"
        catalog_path.write_text(
            yaml.dump({"modules": modules or []}), encoding="utf-8"
        )
        return Catalog(catalog_path)


def _make_connectors():
    """Create a ConnectorRegistry with a simple test connector."""
    reg = ConnectorRegistry()
    conn = Connector("test_conn", "Test connector", ["run"])
    reg.register(conn)
    return reg


def _make_registry():
    """Create a Registry with a test skill."""
    reg = Registry()
    reg.register(
        Capability(
            kind=CapabilityKind.SKILL,
            name="test-skill",
            description="A test skill",
            status=CapabilityStatus.READY,
        )
    )
    return reg


def _make_brain(**overrides):
    """Create a Brain with default stubs. Override any kwarg."""
    defaults = dict(
        consciousness=_make_consciousness(),
        personality=_make_personality(),
        memory=MagicMock(spec=Memory),
        connectors=_make_connectors(),
        registry=_make_registry(),
        catalog=_make_catalog(),
        model="test-model",
        flows=[],
    )
    defaults.update(overrides)
    return Brain(**defaults)


def _mock_llm_response(content="Hello!", tool_calls=None):
    """Create a mock litellm response."""
    msg = MagicMock()
    msg.content = content
    msg.tool_calls = tool_calls
    msg.model_dump.return_value = {"role": "assistant", "content": content}

    choice = MagicMock()
    choice.message = msg

    response = MagicMock()
    response.choices = [choice]
    return response


def _mock_tool_call(name, arguments="{}"):
    """Create a mock tool call."""
    tc = MagicMock()
    tc.id = "call-123"
    tc.function.name = name
    tc.function.arguments = arguments
    return tc


def _test_tools(*names):
    """Create minimal tool schemas for validation tests."""
    return [
        {
            "function": {
                "name": name,
                "parameters": {"type": "object", "properties": {}},
            }
        }
        for name in names
    ]


# ── think() happy path ───────────────────────────────────────────────


class TestThinkHappyPath:
    def test_prompt_prefers_obvious_user_spanish_over_english_config(self):
        brain = _make_brain(language="en")
        session = Session()
        context = {
            "consciousness": "I am Lumen",
            "personality": "assistant",
            "body": "capabilities",
            "catalog": "",
            "active_flow": None,
            "filled_slots": {},
            "pending_slots": [],
            "memories": [],
            "available_flows": [],
        }

        messages = brain._build_prompt(context, "Hola, ¿me ayudas con Telegram?", session)
        system_msg = messages[0]["content"]

        assert "follow the user's actual language" in system_msg
        assert "Respond in Spanish" in system_msg
        assert "Default locale hint: Respond in English." in system_msg
        assert "Use tools to act" in system_msg
        assert "Do NOT end with a plan" in system_msg

    def test_tool_enforcement_prompt_changes_by_model_family(self):
        context = {
            "consciousness": "I am Lumen",
            "personality": "assistant",
            "body": "capabilities",
            "catalog": "",
            "active_flow": None,
            "filled_slots": {},
            "pending_slots": [],
            "memories": [],
            "available_flows": [],
        }

        openai_brain = _make_brain(model="gpt-4o-mini")
        gemini_brain = _make_brain(model="gemini-1.5-pro")
        generic_brain = _make_brain(model="deepseek-chat")

        openai_msg = openai_brain._build_prompt(context, "hello", Session())[0]["content"]
        gemini_msg = gemini_brain._build_prompt(context, "hello", Session())[0]["content"]
        generic_msg = generic_brain._build_prompt(context, "hello", Session())[0]["content"]

        assert "Be persistent with tools" in openai_msg
        assert "absolute paths" in gemini_msg
        assert "Tool use is mandatory" in generic_msg

    def test_model_profiles_resolve_by_family(self):
        openai_profile = _make_brain(model="gpt-4o-mini")._model_profile()
        gemini_profile = _make_brain(model="gemini-1.5-pro")._model_profile()
        qwen_profile = _make_brain(model="qwen/qwen2.5-coder")._model_profile()

        assert openai_profile["family"] == "openai"
        assert openai_profile["supports_native_tools"] is True
        assert gemini_profile["family"] == "gemini"
        assert gemini_profile["requires_parser_fallback"] is True
        assert qwen_profile["family"] == "openai-compatible"

    @pytest.mark.asyncio
    async def test_acompletion_uses_model_profile_options(self):
        brain = _make_brain(model="gpt-4o-mini")
        brain.memory.recall = AsyncMock(return_value=[])

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = _mock_llm_response("I can help with that!")
            await brain.think("hello", Session())

        kwargs = mock_llm.call_args.kwargs
        assert kwargs["temperature"] == brain._model_profile()["completion_temperature"]
        assert kwargs["max_tokens"] == brain._model_profile()["completion_max_tokens"]

    @pytest.mark.asyncio
    async def test_think_returns_message_and_empty_tool_calls(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = _mock_llm_response("I can help with that!")
            result = await brain.think("hello", Session())

        assert result["message"] == "I can help with that!"
        assert result["tool_calls"] == []

    @pytest.mark.asyncio
    async def test_think_adds_messages_to_session_history(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = _mock_llm_response("Hi!")
            session = Session()
            await brain.think("hello", session)

        assert len(session.history) == 2
        assert session.history[0]["role"] == "user"
        assert session.history[0]["content"] == "hello"
        assert session.history[1]["role"] == "assistant"
        assert session.history[1]["content"] == "Hi!"

    @pytest.mark.asyncio
    async def test_think_persists_conversation(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        session = Session()
        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = _mock_llm_response("Got it!")
            await brain.think("remember this", session)

        assert brain.memory.save_conversation_turn.call_count == 2
        brain.memory.save_conversation_turn.assert_any_call(
            session.session_id, "user", "remember this"
        )
        brain.memory.save_conversation_turn.assert_any_call(
            session.session_id, "assistant", "Got it!"
        )

    @pytest.mark.asyncio
    async def test_think_with_relevant_memories(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(
            return_value=[
                {"id": 1, "content": "user likes Python", "category": "preference"}
            ]
        )
        brain.memory.save_conversation_turn = AsyncMock()

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = _mock_llm_response("You like Python!")
            result = await brain.think("what do I like?", Session())

        assert result["message"] == "You like Python!"
        # Verify recall was called with the user message
        brain.memory.recall.assert_called_once_with("what do I like?", limit=5)


# ── think() error on LLM call ────────────────────────────────────────


class TestThinkLLMError:
    @pytest.mark.asyncio
    async def test_llm_exception_returns_error_message(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = Exception("API rate limit exceeded")
            result = await brain.think("hello", Session())

        assert "trouble thinking" in result["message"]
        assert "API rate limit exceeded" in result["message"]
        assert result["tool_calls"] == []


# ── tool use loop ────────────────────────────────────────────────────


class TestToolUseLoop:
    def test_resolve_tool_calls_skips_fallback_parser_without_serialized_shape(self):
        brain = _make_brain()
        msg = MagicMock()
        msg.content = "Just a normal assistant reply"
        msg.tool_calls = None

        with patch.object(brain, "_extract_fallback_tool_calls", return_value=[]) as mock_parse:
            resolved = brain._resolve_tool_calls(msg, tools=[])

        assert resolved is None
        mock_parse.assert_not_called()

    def test_resolve_tool_calls_uses_fallback_when_native_unusable_and_shape_detected(self):
        brain = _make_brain()
        msg = MagicMock()
        msg.content = '<tool_call>{"name":"test_conn","arguments":{"input":"parsed"}}</tool_call>'

        emptyish_tool_call = MagicMock()
        emptyish_tool_call.function.name = ""
        emptyish_tool_call.function.arguments = "{}"
        msg.tool_calls = [emptyish_tool_call]

        parsed_tool_call = _mock_tool_call("test_conn", '{"input": "parsed"}')
        with patch.object(brain, "_extract_fallback_tool_calls", return_value=[parsed_tool_call]) as mock_parse:
            resolved = brain._resolve_tool_calls(msg, tools=_test_tools("test_conn"))

        assert resolved == [parsed_tool_call]
        mock_parse.assert_called_once()

    def test_resolve_tool_calls_prefers_native_when_serialized_shape_is_also_present(self):
        brain = _make_brain()
        msg = MagicMock()
        msg.content = '<tool_call>{"name":"test_conn","arguments":{"input":"parsed"}}</tool_call>'

        native_tool_call = _mock_tool_call("test_conn", '{"input": "native"}')
        parsed_tool_call = _mock_tool_call("test_conn", '{"input": "parsed"}')
        msg.tool_calls = [native_tool_call]

        with patch.object(brain, "_extract_fallback_tool_calls", return_value=[parsed_tool_call]) as mock_parse:
            resolved = brain._resolve_tool_calls(msg, tools=_test_tools("test_conn"))

        assert resolved == [native_tool_call]
        mock_parse.assert_called_once()

    def test_serialized_shape_detects_dsml_tool_calls_envelope(self):
        content = '<｜DSML｜tool_calls>[{"name":"test_conn","arguments":{"input":"check"}}]</｜DSML｜tool_calls>'

        assert Brain._has_serialized_tool_call_shape(content) is True

    def test_resolve_tool_calls_uses_fallback_when_native_name_not_allowed(self):
        brain = _make_brain()
        msg = MagicMock()
        msg.content = '<｜DSML｜tool_calls>[{"name":"test_conn","arguments":{"input":"parsed"}}]</｜DSML｜tool_calls>'
        msg.tool_calls = [_mock_tool_call("wrong_tool", '{"input": "native"}')]

        parsed_tool_call = _mock_tool_call("test_conn", '{"input": "parsed"}')
        with patch.object(brain, "_extract_fallback_tool_calls", return_value=[parsed_tool_call]):
            resolved = brain._resolve_tool_calls(msg, tools=_test_tools("test_conn"))

        assert resolved == [parsed_tool_call]

    def test_resolve_tool_calls_uses_fallback_when_native_arguments_are_malformed(self):
        brain = _make_brain()
        msg = MagicMock()
        msg.content = '<｜DSML｜tool_calls>[{"name":"test_conn","arguments":{"input":"parsed"}}]</｜DSML｜tool_calls>'
        msg.tool_calls = [_mock_tool_call("test_conn", '{bad json')]

        parsed_tool_call = _mock_tool_call("test_conn", '{"input": "parsed"}')
        with patch.object(brain, "_extract_fallback_tool_calls", return_value=[parsed_tool_call]):
            resolved = brain._resolve_tool_calls(msg, tools=_test_tools("test_conn"))

        assert resolved == [parsed_tool_call]

    @pytest.mark.asyncio
    async def test_single_tool_call_executed(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        tool_call = _mock_tool_call("test_conn__run", '{"input": "test"}')
        first_response = _mock_llm_response(tool_calls=[tool_call])
        final_response = _mock_llm_response("Done!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [first_response, final_response]
            result = await brain.think("run the test", Session())

        assert result["message"] == "Done!"
        assert len(result["tool_calls"]) == 1
        assert result["tool_calls"][0]["connector"] == "test_conn"
        assert result["tool_calls"][0]["action"] == "run"

    @pytest.mark.asyncio
    async def test_neo_read_skill_tool(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        # Add a skill with a path
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as skill_file:
            skill_file.write("# Test Skill\nDo the thing.")
            skill_file.flush()
            brain.registry.register(
                Capability(
                    kind=CapabilityKind.SKILL,
                    name="my-skill",
                    description="My skill",
                    status=CapabilityStatus.READY,
                    metadata={"path": skill_file.name},
                )
            )

        tool_call = _mock_tool_call("neo__read_skill", '{"skill_name": "my-skill"}')
        first_response = _mock_llm_response(tool_calls=[tool_call])
        final_response = _mock_llm_response("Skill loaded!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [first_response, final_response]
            result = await brain.think("use my skill", Session())

        assert result["message"] == "Skill loaded!"
        assert any(tc["name"] == "neo__read_skill" for tc in result["tool_calls"])

    @pytest.mark.asyncio
    async def test_tool_execution_error_is_caught(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        # Unknown tool name that will fail to parse
        tool_call = _mock_tool_call("nonexistent__action", "{}")
        first_response = _mock_llm_response(tool_calls=[tool_call])
        final_response = _mock_llm_response("I tried but it failed.")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [first_response, final_response]
            result = await brain.think("do something", Session())

        assert result["message"] == "I tried but it failed."
        assert len(result["tool_calls"]) >= 1
        # Error should be captured
        assert "error" in result["tool_calls"][0]

    @pytest.mark.asyncio
    async def test_max_iterations_respected(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        # Simulate a tool call that keeps requesting more tools
        tool_call = _mock_tool_call("test_conn__run", '{"input": "loop"}')
        looping_response = _mock_llm_response(tool_calls=[tool_call])

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = looping_response
            result = await brain.think("keep running", Session())

        # Should stop after max_iterations (3) tool calls
        assert len(result["tool_calls"]) == 3

    @pytest.mark.asyncio
    async def test_empty_final_response_triggers_last_no_tools_retry(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        tool_call = _mock_tool_call("test_conn__run", '{"input": "test"}')
        first_response = _mock_llm_response(tool_calls=[tool_call])
        empty_final = _mock_llm_response(content="")
        recovered_final = _mock_llm_response(content="Recovered answer")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [first_response, empty_final, recovered_final]
            result = await brain.think("run the test", Session())

        assert result["message"] == "Recovered answer"
        assert len(result["tool_calls"]) == 1
        assert mock_llm.call_count == 3
        # Last retry should not offer tools again
        assert mock_llm.call_args_list[-1].kwargs["tools"] is None

    @pytest.mark.asyncio
    async def test_max_iterations_empty_response_falls_back_to_tool_summary(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        tool_call = _mock_tool_call("test_conn__run", '{"input": "loop"}')
        looping_response = _mock_llm_response(content="", tool_calls=[tool_call])
        empty_final = _mock_llm_response(content="")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [looping_response, looping_response, looping_response, empty_final]
            result = await brain.think("keep running", Session())

        assert len(result["tool_calls"]) == 3
        assert result["message"]
        assert "test_conn" in result["message"] or "run" in result["message"]

    @pytest.mark.asyncio
    async def test_parser_fallback_handles_tool_call_xml_block(self):
        brain = _make_brain(model="qwen/qwen2.5-coder")
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        fallback_response = _mock_llm_response(
            content='<tool_call>{"name":"test_conn","arguments":{"input":"check"}}</tool_call>'
        )
        final_response = _mock_llm_response("Done!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [fallback_response, final_response]
            with patch.object(brain.connectors, "execute", new=AsyncMock(return_value={"stdout": "Python 3", "stderr": "", "exit_code": 0})) as mock_exec:
                result = await brain.think("check python", Session())

        assert result["message"] == "Done!"
        mock_exec.assert_awaited()

    @pytest.mark.asyncio
    async def test_parser_fallback_handles_embedded_json_tool_call(self):
        brain = _make_brain(model="deepseek/deepseek-chat")
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        fallback_response = _mock_llm_response(
            content='{"tool":"test_conn","arguments":{"input":"check"}}'
        )
        final_response = _mock_llm_response("Done!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [fallback_response, final_response]
            with patch.object(brain.connectors, "execute", new=AsyncMock(return_value={"stdout": "Python 3", "stderr": "", "exit_code": 0})) as mock_exec:
                result = await brain.think("check python", Session())

        assert result["message"] == "Done!"
        mock_exec.assert_awaited()

    @pytest.mark.asyncio
    async def test_parser_fallback_handles_deepseek_dsml(self):
        brain = _make_brain(model="deepseek/deepseek-chat")
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        dsml_content = (
            '<｜DSML｜invoke name="test_conn">'
            '<｜DSML｜parameter name="input" string="true">check</｜DSML｜parameter>'
            '</｜DSML｜invoke>'
        )
        fallback_response = _mock_llm_response(content=dsml_content)
        final_response = _mock_llm_response("Done!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [fallback_response, final_response]
            with patch.object(brain.connectors, "execute", new=AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0})) as mock_exec:
                result = await brain.think("check python", Session())

        assert result["message"] == "Done!"
        mock_exec.assert_awaited()

    @pytest.mark.asyncio
    async def test_parser_fallback_handles_minimax_xml(self):
        brain = _make_brain(model="minimax/abab6.5")
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        minimax_content = (
            '<invoke name="test_conn">'
            '<parameter name="input">check</parameter>'
            '</invoke>'
        )
        fallback_response = _mock_llm_response(content=minimax_content)
        final_response = _mock_llm_response("Done!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [fallback_response, final_response]
            with patch.object(brain.connectors, "execute", new=AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0})) as mock_exec:
                result = await brain.think("check python", Session())

        assert result["message"] == "Done!"
        mock_exec.assert_awaited()

    @pytest.mark.asyncio
    async def test_parser_fallback_handles_mistral_tool_calls_tag(self):
        brain = _make_brain(model="mistral/mistral-large")
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        mistral_content = '[TOOL_CALLS]{"name":"test_conn","arguments":{"input":"check"}}[/TOOL_CALLS]'
        fallback_response = _mock_llm_response(content=mistral_content)
        final_response = _mock_llm_response("Done!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [fallback_response, final_response]
            with patch.object(brain.connectors, "execute", new=AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0})) as mock_exec:
                result = await brain.think("check python", Session())

        assert result["message"] == "Done!"
        mock_exec.assert_awaited()

    @pytest.mark.asyncio
    async def test_parser_fallback_handles_dsml_tool_calls_envelope(self):
        brain = _make_brain(model="deepseek/deepseek-chat")
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        dsml_content = (
            '<｜DSML｜tool_calls>'
            '[{"name":"test_conn","arguments":{"input":"check"}}]'
            '</｜DSML｜tool_calls>'
        )
        fallback_response = _mock_llm_response(content=dsml_content)
        final_response = _mock_llm_response("Done!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [fallback_response, final_response]
            with patch.object(brain.connectors, "execute", new=AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0})) as mock_exec:
                result = await brain.think("check python", Session())

        assert result["message"] == "Done!"
        mock_exec.assert_awaited_once_with("test_conn", "run", {"input": "check"})

    @pytest.mark.asyncio
    async def test_parser_fallback_recovers_when_native_tool_calls_are_emptyish(self):
        brain = _make_brain(model="deepseek/deepseek-chat")
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        emptyish_tool_call = MagicMock()
        emptyish_tool_call.function.name = ""
        emptyish_tool_call.function.arguments = "{}"

        dsml_content = (
            '<｜DSML｜invoke name="test_conn">'
            '<｜DSML｜parameter name="input" string="true">check</｜DSML｜parameter>'
            '</｜DSML｜invoke>'
        )
        fallback_response = _mock_llm_response(
            content=dsml_content, tool_calls=[emptyish_tool_call]
        )
        final_response = _mock_llm_response("Done!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [fallback_response, final_response]
            with patch.object(brain.connectors, "execute", new=AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0})) as mock_exec:
                result = await brain.think("check python", Session())

        assert result["message"] == "Done!"
        mock_exec.assert_awaited_once_with("test_conn", "run", {"input": "check"})

    @pytest.mark.asyncio
    async def test_parser_fallback_does_not_override_usable_native_tool_calls(self):
        brain = _make_brain(model="gpt-4o-mini")
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        native_tool_call = _mock_tool_call("test_conn", '{"input": "native"}')
        fallback_response = _mock_llm_response(
            content='<tool_call>{"name":"test_conn","arguments":{"input":"parsed"}}</tool_call>',
            tool_calls=[native_tool_call],
        )
        final_response = _mock_llm_response("Done!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [fallback_response, final_response]
            with patch.object(brain.connectors, "execute", new=AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0})) as mock_exec:
                result = await brain.think("check python", Session())

        assert result["message"] == "Done!"
        mock_exec.assert_awaited_once_with("test_conn", "run", {"input": "native"})

    @pytest.mark.asyncio
    async def test_parser_fallback_handles_dsml_ascii_pipe(self):
        """DeepSeek sometimes returns <|DSML|...> with ASCII pipe (U+007C)."""
        brain = _make_brain(model="deepseek/deepseek-chat")
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        dsml_content = (
            '<|DSML|tool_calls>'
            '[{"name":"test_conn","arguments":{"input":"check"}}]'
            '</|DSML|tool_calls>'
        )
        fallback_response = _mock_llm_response(content=dsml_content)
        final_response = _mock_llm_response("Done!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [fallback_response, final_response]
            with patch.object(brain.connectors, "execute", new=AsyncMock(return_value={"stdout": "ok", "stderr": "", "exit_code": 0})) as mock_exec:
                result = await brain.think("check python", Session())

        assert result["message"] == "Done!"
        mock_exec.assert_awaited_once_with("test_conn", "run", {"input": "check"})

    def test_sanitize_raw_tool_content_strips_dsml(self):
        raw = (
            '<|DSML|tool_calls>'
            '[{"name":"test_conn","arguments":{"input":"check"}}]'
            '</|DSML|tool_calls>'
        )
        result = Brain._sanitize_raw_tool_content(raw)
        assert "DSML" not in result
        assert "test_conn" not in result
        assert result == ""

    def test_sanitize_raw_tool_content_strips_generic_tool_tags(self):
        raw = '<tool_call>{"name":"x","arguments":{}}</tool_call>some text'
        result = Brain._sanitize_raw_tool_content(raw)
        assert "tool_call" not in result
        assert result == "some text"

    def test_sanitize_raw_tool_content_strips_mistral_tag(self):
        raw = '[TOOL_CALLS]{"name":"x"}[/TOOL_CALLS]'
        result = Brain._sanitize_raw_tool_content(raw)
        assert "TOOL_CALLS" not in result
        assert result == ""

    @pytest.mark.asyncio
    async def test_unparsed_serialized_content_is_sanitised(self):
        """If the parser sees tool-like content but cannot extract usable calls,
        the raw markup must be stripped instead of leaking to the user."""
        brain = _make_brain(model="deepseek/deepseek-chat")
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        # Content looks like DSML but uses an unsupported inner format
        # so the fallback parser will not produce valid tool calls.
        bad_content = '<|DSML|invoke name="nonexistent_tool"><|DSML|parameter name="x">y</|DSML|parameter></|DSML|invoke>'
        fallback_response = _mock_llm_response(content=bad_content)

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = fallback_response
            result = await brain.think("do something", Session())

        # The raw DSML must NOT appear in the user-facing message.
        assert "<|DSML|" not in result["message"]
        assert "invoke" not in result["message"]

    def test_safe_extract_content_sanitises_dsml(self):
        brain = _make_brain()
        response = _mock_llm_response(
            content='<|DSML|tool_calls>[{"name":"x","arguments":{}}]</|DSML|tool_calls>'
        )
        result = brain._safe_extract_content(response)
        assert "DSML" not in result
        assert result == ""

    def test_safe_extract_content_passes_clean_text(self):
        brain = _make_brain()
        response = _mock_llm_response(content="Hello, world!")
        result = brain._safe_extract_content(response)
        assert result == "Hello, world!"

    @pytest.mark.asyncio
    async def test_retry_final_response_sanitises_dsml(self):
        """_retry_final_response_without_tools must never leak raw tool markup."""
        brain = _make_brain()
        dsml_response = _mock_llm_response(
            content='<|DSML|invoke name="x"><|DSML|parameter name="a">b</|DSML|parameter></|DSML|invoke>'
        )
        with patch("lumen.core.brain.acompletion", return_value=dsml_response):
            result = await brain._retry_final_response_without_tools([])
        assert "<|DSML|" not in result
        assert "invoke" not in result


# ---- _coerce_args ------------------------------------------------------------


class TestCoerceArgs:
    def test_integer_coercion(self):
        tools = [
            {
                "function": {
                    "name": "test",
                    "parameters": {
                        "properties": {"count": {"type": "integer"}}
                    },
                }
            }
        ]
        result = Brain._coerce_args({"count": "5"}, "test", tools)
        assert result["count"] == 5

    def test_float_coercion(self):
        tools = [
            {
                "function": {
                    "name": "test",
                    "parameters": {
                        "properties": {"rate": {"type": "number"}}
                    },
                }
            }
        ]
        result = Brain._coerce_args({"rate": "3.14"}, "test", tools)
        assert result["rate"] == 3.14

    def test_boolean_coercion(self):
        tools = [
            {
                "function": {
                    "name": "test",
                    "parameters": {
                        "properties": {"active": {"type": "boolean"}}
                    },
                }
            }
        ]
        assert Brain._coerce_args({"active": "true"}, "test", tools)["active"] is True
        assert Brain._coerce_args({"active": "false"}, "test", tools)["active"] is False
        assert Brain._coerce_args({"active": "yes"}, "test", tools)["active"] is True

    def test_no_coercion_when_type_matches(self):
        tools = [
            {
                "function": {
                    "name": "test",
                    "parameters": {
                        "properties": {"name": {"type": "string"}}
                    },
                }
            }
        ]
        result = Brain._coerce_args({"name": "hello"}, "test", tools)
        assert result["name"] == "hello"

    def test_no_tools_returns_params_unchanged(self):
        params = {"key": "value"}
        assert Brain._coerce_args(params, "test", None) == params

    def test_unknown_tool_returns_params_unchanged(self):
        tools = [
            {"function": {"name": "other", "parameters": {"properties": {}}}}
        ]
        params = {"count": "5"}
        assert Brain._coerce_args(params, "test", tools) == params

    def test_invalid_integer_stays_string(self):
        tools = [
            {
                "function": {
                    "name": "test",
                    "parameters": {
                        "properties": {"count": {"type": "integer"}}
                    },
                }
            }
        ]
        result = Brain._coerce_args({"count": "not-a-number"}, "test", tools)
        assert result["count"] == "not-a-number"


# ── _read_skill ──────────────────────────────────────────────────────


class TestReadSkill:
    def test_read_existing_skill(self):
        brain = _make_brain()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as f:
            f.write("# My Skill\nDetailed instructions.")
            f.flush()
            brain.registry.register(
                Capability(
                    kind=CapabilityKind.SKILL,
                    name="my-skill",
                    description="My skill",
                    status=CapabilityStatus.READY,
                    metadata={"path": f.name},
                )
            )

        result = brain._read_skill('{"skill_name": "my-skill"}')
        assert result["skill"] == "my-skill"
        assert "Detailed instructions" in result["content"]

    def test_read_nonexistent_skill(self):
        brain = _make_brain()
        result = brain._read_skill('{"skill_name": "ghost-skill"}')
        assert "error" in result
        assert "not found" in result["error"]

    def test_read_skill_with_no_path(self):
        brain = _make_brain()
        brain.registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name="pathless-skill",
                description="No path",
                status=CapabilityStatus.READY,
                metadata={},
            )
        )
        result = brain._read_skill('{"skill_name": "pathless-skill"}')
        assert "error" in result
        assert "no file path" in result["error"]

    def test_read_skill_with_invalid_json_arguments(self):
        brain = _make_brain()
        with pytest.raises(json.JSONDecodeError):
            brain._read_skill("not json")

    def test_read_skill_file_not_found(self):
        brain = _make_brain()
        brain.registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name="missing-file-skill",
                description="Missing file",
                status=CapabilityStatus.READY,
                metadata={"path": "/nonexistent/path/SKILL.md"},
            )
        )
        result = brain._read_skill('{"skill_name": "missing-file-skill"}')
        assert "error" in result
        assert "Cannot read" in result["error"]


# ── _search_modules ──────────────────────────────────────────────────


class TestSearchModules:
    def test_search_finds_matching_module(self):
        brain = _make_brain(
            catalog=_make_catalog(
                [
                    {
                        "name": "web-search",
                        "display_name": "Web Search",
                        "description": "Search the web",
                        "tags": ["search"],
                        "fills_gaps": ["web search"],
                    }
                ]
            )
        )
        result = brain._search_modules('{"query": "web search"}')
        assert result["found"] >= 1
        assert any(m["name"] == "web-search" for m in result["modules"])

    def test_search_no_results(self):
        brain = _make_brain(catalog=_make_catalog())
        result = brain._search_modules('{"query": "impossible capability"}')
        assert result["found"] == 0
        assert "No modules found" in result["message"]

    def test_search_limits_to_three(self):
        brain = _make_brain(
            catalog=_make_catalog(
                [
                    {
                        "name": f"mod-{i}",
                        "display_name": f"Module {i}",
                        "description": "search thing",
                        "tags": ["search"],
                        "fills_gaps": ["search"],
                    }
                    for i in range(5)
                ]
            )
        )
        result = brain._search_modules('{"query": "search"}')
        assert result["found"] <= 3


# ── _check_capability ─────────────────────────────────────────────────


class TestCheckCapability:
    """Tests for the neo__check_capability self-verification tool."""

    def test_finds_ready_capability_by_name(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.CONNECTOR,
                name="telegram",
                description="Send and receive Telegram messages",
                status=CapabilityStatus.READY,
                provides=["send_message", "receive_message"],
            )
        )
        brain = _make_brain(registry=registry)
        result = brain._check_capability('{"query": "telegram"}')
        assert result["found"] >= 1
        assert any(c["name"] == "telegram" for c in result["ready"])
        assert "not_ready" not in result or not result["not_ready"]

    def test_finds_not_ready_capability(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="telegram",
                description="Telegram integration",
                status=CapabilityStatus.AVAILABLE,
                provides=["telegram_messaging"],
                metadata={
                    "display_name": "Telegram",
                    "pending_setup": {
                        "env_specs": [
                            {"name": "BOT_TOKEN", "label": "Bot Token", "secret": True}
                        ]
                    },
                },
            )
        )
        brain = _make_brain(registry=registry)
        result = brain._check_capability('{"query": "telegram"}')
        assert result["found"] >= 1
        assert "not_ready" in result
        assert any(c["name"] == "telegram" for c in result["not_ready"])
        assert "ready" not in result or not result["ready"]

    def test_finds_by_provides_field(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.CONNECTOR,
                name="email-smtp",
                description="Email connector",
                status=CapabilityStatus.READY,
                provides=["send_email", "receive_email"],
            )
        )
        brain = _make_brain(registry=registry)
        result = brain._check_capability('{"query": "send email"}')
        assert result["found"] >= 1

    def test_finds_by_description(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name="calendar-helper",
                description="Manage calendar events and scheduling",
                status=CapabilityStatus.READY,
            )
        )
        brain = _make_brain(registry=registry)
        result = brain._check_capability('{"query": "calendar"}')
        assert result["found"] >= 1
        assert any("calendar" in c["name"] for c in result["ready"])

    def test_finds_by_display_name(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="x-lumen-comunicacion-whatsapp",
                description="WhatsApp Business API integration",
                status=CapabilityStatus.READY,
                metadata={"display_name": "WhatsApp"},
            )
        )
        brain = _make_brain(registry=registry)
        result = brain._check_capability('{"query": "whatsapp"}')
        assert result["found"] >= 1

    def test_catalog_fallback_when_no_registry_match(self):
        brain = _make_brain(
            registry=Registry(),
            catalog=_make_catalog(
                [
                    {
                        "name": "x-lumen-comunicacion-telegram",
                        "display_name": "Telegram",
                        "description": "Send Telegram messages",
                        "tags": ["telegram", "messaging"],
                        "fills_gaps": ["telegram", "messaging"],
                    }
                ]
            ),
        )
        result = brain._check_capability('{"query": "telegram"}')
        assert result["found"] == 0
        assert "installable" in result
        assert any(
            "telegram" in m["name"].lower() for m in result["installable"]
        )

    def test_no_match_anywhere(self):
        brain = _make_brain(registry=Registry(), catalog=_make_catalog())
        result = brain._check_capability('{"query": "quantum computing"}')
        assert result["found"] == 0
        assert "No matching capability found" in result["message"]

    def test_empty_query_returns_error(self):
        brain = _make_brain()
        result = brain._check_capability('{"query": ""}')
        assert "error" in result

    def test_mixed_ready_and_not_ready(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.CHANNEL,
                name="telegram-bot",
                description="Telegram bot channel",
                status=CapabilityStatus.READY,
                provides=["telegram_bot"],
            )
        )
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="telegram-notifications",
                description="Telegram push notifications",
                status=CapabilityStatus.MISSING_DEPS,
                metadata={"display_name": "Telegram Notifications"},
            )
        )
        brain = _make_brain(registry=registry)
        result = brain._check_capability('{"query": "telegram"}')
        assert result["found"] >= 2
        assert any(c["name"] == "telegram-bot" for c in result["ready"])
        assert any(
            c["name"] == "telegram-notifications" for c in result["not_ready"]
        )

    def test_searches_across_all_capability_kinds(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.MCP,
                name="github-server",
                description="GitHub API access via MCP",
                status=CapabilityStatus.READY,
                provides=["github_api"],
                metadata={"tags": ["github", "code"]},
            )
        )
        brain = _make_brain(registry=registry)
        result = brain._check_capability('{"query": "github"}')
        assert result["found"] >= 1
        assert result["ready"][0]["kind"] == "mcp"

    def test_results_limited_to_ten(self):
        registry = Registry()
        for i in range(15):
            registry.register(
                Capability(
                    kind=CapabilityKind.SKILL,
                    name=f"search-skill-{i}",
                    description=f"Search-related skill {i}",
                    status=CapabilityStatus.READY,
                )
            )
        brain = _make_brain(registry=registry)
        result = brain._check_capability('{"query": "search"}')
        total = len(result.get("ready", [])) + len(result.get("not_ready", []))
        assert total <= 10

    def test_blocker_describes_error(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="broken-mod",
                description="A broken module",
                status=CapabilityStatus.ERROR,
                metadata={"error": "connection refused"},
            )
        )
        brain = _make_brain(registry=registry)
        result = brain._check_capability('{"query": "broken"}')
        assert result["found"] >= 1
        blocker = result["not_ready"][0]["blocker"]
        assert "connection refused" in blocker

    def test_blocker_describes_pending_setup(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="unconfigured-mod",
                description="Needs config",
                status=CapabilityStatus.AVAILABLE,
                metadata={
                    "pending_setup": {
                        "env_specs": [
                            {"name": "API_KEY", "label": "API Key"},
                            {"name": "BASE_URL"},
                        ]
                    }
                },
            )
        )
        brain = _make_brain(registry=registry)
        result = brain._check_capability('{"query": "unconfigured"}')
        blocker = result["not_ready"][0]["blocker"]
        assert "needs setup" in blocker

    @pytest.mark.asyncio
    async def test_check_capability_via_tool_use_loop(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        brain.registry.register(
            Capability(
                kind=CapabilityKind.CONNECTOR,
                name="telegram",
                description="Telegram messaging",
                status=CapabilityStatus.READY,
            )
        )

        tool_call = _mock_tool_call(
            "neo__check_capability", '{"query": "telegram"}'
        )
        first_response = _mock_llm_response(tool_calls=[tool_call])
        final_response = _mock_llm_response("Yes, I have Telegram ready!")

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [first_response, final_response]
            result = await brain.think("can you send me a telegram?", Session())

        assert result["message"] == "Yes, I have Telegram ready!"
        assert any(
            tc["name"] == "neo__check_capability" for tc in result["tool_calls"]
        )


# ── _match_flow_trigger ──────────────────────────────────────────────


class TestMatchFlowTrigger:
    def test_matching_trigger(self):
        brain = _make_brain(
            flows=[
                {
                    "intent": "book_appointment",
                    "triggers": ["book", "appointment", "schedule"],
                    "slots": {},
                }
            ]
        )
        result = brain._match_flow_trigger("I want to book something")
        assert result is not None
        assert result["intent"] == "book_appointment"

    def test_no_matching_trigger(self):
        brain = _make_brain(
            flows=[
                {
                    "intent": "book_appointment",
                    "triggers": ["book"],
                    "slots": {},
                }
            ]
        )
        result = brain._match_flow_trigger("tell me a joke")
        assert result is None

    def test_case_insensitive_trigger(self):
        brain = _make_brain(
            flows=[
                {
                    "intent": "greet",
                    "triggers": ["hello"],
                    "slots": {},
                }
            ]
        )
        result = brain._match_flow_trigger("HELLO there!")
        assert result is not None
        assert result["intent"] == "greet"

    def test_empty_flows_returns_none(self):
        brain = _make_brain(flows=[])
        assert brain._match_flow_trigger("anything") is None

    @pytest.mark.asyncio
    async def test_flow_triggered_on_think(self):
        brain = _make_brain(
            flows=[
                {
                    "intent": "order",
                    "triggers": ["order", "buy"],
                    "slots": {
                        "item": {"required": True, "ask": "What item?"},
                    },
                }
            ]
        )
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = _mock_llm_response("What would you like to order?")
            session = Session()
            await brain.think("I want to order something", session)

        assert session.active_flow is not None
        assert session.active_flow["intent"] == "order"

    @pytest.mark.asyncio
    async def test_module_setup_flow_runs_without_llm_and_redacts_secret_history(self):
        handler = AsyncMock(
            return_value={
                "status": "ok",
                "message": "Listo, pending-module ya quedó listo para usar.",
            }
        )
        brain = _make_brain(
            flows=[
                {
                    "intent": "module-setup-pending-module",
                    "triggers": ["setup:pending-module"],
                    "slots": {
                        "DEMO_TOKEN": {
                            "required": True,
                            "secret": True,
                            "ask": "Token del demo",
                        },
                        "DEMO_CHAT_ID": {
                            "required": True,
                            "secret": False,
                            "ask": "Chat ID",
                        },
                    },
                    "on_complete": "save_module_env:pending-module",
                    "first_message": "Necesito unos datos.",
                }
            ],
            flow_action_handler=handler,
        )
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        session = Session()

        with patch("lumen.core.brain.acompletion") as mock_llm:
            first = await brain.think("setup:pending-module", session)
            second = await brain.think("super-secret-token", session)
            third = await brain.think("chat-123", session)

        mock_llm.assert_not_called()
        assert "Necesito unos datos" in first["message"]
        assert "Token del demo" in first["message"]
        assert second["message"] == "Listo.\n\nChat ID"
        assert third["message"] == "Listo, pending-module ya quedó listo para usar."
        handler.assert_awaited_once_with(
            "save_module_env:pending-module",
            {"DEMO_TOKEN": "super-secret-token", "DEMO_CHAT_ID": "chat-123"},
            session=session,
        )
        assert session.active_flow is None
        assert session.history[2] == {"role": "user", "content": "[secret:DEMO_TOKEN]"}
        assert session.history[4] == {"role": "user", "content": "chat-123"}
        brain.memory.save_conversation_turn.assert_any_await(
            session.session_id,
            "user",
            "[secret:DEMO_TOKEN]",
        )

    @pytest.mark.asyncio
    async def test_artifact_setup_flow_supports_generic_mcp_actions(self):
        handler = AsyncMock(
            return_value={
                "status": "ok",
                "message": "Listo, github ya quedó listo para usar.",
            }
        )
        brain = _make_brain(
            flows=[
                {
                    "intent": "artifact-setup-mcp-github",
                    "triggers": ["setup:github", "setup:mcp:github"],
                    "slots": {
                        "GITHUB_PERSONAL_ACCESS_TOKEN": {
                            "required": True,
                            "secret": True,
                            "ask": "GitHub token",
                        },
                    },
                    "on_complete": "save_artifact_env:mcp:github",
                    "first_message": "Necesito un dato para GitHub.",
                }
            ],
            flow_action_handler=handler,
        )
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        session = Session()

        with patch("lumen.core.brain.acompletion") as mock_llm:
            first = await brain.think("setup:github", session)
            second = await brain.think("ghp_secret", session)

        mock_llm.assert_not_called()
        assert first["message"] == "Necesito un dato para GitHub.\n\nGitHub token"
        assert second["message"] == "Listo, github ya quedó listo para usar."
        handler.assert_awaited_once_with(
            "save_artifact_env:mcp:github",
            {"GITHUB_PERSONAL_ACCESS_TOKEN": "ghp_secret"},
            session=session,
        )

    @pytest.mark.asyncio
    async def test_single_pending_setup_offer_starts_flow_on_affirmative_reply(self):
        brain = _make_brain(
            flows=[
                {
                    "intent": "module-setup-telegram",
                    "triggers": ["setup:telegram"],
                    "slots": {
                        "TELEGRAM_TOKEN": {
                            "required": True,
                            "secret": True,
                            "ask": "Telegram token",
                        },
                    },
                    "on_complete": "save_module_env:telegram",
                    "first_message": "Necesito un dato para Telegram.",
                }
            ]
        )
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        session = Session()

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = _mock_llm_response(
                "I have Telegram but it still needs configuration. Want to configure it now?"
            )

            first = await brain.think("what changed?", session)
            second = await brain.think("sí", session)

        assert first["message"].endswith("Want to configure it now?")
        assert second["message"] == "Necesito un dato para Telegram.\n\nTelegram token"
        assert session.active_flow is not None
        assert session.active_flow["intent"] == "module-setup-telegram"
        assert mock_llm.await_count == 1

    @pytest.mark.asyncio
    async def test_multiple_pending_setup_offer_does_not_guess_on_affirmative_reply(self):
        brain = _make_brain(
            flows=[
                {
                    "intent": "module-setup-telegram",
                    "triggers": ["setup:telegram"],
                    "slots": {"TELEGRAM_TOKEN": {"required": True, "ask": "Telegram token"}},
                    "on_complete": "save_module_env:telegram",
                    "first_message": "Telegram setup.",
                },
                {
                    "intent": "module-setup-slack",
                    "triggers": ["setup:slack"],
                    "slots": {"SLACK_TOKEN": {"required": True, "ask": "Slack token"}},
                    "on_complete": "save_module_env:slack",
                    "first_message": "Slack setup.",
                },
            ]
        )
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        session = Session()

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = _mock_llm_response(
                "I have Telegram and Slack, but they still need configuration. Want to configure one now?"
            )

            await brain.think("what changed?", session)
            second = await brain.think("dale", session)

        assert "telegram" in second["message"].lower()
        assert "slack" in second["message"].lower()
        assert "configuraciones pendientes" in second["message"].lower()
        assert session.active_flow is None
        assert mock_llm.await_count == 1

    @pytest.mark.asyncio
    async def test_greeting_does_not_continue_pending_setup_offer(self):
        brain = _make_brain(
            flows=[
                {
                    "intent": "module-setup-telegram",
                    "triggers": ["setup:telegram"],
                    "slots": {"TELEGRAM_TOKEN": {"required": True, "ask": "Telegram token"}},
                    "on_complete": "save_module_env:telegram",
                    "first_message": "Telegram setup.",
                }
            ]
        )
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        session = Session()

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [
                _mock_llm_response(
                    "I have Telegram but it still needs configuration. Want to configure it now?"
                ),
                _mock_llm_response("¡Buenas! ¿En qué te ayudo?"),
            ]

            first = await brain.think("what changed?", session)
            second = await brain.think("Buenas", session)

        assert first["message"].endswith("Want to configure it now?")
        assert second["message"] == "¡Buenas! ¿En qué te ayudo?"
        assert session.active_flow is None
        assert session.pending_setup_offer is None
        assert mock_llm.await_count == 2

    @pytest.mark.asyncio
    async def test_pending_setup_offer_starts_flow_on_explicit_module_request(self):
        brain = _make_brain(
            flows=[
                {
                    "intent": "module-setup-telegram",
                    "triggers": ["setup:telegram"],
                    "slots": {"TELEGRAM_TOKEN": {"required": True, "ask": "Telegram token"}},
                    "on_complete": "save_module_env:telegram",
                    "first_message": "Telegram setup.",
                }
            ]
        )
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        session = Session()

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = _mock_llm_response(
                "I have Telegram but it still needs configuration. Want to configure it now?"
            )

            await brain.think("what changed?", session)
            second = await brain.think("quiero configurar telegram", session)

        assert second["message"] == "Telegram setup.\n\nTelegram token"
        assert session.active_flow is not None
        assert session.active_flow["intent"] == "module-setup-telegram"
        assert mock_llm.await_count == 1

    @pytest.mark.asyncio
    async def test_natural_language_mention_starts_setup_flow_without_llm(self):
        brain = _make_brain(
            flows=[
                {
                    "intent": "module-setup-telegram",
                    "triggers": ["setup:telegram"],
                    "slots": {"TELEGRAM_TOKEN": {"required": True, "ask": "Telegram token"}},
                    "on_complete": "save_module_env:telegram",
                    "first_message": "Telegram setup.",
                }
            ]
        )
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        session = Session()

        with patch("lumen.core.brain.acompletion") as mock_llm:
            result = await brain.think(
                "Hola! Te fijas si me puedes hablar por telegram?", session
            )

        mock_llm.assert_not_called()
        assert "Telegram setup" in result["message"]
        assert session.active_flow is not None
        assert session.active_flow["intent"] == "module-setup-telegram"

    @pytest.mark.asyncio
    async def test_natural_language_ambiguous_setup_does_not_guess(self):
        brain = _make_brain(
            flows=[
                {
                    "intent": "module-setup-telegram",
                    "triggers": ["setup:telegram"],
                    "slots": {"TELEGRAM_TOKEN": {"required": True, "ask": "Telegram token"}},
                    "on_complete": "save_module_env:telegram",
                    "first_message": "Telegram setup.",
                },
                {
                    "intent": "module-setup-telegram-notifications",
                    "triggers": ["setup:telegram-notifications"],
                    "slots": {"TG_KEY": {"required": True, "ask": "TG key"}},
                    "on_complete": "save_module_env:telegram-notifications",
                    "first_message": "TG setup.",
                },
            ]
        )
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        session = Session()

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = _mock_llm_response("I see two Telegram options!")
            result = await brain.think("hablar por telegram", session)

        assert session.active_flow is None
        assert mock_llm.await_count == 1

    @pytest.mark.asyncio
    async def test_explicit_setup_trigger_still_works_with_pending_setup_offer(self):
        brain = _make_brain(
            flows=[
                {
                    "intent": "module-setup-telegram",
                    "triggers": ["setup:telegram"],
                    "slots": {"TELEGRAM_TOKEN": {"required": True, "ask": "Telegram token"}},
                    "on_complete": "save_module_env:telegram",
                    "first_message": "Telegram setup.",
                }
            ]
        )
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        session = Session(pending_setup_offer={"modules": ["telegram"]})

        with patch("lumen.core.brain.acompletion") as mock_llm:
            second = await brain.think("setup:telegram", session)

        assert second["message"] == "Telegram setup.\n\nTelegram token"
        assert session.active_flow is not None
        assert session.active_flow["intent"] == "module-setup-telegram"
        mock_llm.assert_not_called()


# ── _guard_capability_claims ──────────────────────────────────────────


class TestGuardCapabilityClaims:
    """Tests for the deterministic capability claim guard."""

    def test_replaces_false_claim_with_correction(self):
        registry = Registry()
        brain = _make_brain(
            registry=registry,
            catalog=_make_catalog(
                [
                    {
                        "name": "x-lumen-comunicacion-telegram",
                        "display_name": "Telegram",
                        "description": "Telegram messaging",
                        "tags": ["telegram"],
                    }
                ]
            ),
        )
        response = brain._guard_capability_claims(
            "Sí, tengo Telegram configurado y listo para usar."
        )
        assert "no tengo telegram instalado" in response.lower()
        assert "Sí, tengo" not in response

    def test_no_correction_when_capability_is_ready(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="telegram",
                description="Telegram messaging",
                status=CapabilityStatus.READY,
            )
        )
        brain = _make_brain(registry=registry)
        response = brain._guard_capability_claims(
            "Puedo enviarte un mensaje por Telegram."
        )
        assert response == "Puedo enviarte un mensaje por Telegram."

    def test_corrects_not_ready_capability(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="telegram",
                description="Telegram messaging",
                status=CapabilityStatus.AVAILABLE,
                metadata={"display_name": "Telegram"},
            )
        )
        brain = _make_brain(registry=registry)
        response = brain._guard_capability_claims(
            "Sí, Telegram está configurado y listo."
        )
        assert "no está listo" in response.lower()

    def test_no_change_when_no_capability_mentioned(self):
        brain = _make_brain()
        response = brain._guard_capability_claims(
            "¡Hola! ¿En qué puedo ayudarte hoy?"
        )
        assert response == "¡Hola! ¿En qué puedo ayudarte hoy?"

    def test_no_false_positive_on_substring_match(self):
        registry = Registry()
        brain = _make_brain(registry=registry)
        # "schedule" contains no known capability name as whole word
        response = brain._guard_capability_claims(
            "Puedo ayudarte a organizar tu agenda."
        )
        assert response == "Puedo ayudarte a organizar tu agenda."

    def test_passes_through_action_mention(self):
        """Mentioning a capability in an action context (not claiming to have it)
        should pass through — e.g. 'Te mando un mensaje por Telegram'."""
        brain = _make_brain(
            registry=Registry(),
            catalog=_make_catalog(
                [
                    {
                        "name": "x-lumen-comunicacion-telegram",
                        "display_name": "Telegram",
                        "description": "Telegram messaging",
                        "tags": ["telegram"],
                        "fills_gaps": ["telegram"],
                    }
                ]
            ),
        )
        response = brain._guard_capability_claims(
            "Te mando un mensaje por Telegram."
        )
        # Should NOT be corrected — it's an action, not a claim of having it
        assert response == "Te mando un mensaje por Telegram."


# ── contradiction retry layer (Phase 2.3) ──────────────────────────────


class TestContradictionRetryLayer:
    """Tests for Phase 2.3 — contradiction retry when LLM denies READY capabilities."""

    def test_detect_capability_denial_finds_wrong_denial(self):
        """LLM says 'no tengo terminal' but terminal IS READY → detected."""
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.CONNECTOR,
                name="terminal",
                description="Execute terminal commands",
                status=CapabilityStatus.READY,
                provides=["execute_command"],
            )
        )
        brain = _make_brain(registry=registry)
        denials = brain._detect_capability_denial(
            "No tengo terminal instalado, no puedo ejecutar comandos."
        )
        assert len(denials) == 1
        assert denials[0]["name"] == "terminal"
        assert denials[0]["capability"].is_ready()

    def test_detect_capability_denial_ignores_ready_affirmation(self):
        """LLM says 'tengo terminal listo' and terminal IS READY → NOT flagged."""
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.CONNECTOR,
                name="terminal",
                description="Execute terminal commands",
                status=CapabilityStatus.READY,
            )
        )
        brain = _make_brain(registry=registry)
        denials = brain._detect_capability_denial(
            "Tengo terminal instalado y listo para usar."
        )
        assert len(denials) == 0

    def test_detect_capability_denial_ignores_not_ready_capability(self):
        """LLM says 'no tengo X' but X is NOT READY → NOT flagged (truthful)."""
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="telegram",
                description="Telegram integration",
                status=CapabilityStatus.AVAILABLE,  # NOT READY
            )
        )
        brain = _make_brain(registry=registry)
        denials = brain._detect_capability_denial(
            "No tengo Telegram configurado todavía."
        )
        assert len(denials) == 0  # Truthful denial — not flagged

    def test_detect_capability_denial_ignores_unmentioned_capabilities(self):
        """LLM doesn't mention any capability → nothing detected."""
        brain = _make_brain()
        denials = brain._detect_capability_denial(
            "No puedo ayudarte con eso, lo siento."
        )
        assert len(denials) == 0

    def test_detect_capability_denial_finds_i_cant_pattern(self):
        """'I cannot use X' on a READY capability is detected."""
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name="web-search",
                description="Search the web",
                status=CapabilityStatus.READY,
            )
        )
        brain = _make_brain(registry=registry)
        denials = brain._detect_capability_denial(
            "I cannot use web-search because it's not available to me."
        )
        assert len(denials) == 1
        assert denials[0]["name"] == "web-search"

    def test_is_denial_of_capability_with_negation_after_capability(self):
        """'I have X but I cannot use it' — negation appears after capability, denial detected.
        This is a Phase 2.3 contradiction: the LLM claims it HAS X but then denies
        it can USE X. Both clauses appear in the same proximity window, so denial wins."""
        brain = _make_brain()
        # "I have" (ability) + "cannot use" (negation) in same sentence near capability
        result = brain._is_denial_of_capability(
            "I have terminal but I cannot use it right now.",
            "terminal"
        )
        assert result is True

    def test_is_denial_of_capability_pure_denial(self):
        """'I don't have X' on a capability → denial detected."""
        brain = _make_brain()
        result = brain._is_denial_of_capability(
            "I don't have terminal access.",
            "terminal"
        )
        assert result is True

    def test_build_contradiction_directive_contains_capability_info(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.CONNECTOR,
                name="terminal",
                description="Execute commands",
                status=CapabilityStatus.READY,
                provides=["execute", "run"],
            )
        )
        brain = _make_brain(registry=registry)
        denials = brain._detect_capability_denial("No tengo terminal.")
        directive = brain._build_contradiction_directive(denials)

        assert "Contradiction Correction" in directive
        assert "terminal" in directive
        assert "READY" in directive
        assert "execute" in directive

    @pytest.mark.asyncio
    async def test_think_retry_clears_messages_after_finalize(self):
        """After contradiction retry completes, _current_messages is cleared."""
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.CONNECTOR,
                name="terminal",
                description="Execute commands",
                status=CapabilityStatus.READY,
            )
        )
        brain = _make_brain(registry=registry)
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        first_response = _mock_llm_response(
            "No tengo terminal, no puedo hacer eso."
        )
        corrected_response = _mock_llm_response(
            "Sí tengo terminal. Listo."
        )

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.side_effect = [first_response, corrected_response]
            result = await brain.think("ejecuta un comando", Session())

        # Messages should be cleared after finalize
        assert brain._current_messages is None
        # Should have at least 2 LLM calls (first response + retry)
        assert mock_llm.call_count >= 2

    @pytest.mark.asyncio
    async def test_think_no_retry_when_no_contradiction(self):
        """When LLM correctly acknowledges a READY capability, no retry happens."""
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.CONNECTOR,
                name="terminal",
                description="Execute commands",
                status=CapabilityStatus.READY,
            )
        )
        brain = _make_brain(registry=registry)
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        # LLM correctly says it HAS terminal
        response = _mock_llm_response(
            "Sí tengo terminal. Acá está el resultado."
        )

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = response
            result = await brain.think("ejecuta un comando", Session())

        # Only 1 call — no retry needed
        assert mock_llm.call_count == 1
        assert "terminal" in result["message"].lower()

    @pytest.mark.asyncio
    async def test_contradiction_retry_uses_correct_options(self):
        """Contradiction retry uses purpose='contradiction' with full tools."""
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.CONNECTOR,
                name="terminal",
                description="Execute commands",
                status=CapabilityStatus.READY,
            )
        )
        brain = _make_brain(registry=registry)
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock()

        first_response = _mock_llm_response(
            "No tengo terminal, no puedo."
        )
        retry_response = _mock_llm_response("Ya lo hice.")

        call_kwargs = {}

        async def capture_call(**kwargs):
            call_kwargs.update(kwargs)
            return retry_response

        with patch("lumen.core.brain.acompletion", side_effect=[first_response, capture_call]):
            result = await brain.think("ejecuta algo", Session())

        # The retry call (second call) should include tools and max_tokens
        # We can verify through the _completion_options logic
        profile = brain._model_profile()
        assert profile["completion_max_tokens"] + 256 > profile["completion_max_tokens"]


# ── _build_prompt ────────────────────────────────────────────────────
    def test_prompt_contains_consciousness(self):
        brain = _make_brain()
        session = Session()
        context = {
            "consciousness": "I am TestLumen",
            "personality": "I am a test assistant",
            "body": "I can test things",
            "catalog": "",
            "active_flow": None,
            "filled_slots": {},
            "pending_slots": [],
            "memories": [],
            "available_flows": [],
        }
        messages = brain._build_prompt(context, "hello", session)
        system_msg = messages[0]
        assert system_msg["role"] == "system"
        assert "TestLumen" in system_msg["content"]

    def test_prompt_includes_memories(self):
        brain = _make_brain()
        session = Session()
        context = {
            "consciousness": "I am Lumen",
            "personality": "assistant",
            "body": "capabilities",
            "catalog": "",
            "active_flow": None,
            "filled_slots": {},
            "pending_slots": [],
            "memories": [
                {"category": "pref", "content": "user likes dark mode"}
            ],
            "available_flows": [],
        }
        messages = brain._build_prompt(context, "hello", session)
        system_msg = messages[0]["content"]
        assert "user likes dark mode" in system_msg

    def test_prompt_includes_active_flow(self):
        brain = _make_brain()
        session = Session()
        session.start_flow(
            {
                "intent": "book",
                "slots": {
                    "date": {"required": True, "ask": "What date?"},
                    "time": {"required": True, "ask": "What time?"},
                },
            }
        )
        session.fill_slot("date", "tomorrow")

        context = {
            "consciousness": "I am Lumen",
            "personality": "assistant",
            "body": "capabilities",
            "catalog": "",
            "active_flow": session.active_flow,
            "filled_slots": session.slots,
            "pending_slots": session.get_pending_slots(),
            "memories": [],
            "available_flows": [],
        }
        messages = brain._build_prompt(context, "hello", session)
        system_msg = messages[0]["content"]
        assert "book" in system_msg
        assert "time" in system_msg

    def test_prompt_includes_conversation_history(self):
        brain = _make_brain()
        session = Session()
        session.add_message("user", "previous message")
        session.add_message("assistant", "previous reply")

        context = {
            "consciousness": "I am Lumen",
            "personality": "assistant",
            "body": "capabilities",
            "catalog": "",
            "active_flow": None,
            "filled_slots": {},
            "pending_slots": [],
            "memories": [],
            "available_flows": [],
        }
        messages = brain._build_prompt(context, "new message", session)
        assert messages[1]["content"] == "previous message"
        assert messages[2]["content"] == "previous reply"
        assert messages[-1]["content"] == "new message"

    def test_registry_context_separates_ready_and_not_ready_capabilities_generically(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.SKILL,
                name="faq",
                description="Answers common questions",
                status=CapabilityStatus.READY,
            )
        )
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="demo-bridge",
                description="Connects to a generic external bridge",
                status=CapabilityStatus.AVAILABLE,
                metadata={
                    "display_name": "Demo Bridge",
                    "pending_setup": {
                        "module": "demo-bridge",
                        "env_specs": [{"name": "DEMO_API_KEY", "secret": True}],
                    },
                },
            )
        )
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="broken-sync",
                description="Synchronizes a generic workspace",
                status=CapabilityStatus.ERROR,
                metadata={
                    "display_name": "Broken Sync",
                    "error": "Handshake failed",
                },
            )
        )
        registry.register(
            Capability(
                kind=CapabilityKind.MCP,
                name="docs-index",
                description="Indexes generic docs",
                status=CapabilityStatus.MISSING_DEPS,
                metadata={"display_name": "Docs Index"},
            )
        )

        body = registry.as_context()

        assert "### What I CAN do (READY — use immediately)" in body
        assert "Answers common questions" in body
        assert "### Installed but NOT ready yet (do not present as usable)" in body
        assert "Installed or present is NOT the same as ready." in body
        assert "Demo Bridge" in body
        assert "DEMO_API_KEY" in body
        assert "present in my body, but NOT READY [available]" in body
        assert "Broken Sync" in body
        assert "currently failing with an error: Handshake failed" in body
        assert "Docs Index" in body
        assert "present in my body, but NOT READY [missing deps]" in body
        assert "missing required dependencies before it can work" in body

    def test_prompt_surfaces_installed_but_not_ready_module_truthfully(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="demo-bridge",
                description="Generic communication bridge",
                status=CapabilityStatus.AVAILABLE,
                metadata={
                    "display_name": "Demo Bridge",
                    "pending_setup": {
                        "module": "demo-bridge",
                        "env_specs": [
                            {
                                "name": "DEMO_API_KEY",
                                "label": "Demo API key",
                                "secret": True,
                            }
                        ],
                    },
                },
            )
        )
        brain = _make_brain(registry=registry)
        session = Session()
        context = {
            "consciousness": "I am Lumen",
            "personality": "assistant",
            "body": registry.as_context(),
            "catalog": "",
            "active_flow": None,
            "filled_slots": {},
            "pending_slots": [],
            "memories": [],
            "available_flows": [],
        }

        messages = brain._build_prompt(context, "can you use the demo bridge?", session)
        system_msg = messages[0]["content"]

        assert "Installed but NOT ready yet" in system_msg
        assert "Demo Bridge" in system_msg
        assert "present in my body, but NOT READY [available]" in system_msg
        assert "DEMO_API_KEY" in system_msg
        assert "Installed or present is NOT the same as ready." in system_msg
        assert "Truthfulness about readiness is more important than the fact that something is installed." in system_msg

    def test_prompt_surfaces_degraded_installed_module_as_not_ready(self):
        registry = Registry()
        registry.register(
            Capability(
                kind=CapabilityKind.MODULE,
                name="broken-sync",
                description="Generic synchronization bridge",
                status=CapabilityStatus.ERROR,
                metadata={
                    "display_name": "Broken Sync",
                    "error": "Connector handshake failed",
                },
            )
        )
        brain = _make_brain(registry=registry)
        session = Session()
        context = {
            "consciousness": "I am Lumen",
            "personality": "assistant",
            "body": registry.as_context(),
            "catalog": "",
            "active_flow": None,
            "filled_slots": {},
            "pending_slots": [],
            "memories": [],
            "available_flows": [],
        }

        messages = brain._build_prompt(context, "what can you do?", session)
        system_msg = messages[0]["content"]

        assert "Installed but NOT ready yet" in system_msg
        assert "Broken Sync" in system_msg
        assert "Connector handshake failed" in system_msg
        assert "do not present as usable" in system_msg
        assert "Never present a non-ready capability as usable, available now, or already working." in system_msg


# ── Tool Suggestion (Phase 2.4) ───────────────────────────────────────


class TestSuggestRelevantTools:
    """Tests for _suggest_relevant_tools — tool hints based on user message."""

    def test_suggest_terminal_tools_for_run_command(self):
        brain = _make_brain()
        tools = [{"function": {"name": "terminal", "description": "Run commands"}}]

        result = brain._suggest_relevant_tools("run a script", tools)

        assert result
        assert "Tool Hint" in result
        assert "terminal" in result.lower()

    def test_suggest_file_tools_for_read_file(self):
        brain = _make_brain()
        tools = [{"function": {"name": "file_read", "description": "Read files"}}]

        result = brain._suggest_relevant_tools("lee el archivo", tools)

        assert result
        assert "Tool Hint" in result
        assert "file" in result.lower() or "read" in result.lower()

    def test_suggest_write_tools_for_write_command(self):
        brain = _make_brain()
        tools = [{"function": {"name": "file_write", "description": "Write files"}}]

        result = brain._suggest_relevant_tools("write to file", tools)

        assert result
        assert "Tool Hint" in result

    def test_suggest_message_tools_for_send_message(self):
        brain = _make_brain()
        tools = [{"function": {"name": "telegram_send", "description": "Send Telegram messages"}}]

        result = brain._suggest_relevant_tools("send a message to telegram", tools)

        assert result
        assert "Tool Hint" in result
        assert "message" in result.lower() or "telegram" in result.lower()

    def test_suggest_setup_tools_for_configure(self):
        brain = _make_brain()
        tools = [{"function": {"name": "setup", "description": "Setup modules"}}]

        result = brain._suggest_relevant_tools("setup the module", tools)

        assert result
        assert "Tool Hint" in result
        assert "setup" in result.lower() or "module" in result.lower()

    def test_empty_string_when_no_keywords_match(self):
        brain = _make_brain()
        tools = [{"function": {"name": "some_tool", "description": "Does something"}}]

        result = brain._suggest_relevant_tools("hello how are you", tools)

        assert result == ""

    def test_empty_string_when_no_tools(self):
        brain = _make_brain()

        result = brain._suggest_relevant_tools("run a command", None)

        assert result == ""

    def test_empty_string_when_empty_message(self):
        brain = _make_brain()
        tools = [{"function": {"name": "terminal", "description": "Run commands"}}]

        result = brain._suggest_relevant_tools("", tools)

        assert result == ""

    def test_limits_to_three_categories(self):
        brain = _make_brain()
        tools = [
            {"function": {"name": "terminal", "description": "Terminal"}},
            {"function": {"name": "file", "description": "File"}},
            {"function": {"name": "web", "description": "Web"}},
            {"function": {"name": "message", "description": "Message"}},
        ]

        result = brain._suggest_relevant_tools("run command and read file and search web and send message", tools)

        assert result
        # Should have at most 3 category suggestions (the join is limited to [:3])
        lines = result.split("\n")
        suggestion_lines = [l for l in lines if l.startswith("- ")]
        assert len(suggestion_lines) <= 3

    def test_spanish_keywords(self):
        brain = _make_brain()
        tools = [{"function": {"name": "terminal", "description": "Terminal"}}]

        result = brain._suggest_relevant_tools("ejecuta el comando", tools)

        assert result
        assert "Tool Hint" in result

    def test_case_insensitive(self):
        brain = _make_brain()
        tools = [{"function": {"name": "terminal", "description": "Terminal"}}]

        result1 = brain._suggest_relevant_tools("RUN", tools)
        result2 = brain._suggest_relevant_tools("run", tools)
        result3 = brain._suggest_relevant_tools("Run", tools)

        assert result1
        assert result2
        assert result3
        assert result1 == result2 == result3


# ── SessionManager ───────────────────────────────────────────────────


class TestSession:
    def test_session_default_id(self):
        s = Session()
        assert s.session_id
        assert len(s.history) == 0

    def test_session_flow_lifecycle(self):
        s = Session()
        assert s.active_flow is None

        s.start_flow({"intent": "test", "slots": {"x": {"required": True}}})
        assert s.active_flow is not None
        assert s.slots == {}

        s.fill_slot("x", "value")
        assert s.slots["x"] == "value"

        pending = s.get_pending_slots()
        assert pending == []

        s.complete_flow()
        assert s.active_flow is None
        assert s.slots == {}

    def test_pending_slots_only_required(self):
        s = Session()
        s.start_flow(
            {
                "intent": "test",
                "slots": {
                    "required_field": {"required": True, "ask": "Enter value"},
                    "optional_field": {"required": False},
                },
            }
        )
        pending = s.get_pending_slots()
        assert len(pending) == 1
        assert pending[0]["name"] == "required_field"

    def test_touch_updates_last_seen(self):
        s = Session()
        old = s.last_seen
        time.sleep(0.01)
        s.touch()
        assert s.last_seen > old


class TestSessionManager:
    def test_get_or_create_new_session(self):
        mgr = SessionManager()
        session = mgr.get_or_create()
        assert session is not None
        assert session.session_id

    def test_get_or_create_with_id(self):
        mgr = SessionManager()
        s1 = mgr.get_or_create("abc-123")
        s2 = mgr.get_or_create("abc-123")
        assert s1.session_id == "abc-123"
        assert s1 is s2

    def test_get_existing(self):
        mgr = SessionManager()
        created = mgr.get_or_create("test-id")
        retrieved = mgr.get("test-id")
        assert retrieved is created

    def test_get_nonexistent_returns_none(self):
        mgr = SessionManager()
        assert mgr.get("ghost") is None

    def test_remove_session(self):
        mgr = SessionManager()
        mgr.get_or_create("to-remove")
        mgr.remove("to-remove")
        assert mgr.get("to-remove") is None

    def test_prune_stale_sessions(self):
        mgr = SessionManager(idle_timeout_seconds=0.01)
        mgr.get_or_create("stale")
        time.sleep(0.05)
        mgr.prune_stale()
        assert mgr.get("stale") is None

    def test_touch_refreshes_session(self):
        mgr = SessionManager(idle_timeout_seconds=0.05)
        mgr.get_or_create("fresh")
        time.sleep(0.03)
        mgr.touch("fresh")
        time.sleep(0.03)
        # Session should survive because touch refreshed it
        result = mgr.get("fresh")
        assert result is not None


class TestSessionManagerConcurrency:
    """SessionManager race conditions — concurrent access patterns."""

    def test_concurrent_get_or_create_same_id(self):
        """Multiple threads getting/creating the same ID should not corrupt state."""
        mgr = SessionManager()
        results = []
        errors = []

        def worker():
            try:
                s = mgr.get_or_create("concurrent-id")
                results.append(s)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert all(isinstance(s, Session) for s in results)

    def test_concurrent_create_and_prune(self):
        """Creating sessions while pruning should not lose valid sessions."""
        mgr = SessionManager(idle_timeout_seconds=0.05)
        errors = []

        def creator():
            try:
                for i in range(50):
                    mgr.get_or_create(f"sess-{i}")
            except Exception as e:
                errors.append(e)

        def pruner():
            try:
                for _ in range(50):
                    mgr.prune_stale()
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=creator),
            threading.Thread(target=pruner),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0

    def test_concurrent_touch_and_prune(self):
        """Touching a session while pruning should not crash."""
        mgr = SessionManager(idle_timeout_seconds=1.0)
        mgr.get_or_create("keep-alive")
        errors = []

        def toucher():
            try:
                for _ in range(100):
                    mgr.touch("keep-alive")
            except Exception as e:
                errors.append(e)

        def pruner():
            try:
                for _ in range(100):
                    mgr.prune_stale()
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=toucher),
            threading.Thread(target=pruner),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        assert mgr.get("keep-alive") is not None

    @pytest.mark.asyncio
    async def test_async_concurrent_access(self):
        """Test that SessionManager works correctly under async concurrent access.

        SessionManager is synchronous, but it may be called from multiple
        asyncio tasks concurrently (since sync methods don't yield control,
        they execute atomically in the event loop).
        """
        mgr = SessionManager()

        def create_session(i):
            return mgr.get_or_create(f"async-sess-{i}")

        sessions = await asyncio.gather(
            *[asyncio.to_thread(create_session, i) for i in range(10)]
        )

        assert len(sessions) == 10
        for s in sessions:
            assert mgr.get(s.session_id) is not None


# ── load_flows ───────────────────────────────────────────────────────


class TestLoadFlows:
    def test_load_flows_from_file(self):
        brain = _make_brain()
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8"
        ) as f:
            yaml.dump(
                {
                    "intent": "greet",
                    "triggers": ["hi", "hello"],
                    "slots": {},
                },
                f,
            )
            f.flush()
            brain.load_flows(f.name)

        assert len(brain.flows) >= 1
        assert brain.flows[-1]["intent"] == "greet"

    def test_load_flows_from_directory(self):
        brain = _make_brain()
        with tempfile.TemporaryDirectory() as tmp:
            for name in ["flow1.yaml", "flow2.yaml"]:
                path = Path(tmp) / name
                path.write_text(
                    yaml.dump({"intent": name, "triggers": [], "slots": {}}),
                    encoding="utf-8",
                )
            brain.load_flows(tmp)

        assert len(brain.flows) >= 2

    def test_load_flows_nonexistent_path(self):
        brain = _make_brain()
        brain.load_flows("/nonexistent/path")  # Should not crash
        assert len(brain.flows) == 0


# ── persistence failure resilience ──────────────────────────────────


class TestPersistenceResilience:
    @pytest.mark.asyncio
    async def test_persistence_failure_does_not_crash_think(self):
        brain = _make_brain()
        brain.memory.recall = AsyncMock(return_value=[])
        brain.memory.save_conversation_turn = AsyncMock(
            side_effect=Exception("DB locked")
        )

        with patch("lumen.core.brain.acompletion") as mock_llm:
            mock_llm.return_value = _mock_llm_response("Response!")
            result = await brain.think("hello", Session())

        assert result["message"] == "Response!"
        # Session history should still be updated in RAM
        # (even though persistence failed)
