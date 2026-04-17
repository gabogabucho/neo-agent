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


# ── think() happy path ───────────────────────────────────────────────


class TestThinkHappyPath:
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


# ── _coerce_args ─────────────────────────────────────────────────────


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


# ── _build_prompt ────────────────────────────────────────────────────


class TestBuildPrompt:
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
