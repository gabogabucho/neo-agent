"""Capability Watchers — Lumen's senses for runtime changes.

Level 5 of the Capability Awareness system.

Three mechanisms:
  1. FilePoller — scans directories periodically, detects new/removed modules and skills
  2. MCPHealthMonitor — pings MCP servers and updates their status
  3. Hook receiver — external systems can POST capability changes

All three feed into the Registry, which emits events,
which CapabilityAwareness translates into feelings.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Callable, Coroutine

from lumen.core.registry import (
    Capability,
    CapabilityKind,
    CapabilityStatus,
    Registry,
)

logger = logging.getLogger(__name__)


class FilePoller:
    """Periodically scans directories for new/removed capabilities.

    Compares filesystem state against the Registry and triggers a
    refresh callback when changes are detected.

    Usage:
        poller = FilePoller(registry, paths=[modules_dir], on_change=refresh_fn)
        await poller.start(interval=120)
    """

    def __init__(
        self,
        registry: Registry,
        paths: list[Path],
        on_change: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ):
        self.registry = registry
        self.paths = paths
        self.on_change = on_change
        self._snapshot: dict[str, float] = {}
        self._task: asyncio.Task | None = None

    async def start(self, interval: float = 60):
        """Start polling in the background."""
        self._snapshot = self._take_snapshot()
        self._task = asyncio.create_task(self._poll_loop(interval))

    async def stop(self):
        """Stop the polling task."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _poll_loop(self, interval: float):
        """Main polling loop."""
        while True:
            try:
                await asyncio.sleep(interval)
                has_changes = self.check()
                if has_changes and self.on_change:
                    await self.on_change()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("FilePoller error")

    def check(self) -> bool:
        """Compare filesystem against snapshot. Returns True if changes detected."""
        current = self._take_snapshot()
        changed = False

        # New or modified files
        for key, mtime in current.items():
            if key not in self._snapshot or self._snapshot[key] != mtime:
                changed = True
                logger.debug("FilePoller detected change: %s", key)

        # Removed files
        for key in list(self._snapshot.keys()):
            if key not in current:
                changed = True
                logger.debug("FilePoller detected removal: %s", key)

        self._snapshot = current
        return changed

    def _take_snapshot(self) -> dict[str, float]:
        """Build a map of file_path → mtime for all watched directories."""
        snapshot = {}
        for base_path in self.paths:
            if not base_path.exists():
                continue
            for f in base_path.rglob("*"):
                if f.is_file() and not f.name.startswith("."):
                    try:
                        snapshot[str(f)] = f.stat().st_mtime
                    except OSError:
                        pass
        return snapshot


class MCPHealthMonitor:
    """Periodically checks MCP server connections and updates registry status.

    When a server goes down, the registry status changes to ERROR.
    When it comes back, status changes to READY.
    Both trigger capability events → awareness → Lumen feels it.
    """

    def __init__(self, registry: Registry, mcp_manager=None):
        self.registry = registry
        self.mcp_manager = mcp_manager
        self._task: asyncio.Task | None = None

    async def start(self, interval: float = 30):
        """Start health monitoring in the background."""
        self._task = asyncio.create_task(self._check_loop(interval))

    async def stop(self):
        """Stop the health monitor."""
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _check_loop(self, interval: float):
        """Main monitoring loop."""
        while True:
            try:
                await asyncio.sleep(interval)
                await self.check()
            except asyncio.CancelledError:
                break
            except Exception:
                logger.exception("MCPHealthMonitor error")

    async def check(self):
        """Ping all known MCP servers and update statuses."""
        if not self.mcp_manager:
            return

        connections = getattr(self.mcp_manager, "connections", {})
        for server_id, connection in connections.items():
            try:
                alive = await self._ping(connection)
                new_status = CapabilityStatus.READY if alive else CapabilityStatus.ERROR
                self.registry.update_status(
                    CapabilityKind.MCP, server_id, new_status
                )
            except Exception:
                self.registry.update_status(
                    CapabilityKind.MCP, server_id, CapabilityStatus.ERROR
                )

    async def _ping(self, connection) -> bool:
        """Ping an MCP server connection. Override for custom health checks."""
        # Default: try to list tools as a health check
        try:
            if hasattr(connection, "list_tools"):
                await asyncio.wait_for(connection.list_tools(), timeout=5)
                return True
        except Exception:
            pass
        return False
