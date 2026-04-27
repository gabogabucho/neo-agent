# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.1] - 2026-04-27

### Fixed
- **Template error (`TemplateNotFound`) on `/settings/confirmations`**: `confirmations.html` was referencing `_partials/sidebar.html` which does not exist. Fixed by creating `_partials/settings_sidebar.html` and updating the template to use it.
- **`NameError: _get_brain` in `/api/outputs` and `/api/tools/confirmations`**: Two API endpoints were calling a non-existent `_get_brain()` function. Changed to use the global `_brain` variable directly, consistent with all other endpoints.

### Changed
- **Dashboard navigation refactor**: Sidebar reduced from 13 items (with duplicates and flat hierarchy) to 5 clean items: Charlas, Memoria, Módulos, Estado, Ajustes. Removed the inline `panel-config` from the dashboard — settings now live under a dedicated `/settings` page with its own sidebar navigation.
- **Unified settings sidebar**: New `_partials/settings_sidebar.html` Jinja partial shared across all `/settings/*` pages. Settings pages now have consistent navigation with a "Volver" link back to the dashboard.
- **Merged Providers into Models**: `/settings/providers` now redirects to `/settings/models`. The providers health table is displayed within the Models page alongside model routing configuration.
- **New `/settings/general` page**: Migrated the provider/model/API key/theme configuration from the dashboard's inline `panel-config` to a standalone settings page with a clean form layout.

## [1.1.0] - 2026-04-27

### Added
- **Confirmation Layer** (`confirmation_gate.py`): Runtime tool confirmation gate for `privileged` and `destructive` risk tools. When a tool requires confirmation, the brain yields a `tool_confirm_request` event (SSE/WebSocket) and blocks until the user approves, rejects, or a timeout expires (configurable, default 60s). Auto-approves when no handler is registered (backward compat). New `ConfirmDecision` enum: `approved`, `rejected`, `timeout`, `auto_approved`.
- **Tool Policy integration in Brain**: Brain now instantiates `ToolPolicy` with defaults + config, and calls `_check_tool_confirmation()` before executing every tool in both `_tool_use_loop` and `_tool_use_loop_streaming()`. Blocked tools report a friendly error to the LLM instead of silently failing.
- **Web channel confirmation handler** (`web.py`): SSE streams yield `tool_confirm_request` events via a shared queue. WebSocket clients receive confirm requests via `broadcast_event()`. New POST endpoint `/api/tools/{call_id}/confirm` resolves pending confirmations. New GET `/api/tools/confirmations` lists pending + history.
- **Confirmation UI page** (`/settings/confirmations`): New page showing pending confirmations and decision history with auto-refresh every 5s. Dark theme, sidebar nav, Spanish labels.
- **Structured Output persistence** (`memory.py`): New `outputs` SQLite table with CRUD methods (`save_output`, `get_outputs`, `count_outputs`, `delete_output`). Brain auto-persists non-trivial tool results (dicts/lists, not simple strings < 20 chars) after execution. `GET /api/outputs` now serves real data instead of placeholder empty list.
- **Lessons pre-loading on startup** (`web.py`): `_init_brain_from_config()` now calls `await _brain.load_lessons()` so lessons are injected into prompts in serve mode. `configure()` also sets the confirmation handler.

### Fixed
- **load_lessons() not called at startup in serve mode**: Lessons existed in DB but were never injected into prompts because `load_lessons()` was never called after brain initialization.
- **`/api/outputs` returning empty list**: Now returns persisted structured outputs from memory.db with filtering by `session_id` and `output_type`.

## [1.0.1] - 2026-04-27

### Fixed
- **Empty response when tool calls are invalid or DSML sanitized to empty**: When DeepSeek returns native tool calls with invalid names (not in schema), `_resolve_tool_calls` rejects them. If DSML content is also present, `_safe_extract_content` sanitizes it to empty. The recovery path (`_retry_final_response_without_tools` / `_summarize_tool_results`) was only activated when `all_tool_calls` was non-empty, but since invalid tools were never executed, the list stayed empty and recovery never fired. Fixed in all 3 recovery sites (inside loop + max_iterations exit, for both `_tool_use_loop` and `_tool_use_loop_streaming`) to prioritize `_summarize_tool_results` when tools were executed, use `partial_text` as fallback, and only attempt the retry-without-tools LLM call as last resort. This also covers the case where the retry itself returns DSML that gets sanitized to empty.

## [1.0.0] - 2026-04-27

### Added
- **Structured Output Types** (`output_types.py`): Type system for agent responses beyond plain text. 6 output types: text, document, notification, web, image, plot. `StructuredOutput` dataclass with factory methods (`text()`, `document()`, `notification()`, `web()`, `image()`, `plot()`), JSON serialization, and roundtrip support.
- **Channel Gateway** (extended `inbox.py`): Runtime channel status tracking with `ChannelStatus` dataclass. Tracks connected/disconnected/error state, message count, last activity, and internal vs external distinction. Unified `get_channel_status()` API across all channels.
- **1 new CLI command**: `lumen channels` — shows all registered channels (web internal + module-based) with status.
- **2 new API endpoints**: `GET /api/channels` (live channel status with WebSocket count), `GET /api/outputs` (structured output store placeholder + available types).
- **2 new UI pages**: Canales (`/settings/channels` — channel table with auto-refresh), Outputs (`/settings/outputs` — output type cards).

### Tests
- **28 new tests** (15 output_types + 13 inbox). 241 total new tests across all 4 phases.

## [0.10.0] - 2026-04-27

### Added
- **Tool Policy** (`tool_policy.py`): Risk classification for every tool/action (read_only, mutating, destructive, privileged). 19 default policies for built-in connectors. Config-driven overrides for risk level and confirmation requirements. Tools annotated with risk metadata via `ConnectorRegistry.as_tools()`.
- **Security Config**: Toggles for confirm_deletions, confirm_terminal, confirm_system_actions, auto_approve_read_only. Configurable confirmation timeout. Privileged tool list.
- **3 new CLI commands**: `lumen tools` (list with risk filter), `lumen security` (show settings), `lumen security-set` (update settings).
- **3 new API endpoints**: `GET /api/tools` (all policies), `GET /api/security`, `POST /api/security`.
- **2 new UI pages**: Tools (`/settings/tools` — risk table with filter), Seguridad (`/settings/security` — toggle switches + timeout). Sidebar navigation updated across all pages.

### Tests
- **24 new tests** for tool policy (defaults, confirmation logic, security config, overrides, summaries).

## [0.9.1] - 2026-04-27

### Fixed
- **Streaming tool use loop — final response lost**: `think_stream()` called blocking `_tool_use_loop()` inside an async generator. During multi-step tool execution (30-90s), no events reached SSE/WebSocket clients, causing silent connection drops and lost final responses. New `_tool_use_loop_streaming()` async generator yields 4 progress event types (`tool_progress`, `tool_result`, `tool_status`, `delta`) keeping the connection alive. SSE handler forwards all new event types.

## [0.9.0] - 2026-04-27

### Added
- **Session Distillation** (`distiller.py`): After sessions end, LLM extracts 5-15 durable facts from conversations. Facts stored in `session_facts` table with category, importance, and session source. Inspired by Aiden's session-end memory distillation.
- **Persistent Lessons** (`lessons.py`): Store of learned rules that persist across sessions. Auto-generated when the same error occurs 3+ times. Manual creation via CLI/UI/API. Confidence scoring with decay on violation. Pin/unpin support. Injected into system prompt as "Learned Rules" block.
- **Memory extension**: 3 new tables (`session_facts`, `session_summaries`, `lessons`) with indexes. 12 new methods on Memory class including `get_stats()` for observability.
- **6 new CLI commands**: `lumen memory-facts`, `lumen memory-summary`, `lumen lessons`, `lumen lesson-add`, `lumen lesson-pin`, `lumen lesson-delete`.
- **6 new API endpoints**: `GET /api/memory/facts`, `GET /api/memory/sessions`, `GET /api/lessons`, `POST /api/lessons`, `DELETE /api/lessons/{id}`, `POST /api/lessons/{id}/pin`.
- **Memoria UI page** (`/memory`): 3 tabs — Hechos (distilled facts with search), Resúmenes (session summaries, expandable), Aprendizajes (lessons CRUD, category filter, pin/delete with confirmation).

### Changed
- **Brain prompt injection**: Active lessons are pre-loaded at startup and injected into the system prompt after Personality, before Body. Cached for performance (`_cached_lessons_text`).

### Tests
- **36 new tests** (10 distiller + 26 lessons). 189 total new tests across Fase A+B.

## [0.8.0] - 2026-04-27

### Added
- **Model Router** (`model_router.py`): Role-based model selection (planner, executor, summarizer, responder) with configurable default and guaranteed fallback. Toggle "use default for all" for backward compat. Config-driven — no code edits needed.
- **Provider Health** (`provider_health.py`): EWMA-based latency tracking, exponential backoff on failures, auto-recovery after cooldown, priority-based fallback chain, degraded mode detection. Inspired by Aiden's self-healing provider routing.
- **Agent Status** (`agent_status.py`): Consolidated observable state — model, provider health, channels, modules, tools, memory stats, warnings. Callback-based collector for loose coupling. Lightweight `/health` endpoint enrichment.
- **5 new CLI commands**: `lumen model`, `lumen model-set`, `lumen model-toggle`, `lumen provider`, `lumen provider-retry`. `lumen status` enriched with model routing + provider health.
- **6 new API endpoints**: `GET/POST /api/models`, `GET /api/providers`, `POST /api/providers/retry`, `GET /api/agent/status`. `/health` now includes model + provider status.
- **3 new UI pages**: Ajustes > Modelos (role routing config), Ajustes > Providers (health table with auto-refresh), Estado del Agente (diagnostic panel). Sidebar navigation updated.

### Changed
- **Brain integration**: `_resolved_model()` now accepts role parameter and delegates to `ModelRouter`. `_completion_options()` maps purpose to model role. `think()` records provider health (success latency + failures).

### Tests
- **153 new tests** (45 model_router + 66 provider_health + 42 agent_status). 334 total ran, zero regressions.

## [0.7.1] - 2026-04-27

### Fixed
- **`_build_terminal_env` overwrite & cross-section bugs**: When multiple modules defined the same env key (e.g. `SCRIPTS_DIR`), the second silently overwrote the first. Now module-prefixed env vars are injected (e.g. `MODULE_A_SCRIPTS_DIR`, `MODULE_B_SCRIPTS_DIR`) to prevent data loss. Unprefixed versions kept for backward compat (last module wins). Also fixed: keys listed in `terminal.env.public` that lived in a module's `secret` section were never found — now cross-section lookup searches both.

### Tests
- **2 new tests**: `test_build_terminal_env_no_overwrite`, `test_build_terminal_env_cross_section`

## [0.7.0] - 2026-04-26

### Added
- **Native Module Handlers + WebSocket Event Emission**: Modules can now emit real-time events to the web frontend via WebSocket from within their `activate()` lifecycle hook. `ModuleRuntimeContext.broadcast_event(event_type, payload)` delegates to the web channel, which broadcasts JSON to all connected WebSocket clients. Stale sockets are cleaned up automatically. Purely additive — no breaking changes.
- **9 new tests** for broadcast callback delegation, stale WebSocket removal, multi-module independence, and wiring.

### Tests
- **550 tests passing** (was 541)

## [0.6.0] - 2026-04-26

### Added
- **SSE Streaming for `/api/chat`**: POST `/api/chat` now accepts `stream: true` and returns `text/event-stream` with events `session`, `delta`, `done`, `error`. Fully backward compatible — `stream: false` or absent returns identical JSON. New `brain.think_stream()` async generator with litellm `acompletion(stream=True)` and tool-call buffering.
- **Shared Capabilities / Libraries between Modules**: Modules can declare `capabilities: [name1, name2]` in `module.yaml`. Lumen installs them to `~/.lumen/capabilities/<name>/` and injects them into `sys.path` during `connector.py` load via `CapabilityPathInjector` context manager. Terminal subprocesses receive capability paths in `PYTHONPATH`. Read-only enforcement on installed capability directories.
- **31 new tests** (5 SSE streaming + 26 shared capabilities).

### Fixed
- **Brain Tool-Use Loop Alias Mismatch**: `ConnectorRegistry.as_tools()` now exposes both canonical full name and alias for single-action connectors. `_resolve_tool_calls()` allows unknown native tools when no fallback is present, letting execution errors be captured properly. Fixes 5 pre-existing `test_brain.py::TestToolUseLoop` failures.

### Tests
- **541 tests passing** (was 490)

## [0.5.1] - 2026-04-24

### Fixed
- **Contradiction Retry Layer (Phase 2.3)**: LLM now retries when it wrongly denies a READY capability. The model receives evidence of the contradiction and corrects itself. Handles patterns like "no tengo", "I don't have", "I can't" near capability mentions.
- **Tool Enforcement Directive**: Restored `_tool_enforcement_directive` method that was accidentally removed during edits. Provides model-specific guidance for tool usage (OpenAI, Gemini, generic).
- **Tool Suggestion (Phase 2.4)**: `_suggest_relevant_tools` now correctly suggests tools based on user message keywords (terminal, file, write, web, message, setup categories).
- **Build Order Fix**: Moved `tools` construction before `_build_prompt` to fix `NameError: name 'tools' is not defined`.
- **Return Type Fix**: Changed `_suggest_relevant_tools` return type from `str | None` to `str` to prevent `None` elements in system prompt.

### Added
- **11 new tests** for `_suggest_relevant_tools` covering all categories and edge cases.

### Tests
- **490 tests passing** (was 479)

## [0.5.0] - 2026-04-23

### Added
- **v0.5.0 Phase 1**: Model-robust execution foundation
- **Contradiction Detection**: Capability denial patterns for Spanish and English
- **Final Response Recovery**: `_retry_final_response_without_tools` and `_summarize_tool_results` for when model doesn't produce final text
- **Model Profiles**: `_model_profile()` method with model-family detection (OpenAI, Gemini, openai-compatible)
- **Tool Argument Coercion**: `_coerce_args` handles string-to-integer/boolean conversion for LLM arguments
- **Parser Fallback**: `_extract_fallback_tool_calls` handles XML/JSON blocks for non-native-tool models

### Tests
- **479 tests passing**

## [0.4.9] - 2026-04-22

### Fixed
- Inject runtime config into terminal connector handlers

## [0.4.8] - 2026-04-22

### Fixed
- CLI reload targets live running instance

## [0.4.7] - 2026-04-22

### Fixed
- Reload propagates fresh config into live brain

## [0.4.6] - 2026-04-22

### Fixed
- Reload updates brain.config for fresh skill interpolation

## [0.4.5] - 2026-04-22

### Fixed
- Reload fully rehydrates config and secrets from disk

## [0.4.4] - 2026-04-22

### Fixed
- Safe skill interpolation uses public config only

## [0.4.3] - 2026-04-22

### Fixed
- Runtime skill interpolation uses instance secrets

## [0.4.2] - 2026-04-22

### Fixed
- Instance-local modules and skill interpolation

## [0.4.0] - 2026-04-22

### Added
- Productive kits and extensible UI

## [0.3.2] - 2026-04-20

### Fixed
- REST auth reads CONFIG_PATH directly for api.rest_key

## [0.3.1] - 2026-04-20

### Added
- VPS bugs + CLI twin wizard

## [0.3.0] - 2026-04-20

### Added
- 11 new features across 3 sprints (398 tests)

## [0.2.2] - 2026-04-15

### Security
- Bumped to v0.2.2, remove leaked telegram token from examples

## [0.2.1] - 2026-04-15

### Security
- Replace leaked telegram bot token with placeholder