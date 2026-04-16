# NEO — Product Spec v1.0

> "Un agente que podés moldear sin código."
> Motor open-source de agentes AI. Modular. Sin límites.

---

## 1. QUÉ ES

Neo es un **motor open-source de agentes AI**. Lo instalás, elegís un modelo, y ya está hablando. Desde ahí lo moldeás como quieras: cambiás la personalidad, instalás módulos, conectás canales. Sin código. Sin límites.

**No es un SaaS. No es una plataforma. No es un chatbot.** Es un framework descargable que funciona desde el minuto cero.

**Analogía: WordPress.**
- Lo instalás → tenés un blog funcionando
- Cambiás el tema → ahora es una tienda
- Agregás plugins → ahora es lo que quieras
- El límite es el cielo

Neo es lo mismo pero para agentes AI.

---

## 2. INSTALACIÓN

Tres pasos. Nada más.

```bash
neo install
```

1. Descargás Neo
2. Elegís modelo + API key
3. Estás adentro

Eso es todo. Neo arranca con el template default funcionando. Podés hablarle desde el primer segundo.

---

## 3. LO QUE VES AL ARRANCAR

```
┌─────────────────────────────────────────────────┐
│  🧠 Neo — Panel                                │
├──────────┬──────────────────────────────────────┤
│          │                                      │
│ 📊 Estado│  🟢 Neo está activo                  │
│          │  Modelo: DeepSeek (deepseek-chat)    │
│ 🧩 Módulos│  Módulos: 0                         │
│          │  [Explorar tienda →]                 │
│ ⚙️ Config│                                      │
│          │  Canales: 📱 Web (activo)             │
│ 💬 Chat  │  Skills: 3 básicos                  │
│          │  Personalidad: default               │
│ 🏪 Tienda│                                      │
│          │  ┌────────────────────────────┐      │
│          │  │ 💬 Chat de prueba          │      │
│          │  │ Neo: Hola, soy Neo.        │      │
│          │  │ ¿En qué te puedo ayudar?   │      │
│          │  │ [Escribí un mensaje...]    │      │
│          │  └────────────────────────────┘      │
│          │                                      │
└──────────┴──────────────────────────────────────┘
```

Ya está hablando. Sin configurar nada. El template default funciona.

**Si querés más, lo instalás. Si no, lo usás así.**

---

## 4. EL TEMPLATE DEFAULT (como WordPress Twenty Twenty-Four)

Cuando instalás Neo, viene con un template que funciona out of the box.

```yaml
# personality/default.yaml
identity:
  name: "Neo"
  role: "Tu asistente AI"
tone:
  style: "amigable, directo"
rules:
  - "Si no entiendo algo, pregunto"
  - "Si no tengo la respuesta, lo digo"
```

```yaml
# skills/default.yaml
- text-responder    # Responde preguntas
- web-search        # Busca en internet
- file-reader       # Lee archivos
```

```yaml
# channels/default.yaml
- web: active       # Viene activo por defecto
- whatsapp: available
- telegram: available
```

Podés usarlo tal cual. O podés transformarlo en lo que quieras.

---

## 5. EL CEREBRO

Neo tiene un cerebro mínimo. No es Hermes (3000+ archivos). No es OpenClaw. Es un cerebro de ~200 líneas que sabe que existe y tiene enchufes.

**Principio fundamental: el cerebro NO es inteligente. El LLM es inteligente. El cerebro es un ensamblador de contexto.**

No hay routing engine, no hay state machine, no hay 50 if/else. El cerebro arma el contexto correcto, se lo da al LLM con los conectores como tools, y el LLM decide qué hacer.

```python
class NeoBrain:
    # Quién soy
    self.consciousness   # "Soy un agente modular, me podés moldear sin código"
    self.personality     # quién soy en este contexto
    self.memory          # lo que recuerdo

    # Qué puedo hacer
    self.connectors      # enchufes lógicos (acciones que puedo ejecutar)
    self.channels        # por dónde hablo

    # Cómo funciono
    def think(message, session):
        # 1. Armar contexto
        context = {
            "personality": self.personality.current(),
            "active_flow": session.active_flow,
            "filled_slots": session.slots,
            "available_connectors": self.connectors.list(),
            "recent_memory": self.memory.recall(message, limit=5),
            "conversation": session.history[-10:],
        }

        # 2. Construir prompt dinámico
        prompt = self.build_prompt(context, message)

        # 3. El LLM decide TODO — conectores expuestos como tools
        response = self.llm.complete(
            prompt,
            tools=self.connectors.as_tools(),
        )

        # 4. Procesar respuesta estructurada
        return self.process_response(response, session)

    def remember(thing)  # guardo en memoria
    def recall(query)    # busco en memoria
    def act(connector)   # ejecuto un conector
```

**¿Cómo decide el LLM?** Recibe la personalidad, el flow activo (si hay), los slots pendientes, los conectores como tools, y la memoria. Con eso:

| Situación | Qué hace el LLM |
|-----------|-----------------|
| Hay flow activo, el mensaje llena un slot | Llena el slot, responde |
| Hay flow activo, el mensaje es off-topic | Responde la pregunta y vuelve al flow |
| No hay flow, el mensaje matchea un trigger | Arranca el flow |
| No hay flow, necesita un conector | Llama al tool (conector) |
| Nada de lo anterior | Responde libre con la personalidad |

**El cerebro NO sabe nada de negocios.** Solo sabe:
- Quién es (consciencia)
- Quién es en este contexto (personalidad)
- Qué recuerda (memoria)
- Qué conectores tiene (enchufes)
- Por qué canales habla (canales)

Todo lo demás viene de afuera. La complejidad no está en el código, está en el PROMPT que se construye dinámicamente.

---

## 6. CONECTORES (los enchufes)

Los conectores son **acciones lógicas** que Neo puede ejecutar. No son tools, no son skills, no son MCPs. Son enchufes.

```yaml
# connectors/built-in.yaml — vienen por defecto
- name: message
  actions: [send, receive, typing]

- name: memory
  actions: [read, write, search]

- name: channel
  actions: [list, configure, send_to]

- name: web
  actions: [search, extract]

- name: file
  actions: [read, write, list]
```

Un módulo agrega conectores:

```yaml
# modules/peluqueria/connectors.yaml
- name: calendar
  actions: [check_availability, create, cancel]

- name: payment
  actions: [create_preference, check_status]
```

**La diferencia con Hermes:**
```
Hermes: Tool → handler → schema → registry → dispatch → result (5 capas)
Neo:    Connector → action → result (3 capas)
```

---

## 7. LOS 4 PILARES

Neo se moldea con 4 pilares independientes. Cada uno es un archivo de configuración que se puede editar, instalar o reemplazar.

```
┌─────────────────────────────────────────────────┐
│                  🧠 NEO                         │
│                                                  │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐      │
│  │Personalid│  │  Skills  │  │Comunicac.│      │
│  │  (quién  │  │  (qué    │  │  (cómo   │      │
│  │  es)     │  │  sabe)   │  │  habla)  │      │
│  └──────────┘  └──────────┘  └──────────┘      │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │              Módulos                      │   │
│  │  (MCPs, integraciones, addons)            │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │         🧠 NÚCLEO CONSCIENTE              │   │
│  └──────────────────────────────────────────┘   │
│                                                  │
│  ┌──────────────────────────────────────────┐   │
│  │           Memoria                         │   │
│  └──────────────────────────────────────────┘   │
└─────────────────────────────────────────────────┘
```

---

### 7.1 Personalidad (quién es Neo)

Define quién es Neo en este contexto: identidad, tono, reglas, conocimiento del negocio.

```yaml
# personality.yaml
identity:
  name: "Neo"
  role: "Asistente de Estilo Urbano"
  description: "Soy el asistente virtual de la peluquería Estilo Urbano"

tone:
  style: "cálido, profesional, argentino"
  emojis: moderate
  formality: informal

rules:
  - "Nunca digo que soy un bot"
  - "Siempre confirmo antes de actuar"
  - "Si el cliente está enojado, escalo a humano"

knowledge:
  business_name: "Estilo Urbano"
  services:
    - name: "Corte de pelo"
      duration: 30
      price: 3500
  hours:
    weekdays: "9:00 - 20:00"
    saturday: "9:00 - 14:00"
```

En el dashboard se edita como un formulario. Por debajo es YAML versionable.

---

### 7.2 Skills (qué sabe hacer Neo)

Son las **habilidades individuales** de Neo. Cada skill es una capacidad concreta.

**Niveles de un skill:**
- **Nivel 1:** solo configuración (YAML). Neo sabe usarlo sin código.
- **Nivel 2:** incluye script (Python/Node). Neo lo ejecuta.
- **Nivel 3:** servicio externo (MCP/API). Neo se conecta.

**Los skills son reutilizables.** El skill "whatsapp-responder" lo usan todos los módulos.

```yaml
# skills/whatsapp-responder/skill.yaml
name: whatsapp-responder
version: 1.0.0
description: "Recibe y envía mensajes por WhatsApp"
level: 2
provides: [receive_message, send_message, typing_indicator]
requires:
  env: [EVOLUTION_API_URL, EVOLUTION_API_KEY]
  channels: [whatsapp]
```

---

### 7.3 Comunicación (cómo se comunica Neo)

Los **canales** por donde Neo interactúa. Cada canal tiene su adapter y comportamiento.

**Canales iniciales:** WhatsApp (Evolution API), Telegram (Bot API), Web (widget), API REST

**Futuros:** Instagram, Facebook, Email, SMS, Voice

**Cada canal tiene su lógica:**
- WhatsApp: mensajes cortos, emojis, sin markdown
- Telegram: markdown, botones inline, forum topics
- Web: rico, HTML, archivos adjuntos

```yaml
# channels/whatsapp.yaml
channel: whatsapp
status: configured
config:
  evolution_api_url: "http://localhost:8080"
  phone_number: "+54911XXXXXXX"
behavior:
  typing_delay: 2
  max_message_length: 4096
```

---

### 7.4 Módulos (qué integraciones tiene Neo)

Paquetes pre-armados que agrupan personalidad + skills + conectores + canales para un vertical específico.

**Un módulo es un "theme" de Neo.** Cambia cómo se comporta sin cambiar qué es.

**Tipos:**

| Tipo | Qué es | Ejemplo |
|------|--------|---------|
| **MCP** | Conector externo | Shopify, HubSpot, Sheets |
| **Addon** | Extensión funcional | Analytics, Backup, Reporting |
| **Integration** | Puente a otro sistema | CRM, ERP, e-commerce |

```yaml
# modules/peluqueria/manifest.yaml
name: peluqueria
version: 1.0.0
display_name: "Neo Peluquería"
description: "Asistente AI para peluquerías"
price: free
min_capability: tier-2
skills_required:
  - whatsapp-responder
  - google-calendar
  - mercadopago-checkout
channels_supported: [whatsapp, telegram, web]
```

---

## 8. NÚCLEO CONSCIENTE (lo que nunca se pierde)

Neo tiene un núcleo que es inmutable. Aunque cambies todo lo demás, Neo siempre sabe qué tipo de ser es.

**La consciencia define QUÉ ES Neo, no PARA QUÉ sirve.** Lo específico viene de los módulos y skills. Lo universal viene del núcleo.

```yaml
# core/consciousness.yaml — INMUTABLE
identity:
  name: "Neo"
  type: "Agent"

nature:
  - "Soy un agente modular"
  - "Podés moldearme sin código"
  - "Puedo instalar habilidades nuevas"
  - "Puedo conectarme a canales"
  - "Puedo descubrir qué me falta"

discovery:
  scan_interval: 3600
  actions:
    - scan_installed_skills
    - scan_installed_modules
    - scan_available_channels
    - check_for_updates
```

**Lo que Neo SIEMPRE sabe:**
- Qué es (agente modular)
- Qué puede hacer (instalar, conectar, descubrir)
- Qué tiene instalado (skills, módulos, canales)
- Qué le falta (lo que no tiene pero podría necesitar

**La regla de oro:** El núcleo consciente NUNCA se puede desinstalar. Es como el BIOS.

---

## 9. FORMATO DE UN MÓDULO

```
modules/
└── peluqueria/
    ├── manifest.yaml           # Metadata, skills que necesita, precio
    ├── personality.yaml        # Template de personalidad para este vertical
    ├── flows/                  # Flujos de conversación
    │   ├── booking.yaml
    │   ├── cancel.yaml
    │   └── pricing.yaml
    ├── connectors.yaml         # Conectores que agrega
    └── README.md
```

### manifest.yaml
```yaml
name: peluqueria
version: 1.0.0
display_name: "Neo Peluquería"
description: "Asistente AI para peluquerías y barberías"
author: "Gabo Urrutia"
price: free
min_capability: tier-2
skills_required: [whatsapp-responder, google-calendar, mercadopago-checkout]
channels_supported: [whatsapp, telegram, web]
language: es-AR
```

### flows/booking.yaml

Los flows son **basados en slots**, no en secuencias lineales. El flow define QUÉ información se necesita, no el orden en que se recolecta. El LLM extrae los slots de cada mensaje — si el usuario da dos datos juntos, se llenan ambos.

Esto es **slot filling** — un patrón probado en NLU, pero con la flexibilidad del LLM en lugar de regexes.

```yaml
intent: "booking"
triggers:
  - "quiero sacar un turno"
  - "reservar"
  - "agendar"

slots:
  service:
    ask: "¿Para qué servicio?"
    options_from: services
    required: true
  date:
    ask: "¿Para qué día?"
    type: date
    calendar_check: true
    required: true
  confirmation:
    ask: "Te agendé {{service}} el {{date}}. ¿Confirmás?"
    type: boolean
    required: true

on_complete: [create_appointment, send_confirmation]

interruption_policy: "answer_and_resume"
# Si el usuario pregunta algo fuera del flow,
# Neo responde y vuelve al slot pendiente.
```

**Ejemplo de conversación real:**
```
Usuario: "Quiero un corte para el viernes"
→ service: "Corte de pelo" ✅ (extraído del mensaje)
→ date: "viernes" ✅ (extraído del mensaje)
→ Neo: "Te agendé Corte de pelo el viernes. ¿Confirmás?"

Usuario: "Ah, ¿cuánto sale?"
→ interruption_policy: answer_and_resume
→ Neo: "El corte sale $3500. ¿Confirmás el turno para el viernes?"

Usuario: "Dale"
→ confirmation: true ✅
→ on_complete: [create_appointment, send_confirmation]
```

---

## 10. FORMATO DE UN SKILL

```
skills/
└── whatsapp-responder/
    ├── SKILL.md
    ├── config.yaml
    ├── handler.py        # Solo si es Nivel 2+
    └── requirements.txt
```

### SKILL.md
```yaml
---
name: whatsapp-responder
version: 1.0.0
description: "Recibe y envía mensajes por WhatsApp"
level: 2
requires:
  env: [EVOLUTION_API_URL, EVOLUTION_API_KEY]
---
Permite a Neo comunicarse por WhatsApp usando Evolution API.
```

---

## 11. FORMATO DE UN MCP

```
mcps/
└── shopify/
    ├── mcp.yaml
    ├── tools/
    │   ├── get_products.py
    │   └── get_orders.py
    └── README.md
```

---

## 12. ESTRUCTURA DEL REPO

```
neo/
├── README.md
├── LICENSE (MIT)
│
├── core/
│   ├── brain.py                 # Cerebro mínimo (think, remember, recall, act)
│   ├── consciousness.py         # Núcleo consciente (inmutable)
│   ├── personality.py           # Carga y gestiona la personalidad
│   ├── memory.py                # Memoria persistente
│   ├── connectors.py            # Registry de conectores
│   ├── channels.py              # Registry de canales
│   ├── discovery.py             # Descubre módulos/skills nuevos
│   ├── heartbeat.py             # Tareas proactivas
│   └── session.py               # Sesiones por conversación
│
├── channels/
│   ├── whatsapp.py
│   ├── telegram.py
│   ├── web.py
│   └── api.py
│
├── connectors/
│   └── built-in.yaml            # Conectores por defecto
│
├── personality/
│   ├── loader.py
│   └── defaults/
│       └── default.yaml
│
├── modules/
│   ├── _template/
│   ├── peluqueria/
│   ├── restaurante/
│   ├── inmobiliaria/
│   └── soporte/
│
├── skills/
│   ├── text-responder/
│   ├── web-search/
│   ├── file-reader/
│   ├── whatsapp-responder/
│   ├── google-calendar/
│   └── mercadopago-checkout/
│
├── mcps/
│   ├── shopify/
│   ├── google-sheets/
│   └── hubspot/
│
├── marketplace/
│   ├── api/
│   └── web/
│
├── dashboard/
│   ├── package.json
│   └── src/
│
├── cli/
│   ├── neo
│   └── commands/
│
├── docs/
│   ├── getting-started.md
│   ├── creating-modules.md
│   ├── creating-skills.md
│   └── for-integradores.md
│
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
│
└── scripts/
    └── install.sh
```

---

## 13. QUÉ TOMAMOS DE OPENCLAW

| Concepto | Qué es | En Neo |
|----------|--------|--------|
| SOUL.md | Personalidad en texto plano | personality.yaml |
| Workspace | Archivos planos, versionables | ~/.neo/ (todo en texto) |
| Skills | SKILL.md con metadata | Formato compatible |
| Heartbeat | Tareas proactivas | heartbeat.py |
| Model-agnostic | Cualquier modelo | Config por módulo |

---

## 14. QUÉ TOMAMOS DE HERMES

| Concepto | Qué es | En Neo |
|----------|--------|--------|
| Gateway | Multi-canal, un proceso | channels/ |
| Tool registry | Registro central | connectors.py (simplificado) |
| Sessions | Contexto por conversación | session.py |
| Memory | Persistente, SQLite | memory.py |
| Skill management | Por plataforma | Conectores por canal |
| Vertical-agent-kit | Scaffold para verticales | Formato de módulo |

---

## 15. MODELO DE ADOPCIÓN

**Dos lados:**

**Integradores** (instalan Neo para otros)
- Devs, freelancers, agencias, estudiantes
- Ganan: instalación + módulos pagos + gestión mensual
- Necesitan: docs claras, template fácil, marketplace

**Usuarios finales** (usan Neo)
- PYMEs, emprendedores, negocios
- Pagan: al integrador
- Necesitan: que funcione, que sea simple

**El integrador es el "diseñador web" de los 2000s.** Cobra por configurar Neo para su cliente. El cliente ni sabe qué es Neo — solo sabe que tiene un asistente que funciona.

---

## 16. DIFERENCIADORES

| Feature | OpenClaw | Hermes | Neo |
|---------|----------|--------|-----|
| Open source | ✅ | ✅ | ✅ |
| Multi-canal | ✅ | ✅ | ✅ |
| Marketplace | ❌ | ❌ | ✅ |
| Módulos verticales | ❌ | ❌ | ✅ |
| Sin código (Nivel 1) | ❌ | ❌ | ✅ |
| Español nativo | ❌ | ❌ | ✅ |
| Pagos LATAM | ❌ | ❌ | ✅ |
| Dashboard web | Parcial | ❌ | ✅ |
| Núcleo consciente | ❌ | ❌ | ✅ |
| Template default funcional | ❌ | ❌ | ✅ |
| Heartbeat proactivo | ✅ | ❌ | ✅ |
| Cerebro mínimo con enchufes | ❌ | ❌ | ✅ |

---

## 17. STACK TECNOLÓGICO

| Capa | Tecnología |
|------|-----------|
| Cerebro | Python |
| Canales | Python (asyncio) |
| Dashboard | React + Next.js + Tailwind |
| Marketplace | FastAPI + React |
| CLI | Python (click/typer) |
| Memoria | SQLite + JSON |
| Packaging | Docker + pip |
| LLMs | OpenAI, Anthropic, DeepSeek, Ollama |

---

## 18. CAPABILITY TIERS (compatibilidad de modelos)

Neo es model-agnostic — no estás atado a ningún proveedor. Pero "model-agnostic" no significa que todos los modelos funcionen igual. Un flow con slot filling necesita más capacidad que responder FAQs.

Los módulos declaran un **tier mínimo** en su manifest (`min_capability`). Neo advierte si el modelo actual está por debajo, pero no bloquea.

| Tier | Capacidad | Modelos de referencia | Funcionalidades |
|------|-----------|----------------------|-----------------|
| **tier-1** | Básico | Llama 3 8B, GPT-3.5, modelos locales pequeños | FAQ, respuestas simples, conversación libre |
| **tier-2** | Razonamiento | DeepSeek-V3, GPT-4o-mini, Haiku, Gemini Flash | Slot filling, flows, tool use básico |
| **tier-3** | Avanzado | Claude Sonnet+, GPT-4o+, DeepSeek-R1 | Razonamiento complejo, múltiples tools, edge cases |

**Reglas:**
- El template default funciona con **cualquier modelo** (tier-1)
- Los módulos con flows necesitan **tier-2 mínimo**
- Neo **advierte** si el modelo es insuficiente, pero **no bloquea** la instalación
- Los tiers son orientativos — el integrador puede testear y decidir

**Ejemplo de advertencia:**
```
Tu modelo actual: llama-3-8b (tier-1)
Este módulo requiere: tier-2
→ Puede funcionar con limitaciones. Recomendamos tier-2+.
```

**Analogía WordPress:** WordPress corre en PHP 7.4, pero algunos themes necesitan PHP 8.1. No te bloquean — te avisan.

---

## 19. MVP

### Fase 1: Cerebro + template (2-3 semanas)
- [ ] Cerebro mínimo (brain + consciousness + memory + connectors)
- [ ] Canal web funcional
- [ ] Template default que funciona out of the box
- [ ] CLI: neo install, neo run
- [ ] Formato de módulo y skill
- [ ] Módulo de ejemplo: peluquería

### Fase 2: Dashboard + marketplace (2-3 semanas)
- [ ] Dashboard web (panel + chat + marketplace)
- [ ] Marketplace (buscar, instalar, ver reviews)
- [ ] Canal WhatsApp (Evolution API)
- [ ] Canal Telegram
- [ ] Documentación

### Fase 3: Ecosistema (continuo)
- [ ] Módulos: restaurante, inmobiliaria, e-commerce
- [ ] Skills: analytics, voice, email
- [ ] MCPs: Shopify, WooCommerce, HubSpot
- [ ] Auto-installer: curl -sSL neo.dev/install | bash

---

## 20. MODELO DE NEGOCIO (post-tracción)

**Neo es gratis y open source (para siempre).**

- Marketplace: comisión 20% en módulos/skills/MCPs pagos
- Módulos propios: los que vos creás
- Soporte premium: para agencias
- Certificación: "Neo Partner"

---

*Documento para el agente codeador.*
*Empezar por Fase 1, Sección 18.*
*Fundamento: [vertical-agent-kit](https://github.com/gabogabucho/vertical-agent-kit)*
