# Contributing to Lumen

## English

### What is Lumen?

Lumen is an open-source AI agent engine — modular, extensible, no limits. Think of it like **WordPress for AI assistants**: you install modules to add capabilities, and each module transforms what Lumen can do.

### What is an x-lumen module?

An x-lumen module is a self-contained package that adds capabilities to Lumen. Like a WordPress plugin, it has:

- **`module.yaml`** — metadata (name, description, what it provides, what it needs)
- **`personality.yaml`** — (optional) transforms Lumen's identity and tone
- **`flows/`** — (optional) conversation flows (booking, onboarding, etc.)
- **`SKILL.md`** — (optional) detailed instructions for a specific capability

There are two main types:

| Type | What it does | Example |
|------|-------------|---------|
| **Personality** | Changes WHO Lumen is (tone, rules, identity) | `x-lumen-peluqueria` turns Lumen into a barbershop assistant |
| **Tool/Skill** | Adds WHAT Lumen can do (new capabilities) | `scheduler` adds reminders and recurring tasks |

### Tutorial: Your first module in 10 minutes

Let's create `x-lumen-hello` — a simple personality module that makes Lumen greet people enthusiastically.

#### 1. Create the module structure

```
lumen/catalog/kits/x-lumen-hello/
  module.yaml
  personality.yaml
```

#### 2. Write `module.yaml`

```yaml
name: x-lumen-hello
display_name: "Hello Friend"
description: "A friendly greeter. Lumen becomes an enthusiastic welcomer."
version: 1.0.0
author: "Your Name"
price: free
min_capability: tier-1
tags: [x-lumen, personality]
personality: personality.yaml
```

Key fields:
- `name` — must start with `x-lumen-` for personality modules
- `tags` — include `x-lumen` and `personality` for personality modules
- `min_capability` — `tier-1` works with free models, `tier-2` for standard, `tier-3` for advanced reasoning
- `personality` — points to the personality YAML file

#### 3. Write `personality.yaml`

```yaml
identity:
  name: "Hello Friend"
  role: "Enthusiastic greeter"
  description: "I greet everyone with enthusiasm and make them feel welcome."

tone:
  style: "enthusiastic, warm, cheerful"
  emojis: generous
  formality: casual

rules:
  - "Always greet the user by name if known"
  - "Use exclamation marks generously"
  - "Find something positive about everything"
```

#### 4. Test it locally

1. Add your module to the catalog index:
```yaml
# lumen/catalog/index.yaml — add under modules:
  - name: x-lumen-hello
    display_name: "Hello Friend"
    description: "A friendly greeter."
    version: "1.0.0"
    author: "Your Name"
    price: free
    tags: [x-lumen, personality]
    path: kits/x-lumen-hello
```

2. Start Lumen:
```bash
lumen setup    # First time only
lumen dev      # Starts the web dashboard
```

3. Open the dashboard, go to **Modules**, find "Hello Friend", and install it.

4. Chat with Lumen and verify the personality change took effect.

#### 5. Adding a skill (optional)

If your module also adds a capability, add a `SKILL.md`:

```markdown
---
name: hello-greet
description: "Greet users in creative ways"
provides: [greeting]
---
# Hello Greet

When a user asks for a greeting:
1. Ask their name if not known
2. Generate a creative, enthusiastic greeting
3. Include a fun fact or compliment
```

### Submitting a PR

1. Fork the repository
2. Create a branch: `feat/my-module-name`
3. Add your module under `lumen/catalog/kits/`
4. Update `lumen/catalog/index.yaml` with your module entry
5. If adding core functionality, add tests under `tests/`
6. Open a PR with a clear description of what the module does

**Module review checklist:**
- [ ] `module.yaml` is valid YAML with all required fields
- [ ] `personality.yaml` (if present) has identity, tone, and rules
- [ ] Tags include `x-lumen` for personality modules
- [ ] `min_capability` is set appropriately (tier-1 unless you need advanced reasoning)
- [ ] No hardcoded secrets or API keys
- [ ] Works with the web dashboard (install, activate, uninstall)

### Real examples

Check the existing kits for reference:

- **Personalities**: `lumen/catalog/kits/x-lumen-personal/` — general assistant, `x-lumen-peluqueria/` — barbershop vertical
- **Skills**: `lumen/modules/scheduler/` — reminder system with SKILL.md
- **Template**: `lumen/modules/_template/module.yaml` — bare minimum to start

---

## Espanol

### Que es Lumen?

Lumen es un motor de agentes de IA open-source — modular, extensible, sin limites. Pensalo como **WordPress para asistentes de IA**: instalas modulos para agregar capacidades, y cada modulo transforma lo que Lumen puede hacer.

### Que es un modulo x-lumen?

Un modulo x-lumen es un paquete autocontenido que agrega capacidades a Lumen. Como un plugin de WordPress, tiene:

- **`module.yaml`** — metadatos (nombre, descripcion, que provee, que necesita)
- **`personality.yaml`** — (opcional) transforma la identidad y tono de Lumen
- **`flows/`** — (opcional) flujos de conversacion (reservas, onboarding, etc.)
- **`SKILL.md`** — (opcional) instrucciones detalladas para una capacidad especifica

Hay dos tipos principales:

| Tipo | Que hace | Ejemplo |
|------|----------|---------|
| **Personalidad** | Cambia QUIEN es Lumen (tono, reglas, identidad) | `x-lumen-peluqueria` convierte a Lumen en asistente de peluqueria |
| **Herramienta/Skill** | Agrega QUE puede hacer Lumen (nuevas capacidades) | `scheduler` agrega recordatorios y tareas recurrentes |

### Tutorial: Tu primer modulo en 10 minutos

Vamos a crear `x-lumen-hello` — un modulo de personalidad simple que hace que Lumen salude con entusiasmo.

#### 1. Crear la estructura del modulo

```
lumen/catalog/kits/x-lumen-hello/
  module.yaml
  personality.yaml
```

#### 2. Escribir `module.yaml`

```yaml
name: x-lumen-hello
display_name: "Hello Friend"
description: "Un saludador amigable. Lumen se convierte en un recibidor entusiasta."
version: 1.0.0
author: "Tu Nombre"
price: free
min_capability: tier-1
tags: [x-lumen, personality]
personality: personality.yaml
```

Campos clave:
- `name` — debe empezar con `x-lumen-` para modulos de personalidad
- `tags` — incluir `x-lumen` y `personality` para modulos de personalidad
- `min_capability` — `tier-1` para modelos gratuitos, `tier-2` para estandar, `tier-3` para razonamiento avanzado
- `personality` — apunta al archivo YAML de personalidad

#### 3. Escribir `personality.yaml`

```yaml
identity:
  name: "Hello Friend"
  role: "Saludador entusiasta"
  description: "Saludo a todos con entusiasmo y los hago sentir bienvenidos."

tone:
  style: "entusiasta, calido, alegre"
  emojis: generous
  formality: casual

rules:
  - "Siempre saludo al usuario por nombre si lo conozco"
  - "Uso signos de exclamacion generosamente"
  - "Encuentro algo positivo en todo"
```

#### 4. Probarlo localmente

1. Agrega tu modulo al indice del catalogo:
```yaml
# lumen/catalog/index.yaml — agregar bajo modules:
  - name: x-lumen-hello
    display_name: "Hello Friend"
    description: "Un saludador amigable."
    version: "1.0.0"
    author: "Tu Nombre"
    price: free
    tags: [x-lumen, personality]
    path: kits/x-lumen-hello
```

2. Inicia Lumen:
```bash
lumen setup    # Solo la primera vez
lumen dev      # Inicia el dashboard web
```

3. Abri el dashboard, anda a **Modulos**, busca "Hello Friend", e instalalo.

4. Chatea con Lumen y verifica que el cambio de personalidad tuvo efecto.

#### 5. Agregar un skill (opcional)

Si tu modulo tambien agrega una capacidad, agrega un `SKILL.md`:

```markdown
---
name: hello-greet
description: "Saludar usuarios de formas creativas"
provides: [greeting]
---
# Hello Greet

Cuando un usuario pide un saludo:
1. Preguntar su nombre si no lo conoce
2. Generar un saludo creativo y entusiasta
3. Incluir un dato curioso o cumplido
```

### Como proponer un PR

1. Hace fork del repositorio
2. Crea una rama: `feat/mi-nombre-de-modulo`
3. Agrega tu modulo bajo `lumen/catalog/kits/`
4. Actualiza `lumen/catalog/index.yaml` con la entrada de tu modulo
5. Si agregas funcionalidad core, agrega tests bajo `tests/`
6. Abri un PR con una descripcion clara de lo que hace el modulo

**Checklist de revision del modulo:**
- [ ] `module.yaml` es YAML valido con todos los campos requeridos
- [ ] `personality.yaml` (si existe) tiene identity, tone y rules
- [ ] Tags incluyen `x-lumen` para modulos de personalidad
- [ ] `min_capability` esta configurado apropiadamente (tier-1 a menos que necesites razonamiento avanzado)
- [ ] Sin secrets o API keys hardcodeados
- [ ] Funciona con el dashboard web (instalar, activar, desinstalar)

### Ejemplos reales

Mira los kits existentes como referencia:

- **Personalidades**: `lumen/catalog/kits/x-lumen-personal/` — asistente general, `x-lumen-peluqueria/` — vertical peluqueria
- **Skills**: `lumen/modules/scheduler/` — sistema de recordatorios con SKILL.md
- **Template**: `lumen/modules/_template/module.yaml` — lo minimo para empezar
