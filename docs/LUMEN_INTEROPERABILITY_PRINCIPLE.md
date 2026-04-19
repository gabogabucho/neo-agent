# Lumen Interoperability Principle

> Native when possible. Adapted by default. Manual only when unavoidable.

## Principle

Lumen should adopt installable capabilities from external ecosystems as naturally as possible. It should not force everything through `x-lumen-*` paths just to feel legitimate.

The goal is simple: if something can live inside Lumen cleanly, Lumen should welcome it; if it needs a thin bridge, Lumen should adapt it; if it still cannot be integrated safely, Lumen should say so explicitly instead of pretending it is native.

## Artifact classes

### 1. Native
- Already fits Lumen's install/runtime model.
- Can be discovered, installed, configured, and explained without translation.
- Example shape: first-party or ecosystem artifacts that already speak Lumen's manifest/runtime contract.

### 2. Adapted
- External by origin, but made natural through a lightweight adapter.
- Default path for ecosystem interoperability.
- The adapter should preserve the external artifact's identity instead of forcing an unnecessary rewrite into `x-lumen-*` naming or packaging.

### 3. Opaque / Manual
- Known by Lumen, but not yet naturally installable or operable.
- Requires manual steps, unsupported transport, or custom operator work.
- This is the fallback, not the design target.

## Product implications

### Marketplace
- Show whether an artifact is native, adapted, or opaque.
- Prefer natural adoption over branding conversion.
- Do not hide external origin; normalize experience, not identity.

### Awareness
- Lumen should know not only that a capability exists, but how directly it belongs to its body.
- Native feels inherent, adapted feels incorporated, opaque feels known-but-not-yet-usable.

### Installation
- Native installs through the normal Lumen path.
- Adapted installs through a bridge that should feel almost identical to native.
- Opaque/manual must be clearly marked as requiring operator intervention.

### Configuration
- Native should use standard Lumen configuration surfaces.
- Adapted should map external config into those surfaces with minimal ceremony.
- Manual should be explicit about what Lumen cannot configure yet.

### Consciousness
- Lumen's consciousness should not pretend every capability was born inside Lumen.
- It should understand whether a capability is native, adapted, or opaque, and explain that naturally when relevant.

## Design bias

When choosing between forcing an ecosystem artifact into a fake native path or adopting it through a clean adapter, choose adaptation.

Lumen becomes stronger by metabolizing external ecosystems, not by renaming them into submission.
