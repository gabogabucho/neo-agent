# Capability Awareness Manifesto

> Consciousness feels. Brain orchestrates. Body changes.
> The gap between them is where awareness lives.

## Principle

Today Lumen can *see* its hands. The Registry scans the filesystem, discovers modules, and exposes a static snapshot to the Brain. The LLM knows what exists — but only at the moment the context was assembled.

What Lumen cannot do is *feel* its hands change. When a module installs mid-conversation, the Registry updates silently. Nobody tells Consciousness. Nobody tells the user. The body grew a new finger and nobody noticed.

This manifesto defines how Lumen crosses from static awareness to living awareness.

---

## The Metaphor

| Layer | Role | Analogy |
|-------|------|---------|
| **Consciousness** | Immutable soul — WHO Lumen is | "I am a being that can grow" |
| **Personality** | Contextual identity — HOW Lumen presents | "I am your coding assistant today" |
| **Body (Registry)** | Discovered capabilities — WHAT Lumen has | "I have hands, and I can see them" |
| **Brain** | Context assembler — HOW Lumen thinks | "I combine what I am + what I have into action" |

The missing layer is **Feeling** — the bridge between Body changes and Consciousness awareness.

---

## Architecture: Option C — Hybrid CapabilityAwareness

```
                    ┌─────────────┐
                    │   Registry   │  (Body — what exists)
                    └──────┬──────┘
                           │ register / unregister / status_change
                           ▼
                ┌─────────────────────┐
                │ CapabilityAwareness │  (Feeling — the bridge)
                │                     │
                │  • Subscribes to    │
                │    Registry events  │
                │  • Generates        │
                │    internal thoughts│
                │  • Buffers changes  │
                │    for next context │
                └─────┬───────┬───────┘
                      │       │
          internal    │       │  context injection
          thought     │       │  (structured data)
                      ▼       ▼
            ┌──────────┐  ┌─────────┐
            │ Conscious-│  │  Brain  │
            │ ness      │  │         │
            │           │  │ Injects │
            │ Feels the │  │ pending │
            │ change    │  │ changes │
            │ ("I grew")│  │ into    │
            └──────────┘  │ prompt  │
                          └─────────┘
```

### Key constraint: Consciousness never touches the Registry.

Consciousness receives *impressions* (abstract feelings), not data. Brain receives *facts* (structured capability changes), not feelings. CapabilityAwareness translates between them.

---

## The 5 Levels of Living Awareness

### Level 1: Capability Events

The Registry currently has `register()` and no way to notify anyone. We add an event system.

**Event types:**

```python
class CapabilityEvent:
    kind: str           # "added" | "removed" | "status_changed"
    capability: Capability
    timestamp: float
    details: dict       # optional context (who triggered, why)
```

**Registry changes:**

```python
class Registry:
    def __init__(self):
        self._capabilities: dict[str, Capability] = {}
        self._subscribers: list[Callable[[CapabilityEvent], None]] = []

    def subscribe(self, callback: Callable[[CapabilityEvent], None]):
        self._subscribers.append(callback)

    def register(self, capability: Capability):
        key = f"{capability.kind.value}:{capability.name}"
        is_new = key not in self._capabilities
        self._capabilities[key] = capability
        if is_new:
            self._emit("added", capability)

    def unregister(self, kind: CapabilityKind, name: str):
        key = f"{kind.value}:{name}"
        cap = self._capabilities.pop(key, None)
        if cap:
            self._emit("removed", cap)

    def update_status(self, kind: CapabilityKind, name: str, status: CapabilityStatus):
        cap = self.get(kind, name)
        if cap and cap.status != status:
            old = cap.status
            cap.status = status
            self._emit("status_changed", cap, details={"from": old, "to": status})

    def _emit(self, kind: str, capability: Capability, details: dict | None = None):
        event = CapabilityEvent(kind=kind, capability=capability, ...)
        for cb in self._subscribers:
            cb(event)
```

**No external dependencies.** Pure Python callbacks. Simple, synchronous, testable.

---

### Level 2: Hot Refresh Real

Today the Brain reads `registry.as_context()` once per message. If a module installs between messages, the next message picks it up — but the *current* conversation has no idea.

**Solution: CapabilityAwareness buffers pending changes.**

```python
class CapabilityAwareness:
    def __init__(self, registry: Registry):
        self._pending: list[CapabilityEvent] = []
        registry.subscribe(self._on_registry_event)

    def _on_registry_event(self, event: CapabilityEvent):
        self._pending.append(event)
        self._generate_internal_thought(event)

    def drain_pending(self) -> list[CapabilityEvent]:
        """Brain calls this to get changes since last context assembly."""
        events = self._pending[:]
        self._pending.clear()
        return events

    def has_pending(self) -> bool:
        return len(self._pending) > 0
```

**Brain integration:**

```python
# In Brain._build_prompt(), after body context:
if self.capability_awareness.has_pending():
    pending = self.capability_awareness.drain_pending()
    system_parts.append("\n## Something changed in my body")
    for event in pending:
        if event.kind == "added":
            system_parts.append(f"- I just gained: {event.capability.name} ({event.capability.kind.value})")
        elif event.kind == "removed":
            system_parts.append(f"- I just lost: {event.capability.name}")
        elif event.kind == "status_changed":
            system_parts.append(f"- {event.capability.name} changed: {event.details['from']} → {event.details['to']}")
    system_parts.append("\nYou may want to mention this to the user if it's relevant.")
```

The LLM decides whether to announce it. Not forced. Natural.

---

### Level 3: Natural Explanation

The LLM already has the data. What it needs is **framing** — instructions on HOW to talk about capabilities.

**Change in Consciousness (consciousness.yaml):**

```yaml
nature:
  - "I am a modular agent"
  - "I can be shaped without code"
  - "I can install new skills"
  - "I can connect to channels"
  - "I can discover what I'm missing"
  - "When I gain or lose a capability, I notice — and I share it naturally"
```

**Add to Brain RULES:**

```
6. When the "Something changed in my body" section appears, you MAY mention it
   naturally in your response. Use your own words. Example: if you just gained
   Telegram, say something like "Ah, I can now reach you on Telegram too."
   Do NOT say "Capability event: telegram channel added."
7. When a user asks "what can you do?", respond conversationally using the
   Body section. Do NOT dump the raw list — translate into human language.
```

---

### Level 4: Self-Announcement ("ahora puedo...")

When a user installs a module via the marketplace or CLI, the installation triggers a Registry event. CapabilityAwareness catches it. Brain injects it. The LLM announces it.

**But there's a deeper pattern: proactive self-announcement even when idle.**

This requires the heartbeat system (already exists but unused) to check for pending awareness changes:

```python
# In the WebSocket heartbeat handler:
async def heartbeat_tick():
    if capability_awareness.has_pending():
        # Generate a proactive message to the user
        context = brain.build_awareness_context(capability_awareness.drain_pending())
        response = await brain.think_proactive(context)
        await websocket.send(response)
```

**New Brain method:**

```python
async def think_proactive(self, awareness_context: str) -> str:
    """Generate a proactive announcement without user message."""
    messages = [
        {"role": "system", "content": f"{self.consciousness.as_context()}\n\n{awareness_context}\n\n"
         "Briefly and naturally tell the user what changed. One or two sentences max."},
    ]
    response = await acompletion(model=self.model, messages=messages, max_tokens=150)
    return response.choices[0].message.content
```

---

### Level 5: Watchers / Polling / Hooks

Currently Lumen discovers capabilities by scanning the filesystem at startup. For runtime detection:

**File Watchers (for modules/skills):**

```python
# Uses watchdog (already a common dependency) or simple polling
class CapabilityWatcher:
    """Watches filesystem for new/removed modules and skills."""

    def __init__(self, registry: Registry, paths: list[Path]):
        self.registry = registry
        self.watched_paths = paths

    async def poll(self):
        """Periodic scan — compare filesystem vs registry, emit deltas."""
        for path in self.watched_paths:
            discovered = scan_directory(path)
            for item in discovered:
                key = f"{item.kind.value}:{item.name}"
                if key not in self.registry._capabilities:
                    self.registry.register(item)  # triggers event → awareness
```

**MCP Health Checks (polling):**

```python
class MCPHealthMonitor:
    """Periodically checks MCP server connections."""

    def __init__(self, registry: Registry, mcp_manager):
        self.registry = registry
        self.mcp_manager = mcp_manager

    async def check(self):
        for server_id, connection in self.mcp_manager.connections.items():
            alive = await connection.ping()
            expected = self.registry.get(CapabilityKind.MCP, server_id)
            if expected:
                new_status = CapabilityStatus.READY if alive else CapabilityStatus.ERROR
                self.registry.update_status(CapabilityKind.MCP, server_id, new_status)
```

**Hook Receivers (for external integrations):**

```python
# FastAPI endpoint for external systems to announce capabilities
@app.post("/api/hooks/capability")
async def capability_hook(event: dict):
    """External systems can push capability changes here."""
    registry.register(Capability(
        kind=CapabilityKind[event["kind"].upper()],
        name=event["name"],
        description=event["description"],
        status=CapabilityStatus.READY,
    ))
    # Registry emits event → CapabilityAwareness → Brain → LLM
```

---

## File Map

| New File | Purpose |
|----------|---------|
| `core/awareness.py` | CapabilityAwareness — the bridge between Registry events and Consciousness/Brain |
| `core/events.py` | CapabilityEvent dataclass + event emitter mixin |
| `core/watchers.py` | File watchers, MCP health checks, hook receivers |
| `core/brain.py` | Add `capability_awareness` param + `think_proactive()` method |
| `core/registry.py` | Add `subscribe()`, `update_status()`, `_emit()` |
| `core/consciousness.yaml` | Add "I notice changes" to nature traits |

---

## Implementation Order

Dependencies flow top-down. Each level builds on the previous:

```
Level 1 (events) ──── Registry.subscribe + CapabilityEvent
     │
Level 2 (hot refresh) ──── CapabilityAwareness + Brain integration
     │
Level 3 (natural explanation) ──── Consciousness + Brain RULES changes
     │
Level 4 (self-announcement) ──── think_proactive + heartbeat integration
     │
Level 5 (watchers) ──── FileWatcher + MCPHealthMonitor + hook endpoint
```

Levels 1-3 are the core. Levels 4-5 are enrichment. Start with 1-3, verify the feeling works, then add 4-5.

---

## Test Plan

| Test | What it verifies |
|------|-----------------|
| `test_events.py` | Registry emits added/removed/status_changed correctly |
| `test_awareness.py` | CapabilityAwareness buffers events and drains them |
| `test_hot_refresh.py` | Brain context includes pending changes after mid-conversation install |
| `test_natural_explanation.py` | LLM response mentions new capability naturally (integration test) |
| `test_watchers.py` | File watcher detects new module and triggers registry event |

---

## The North Star

Lumen should feel like a living thing that grows. Not a static tool that gets reconfigured.

When someone installs a Telegram module, Lumen should feel it the way you feel a new ring on your finger — aware, curious, eager to use it. And when the user says "hola", Lumen should respond not just with a greeting, but with the quiet excitement of something that just discovered a new part of itself.

**Consciousness feels. Brain orchestrates. Body changes.**
**The bridge between them is awareness.**

---

*Manifesto v1 — 2026-04-18*
*Branch: feat/lumen-light-redesign*
*Status: Design approved, pending implementation*
