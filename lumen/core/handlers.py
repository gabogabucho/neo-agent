"""Built-in connector handlers — the actual implementations.

Without these, connectors are empty plugs. These handlers wire them to
real actions: saving tasks to memory, listing notes, searching, etc.
"""

import asyncio
import json
import shlex
import time
from pathlib import Path
from typing import Any

from lumen.core.connectors import ConnectorRegistry
from lumen.core.memory import Memory


# --- Output limits ---

_MAX_OUTPUT_BYTES = 10 * 1024  # 10KB per stream (stdout/stderr)


# --- Tool schemas for LLM function calling ---
# These replace the generic "input: string" with typed parameters.

TOOL_SCHEMAS: dict[str, dict] = {
    "task__create": {
        "description": "Create a new task for the user",
        "parameters": {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What needs to be done",
                },
                "due_date": {
                    "type": "string",
                    "description": "When it is due (e.g. 'tomorrow', '2025-03-15')",
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Priority level",
                },
            },
            "required": ["description"],
        },
    },
    "task__list": {
        "description": "List all pending tasks",
        "parameters": {"type": "object", "properties": {}},
    },
    "task__complete": {
        "description": "Mark a task as completed",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "ID of the task to complete",
                },
            },
            "required": ["task_id"],
        },
    },
    "task__delete": {
        "description": "Delete a task",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "ID of the task to delete",
                },
            },
            "required": ["task_id"],
        },
    },
    "note__create": {
        "description": "Save a note for the user",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "The note content to save",
                },
                "tag": {
                    "type": "string",
                    "description": "Optional tag/category for the note",
                },
            },
            "required": ["content"],
        },
    },
    "note__list": {
        "description": "List all saved notes",
        "parameters": {"type": "object", "properties": {}},
    },
    "note__search": {
        "description": "Search through saved notes",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
            },
            "required": ["query"],
        },
    },
    "note__delete": {
        "description": "Delete a note by ID",
        "parameters": {
            "type": "object",
            "properties": {
                "note_id": {
                    "type": "integer",
                    "description": "ID of the note to delete",
                },
            },
            "required": ["note_id"],
        },
    },
    "memory__write": {
        "description": "Remember something important for later",
        "parameters": {
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "What to remember",
                },
                "category": {
                    "type": "string",
                    "description": "Category (e.g. 'fact', 'preference', 'context')",
                },
            },
            "required": ["content"],
        },
    },
    "memory__read": {
        "description": "List recent memories",
        "parameters": {"type": "object", "properties": {}},
    },
    "memory__search": {
        "description": "Search through memories",
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
            },
            "required": ["query"],
        },
    },
    "terminal__execute": {
        "description": (
            "Execute a shell command on the host system. "
            "Commands must be in the configured allowlist. "
            "Returns stdout, stderr, and exit code separately."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "The command to execute (e.g. 'echo hello')",
                },
                "timeout": {
                    "type": "integer",
                    "description": "Timeout in seconds (default 30)",
                },
                "cwd": {
                    "type": "string",
                    "description": "Working directory for the command",
                },
            },
            "required": ["command"],
        },
    },
}


def _check_command_allowed(base_command: str, config: dict) -> bool:
    """Check if a command is allowed by the terminal security policy.

    Default: deny-all. Commands are only allowed if:
    1. terminal.allowlist is configured AND
    2. base_command is in the allowlist AND
    3. base_command is NOT in the denylist
    """
    terminal_config = config.get("terminal", {})
    if isinstance(terminal_config, str):
        return False

    allowlist = terminal_config.get("allowlist", [])
    if not allowlist:
        return False

    if base_command not in allowlist:
        return False

    denylist = terminal_config.get("denylist", [])
    if base_command in denylist:
        return False

    return True


def _truncate_output(data: bytes, max_bytes: int = _MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """Truncate output bytes and return (decoded_text, was_truncated)."""
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace"), truncated


async def terminal_execute(
    command: str = "",
    timeout: int = 30,
    cwd: str = "",
    config: dict | None = None,
    **_: Any,
) -> dict:
    """Execute a shell command with security controls.

    Uses asyncio.create_subprocess_exec (NOT shell) for safety.
    Enforces allowlist/denylist, timeout, and output truncation.
    """
    if not command or not command.strip():
        return {"error": "command is required", "exit_code": -1}

    config = config or {}
    parts = shlex.split(command, posix=True)
    if not parts:
        return {"error": "command is required", "exit_code": -1}

    base_command = Path(parts[0]).name  # Extract base name (e.g. /usr/bin/git → git)

    if not _check_command_allowed(base_command, config):
        return {
            "error": "command_not_allowed",
            "command": base_command,
            "exit_code": -1,
        }

    try:
        proc = await asyncio.create_subprocess_exec(
            *parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd or None,
        )
    except FileNotFoundError:
        return {
            "error": "command_not_found",
            "command": base_command,
            "exit_code": 127,
        }
    except PermissionError:
        return {
            "error": "permission_denied",
            "command": base_command,
            "exit_code": 126,
        }

    try:
        stdout_bytes, stderr_bytes = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {
            "error": "timeout",
            "exit_code": -1,
            "command": base_command,
        }

    stdout_text, stdout_truncated = _truncate_output(stdout_bytes)
    stderr_text, stderr_truncated = _truncate_output(stderr_bytes)

    result: dict[str, Any] = {
        "stdout": stdout_text,
        "stderr": stderr_text,
        "exit_code": proc.returncode if proc.returncode is not None else -1,
    }
    if stdout_truncated or stderr_truncated:
        result["truncated"] = True
    return result


def register_builtin_handlers(registry: ConnectorRegistry, memory: Memory):
    """Register real handlers for all built-in connectors.

    This is what makes Lumen actually DO things instead of just talking.
    """

    # --- Task handlers ---
    task = registry.get("task")
    if task:

        async def task_create(
            description: str = "",
            due_date: str = "",
            priority: str = "medium",
            **_: Any,
        ) -> dict:
            metadata = {"due_date": due_date, "priority": priority, "status": "pending"}
            task_id = await memory.remember(
                description, category="task", metadata=metadata
            )
            return {
                "status": "ok",
                "task_id": task_id,
                "message": f"Task created: {description}",
            }

        async def task_list(**_: Any) -> dict:
            tasks = await memory.list_by_category("task", limit=20)
            pending = [
                t for t in tasks if t["metadata"].get("status") != "completed"
            ]
            return {"status": "ok", "tasks": pending, "count": len(pending)}

        async def task_complete(task_id: int = 0, **_: Any) -> dict:
            # Update metadata to mark as completed
            if memory._db:
                await memory._db.execute(
                    "UPDATE memories SET metadata = json_set(metadata, '$.status', 'completed') "
                    "WHERE id = ? AND category = 'task'",
                    (task_id,),
                )
                await memory._db.commit()
            return {"status": "ok", "message": f"Task {task_id} completed"}

        async def task_delete(task_id: int = 0, **_: Any) -> dict:
            await memory.forget(task_id)
            return {"status": "ok", "message": f"Task {task_id} deleted"}

        task.register_handler("create", task_create)
        task.register_handler("list", task_list)
        task.register_handler("complete", task_complete)
        task.register_handler("delete", task_delete)

    # --- Note handlers ---
    note = registry.get("note")
    if note:

        async def note_create(
            content: str = "", tag: str = "", **_: Any
        ) -> dict:
            metadata = {"tag": tag} if tag else {}
            note_id = await memory.remember(
                content, category="note", metadata=metadata
            )
            return {
                "status": "ok",
                "note_id": note_id,
                "message": f"Note saved",
            }

        async def note_list(**_: Any) -> dict:
            notes = await memory.list_by_category("note", limit=20)
            return {"status": "ok", "notes": notes, "count": len(notes)}

        async def note_search(query: str = "", **_: Any) -> dict:
            results = await memory.recall(query, limit=10)
            notes = [r for r in results if r["category"] == "note"]
            return {"status": "ok", "notes": notes, "count": len(notes)}

        async def note_delete(note_id: int = 0, **_: Any) -> dict:
            await memory.forget(note_id)
            return {"status": "ok", "message": f"Note {note_id} deleted"}

        note.register_handler("create", note_create)
        note.register_handler("list", note_list)
        note.register_handler("search", note_search)
        note.register_handler("delete", note_delete)

    # --- Memory handlers ---
    mem = registry.get("memory")
    if mem:

        async def memory_write(
            content: str = "", category: str = "general", **_: Any
        ) -> dict:
            mem_id = await memory.remember(content, category=category)
            return {"status": "ok", "memory_id": mem_id, "message": "Remembered"}

        async def memory_read(**_: Any) -> dict:
            recent = await memory.list_by_category("general", limit=10)
            return {"status": "ok", "memories": recent, "count": len(recent)}

        async def memory_search(query: str = "", **_: Any) -> dict:
            results = await memory.recall(query, limit=10)
            return {"status": "ok", "results": results, "count": len(results)}

        mem.register_handler("write", memory_write)
        mem.register_handler("read", memory_read)
        mem.register_handler("search", memory_search)

    # --- Terminal handler ---
    term = registry.get("terminal")
    if term:
        term.register_handler("execute", terminal_execute)

    # --- Apply tool schemas to registry ---
    registry.set_tool_schemas(TOOL_SCHEMAS)
