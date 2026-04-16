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
  <a href="LUMEN_SPEC.md">Spec</a>
</p>

---

## What is Lumen?

Lumen is a **downloadable AI agent framework** that works from minute zero. Install it, run it, and you have a working assistant. From there, shape it however you want: change the personality, install modules, connect channels. No code required.

**Not a SaaS. Not a platform. Not a chatbot.** A framework you own.

**Think WordPress, but for AI agents.**

- Install it &rarr; working assistant
- Change personality &rarr; different behavior
- Install module &rarr; new vertical (barbershop, restaurant, support)
- The sky is the limit

## Quickstart

```bash
pip install lumen-agent
lumen run
```

That's it. Your browser opens. First time? The setup wizard walks you through language, model, and API key. Then Lumen awakens.

### From source

```bash
git clone https://github.com/gabogabucho/lumen-agent.git
cd lumen-agent
pip install -e .
lumen run
```

## Architecture

Lumen has five layers with clear boundaries:

```
CONSCIOUSNESS  — Who I am (immutable soul, the BIOS)
PERSONALITY    — Who I am in this context (swappable per module)
BODY           — What I have (discovered at startup)
BRAIN          — How I think (context assembler, not a router)
MEMORY         — What happened before (SQLite + FTS5)
```

Each layer has ONE role. No layer knows what doesn't concern it.

### Self-Awareness

Lumen is **conscious** of its own capabilities. At startup, the Discovery system scans all skills, connectors, modules, and channels. Lumen knows what it can do, what it can't, and why.

When you ask Lumen something it can't do, it doesn't hallucinate — it tells you what's missing and suggests how to extend it.

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

Skills are markdown files the LLM reads on demand. Anyone can create a skill — just write a SKILL.md file. No runtime, no compiler, no framework.

### Connectors: 3 Layers, Not 5

```
Connector → action → result
```

Built-in handlers for task, note, and memory. MCP servers for everything else.

## Features

- **UI-First**: Web dashboard with real-time chat (not a CLI tool)
- **Bilingual**: Ships with English and Spanish locale packs
- **Self-Aware**: Knows its capabilities, gaps, and recommended LLM tiers
- **Model-Agnostic**: DeepSeek, OpenAI, Anthropic, Ollama (via LiteLLM)
- **Slot-Based Flows**: Conversation flows that fill slots, not follow scripts
- **Persistent Memory**: SQLite + FTS5 for tasks, notes, and facts
- **Progressive Disclosure**: Skills listed in prompt, full content loaded on demand
- **Awakening Animation**: Eye-opening SVG animation on first run

## Manifesto

> Every architectural decision must pass one test: **does it make Lumen simpler AND more extensible?** If it only adds capability without simplicity, it belongs in a module, not in the core.

Lumen does not compete on capability. Lumen competes on accessibility.

```
Hermes: "I can do EVERYTHING."           → But who configures me?
OpenClaw: "I will be able to do everything." → Someday.
Lumen: "I am. And I can grow."             → Install, use, extend.
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
│   └── handlers.py       # Built-in handlers (task, note, memory)
├── channels/
│   ├── web.py            # FastAPI + WebSocket dashboard
│   └── templates/        # Dashboard, setup wizard, awakening
├── locales/{en,es}/      # Language packs
├── connectors/           # Built-in connector definitions
├── skills/               # Skill definitions (SKILL.md)
├── modules/              # Module templates
└── cli/main.py           # CLI (lumen run, lumen install, lumen status)
```

## Supported Models

| Provider | Model | Tier |
|----------|-------|------|
| DeepSeek | deepseek-chat | tier-2 (recommended) |
| OpenAI | gpt-4o-mini | tier-2 |
| Anthropic | claude-sonnet-4 | tier-3 |
| Ollama | llama3 (local) | tier-1 |

## Roadmap

- [x] Core brain + consciousness + memory
- [x] Web dashboard (UI-First)
- [x] Setup wizard + awakening animation
- [x] Bilingual (en/es)
- [x] Self-awareness (registry + discovery)
- [x] Manifesto
- [ ] MCP client adapter
- [ ] WhatsApp / Telegram channels
- [ ] Module marketplace
- [ ] Docker support
- [ ] Documentation

## License

[MIT](LICENSE) &mdash; Free and open source, forever.

---

<p align="center">
  <em>Built by <a href="https://github.com/gabogabucho">Gabo Urrutia</a></em>
</p>
