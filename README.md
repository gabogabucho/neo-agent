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
- Install a module &rarr; new vertical (barbershop, restaurant, support, ...)
- Bring your own module &rarr; load any custom `module.yaml`

## Quickstart

```bash
pip install lumen-agent
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

### Run the test suite

```bash
pytest -q
```

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

Lumen is **conscious** of its own capabilities. At startup the Discovery system scans skills, connectors, modules, and channels. Lumen knows what it can do, what it can't, and why. When you ask it for something it lacks, it doesn't hallucinate &mdash; it tells you what's missing and suggests how to extend it.

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

Skills are markdown files the LLM reads on demand. Anyone can create a skill &mdash; just write a `SKILL.md`. No runtime, no compiler, no framework.

### Connectors and MCP

```
Connector → action → result
```

Built-in handlers for `task`, `note`, and `memory`. Anything else plugs in via MCP servers (advisory requirements declared in `module.yaml`, validated at install time).

## Features

- **Three-path onboarding** &mdash; quick start, personality picker, or bring-your-own module
- **Consumer UI** &mdash; clean chat-first dashboard, collapsable sidebar, no dev panel
- **Personality marketplace** &mdash; personality modules surfaced first with filters and badges
- **Bilingual** &mdash; English and Spanish locale packs out of the box
- **Self-aware** &mdash; knows its capabilities, gaps, and recommended LLM tiers
- **Model-agnostic** &mdash; DeepSeek, OpenAI, Anthropic, OpenRouter (OAuth + free tier curated), Ollama via LiteLLM
- **Persistent memory** &mdash; SQLite + FTS5 for tasks, notes, and facts
- **Live runtime** &mdash; FastAPI + WebSocket with heartbeat and session pruning
- **MCP runtime** &mdash; load MCP servers declared by modules
- **Module catalog + uploads** &mdash; install from catalog or upload a custom `module.yaml`/zip
- **Tested** &mdash; 148 tests covering brain, memory, web surfaces, marketplace, OAuth, MCP runtime, personality swap (including disk-snapshot guarantees)

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

Read the [full manifesto](MANIFESTO.md).

## Project Structure

```
lumen/
├── core/
│   ├── brain.py          # Context assembler (~200 lines)
│   ├── consciousness.py  # Immutable identity (the soul)
│   ├── registry.py       # Body — discovered capabilities
│   ├── discovery.py      # Scans skills/connectors/modules
│   ├── personality.py    # YAML personality loader
│   ├── memory.py         # SQLite + FTS5
│   ├── session.py        # Per-conversation state
│   ├── connectors.py     # Connector registry + tool schemas
│   ├── handlers.py       # Built-in handlers (task, note, memory)
│   ├── installer.py      # Module install / uninstall
│   ├── runtime.py        # active_personality boot + hot reload
│   └── mcp.py            # MCP client adapter
├── channels/
│   ├── web.py            # FastAPI + WebSocket dashboard
│   └── templates/        # Dashboard, setup wizard, awakening
├── locales/{en,es}/      # Language packs
├── catalog/              # Built-in module catalog (incl. personality kits)
├── modules/              # Installed modules (user-managed)
├── connectors/           # Built-in connector definitions
├── skills/               # Skill definitions (SKILL.md)
└── cli/main.py           # CLI (lumen run, lumen install, lumen status)
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

| Provider | Model | Tier |
|----------|-------|------|
| OpenRouter (OAuth) | curated free models (Llama 3.3, DeepSeek, Mistral, Gemma 3) | tier-1 / tier-2 |
| DeepSeek | deepseek-chat | tier-2 (recommended) |
| OpenAI | gpt-4o-mini | tier-2 |
| Anthropic | claude-sonnet-4 | tier-3 |
| Ollama | llama3 (local) | tier-1 |

## Roadmap

- [x] Core brain + consciousness + memory
- [x] Web dashboard (UI-First, consumer-friendly)
- [x] Three-path setup wizard + awakening animation
- [x] Bilingual (en/es)
- [x] Self-awareness (registry + discovery)
- [x] Personality runtime + clean install/uninstall swap
- [x] Module marketplace (personality-first display)
- [x] MCP client adapter
- [x] OpenRouter OAuth + free-tier curation
- [x] Comprehensive test suite (148 tests)
- [x] CONTRIBUTING.md tutorial
- [ ] Channel modules (`x-lumen-comunicacion-*`: WhatsApp, Telegram, ...)
- [ ] Public module registry / discovery
- [ ] Docker support
- [ ] Full hosted documentation

## License

[MIT](LICENSE) &mdash; Free and open source, forever.

---

<p align="center">
  <em>Built by <a href="https://github.com/gabogabucho">Gabo Urrutia</a></em>
</p>
