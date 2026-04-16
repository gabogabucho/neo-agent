"""Persistent memory — SQLite with FTS5 full-text search."""

import json
import time
from pathlib import Path

import aiosqlite


class Memory:
    """Lumen's persistent memory. Stores and recalls information using SQLite + FTS5.

    Used for: task tracking, notes, conversation facts, anything Lumen should
    remember across sessions.
    """

    def __init__(self, db_path: str | Path = "data/memory.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db: aiosqlite.Connection | None = None

    async def init(self):
        """Initialize database and create tables."""
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                metadata TEXT DEFAULT '{}',
                created_at REAL NOT NULL
            );

            CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
                content,
                content=memories,
                content_rowid=id
            );

            CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories
            BEGIN
                INSERT INTO memories_fts(rowid, content)
                VALUES (new.id, new.content);
            END;

            CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories
            BEGIN
                INSERT INTO memories_fts(memories_fts, rowid, content)
                VALUES ('delete', old.id, old.content);
            END;
            """
        )
        await self._db.commit()

    async def remember(
        self,
        content: str,
        category: str = "general",
        metadata: dict | None = None,
    ) -> int:
        """Store something in memory. Returns the memory ID."""
        cursor = await self._db.execute(
            "INSERT INTO memories (content, category, metadata, created_at) "
            "VALUES (?, ?, ?, ?)",
            (content, category, json.dumps(metadata or {}), time.time()),
        )
        await self._db.commit()
        return cursor.lastrowid

    async def recall(self, query: str, limit: int = 5) -> list[dict]:
        """Search memory using FTS5. Returns matching memories ranked by relevance."""
        safe_query = " ".join(f'"{term}"' for term in query.split() if term.strip())
        if not safe_query:
            return []

        try:
            rows = await self._db.execute_fetchall(
                """
                SELECT m.id, m.content, m.category, m.metadata, m.created_at
                FROM memories_fts f
                JOIN memories m ON f.rowid = m.id
                WHERE memories_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (safe_query, limit),
            )
        except Exception:
            # Fallback to LIKE search if FTS fails
            rows = await self._db.execute_fetchall(
                """
                SELECT id, content, category, metadata, created_at
                FROM memories
                WHERE content LIKE ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (f"%{query}%", limit),
            )

        return [
            {
                "id": row[0],
                "content": row[1],
                "category": row[2],
                "metadata": json.loads(row[3]),
                "created_at": row[4],
            }
            for row in rows
        ]

    async def list_by_category(
        self, category: str, limit: int = 20
    ) -> list[dict]:
        """List memories in a category, newest first."""
        rows = await self._db.execute_fetchall(
            "SELECT id, content, category, metadata, created_at "
            "FROM memories WHERE category = ? ORDER BY created_at DESC LIMIT ?",
            (category, limit),
        )
        return [
            {
                "id": row[0],
                "content": row[1],
                "category": row[2],
                "metadata": json.loads(row[3]),
                "created_at": row[4],
            }
            for row in rows
        ]

    async def forget(self, memory_id: int):
        """Delete a memory by ID."""
        await self._db.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
        await self._db.commit()

    async def close(self):
        """Close the database connection."""
        if self._db:
            await self._db.close()
