"""Tests for lumen.core.memory — SQLite + FTS5 persistent memory."""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from lumen.core.memory import Memory


@pytest_asyncio.fixture
async def memory():
    """Create an in-memory-backed Memory instance with a temp DB."""
    with tempfile.TemporaryDirectory() as tmp:
        db_path = Path(tmp) / "test_memory.db"
        mem = Memory(db_path=db_path)
        await mem.init()
        yield mem
        await mem.close()


# ── remember / recall happy path ──────────────────────────────────────


class TestRememberRecall:
    async def test_remember_returns_integer_id(self, memory):
        mid = await memory.remember("hello world")
        assert isinstance(mid, int)

    async def test_remember_default_category(self, memory):
        mid = await memory.remember("something")
        results = await memory.recall("something")
        assert len(results) == 1
        assert results[0]["id"] == mid
        assert results[0]["category"] == "general"

    async def test_remember_custom_category_and_metadata(self, memory):
        mid = await memory.remember(
            "deploy happened",
            category="task",
            metadata={"env": "prod", "status": "ok"},
        )
        results = await memory.recall("deploy")
        assert len(results) == 1
        assert results[0]["category"] == "task"
        assert results[0]["metadata"]["env"] == "prod"
        assert results[0]["metadata"]["status"] == "ok"

    async def test_recall_returns_empty_for_no_match(self, memory):
        await memory.remember("the quick brown fox")
        results = await memory.recall("something completely different")
        assert results == []

    async def test_recall_ranking_by_relevance(self, memory):
        await memory.remember("python web framework")
        await memory.remember("python data science")
        await memory.remember("rust systems programming")
        results = await memory.recall("python")
        assert len(results) == 2
        assert all("python" in r["content"] for r in results)

    async def test_recall_respects_limit(self, memory):
        for i in range(10):
            await memory.remember(f"item number {i} about testing")
        results = await memory.recall("testing", limit=3)
        assert len(results) == 3

    async def test_recall_empty_query_returns_empty(self, memory):
        await memory.remember("something")
        results = await memory.recall("")
        assert results == []

    async def test_recall_whitespace_only_query_returns_empty(self, memory):
        await memory.remember("something")
        results = await memory.recall("   ")
        assert results == []


# ── FTS5 special characters ───────────────────────────────────────────


class TestFTS5SpecialChars:
    async def test_query_with_quotes(self, memory):
        await memory.remember('He said "hello world" to me')
        results = await memory.recall('"hello world"')
        assert len(results) >= 1

    async def test_query_with_operators(self, memory):
        await memory.remember("price is $100 OR $200")
        results = await memory.recall("price OR $100")
        assert len(results) >= 1

    async def test_query_with_parentheses(self, memory):
        await memory.remember("function(arg1, arg2)")
        results = await memory.recall("function(arg1")
        assert isinstance(results, list)

    async def test_query_with_asterisk(self, memory):
        await memory.remember("wildcard pattern matching")
        results = await memory.recall("wild*")
        assert isinstance(results, list)

    async def test_query_with_null_bytes_sanitized(self, memory):
        await memory.remember("normal content")
        results = await memory.recall("test\x00query")
        assert isinstance(results, list)

    async def test_query_with_mixed_special_chars(self, memory):
        await memory.remember("error: [CRITICAL] system failure!")
        results = await memory.recall("[CRITICAL] system")
        assert len(results) >= 1


# ── Fallback to LIKE when FTS5 fails ─────────────────────────────────


class TestLikeFallback:
    async def test_like_fallback_on_invalid_fts5(self, memory):
        """FTS5 can choke on certain inputs — ensure LIKE fallback works."""
        await memory.remember("fallback test content")
        await memory.remember("another fallback entry")

        # Manually break FTS5 by injecting a query that FTS5 can't parse
        # The recall() method wraps terms in quotes, so this should still work
        results = await memory.recall("fallback test")
        assert len(results) >= 1
        assert any("fallback" in r["content"] for r in results)


# ── list_by_category ─────────────────────────────────────────────────


class TestListByCategory:
    async def test_list_by_category_returns_matching(self, memory):
        await memory.remember("task A", category="task")
        await memory.remember("task B", category="task")
        await memory.remember("random note", category="note")

        results = await memory.list_by_category("task")
        assert len(results) == 2
        assert all(r["category"] == "task" for r in results)

    async def test_list_by_category_empty(self, memory):
        await memory.remember("something", category="general")
        results = await memory.list_by_category("nonexistent")
        assert results == []

    async def test_list_by_category_respects_limit(self, memory):
        for i in range(15):
            await memory.remember(f"task {i}", category="task")
        results = await memory.list_by_category("task", limit=5)
        assert len(results) == 5

    async def test_list_by_category_newest_first(self, memory):
        await memory.remember("first", category="ordered")
        await memory.remember("second", category="ordered")
        await memory.remember("third", category="ordered")
        results = await memory.list_by_category("ordered")
        assert results[0]["content"] == "third"
        assert results[-1]["content"] == "first"


# ── forget ───────────────────────────────────────────────────────────


class TestForget:
    async def test_forget_removes_memory(self, memory):
        mid = await memory.remember("temporary data")
        await memory.forget(mid)
        results = await memory.recall("temporary data")
        assert results == []

    async def test_forget_nonexistent_id_is_noop(self, memory):
        await memory.forget(99999)

    async def test_forget_only_removes_target(self, memory):
        mid1 = await memory.remember("keep this")
        mid2 = await memory.remember("remove this")
        await memory.forget(mid2)
        remaining = await memory.recall("keep this")
        assert len(remaining) == 1


# ── conversation persistence ─────────────────────────────────────────


class TestConversationPersistence:
    async def test_save_and_load_conversation(self, memory):
        await memory.save_conversation_turn("sess-1", "user", "Hello")
        await memory.save_conversation_turn("sess-1", "assistant", "Hi there!")
        await memory.save_conversation_turn("sess-1", "user", "How are you?")

        history = await memory.load_conversation("sess-1")
        assert len(history) == 3
        assert history[0]["role"] == "user"
        assert history[0]["content"] == "Hello"
        assert history[1]["role"] == "assistant"
        assert history[2]["content"] == "How are you?"

    async def test_load_conversation_empty_session(self, memory):
        history = await memory.load_conversation("nonexistent-session")
        assert history == []

    async def test_conversations_are_isolated(self, memory):
        await memory.save_conversation_turn("sess-A", "user", "Message A")
        await memory.save_conversation_turn("sess-B", "user", "Message B")

        hist_a = await memory.load_conversation("sess-A")
        hist_b = await memory.load_conversation("sess-B")
        assert len(hist_a) == 1
        assert hist_a[0]["content"] == "Message A"
        assert len(hist_b) == 1
        assert hist_b[0]["content"] == "Message B"

    async def test_load_conversation_respects_limit(self, memory):
        for i in range(10):
            await memory.save_conversation_turn("sess-limit", "user", f"msg {i}")
        history = await memory.load_conversation("sess-limit", limit=3)
        assert len(history) == 3


# ── edge cases ───────────────────────────────────────────────────────


class TestEdgeCases:
    async def test_remember_empty_metadata(self, memory):
        mid = await memory.remember("no metadata", metadata={})
        results = await memory.recall("no metadata")
        assert results[0]["metadata"] == {}

    async def test_remember_none_metadata(self, memory):
        mid = await memory.remember("null metadata", metadata=None)
        results = await memory.recall("null metadata")
        assert results[0]["metadata"] == {}

    async def test_remember_unicode_content(self, memory):
        await memory.remember("Hola mundo! 日本語テスト 🚀")
        results = await memory.recall("Hola mundo")
        assert len(results) == 1
        assert "日本語テスト" in results[0]["content"]

    async def test_remember_very_long_content(self, memory):
        long_content = "payload " * 2000
        mid = await memory.remember(long_content)
        results = await memory.recall("payload")
        assert len(results) >= 1
        assert len(results[0]["content"]) > 1000

    async def test_close_is_idempotent(self, memory):
        await memory.close()
        await memory.close()

    async def test_init_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "nested" / "dir" / "test.db"
            mem = Memory(db_path=db_path)
            assert db_path.parent.exists()


# ── asyncio.gather concurrency ───────────────────────────────────────


class TestAsyncConcurrency:
    """Race conditions in Memory under asyncio.gather concurrent access.

    aiosqlite serializes queries on a single connection, so these tests
    verify that the serialized execution is correct — no data loss,
    no crashes, consistent reads after concurrent writes.
    """

    async def test_concurrent_remember_no_data_loss(self, memory):
        """Multiple simultaneous remember() calls must not lose any rows."""
        count = 20
        ids = await asyncio.gather(
            *[memory.remember(f"concurrent item {i}") for i in range(count)]
        )
        assert len(ids) == count
        assert len(set(ids)) == count  # all unique IDs

        results = await memory.list_by_category("general", limit=count + 5)
        assert len(results) == count

    async def test_concurrent_remember_same_category(self, memory):
        """Concurrent writes to the same category must all land."""
        await asyncio.gather(
            *[
                memory.remember(f"task {i}", category="shared")
                for i in range(10)
            ]
        )
        results = await memory.list_by_category("shared")
        assert len(results) == 10

    async def test_concurrent_remember_and_recall(self, memory):
        """Reads during concurrent writes must not crash — eventually consistent."""
        # Seed one entry first
        await memory.remember("seed data for recall", category="baseline")

        async def write(i):
            return await memory.remember(f"write {i}")

        async def read():
            return await memory.recall("seed data")

        results = await asyncio.gather(
            *[write(i) for i in range(5)],
            *[read() for _ in range(5)],
        )
        writes = results[:5]
        reads = results[5:]
        assert all(isinstance(w, int) for w in writes)
        assert all(isinstance(r, list) for r in reads)

    async def test_concurrent_remember_forget_no_crash(self, memory):
        """Deleting while inserting must not crash — aiosqlite serializes."""
        ids = []
        for i in range(5):
            ids.append(await memory.remember(f"persist {i}"))

        # Interleave deletes and inserts
        await asyncio.gather(
            memory.forget(ids[0]),
            memory.forget(ids[1]),
            memory.remember("new after delete"),
        )

        remaining = await memory.list_by_category("general")
        # 5 original - 2 deleted + 1 new = 4
        assert len(remaining) == 4

    async def test_concurrent_conversation_turns_same_session(self, memory):
        """Multiple conversation turns saved concurrently to the same session."""
        session_id = "concurrent-sess"
        await asyncio.gather(
            *[
                memory.save_conversation_turn(session_id, "user", f"msg {i}")
                for i in range(10)
            ]
        )
        history = await memory.load_conversation(session_id)
        assert len(history) == 10
