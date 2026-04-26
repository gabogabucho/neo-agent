# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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