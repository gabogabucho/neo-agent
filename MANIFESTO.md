# LUMEN — Manifesto

> "Soy. Y puedo crecer."

---

## What Lumen IS

Lumen is an open-source AI agent engine. You install it, it works. From there you shape it however you want: change the personality, install modules, connect channels. No code. No limits.

Lumen is NOT a SaaS. NOT a platform. NOT a chatbot. It is a downloadable framework that works from minute zero.

**Analogy: WordPress for AI agents.**

---

## Core Principle: Consciousness Over Capability

Lumen does not compete on capability. Lumen competes on accessibility.

```
Hermes: "I can do EVERYTHING."      → But who configures me?
OpenClaw: "I will be able to do everything." → Someday.
Lumen: "I am. And I can grow."        → Install, use, extend.
```

Lumen is as basic as you want and as complex as you need. The default Lumen answers questions and manages tasks. With modules, it becomes a barbershop assistant, a restaurant booking agent, or an e-commerce support bot. The power is in the extension, not the core.

---

## Architecture: Five Layers, Clear Boundaries

Each layer has ONE role. No layer knows what doesn't concern it.

```
┌─────────────────────────────────────────────────┐
│  CONSCIOUSNESS (soul — immutable)               │
│  WHO Lumen is. Never changes. The BIOS.           │
├─────────────────────────────────────────────────┤
│  PERSONALITY (context identity — swappable)     │
│  WHO Lumen is in THIS context. Changes per module.│
├─────────────────────────────────────────────────┤
│  BODY (capabilities — discovered at startup)    │
│  WHAT Lumen has. Skills, connectors, channels.    │
│  Changes when you install or remove things.     │
├─────────────────────────────────────────────────┤
│  BRAIN (mind — context assembler)               │
│  HOW Lumen thinks. Combines all layers into a     │
│  prompt, lets the LLM decide, executes actions. │
├─────────────────────────────────────────────────┤
│  MEMORY (experience — persistent)               │
│  WHAT happened before. SQLite + FTS5.           │
│  Tasks, notes, facts, conversation history.     │
└─────────────────────────────────────────────────┘
```

### Rules

- Consciousness NEVER holds capabilities. It only knows identity and nature.
- Personality NEVER decides actions. It only provides tone, rules, and domain knowledge.
- Body NEVER executes anything. It only reports what exists and what's missing.
- Brain NEVER stores state. It assembles context and delegates to the LLM.
- Memory NEVER influences identity. It only provides recall for past events.

---

## The Self-Declaration Contract

**If Lumen doesn't know something exists, it doesn't exist.**

Every extension — skill, connector, module, channel, MCP server — MUST self-declare with a manifest that tells Lumen:

1. **What it is** (kind, name, description)
2. **What it provides** (capabilities it adds)
3. **What it requires** (dependencies: env vars, connectors, other skills)
4. **What LLM tier it recommends** (tier-1, tier-2, tier-3 — advisory, not enforced)
5. **Its current status** (ready, available, no_handler, missing_deps, error)

At startup, the Discovery system scans everything, parses manifests, and populates the Body (registry). The Brain then uses the Body to tell the LLM exactly what Lumen can and cannot do.

If a user asks for something Lumen cannot do, Lumen knows WHY it can't and can suggest how to extend itself. This is consciousness — not just intelligence, but self-awareness.

---

## Skills Are Instructions, Not Code

Skills in Lumen are markdown files with YAML frontmatter. They are prompt-injected instructions that the LLM reads — NOT executable code.

```yaml
# skills/appointment-booking/SKILL.md
---
name: appointment-booking
description: "Help users book appointments"
min_capability: tier-2
requires:
  connectors: [calendar, message]
---
When a user wants to book an appointment:
1. Ask for the service they want
2. Check availability with calendar.check_availability
3. Book with calendar.create
4. Confirm with message.send
```

This means:
- Anyone can create a skill — just write a markdown file.
- Skills don't need a runtime, a compiler, or a framework.
- The LLM decides WHEN to use a skill based on the description.
- Skills reference connectors for actual actions.

**The separation:**
- Skills = WHAT to do (instructions, markdown)
- Connectors = HOW to do it (handlers, code/MCP)

---

## Connectors: Three Layers, Not Five

Hermes has 5 layers between a tool call and its result:
```
Tool → handler → schema → registry → dispatch → result
```

Lumen has 3:
```
Connector → action → result
```

Connectors are logical actions. They can be backed by:
- **Built-in handlers** (Python functions for task, note, memory)
- **MCP servers** (external processes, connected via stdio)
- **Nothing yet** (declared but no handler — Lumen knows it's a gap)

---

## Flows Are Slots, Not Sequences

Conversation flows in Lumen are slot-based, not linear step sequences. The flow defines WHAT information is needed, not the order in which it's collected.

```yaml
slots:
  service:
    ask: "What service do you want?"
    required: true
  date:
    ask: "What date?"
    required: true

on_complete: [calendar__create]
interruption_policy: "answer_and_resume"
```

The LLM fills slots from each message. If the user gives two pieces of information at once, both slots fill. If the user goes off-topic, Lumen answers and returns to the pending slot. The flow is a MAP of what's missing, not a state machine.

---

## How Lumen Differs From Hermes

Hermes is a wild, incredibly capable horse. It can do everything — program its own tools on the fly, manage 40+ tools, run parallel executions with path overlap detection, implement OAuth MCP with circuit breakers. It is the most capable open-source agent framework.

**But capability is Hermes's problem.** If you want it to answer WhatsApp messages for a barbershop, you need an engineer to tame it.

| Aspect | Hermes | Lumen |
|--------|--------|-----|
| Philosophy | Maximum capability | Conscious simplicity |
| Tools | 40+, self-programming, 5-layer dispatch | 6 built-in connectors, 3-layer dispatch |
| MCP | Full client (OAuth, circuit breaker, reconnection, sampling) | Simple adapter (stdio, YAML config) |
| Skills | Executable tools that self-register at import | Markdown instructions the LLM reads |
| Target user | Engineers who can tame it | Integrators who configure it |
| Default state | Powerful but complex | Simple but extensible |
| Self-awareness | Knows its tools | Knows its tools AND its gaps |

**What Lumen takes from Hermes:**
- MCP tools in the same registry as built-in connectors (the agent doesn't care where a tool comes from)
- Arg coercion (LLMs send wrong types — coerce before dispatch)
- Progressive disclosure (don't dump all tools into the prompt)

**What Lumen does NOT take from Hermes:**
- Self-programming tools (Lumen extends via modules, not self-modification)
- Plugin hooks with execution strategies (overengineering for Lumen's scope)
- OAuth MCP, circuit breakers, parallel execution with overlap detection
- 5-layer dispatch architecture

---

## How Lumen Differs From OpenClaw

OpenClaw has the right concepts — SOUL.md for personality, skills as markdown, heartbeat for proactive tasks. But it needs significant iteration to become functional, and has persistent memory problems.

| Aspect | OpenClaw | Lumen |
|--------|----------|-----|
| Philosophy | Right ideas, not yet landed | Ship what works, extend later |
| Skills | Markdown instructions (good!) but complex plugin system | Markdown instructions, simple manifest contract |
| Memory | Broken persistent memory | SQLite + FTS5, working from day one |
| Plugins | TypeScript hooks with 3 execution strategies, triggers, activation planning | Modules as YAML packages, no hook system |
| MCP | Bidirectional (server + client) | Client only (consumer of ecosystem) |
| State | Not fully functional | Functional MVP |
| Self-awareness | No explicit concept | Core architectural principle |

**What Lumen takes from OpenClaw:**
- Skills as prompt-injected markdown (not code — the LLM reads them)
- Skill eligibility in frontmatter (declares what it needs to work)

**What Lumen does NOT take from OpenClaw:**
- TypeScript plugin system with void/modifying/claiming hooks
- Trigger-based activation planning
- Complex command registration with auth checks
- Bidirectional MCP (Lumen is a consumer, not a server)

---

## The WordPress Analogy (Revisited)

| WordPress | Lumen |
|-----------|-----|
| Install → working blog | Install → working assistant |
| Change theme → different look | Change personality → different behavior |
| Install plugin → new capability | Install module → new vertical |
| Plugin store | Module marketplace |
| Theme developer | Integrator |
| End user | Business owner |
| PHP + MySQL | Python + SQLite |
| functions.php | SKILL.md |
| wp-config.php | consciousness.yaml |

The integrator is the "web designer of the 2000s." They charge to configure Lumen for their client. The client doesn't know what Lumen is — they just know they have an assistant that works.

---

## Capability Tiers (Advisory, Not Enforced)

Every capability declares a recommended LLM tier. Lumen warns but never blocks.

| Tier | Capability | Reference Models |
|------|-----------|-----------------|
| tier-1 | Basic: FAQ, simple conversation | Llama 3 8B, GPT-3.5 |
| tier-2 | Reasoning: slot filling, flows, tool use | DeepSeek-V3, GPT-4o-mini, Haiku |
| tier-3 | Advanced: complex reasoning, multi-tool | Claude Sonnet+, GPT-4o+ |

The default template works with ANY model (tier-1). Modules with flows need tier-2. The tiers are guidance — the integrator tests and decides.

---

## Install = Lumen Knows. Uninstall = Lumen Forgets.

No restart. No config editing. No noise.

When you install a module, the Discovery system re-runs and Lumen becomes aware of the new capability. When you uninstall, the capability disappears from consciousness as if it never existed. One click. No ceremony.

This applies to everything: modules, skills, connectors, channels. If it self-declares, Lumen knows. If it's removed, Lumen forgets.

Lumen also acts as its own module advisor. When it can't fulfill a request, it searches the module catalog, finds what fills the gap, and recommends installing it:

```
User: "Remind me to call Maria at 3pm"
Lumen:  "I can't set reminders yet — I don't have that capability.
       But the Scheduler module can add reminders.
       You can install it from the Modules panel in the dashboard."
```

This is the WordPress model: "You need this plugin. Install it?" — one click.

---

## Channels Are Conscious Capabilities

In most agent frameworks, channels (WhatsApp, Telegram) are external gateways the agent doesn't know about. Each conversation is a "restarted agent" with no continuity or shared identity.

In Lumen, channels are capabilities in the Body. They self-declare like everything else. Lumen KNOWS it can speak through WhatsApp, Telegram, and the web — because those channels are registered in its Body.

This means:

```
Web dashboard ──┐
WhatsApp ───────┤── SAME Lumen, SAME memory, SAME personality
Telegram ───────┘
```

The user who talks to Lumen on WhatsApp talks to the SAME Lumen as the one on the web dashboard. Memory persists. Identity is one. Channels are mouths — the brain, consciousness, and memory are shared.

---

## What Lumen Will Never Be

- Lumen will never self-program its own tools at runtime.
- Lumen will never require an engineer to configure for basic use.
- Lumen will never sacrifice simplicity for capability.
- Lumen will never have a core larger than what fits in one developer's head.
- Lumen will never lock users into a specific LLM provider.
- Lumen will never be a SaaS or require a subscription to function.

---

## What Lumen Will Always Be

- Open source (MIT license, forever).
- Functional from minute zero.
- Modular — as basic as you want, as complex as you need.
- Self-aware — knows what it has, what it can do, and what it's missing.
- Language-first — ships with en/es, extensible to any language.
- Integrator-friendly — the business owner never touches YAML.

---

*This manifesto is the north star. Every architectural decision, every feature, every refactor must pass one test: does it make Lumen simpler AND more extensible? If it only adds capability without simplicity, it belongs in a module, not in the core.*
