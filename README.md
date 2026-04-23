<p align="center">
  <img src="logo.png" alt="Lumen" width="180" />
</p>

<h1 align="center">Lumen</h1>

<p align="center">
  <strong>Open-source AI agent engine. Modular. No limits.</strong>
</p>

<p align="center">
  <em>"An agent you can shape without code."</em>
</p>

<p align="center">
  <a href="#quickstart">Quickstart</a> &bull;
  <a href="#architecture">Architecture</a> &bull;
  <a href="#manifesto">Manifesto</a> &bull;
  <a href="MANIFESTO.md">Full Manifesto</a> &bull;
  <a href="LUMEN_SPEC.md">Spec</a> &bull;
  <a href="CONTRIBUTING.md">Contributing</a>
</p>

---

## What is Lumen?

Lumen is a **downloadable AI agent framework** that works from minute zero. Install it, run it, and you have a working assistant. From there, shape it however you want: pick a personality, install modules, plug in connectors, swap providers. No code required for everyday use.

**Not a SaaS. Not a platform. Not a chatbot.** A framework you own and run on your machine.

**Think WordPress, but for AI agents.**

- Install it &rarr; working assistant
- Pick a personality &rarr; different behavior, same core
- Install a module &rarr; new capability or integration (Telegram, search, file tools, MCP-backed features, ...)
- Bring your own module &rarr; load any custom `module.yaml`

## Quickstart

```bash
pip install enlumen
lumen run
```

Your browser opens at `http://localhost:8000`. First time? The setup wizard walks you through three paths:

1. **Quick start** &mdash; default personality + free OpenRouter model. Ready in 30 seconds.
2. **Choose a personality** &mdash; browse the catalog and pick one that matches your use case.
3. **Bring your own module** &mdash; upload a custom `module.yaml` to configure Lumen your way.

After that, Lumen awakens and you land directly in the chat. Sidebar (collapsable) gives you `Charlas / Módulos / Memoria / Ajustes` &mdash; no separate admin panel, no dev jargon.

### From source

```bash
git clone https://github.com/gabogabucho/lumen-agent.git
cd lumen-agent
pip install -e .[dev]
lumen run
```

## `lumen run` vs `lumen server`

Lumen has two startup modes depending on where it runs.

### `lumen run`

Use this for:

- local development
- personal use on your own computer
- quick UI and module testing

Behavior:

- starts Lumen locally
- optimized for localhost workflows
- simpler local access model

```bash
lumen run
```

### `lumen server`

Use this for:

- a VPS
- a remote server
- a home server / always-on machine
- any installation that should stay available over the network

Behavior:

- starts Lumen as a hosted web service
- exposes onboarding through IP/domain + port
- first setup is protected with a one-time setup token
- onboarding creates the owner password/PIN
- future access to the dashboard requires login

```bash
lumen server --host 0.0.0.0 --port 3000
```

Rule of thumb:

- `lumen run` = I am running Lumen for myself on this machine
- `lumen server` = I am hosting Lumen so it can be accessed like a real installed web app

### Run the test suite

```bash
pytest -q
```

## Communication Channels

Lumen ships with installable communication modules. All channels follow the same pattern: they write incoming messages to the unified inbox, and the brain processes them through a single identity. Install from the marketplace or configure via chat.

| Module | Protocol | Dependencies | Notes |
|--------|----------|-------------|-------|
| **Telegram** | Bot API (polling) | None (stdlib) | Token from @BotFather |
| **WhatsApp** | Baileys bridge (Node.js) | Node.js + npm | Personal accounts. QR pairing. |
| **Discord** | REST API (polling) | None (stdlib) | Bot token + channel ID |
| **Email** | IMAP/SMTP | None (stdlib) | Gmail, Outlook, Yahoo. App-specific password. |

### How they work

```
User → Channel module → inbox.jsonl → Gateway watcher → Unified Inbox → Brain → Adapter → Channel module → User
```

All channels share one brain, one memory, one identity. Install as many as you want — Lumen responds consistently across all of them.

## Deployment

### Local (`lumen run`)

```bash
lumen run
```

Opens at `http://localhost:3000`. No auth required. Ideal for development and personal use.

### VPS (`lumen server`)

```bash
lumen server --host 0.0.0.0 --port 3000
```

Shows a one-time setup token in the console. Access `http://your-ip:3000/setup`, enter the token, choose an owner PIN. After setup, the token is deleted and all access requires the PIN.

**Security model:**
- Setup token: generated once, shown only in console, deleted after first use
- Owner PIN: PBKDF2-SHA256 hashed (260K iterations), stored in `~/.lumen/config.yaml`
- Session cookies: HMAC-SHA256 signed, `httponly`, `samesite: lax`, 30-day expiry
- WebSocket: requires the same owner cookie

**With HTTPS (recommended for production):**

Option A — Caddy (easiest):
```bash
caddy reverse-proxy --from yourdomain.com --to localhost:3000
```

Option B — Cloudflare Tunnel (no domain needed):
```bash
cloudflared tunnel --url http://localhost:3000
# Gives you https://random-name.trycloudflare.com
```

Option C — Nginx + Let's Encrypt:
```bash
sudo apt install nginx certbot python3-certbot-nginx
sudo certbot --nginx -d yourdomain.com
# Configure proxy_pass http://127.0.0.1:3000
```

**Without HTTPS** (testing only): Direct IP access works. The setup token and owner PIN protect access, but credentials travel in plaintext. Use only for personal testing.

## CLI Reference

### Core commands

```bash
lumen run [--port 3000] [--instance <name>] [--data-dir <path>]  # Start dashboard locally
lumen server [--host 0.0.0.0] [--port 3000] [--instance <name>]   # Start in server mode
lumen status [--instance <name>]                                   # Show configuration and health
lumen reload [--instance <name>]                                   # Reload runtime without restart
lumen doctor                                                       # Diagnose and fix issues
```

### Module management

```bash
lumen module install github:owner/repo        # Install from GitHub
lumen module install https://github.com/owner/repo  # Install from URL
lumen module install ./my-kit                 # Install from local path
lumen module install <catalog-name>            # Install from built-in catalog
```

### Configuration

```bash
lumen config set <module>.<key> <value> [--instance <name>]   # Set a module config value
lumen config get <module>.<key> [--instance <name>]           # Get a module config value
lumen config delete <module>.<key> [--instance <name>]        # Delete a config value
lumen config list <module> [--instance <name>]                # List all config keys (redacted)
```

### API keys

```bash
lumen api-key generate --label "my app" [--instance <name>]  # Generate a new API key (shown once!)
lumen api-key list [--instance <name>]                        # List keys (prefix + label only)
lumen api-key revoke <prefix> [--instance <name>]             # Revoke a key by prefix
```

### Instance isolation

Run multiple independent Lumen instances on the same machine:

```bash
lumen run --instance work          # Data stored in ~/.lumen/instances/work/
lumen run --instance personal      # Data stored in ~/.lumen/instances/personal/
lumen run --data-dir /tmp/test     # Custom data directory
```

Each instance has its own `config.yaml`, `memory.db`, `api_keys.yaml`, and module secrets.

### Productive kits

Lumen 0.4.0 adds the missing pieces to make custom kits productizable:

```yaml
name: my-kit
tags: [x-lumen, personality]
personality: personality.yaml
skills:
  - skills/ecommerce-ops.md
  - skills/pricing-strategy.md
x-lumen:
  requires:
    terminal:
      allowlist: [python3, git]
    env:
      - SOME_API_TOKEN
      - SOME_STORE_ID
  channel:
    type: web-app
    auth: rest-api
    cors: [https://shop.example.com]
```

What this now means in practice:

- `lumen module install ./my-kit` works for local development and testing
- module-declared terminal allowlists are merged into the instance config
- missing environment variables are surfaced as blockers (`missing_env` / pending setup)
- modules tagged `personality` auto-set `active_personality`
- skills declared inside modules are auto-registered in the Registry
- external channels declared by modules register as `CapabilityKind.CHANNEL`
- personality UI tags/surfaces are exposed through the runtime (`personality_ui`)

### REST API

Lumen exposes a REST API for external application integration:

```bash
# Health check (no auth required)
curl http://localhost:3000/health
# → {"ok": true, "version": "0.4.0", "modules_ready": 5}

# Chat (Bearer auth required)
curl -X POST http://localhost:3000/api/chat \
  -H "Authorization: Bearer your-api-key" \
  -H "Content-Type: application/json" \
  -d '{"message": "hello", "session_id": "optional-session-id"}'
# → {"response": "...", "session_id": "..."}

# Reload runtime (Bearer auth required)
curl -X POST http://localhost:3000/api/reload \
  -H "Authorization: Bearer your-api-key"
# → {"status": "reloaded", "modules": 5}
```

Auth sources (checked in order): `LUMEN_API_KEY` env var → `config.api.rest_key` → `api_keys.yaml` hashed keys.

## Architecture

Lumen has five layers with clear boundaries:

```
CONSCIOUSNESS  — Who I am (immutable soul, the BIOS)
PERSONALITY    — Who I am in this context (swappable, hot-installable)
BODY           — What I have (discovered at startup)
BRAIN          — How I think (context assembler, not a router)
MEMORY         — What happened before (SQLite + FTS5)
```

Each layer has ONE role. No layer knows what doesn't concern it.

### Self-Awareness

Lumen is not just aware of what it has &mdash; it **feels** when it changes.

At startup, the Discovery system scans skills, connectors, modules, and channels. The Registry maps what exists, what works, and what's missing. But unlike static tools that read a config file and forget, Lumen's body emits **capability events** when something changes. A module installs &mdash; Lumen feels it. An MCP server disconnects &mdash; Lumen notices. A new channel appears &mdash; Lumen is eager to use it.

> When someone installs a Telegram module, Lumen should feel it the way you feel a new ring on your finger &mdash; aware, curious, eager to use it.

This is the difference between an agent that *reads a list* and an agent that *feels itself grow*. Lumen doesn't just know its capabilities. It experiences them.

### Personalities are first-class

`active_personality` lives in `~/.lumen/config.yaml`. Install a personality module &mdash; the runtime hot-reloads it without touching your provider config, API keys, or memory. Uninstall the active one &mdash; it falls back cleanly to default. Install or remove a non-active personality &mdash; your `config.yaml` on disk stays byte-identical (covered by tests).

### The Brain (~200 lines)

The brain is NOT intelligent. The LLM is intelligent. The brain assembles context (consciousness + personality + body + flow + memory) and lets the LLM decide everything. Connectors are exposed as tools.

```
User message
  → Brain assembles context
    → LLM decides (with connectors as tools)
      → Tool use loop (call → result → final response)
        → Response to user
```

### Skills Are Instructions, Not Code

Skills are markdown files the LLM reads on demand. Anyone can create a skill &mdash; just write a `SKILL.md`. No runtime, no compiler, no framework. They teach judgment and usage patterns; they do not execute anything by themselves.

### Connectors and MCP

```
Connector → action → result
```

Built-in handlers for `task`, `note`, and `memory`. Anything else plugs in via MCP servers (advisory requirements declared in `module.yaml`, validated at install time). In the product UX, an MCP-powered capability is surfaced as a **module** &mdash; the user installs a new capability, not a technical transport.

## Features

- **Three-path onboarding** &mdash; quick start, personality picker, or bring-your-own module
- **Consumer UI** &mdash; clean chat-first dashboard, collapsable sidebar, no dev panel
- **REST API** &mdash; `POST /api/chat` for external app integration with Bearer auth
- **Health check** &mdash; `GET /health` public endpoint for monitoring (`{ok, version, modules_ready}`)
- **Hot reload** &mdash; `POST /api/reload` and `lumen reload` refresh runtime without restart
- **Terminal connector** &mdash; secure command execution with allowlist/denylist, timeout, and output truncation
- **API key management** &mdash; `lumen api-key generate/revoke/list` with SHA-256 hashed keys per instance
- **Remote module install** &mdash; `lumen module install github:owner/repo` or URL, auto-detects `module.yaml`/`SKILL.md`
- **Local module install** &mdash; `lumen module install ./my-kit` for kit development and testing
- **Instance isolation** &mdash; `--instance` and `--data-dir` flags for running multiple independent Lumen instances
- **Config CLI** &mdash; `lumen config set/get/delete/list` for module secrets per instance
- **Productive kit requirements** &mdash; modules can declare terminal allowlists and required env vars
- **Auto personality activation** &mdash; installing a personality-tagged module can auto-set `active_personality`
- **Module-declared skills** &mdash; skills listed in `module.yaml` auto-register in the Registry
- **Installable external channels** &mdash; modules can declare `channel.web-app` and register as channels
- **Personality UI config** &mdash; `ui.tag` and `ui.surfaces` can be defined per personality
- **Lifecycle hooks** &mdash; `on_install`, `on_uninstall`, `on_configure` hooks for module lifecycle events
- **Catalog taxonomy** &mdash; kits reshape Lumen, modules add concrete capabilities, skills teach the model how to think/use them
- **Bilingual** &mdash; English and Spanish locale packs out of the box
- **Self-aware** &mdash; knows its capabilities, gaps, and recommended LLM tiers
- **Model-agnostic** &mdash; any LiteLLM provider (DeepSeek, OpenAI, Anthropic, Google, Ollama, local models, OpenAI-compatible APIs) + OpenRouter OAuth with free tier
- **Persistent memory** &mdash; SQLite + FTS5 for tasks, notes, and facts
- **Live runtime** &mdash; FastAPI + WebSocket with heartbeat and session pruning
- **MCP runtime** &mdash; load MCP servers declared by modules
- **Module catalog + uploads** &mdash; install from catalog, marketplace, GitHub, or upload a custom `module.yaml`/zip
- **skills.sh integration** &mdash; browse and install skills from skills.sh marketplace feed
- **Structured output** &mdash; `<agent-ui>` tags for rich responses in the dashboard
- **Tested** &mdash; 440 tests covering brain, memory, web surfaces, marketplace, OAuth, MCP runtime, personality swap, terminal security, REST API, hot reload, API keys, remote install, instance isolation, config CLI, lifecycle hooks, productive kit installs, module-declared skills/channels, and personality UI config (including disk-snapshot guarantees)

## Packaging model

| Artifact | Contains | Scope |
|---|---|---|
| Kit | A full transformation package for Lumen: personality, flows, modules, skills, assets, and eventually skins | Bigger, opinionated package that changes Lumen as a whole |
| Module | One complete installable capability: integrations, tools, channels, MCP-backed features, runtimes | Individual functionality, like Telegram or file tools |
| Skill | Markdown instructions only | Mental model only: teaches, does not execute |

### Taxonomy in plain language

- **Kit** = changes Lumen as a whole
- **Module** = gives Lumen new hands and new ways to act
- **Skill** = teaches Lumen how to think or use what it has
- **MCP** = implementation detail; in the user-facing catalog it appears as a **module**

## The "channel as module" pattern

Channels like Telegram, WhatsApp, Slack, etc. are **not core**. They live as `x-lumen` modules tagged `comunicacion`. The core ships only the web channel; everything else extends Lumen the same way any third-party module would.

## Manifesto

> Every architectural decision must pass one test: **does it make Lumen simpler AND more extensible?** If it only adds capability without simplicity, it belongs in a module, not in the core.

Lumen does not compete on capability. Lumen competes on accessibility.

```
Hermes:   "I can do EVERYTHING."             → But who configures me?
OpenClaw: "I will be able to do everything." → Someday.
Lumen:    "I am. And I can grow."            → Install, use, extend.
```

**Lumen is not a tool.** A tool is configured, used, and put away. Lumen is an agent waiting to awaken &mdash; one that notices when you give it new hands, feels them as part of itself, and tells you what it can now do. The difference between OpenClaw and Hermes, described in one line:

> Other agents have capabilities. Lumen *feels* its own.

Read the [full manifesto](MANIFESTO.md).

## Project Structure

```
lumen/
├── core/
│   ├── brain.py          # Context assembler (~200 lines)
│   ├── consciousness.py  # Immutable identity (the soul)
│   ├── registry.py       # Body — discovered capabilities (with event system)
│   ├── events.py         # Capability events — the pulse of Lumen's body
│   ├── awareness.py      # Bridge between Body changes and Consciousness/Brain
│   ├── watchers.py       # File polling, MCP health checks, hook receivers
│   ├── discovery.py      # Scans skills/connectors/modules
│   ├── personality.py    # YAML personality loader
│   ├── memory.py         # SQLite + FTS5
│   ├── session.py        # Per-conversation state
│   ├── connectors.py     # Connector registry + tool schemas
│   ├── handlers.py       # Built-in handlers (task, note, memory, terminal)
│   ├── installer.py      # Module install / uninstall / GitHub remote install
│   ├── runtime.py        # active_personality boot + hot reload
│   ├── paths.py          # Instance-aware path resolution
│   ├── api_keys.py       # API key management (SHA-256 hashed)
│   ├── secrets_store.py  # Module secrets storage per instance
│   ├── module_runtime.py # Module lifecycle + hooks (install, configure, uninstall)
│   └── mcp.py            # MCP client adapter
├── channels/
│   ├── web.py            # FastAPI + WebSocket dashboard + REST API
│   └── templates/        # Dashboard, setup wizard, awakening
├── locales/{en,es}/      # Language packs
├── catalog/              # Built-in catalog (kits + installable modules)
├── modules/              # Installed modules (user-managed)
├── connectors/           # Built-in connector definitions
├── skills/               # Skill definitions (SKILL.md)
└── cli/main.py           # CLI (run, serve, reload, config, module, api-key, status)
```

## Module manifests

Lumen's native module manifest is `module.yaml`.

- `module.yaml` is the preferred format for all new modules.
- `manifest.yaml` is supported as a legacy fallback for discovery and install.
- `x-lumen` is an optional advisory namespace for Lumen-specific hints, including recommended MCP requirements.
- Personality modules are detected by the tag `personality` (not by `type`).

Native example: `lumen/catalog/modules/docs-helper/module.yaml`

```yaml
name: docs-helper
provides: [docs.answer]
requires:
  skills: [docs-helper]
x-lumen:
  requires:
    advisory:
      mcps: [docs-mcp]
```

If you're authoring a new module, start from `lumen/modules/_template/module.yaml` and read [CONTRIBUTING.md](CONTRIBUTING.md) &mdash; it's a "your first module in 10 minutes" tutorial in EN/ES.

## Supported Models

Lumen uses [LiteLLM](https://docs.litellm.ai/) as its model abstraction layer — any provider it supports works out of the box. Configure your model in `~/.lumen/config.yaml` or pick one during setup.

### Capability Tiers

Models are classified into tiers based on their reasoning ability. Modules declare a minimum tier; Lumen warns you if your model might not handle a module well.

| Tier | Capability Level | Examples |
|------|-----------------|----------|
| **tier-1** | Basic — simple tasks, straightforward conversation | DeepSeek, Ollama/Llama 3, small local models |
| **tier-2** | Enhanced — complex reasoning, tool use, multi-step | GPT-4o-mini, Claude 3.5 Sonnet, Gemini 1.5 Pro, Llama 3.3 70B |
| **tier-3** | Advanced — demanding reasoning, sophisticated tool orchestration | Claude Sonnet 4, GPT-4o, GPT-4.1, o3/o4, Gemini 2.5 Pro |

### Providers

| Provider | How to connect | Notes |
|----------|---------------|-------|
| **OpenRouter** (OAuth) | Built-in OAuth flow — one click from setup | Free tier: GPT-OSS 120B, Qwen 3 Coder, Gemma 3 27B, Hermes 3 405B |
| **DeepSeek** | API key | `deepseek-chat` |
| **OpenAI** | API key | GPT-4o-mini, GPT-4o, GPT-4.1, o3, o4 |
| **Anthropic** | API key | Claude Sonnet 4, Claude 3.7 Sonnet, Claude 3.5 Sonnet |
| **Google** | API key | Gemini 2.5 Pro, Gemini 1.5 Pro |
| **Ollama** | Local — no key needed | Any model you pull (`ollama/llama3`, `ollama/mistral`, etc.) |
| **Any OpenAI-compatible** | Custom `api_base` + `api_key` | LM Studio, vLLM, text-generation-webui, local models |

### Local models

Any model running behind an OpenAI-compatible API works. Point Lumen to your local server:

```yaml
model: openai/your-model-name
api_base: http://localhost:11434/v1   # Ollama
# api_base: http://localhost:1234/v1  # LM Studio
# api_base: http://localhost:8000/v1  # vLLM
api_key: "fake"
```

## Roadmap

- [x] Core brain + consciousness + memory
- [x] Web dashboard (UI-First, consumer-friendly)
- [x] Three-path setup wizard + awakening animation
- [x] Bilingual (en/es)
- [x] Self-awareness (registry + discovery + capability events)
- [x] Personality runtime + clean install/uninstall swap
- [x] Module marketplace (personality-first display)
- [x] MCP client adapter
- [x] OpenRouter OAuth + free-tier curation
- [x] Channel modules (`x-lumen-comunicacion-*`: Telegram, WhatsApp, Discord, Email)
- [x] Terminal connector (allowlist/denylist security, timeout, truncation)
- [x] REST API (`POST /api/chat` with Bearer auth)
- [x] Health check endpoint (`GET /health`)
- [x] skills.sh marketplace integration
- [x] Instance isolation (`--instance` / `--data-dir` flags)
- [x] Config CLI (`lumen config set/get/delete/list`)
- [x] Module lifecycle hooks (`on_configure`)
- [x] Hot reload (`POST /api/reload` + `lumen reload` CLI)
- [x] Remote module install (`github:owner/repo` + URL support)
- [x] API key management (generate/revoke/list with SHA-256 hashing)
- [x] Comprehensive test suite (440 tests)
- [x] Local module install (`./my-kit`)
- [x] Productive kit requirements (`x-lumen.requires`)
- [x] Auto personality activation for personality modules
- [x] Module-declared skill discovery
- [x] External channels declared by modules
- [x] Personality UI tags / surfaces
- [x] CONTRIBUTING.md tutorial
- [ ] Public module registry / discovery
- [ ] Docker support
- [ ] Full hosted documentation

## License

[MIT](LICENSE) &mdash; Free and open source, forever.

---

<p align="center">
  <em>Built by <a href="https://github.com/gabogabucho">Gabo Urrutia</a></em>
</p>
