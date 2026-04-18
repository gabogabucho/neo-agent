# Lumen — Plan de Rediseño Estético

> **Estado:** Planificado, sin ejecutar. Cualquier agente puede continuar desde acá.
> **Fecha:** 2026-04-18
> **Branch sugerida:** `feat/lumen-light-redesign`
> **Driver:** El "redesign" del commit `8933f93` no cumplió el pedido — quedó dark mode con paleta inventada (`#3b82f6`). Hay que rehacerlo desde la identidad de marca.

---

## 1. Concepto rector

> **Lumen significa LUZ.** La estética NO puede ser dark-first. Todo se diseña alrededor del logo (el ojo cristalino azul).

Tres adjetivos que mandan en cada decisión:

1. **CLEAN** — espacios amplios, sin chrome innecesario, una sola jerarquía visual fuerte por pantalla.
2. **AMIGABLE** — tipografía generosa (no densa), bordes suaves, textos en español natural sin jerga dev.
3. **LUZ** — fondos claros como default, el ojo del logo como foco luminoso recurrente.

**No es z.ai.** z.ai fue solo una referencia visual de "limpio". Identidad propia, derivada del logo.

---

## 2. Identidad de marca (extraída del logo)

El logo (`logo.png`) es un ojo de facetas cristalinas en azul profundo con sun-burst central. Los colores se extraen de ahí, no se inventan.

### Paleta canónica

| Token | Hex | Rol |
|---|---|---|
| `--lumen-ink` | `#0f1545` | Texto principal, botones primarios sólidos. El navy del logo. |
| `--lumen-ink-soft` | `#3a3f6e` | Texto secundario, labels |
| `--lumen-mute` | `#8b8fa8` | Texto deshabilitado, placeholders |
| `--lumen-blue` | `#3d3dd6` | Accent primario (faceta media del logo) |
| `--lumen-blue-soft` | `#a4a4f5` | Hover suave, focus rings |
| `--lumen-lavender` | `#e8e8ff` | Backgrounds de selección, badges info |
| `--lumen-coral` | `#ff8a7a` | Acento cálido único (la chispa rosa del iris). Usar SOLO para `personality` badge / highlights especiales |
| `--lumen-paper` | `#fafaf7` | Background principal (light) |
| `--lumen-paper-2` | `#f2f2ec` | Sidebar / panels secundarios (light) |
| `--lumen-line` | `#e6e6df` | Bordes hairline |
| `--lumen-night` | `#0a0d1f` | Background dark mode |
| `--lumen-night-2` | `#141832` | Sidebar dark |
| `--lumen-night-line` | `#1f2347` | Bordes dark |

### Tipografía

- **Familia:** Inter Variable (mantener — ya está cargada, va con la estética clean)
- **Escala:**
  - Hero h1: 56px / weight 600 / tracking -0.02em
  - Display h2: 32px / weight 600
  - Subheading: 18px / weight 500
  - Body: 15px / weight 400 / line-height 1.6
  - Caption: 13px / weight 400 / color `--lumen-mute`
- **Anti-patterns:** nada en `text-xs` (12px) excepto badges. Nada en weight 700 — usamos 600 max (más amigable).

### Espaciado y radios

- Base unit: **8px**
- Radios: `8px` (inputs), `14px` (cards), `999px` (pills/botones), `28px` (modal/hero containers)
- Sombras: una sola (`0 1px 2px rgba(15,21,69,0.04), 0 8px 24px rgba(15,21,69,0.06)`) — sutil, calibrada para light bg

### Modo dark (secundario, default = light)

Mismo sistema, swap de tokens. Implementar con `data-theme="dark"` en `<html>` + variables CSS. **Toggle visible en sidebar footer**, persistencia en `localStorage`. Light es el predefinido si no hay nada guardado.

---

## 3. El logo como elemento vivo

### Reglas

1. **Sacar TODOS los SVG inline de "ojo dibujado a mano"** que están en `dashboard.html` (sidebar logo + iconos del welcome bubble + typing indicator). El usuario los llamó "feos".
2. **Reemplazarlos por el `logo.png`** real (servir desde `lumen/channels/static/logo.png`, exponer ruta en FastAPI).
3. **El "ojo animado"** de [awakening.html](../lumen/channels/templates/awakening.html) (líneas 67–353, animaciones `lightBorn`, `drawEye`, `irisGlow`, `irisReveal`, `pupilReveal`, `reflectionIn`, `nodeLit`) se extrae a un **partial reutilizable** (`templates/_partials/animated_eye.html`) y se usa en:
   - Awakening (ya lo usa)
   - **Avatar del asistente en chat** (versión miniatura, 32px, sin animación o con loop suave)
   - **CLI splash** (ver §6)

### Servir assets estáticos

```python
# lumen/channels/web.py — agregar
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory=PKG_DIR / "channels" / "static"), name="static")
```

Crear `lumen/channels/static/` y mover/copiar `logo.png` ahí. Templates referencian `<img src="/static/logo.png">`.

---

## 4. Cambios pantalla por pantalla

### 4.1 Dashboard (`lumen/channels/templates/dashboard.html`)

**Reescritura completa.** Estructura objetivo:

```
┌───────────────────────────────────────────────────────────┐
│ [logo.png 32px] Lumen           [☀ light/dark] [avatar]  │  ← top bar único, light
├──────────┬────────────────────────────────────────────────┤
│ SIDEBAR  │  CHAT EMPTY STATE                              │
│ (light)  │                                                │
│          │     [eye logo big, sutil]                      │
│ Charlas  │                                                │
│ Memoria  │     Hola, {nombre}                             │  ← h1 56px
│ Módulos  │     ¿Con qué te ayudo hoy?                     │  ← subheading
│ Ajustes  │                                                │
│          │     ┌─────────────────────────────────┐       │
│          │     │ Escribe...                      │  →    │  ← input pill grande
│          │     └─────────────────────────────────┘       │
│ ─────    │                                                │
│ ●Online  │                                                │
└──────────┴────────────────────────────────────────────────┘
```

**Decisiones específicas:**
- Sidebar: `bg: var(--lumen-paper-2)`, sin border-right (separación por color), labels en `--lumen-ink-soft`
- Item activo: pill `bg: var(--lumen-lavender)` + texto `--lumen-ink`, sin barra azul
- Empty state: hero centrado vertical+horizontal, NO bubble pegado a la izquierda
- Mensajes del usuario: pill `bg: var(--lumen-ink)` texto blanco, alineado a la derecha
- Mensajes del asistente: card `bg: white`, sombra sutil, **avatar = ojo animado mini** a la izquierda
- Input: pill grande con `border: 1px solid var(--lumen-line)`, focus → `border: var(--lumen-blue)` + ring suave lavanda

### 4.2 Awakening (`lumen/channels/templates/awakening.html`)

- Cambiar `body { background: #f9fafb }` → `var(--lumen-paper)`
- Cambiar el dot violeta `#7b61ff` por `var(--lumen-blue) #3d3dd6` para coherencia con el logo
- Mantener toda la animación del ojo, solo recolorear strokes y fills al sistema nuevo
- Texto final ("Lumen está lista") en `--lumen-ink`, peso 600, no 200

### 4.3 Setup (`lumen/channels/templates/setup.html`)

- Hoy: `background: #050a18` (dark) — INVERTIR a `var(--lumen-paper)`
- Logo placeholder SVG de las líneas 34–47 → reemplazar por `<img src="/static/logo.png" width="56">`
- Pasos: cards con sombra sutil sobre paper, no boxes azules sobre dark

### 4.4 Marketplace (panel dentro de dashboard.html)

- Cards: `bg: white`, `border: 1px solid var(--lumen-line)`, hover sube sombra sutilmente
- **Mostrar `display_name` del módulo, NUNCA el `name` con prefix `x-lumen-`** (ver §5)
- Badge "Personality" en `--lumen-coral` (la chispa cálida del logo) en vez de purple
- Compatibilidad badges: emojis OK pero con bg `--lumen-lavender` para `ready`, etc. — colores suaves

### 4.5 Config

- Mismo sistema de cards. Agregar al final: **toggle Tema (Luz / Oscuro)**.

---

## 5. `x-lumen-` como tag, no como nombre

**Problema actual:** la UI muestra `x-lumen-comunicacion-telegram` como nombre. Feo.

**Solución:**

### En el backend ([lumen/channels/web.py](../lumen/channels/web.py))

En el endpoint `/api/marketplace` (línea ~808) y al emitir items, asegurarse de que `display_name` esté siempre presente. Si un módulo arranca con `x-lumen-`, el `display_name` se deriva quitando el prefix y title-casing:

```python
def humanize_module_name(name: str, display_name: str | None) -> str:
    if display_name:
        return display_name
    if name.startswith("x-lumen-"):
        return name.removeprefix("x-lumen-").replace("-", " ").title()
    return name.replace("-", " ").title()
```

### En los `module.yaml` de los módulos `x-lumen-*`

Agregar `display_name` explícito a cada uno. Ejemplo en [lumen/catalog/modules/x-lumen-comunicacion-telegram/module.yaml](../lumen/catalog/modules/x-lumen-comunicacion-telegram/module.yaml):

```yaml
name: x-lumen-comunicacion-telegram
display_name: Telegram
tags: [x-lumen, comunicacion, channel]
```

Verificar todos los demás `x-lumen-*` (`dev`, `focus`, `personal`, `peluqueria`, `restaurant`, `scholar`).

### En el frontend (dashboard.html)

`renderKitCard` ya usa `item.display_name || item.name` — eso queda. Tag `x-lumen` se renderiza como pill discreta junto al resto de tags, sin destaque.

---

## 6. Ojo animado en el CLI

Cuando `lumen run` arranca, antes del `Panel` de rich, mostrar una versión ASCII/unicode del "nacimiento del ojo".

**Archivo:** [lumen/cli/main.py](../lumen/cli/main.py) — agregar función `render_eye_boot()` y llamarla desde `run()` antes de `console.print(Panel(...))`.

**Animación sugerida** (3 frames, ~600ms total con `time.sleep(0.2)` entre cada uno):

```
Frame 1:           Frame 2:              Frame 3:
                       ·                  ◌─────◌
       ·            ╭──┴──╮              ╱  ◉  ╲
                    │  ●  │             ◌───────◌
                    ╰──┬──╯              ╲     ╱
                       ·                  ◌───◌
```

Después: `Panel` rich con el texto "Lumen está despierta" + URL del dashboard. Color del Panel: cambiar `border_style="cyan"` por algo derivado de `--lumen-blue` (rich acepta hex: `border_style="#3d3dd6"`).

**Si la terminal no soporta unicode** (detectar `sys.stdout.encoding`): fallback a ASCII puro `(o)` simple sin animación.

---

## 7. Locale es — actualizar labels stale

Archivo: [lumen/locales/es/ui.yaml](../lumen/locales/es/ui.yaml)

Cambios mínimos requeridos para que la UI nueva muestre los labels correctos:

```yaml
dashboard:
  title: "Lumen"                    # quitar "— Panel"
  status: "Memoria"                 # antes: "Estado"
  active: "Lumen activa"            # antes: "Lumen activo" (femenino, coherente con personality.yaml)
  modules: "Módulos"                # antes: "Modulos" (tilde)
  config: "Ajustes"                 # antes: "Configuracion"
  chat: "Charlas"                   # antes: "Chat"
  chat_placeholder: "Escribí lo que necesitás..."   # antes: "Escribe un mensaje..."
  send: "Enviar"
  welcome: "Hola. Soy Lumen, tu asistente. Puedo con tareas, notas y búsquedas. ¿Por dónde empezamos?"
  toggle_sidebar: "Plegar menú"     # nuevo
  theme_light: "Tema luz"           # nuevo
  theme_dark: "Tema oscuro"         # nuevo
```

Hacer el mismo audit en `lumen/locales/en/ui.yaml` para consistencia.

---

## 8. Tareas — checklist ejecutable en orden

> Cada tarea debe quedar en un commit independiente con mensaje convencional. NO mezclar.

### Fase A — Fundamentos (sin tocar visual aún)

- [ ] **A1.** Crear `lumen/channels/static/`, mover una copia de `logo.png` ahí, montar `StaticFiles` en [web.py](../lumen/channels/web.py).
  Commit: `chore(assets): serve static logo via /static mount`
- [ ] **A2.** Crear `lumen/channels/templates/_partials/tokens.html` con `<style>` que define todos los CSS variables de §2 (light + dark vía `data-theme`).
  Commit: `feat(ui): add brand design tokens (light + dark)`
- [ ] **A3.** Crear `lumen/channels/templates/_partials/animated_eye.html` extrayendo SVG + keyframes del awakening. Aceptar param `size` (full / mini).
  Commit: `refactor(ui): extract animated eye to reusable partial`
- [ ] **A4.** Helper `humanize_module_name()` en [web.py](../lumen/channels/web.py) + agregar `display_name` a todos los `module.yaml` de `x-lumen-*`. Verificar que `/api/marketplace` ya no emita el nombre raw.
  Commit: `feat(catalog): treat x-lumen- as tag, surface display_name`

### Fase B — Templates

- [ ] **B1.** Reescribir [setup.html](../lumen/channels/templates/setup.html) con tokens nuevos (light, logo.png, hero centrado).
  Commit: `feat(ui): rebuild setup wizard with brand tokens`
- [ ] **B2.** Reescribir [awakening.html](../lumen/channels/templates/awakening.html) — recolorear al sistema, mantener animación.
  Commit: `feat(ui): awakening uses brand palette around the logo`
- [ ] **B3.** Reescribir [dashboard.html](../lumen/channels/templates/dashboard.html) — empty state hero, sidebar light, top bar, avatar = ojo mini, marketplace cards limpias.
  Commit: `feat(ui): dashboard light-first redesign around the eye`
- [ ] **B4.** Toggle theme light/dark en sidebar footer, persistencia `localStorage`, atributo `data-theme` en `<html>`.
  Commit: `feat(ui): light/dark theme toggle, light is default`

### Fase C — Locale + CLI

- [ ] **C1.** Actualizar [locales/es/ui.yaml](../lumen/locales/es/ui.yaml) y [locales/en/ui.yaml](../lumen/locales/en/ui.yaml) con labels de §7.
  Commit: `i18n: refresh es/en UI labels for new design`
- [ ] **C2.** `render_eye_boot()` en [lumen/cli/main.py](../lumen/cli/main.py) + integrar al `run()`. Cambiar `border_style` del Panel a `#3d3dd6`.
  Commit: `feat(cli): animated eye on lumen run boot`

### Fase D — Verificación

- [ ] **D1.** Smoke: `lumen run` → ver eye en CLI → browser muestra dashboard light → toggle a dark → recargar y persiste → hard refresh limpio.
- [ ] **D2.** Tests: correr `pytest -q`. Si algún test snapshotea HTML, actualizar.
- [ ] **D3.** Screenshots de `/setup`, `/dashboard`, `/dashboard?theme=dark` en `docs/screenshots/` para acta visual del rediseño.

---

## 9. Fuera de scope (separar en otra rama / change)

Estos issues fueron descubiertos en el stress test del 2026-04-18 pero NO son parte de este rediseño. Spawn como tareas separadas:

1. **`POST /api/setup` con body vacío sobrescribe config sin validar.** Bug crítico. Ver memoria engram `bug/lumen-api-stress-test-bugs-found-2026-04-18`.
2. **WebSocket muere con JSON malformado** (no try/except en handler).
3. **Mensaje vacío en chat** (`{"content": ""}`) se acepta y cuelga el typing indicator.

---

## 10. Criterios de aceptación

El rediseño está listo cuando:

- [ ] Abrir `/dashboard` por primera vez muestra fondo claro (paper), no oscuro
- [ ] Ningún SVG de "ojo dibujado a mano" sobrevive en los templates — todo usa `logo.png` o el partial animado
- [ ] El nombre `x-lumen-comunicacion-telegram` no aparece en pantalla en ningún lado — sí su display_name "Telegram" + tag `x-lumen` como pill
- [ ] `lumen run` desde la terminal muestra el eye animation antes del Panel
- [ ] Toggle dark/light funciona y persiste
- [ ] Labels en español: `Charlas / Memoria / Módulos / Ajustes` (no `Estado / Chat / Configuracion`)
- [ ] Welcome message tiene tildes correctas
- [ ] El logo `logo.png` está visible en sidebar, en setup wizard, y como avatar mini del asistente

---

## 11. Referencias rápidas para el próximo agente

- **Logo:** [logo.png](../logo.png) — ojo cristalino azul
- **Paleta extraída:** §2 de este doc (NO inventar otros azules — `#3b82f6` está prohibido)
- **Animación de ojo existente:** [awakening.html:67-353](../lumen/channels/templates/awakening.html)
- **Memoria engram relevante:**
  - `preference/lumen-aesthetic-direction-light-first-z-ai-inspired` — la dirección visual + por qué
  - `bug/lumen-api-stress-test-bugs-found-2026-04-18` — bugs separados a no mezclar acá
- **Branch base:** `feat/comunicacion-modules` (HEAD actual). Crear `feat/lumen-light-redesign` desde ahí.
- **Comando para correr local:** `python -c "from lumen.channels.web import app; import uvicorn; uvicorn.run(app, host='127.0.0.1', port=3000)"` o `lumen run`
- **Preview MCP:** [.claude/launch.json](../.claude/launch.json) ya configurado con `name: "lumen"` apuntando a 3000

---

## 12. Lo que NO hay que hacer

- ❌ Copiar la estética de z.ai 1:1. Era referencia de "limpio", no identidad.
- ❌ Inventar colores nuevos — todo sale del logo.
- ❌ Mantener el dark mode como default. Lumen = LUZ.
- ❌ Mezclar el rediseño con los bug fixes del stress test (§9).
- ❌ Dejar SVG de ojos pintados a mano "porque queda lindo" — el usuario los rechazó.
- ❌ Hacer todo en un solo commit. Cada bullet de §8 es un commit.
