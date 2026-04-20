# Plan: Remote Catalogs (ClawHub + MCP Registry) + PyPI Publication

**Status**: ready to execute. All prerequisites verified. Self-contained handoff.
**Date**: 2026-04-18
**Goal**: Ship Lumen v0.1 to PyPI with real marketplace access (cientos de skills/MCPs), not just bundled x-lumen kits.

---

## Context you need before starting

### What already works (do not touch)
- `enlumen` PyPI name is **available and published** (verified 2026-04-20). `lumen-agent` was blocked by name similarity.
- Marketplace infra exists: [lumen/core/marketplace.py:352 `_load_remote`](../lumen/core/marketplace.py) fetches feeds, caches 300s, merges with local catalog.
- Feeds configurable via `config.marketplace.feeds` or `LUMEN_MARKETPLACE_FEEDS` env.
- Cerebellum already has `normalize_openclaw_metadata` in [lumen/core/cerebellum.py](../lumen/core/cerebellum.py) for shape translation.
- Capability awareness pipeline is end-to-end green (6 tests in `tests/test_awareness.py`).

### What was discovered about external markets
- **ClawHub public API**: `GET https://clawhub.ai/api/v1/search?q=X&limit=N` → `{results: [{slug, displayName, summary, score, version, updatedAt}]}`. The `/api/v1/skills` list endpoint returns empty publicly; must use `search` with a wildcard or keyword.
- **Anthropic MCP Registry**: `GET https://registry.modelcontextprotocol.io/v0/servers?limit=N` → `{servers: [{server: {name, description, title, version, remotes, repository}, _meta: {...}}], metadata: {nextCursor?}}`.
- **Hermes**: philosophical reference only. No registry. Skip.

### The shape mismatch problem
Lumen's current parser at [marketplace.py:_parse_remote_payload](../lumen/core/marketplace.py) line ~414 expects:
```json
{ "skills": [...], "mcps": [...] }
```
Neither ClawHub nor MCP Registry match. We need **format detection + adapters** that produce the internal `NormalizedArtifact` shape the cerebellum already understands.

---

## The Plan — 6 steps in order

### Step 1 — Add format detection to `_parse_remote_payload`
**File**: `lumen/core/marketplace.py` (~line 414)
**Change**: Detect payload shape and dispatch to the right adapter.

```python
def _parse_remote_payload(self, payload, feed, runtime_surface):
    # Existing native shape: {skills, mcps}
    if isinstance(payload, dict) and ("skills" in payload or "mcps" in payload):
        return self._parse_native_payload(payload, feed, runtime_surface)

    # ClawHub: {results: [...]} with per-item {slug, displayName, summary}
    if isinstance(payload, dict) and "results" in payload:
        return self._parse_clawhub_payload(payload, feed, runtime_surface)

    # MCP Registry: {servers: [...]} with per-item {server: {name, description, ...}}
    if isinstance(payload, dict) and "servers" in payload:
        return self._parse_mcp_registry_payload(payload, feed, runtime_surface)

    # Legacy/list fallback (already supported)
    if isinstance(payload, list):
        return self._parse_native_payload({"items": payload}, feed, runtime_surface)

    return []
```

Rename the current body into `_parse_native_payload` (move lines 423-454 into it, no logic change).

### Step 2 — ClawHub adapter
**File**: `lumen/core/marketplace.py`
**New method**: `_parse_clawhub_payload`

Each ClawHub item becomes a `skills` card. Map:
- `slug` → `name`
- `displayName` → `display_name`
- `summary` → `description`
- `version` → `version` (may be null, default to "latest")
- Synthesize `install_command`: `npx clawhub@latest install {slug}`
- Synthesize `source_url`: `https://clawhub.ai/skills/{slug}`
- `tags`: `["clawhub", "skill"]`
- `source_type`: `"clawhub"` (already in [marketplace.py:651](../lumen/core/marketplace.py))

Reuse `_remote_skill_card` + `normalize_openclaw_metadata` — pass a dict in the shape that function expects, or add a thin pre-mapper that matches its expected keys.

**Verify with**: `curl "https://clawhub.ai/api/v1/search?q=email&limit=3"` returns real items you can test against.

### Step 3 — MCP Registry adapter
**File**: `lumen/core/marketplace.py`
**New method**: `_parse_mcp_registry_payload`

Each server becomes an `mcps` card. Map:
- `server.name` → `name` (may have slashes like `ac.inference.sh/mcp` — sanitize with `.replace("/", "-")` for filesystem safety but keep original in `display_name`)
- `server.title` → `display_name`
- `server.description` → `description`
- `server.version` → `version`
- `server.repository.url` → `source_url`
- `server.remotes[0].url` → `install_command` for remote MCP (SSE/HTTP), or flag as "remote-http" type
- `tags`: `["mcp-registry", "mcp"]`
- `source_type`: `"mcp-registry"`

**Note on remotes**: the MCP Registry has servers with `remotes: [{type: "sse"|"streamable-http", url: "..."}]`. Our MCP runtime in [lumen/core/mcp_runtime.py](../lumen/core/mcp_runtime.py) must support connecting to remote HTTP/SSE MCPs. **VERIFY THIS FIRST** — if the runtime is stdio-only, remote MCPs won't work end-to-end; still list them but mark `compatibility: partial` with reason "requires remote MCP support".

### Step 4 — Cerebellum translation (the "does cerebellum understand?" question)
**File**: `lumen/core/cerebellum.py`
**Why**: The cerebellum decides if an artifact is `COMPAT_READY | INSTALLABLE | PARTIAL | BLOCKED`. Currently it uses `normalize_openclaw_metadata` which assumes OpenClaw shape.

**Change**: Extend `normalize_openclaw_metadata` (or add `normalize_external_artifact`) to handle pre-mapped dicts from adapters. The adapters in Steps 2-3 should produce a dict with these canonical keys that the cerebellum already expects:
- `name`, `display_name`, `description`, `version`
- `requires: {connectors: [], mcps: [], skills: [], min_model_tier: "..."}`
- `provides: [...]`
- `install: {method: "npx"|"git"|"pip"|"remote-http", target: "..."}`

Then `calculate_compatibility` in cerebellum already translates `requires` vs `runtime_surface` into a badge. No change needed there if adapters produce the canonical dict.

**Acceptance**: a ClawHub skill with no Python dependencies → `INSTALLABLE`. A ClawHub skill requiring `python>=3.11` → `READY` if Python matches. An MCP Registry entry with `remotes[0].type = "sse"` → `PARTIAL` if we don't support SSE yet, `INSTALLABLE` if we do.

### Step 5 — Default feeds bundled
**File**: `lumen/core/consciousness.yaml` OR a new `lumen/config/defaults.yaml` (prefer the latter — consciousness shouldn't hold config).

```yaml
marketplace:
  feeds:
    - name: "MCP Registry"
      url: "https://registry.modelcontextprotocol.io/v0/servers?limit=100"
    - name: "ClawHub"
      url: "https://clawhub.ai/api/v1/search?q=skill&limit=50"
```

**Wire it**: in [lumen/core/runtime.py:bootstrap_runtime](../lumen/core/runtime.py), merge defaults into `config.marketplace` if user hasn't overridden. User config always wins.

**Alternative for ClawHub**: instead of hardcoding `q=skill`, call the search multiple times with common keywords (`email, calendar, notes, code, docs, github, slack`) and union the results. Trade-off: more HTTP calls on cold cache. Better: cache 300s is already there, so 7 calls once every 5 minutes is fine. Implement in `_load_remote` if ClawHub URL is detected as a "search-multi" feed type.

### Step 6 — Install bridge (the hard part)
**File**: `lumen/core/installer.py`

Current installer copies from local catalog. For remote items we need:

**For ClawHub skills**: run `npx clawhub@latest install <slug> --dir <lumen-installed-dir>`. Requires Node.js. Check with `shutil.which("npx")`. If missing → fail with clear error "Install Node.js to use ClawHub skills" + link to https://nodejs.org.

**For MCP Registry servers**:
- If `remotes[0].type` is `stdio` with a reference to an npm/pip package → delegate to existing installer paths.
- If `remotes[0].type` is `sse` or `streamable-http` → register as a remote MCP in `~/.lumen/mcp_config.yaml`. No install needed, just config. Verify [lumen/core/mcp_runtime.py](../lumen/core/mcp_runtime.py) supports remote transports.

Add a new `InstallSource` enum: `CATALOG | CLAWHUB | MCP_REGISTRY | MANUAL`, each with its own install path.

### Step 7 — Tests
**File**: `tests/test_remote_catalogs.py` (new)

Mock `urlopen` with fixture payloads captured from the real APIs (do this once, bake the fixtures into `tests/fixtures/clawhub_search.json` and `tests/fixtures/mcp_registry.json`).

Tests:
- `test_format_detection_native_payload` → old `{skills, mcps}` still works
- `test_format_detection_clawhub` → dispatches to clawhub adapter
- `test_format_detection_mcp_registry` → dispatches to MCP adapter
- `test_clawhub_adapter_produces_canonical_shape`
- `test_mcp_registry_adapter_produces_canonical_shape`
- `test_mcp_registry_remote_transport_marked_partial_when_unsupported`
- `test_default_feeds_bundled_in_runtime`
- `test_install_bridge_clawhub_missing_node_fails_gracefully`

---

## PyPI publication — after Steps 1-7 land

### Pre-flight
1. Run full test suite: `python -m pytest tests/ -q` → all green.
2. Verify `pyproject.toml` has author email: ✅ already done (`gabogabucho@gmail.com`).
3. `python -m build` → produces `dist/lumen_agent-0.1.0.tar.gz` + wheel.
4. `twine check dist/*` → metadata valid.

### TestPyPI dry-run (mandatory first)
```bash
twine upload --repository testpypi dist/*
# Test install in clean venv:
python -m venv /tmp/lumen-test && source /tmp/lumen-test/bin/activate
pip install --index-url https://test.pypi.org/simple/ --extra-index-url https://pypi.org/simple/ enlumen
lumen --help
lumen run  # smoke test
```

### Real PyPI publish
Only after TestPyPI succeeds AND user confirms:
```bash
twine upload dist/*
```

### Post-publish
1. Tag git: `git tag v0.1.0 && git push --tags`.
2. Create GitHub release with changelog summarizing bugfixes + awareness pipeline + remote catalogs.
3. Announce with ready-to-copy paste: `pip install enlumen && lumen run`.

---

## Risks and open questions

1. **MCP remote transport support**: must verify `mcp_runtime.py` handles `sse` and `streamable-http`. If not, MCP Registry is only half useful. Check BEFORE step 3.
2. **ClawHub install requires Node**: not a blocker for v0.1 but should be mentioned in README prerequisites.
3. **Network dependency at boot**: if user is offline, feeds fail. Cache already handles this gracefully (empty results), but `_load_remote` should log a single warning, not spam.
4. **Rate limiting**: neither API documents limits. Add `User-Agent: enlumen/0.2.0` header to be good citizens and allow them to block us if we misbehave.
5. **Telegram module untracked cleanup**: DONE (see `.gitignore` change 2026-04-18).
6. **Windows CRLF warnings**: cosmetic, ignore.

---

## What's already fixed (do NOT redo)

- Circular import `events.py ↔ registry.py` → fixed via `TYPE_CHECKING`.
- Missing `@app.delete` decorator on `api_modules_uninstall` → restored.
- UI ignored `type: awareness` WS messages → handler added in `dashboard.html`.
- `pyproject.toml` author email → added `gabogabucho@gmail.com`.
- `.gitignore` now excludes user-installed modules (keeps `_template` + `scheduler`).
- 6 new tests in `tests/test_awareness.py` cover the full awareness pipeline.

**Current test state**: 158 passing, 1 pre-existing failure (`test_runtime_refresh_preserves_mcp_truth_and_syncs_marketplace`) unrelated to this work.

---

## Suggested execution order for the next session

Day 1 (4h):
- Step 1 (detector) + Step 2 (ClawHub adapter) + tests for both.
- Step 5 (default feeds wiring for ClawHub only).
- Manual smoke: boot Lumen, open marketplace UI, verify ClawHub skills appear.

Day 2 (3h):
- Step 3 (MCP Registry adapter) + tests.
- Step 4 (cerebellum check — usually zero-code if adapters produce canonical shape).
- Step 5 (add MCP registry feed).
- Smoke: MCP cards visible, install bridge works for at least one remote MCP.

Day 3 (2h):
- Step 6 (install bridges) + tests.
- Step 7 (full test sweep, fix anything red).
- PyPI pre-flight + TestPyPI dry-run.
- User confirms → real PyPI publish.

Total: ~9 hours of focused work across 3 sessions.

---

## Handoff checklist (if another agent picks this up)

- [ ] Read this document top to bottom.
- [ ] `mem_search(query: "neo-agent capability awareness")` to load context.
- [ ] `git status` to see current uncommitted state.
- [ ] `python -m pytest tests/ -q` to confirm green baseline.
- [ ] Verify `mcp_runtime.py` supports remote transports BEFORE Step 3.
- [ ] Execute steps in order. Do NOT skip tests.
- [ ] When done, update this doc's status header to "completed" + link to PR.
