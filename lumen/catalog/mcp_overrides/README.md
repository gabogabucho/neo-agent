# MCP Overrides — curated env metadata for third-party MCP servers

The Anthropic MCP Registry does not standardize how a server declares the
environment variables it needs. Each server documents its own requirements
in its README. To give users a consistent chat-driven setup experience,
Lumen maintains curated overlays here.

## File shape

One YAML per server, named `<server_id>.yaml`. The `server_id` matches the
key used in `mcp_config["servers"]`.

```yaml
display_name: "GitHub"             # optional; shown to the user
doc_url: "https://github.com/..."  # optional; surfaced for the manual case
env:
  - name: GITHUB_PERSONAL_ACCESS_TOKEN
    label: "GitHub personal access token"
    hint: "Generate one at https://github.com/settings/tokens (fine-grained works)."
    secret: true
    pattern: "(ghp_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{80,})"
    format_guidance: "Starts with `ghp_` (classic) or `github_pat_` (fine-grained)."
    examples:
      - "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789"
```

## Field semantics

Same as `x-lumen.runtime.env` on native modules:

| Field              | Meaning                                                              |
|--------------------|----------------------------------------------------------------------|
| `name`             | Env var name, as the MCP server expects it                           |
| `label`            | Short human label (shown when asking)                                |
| `hint`             | One-liner guidance (where to get the value)                          |
| `secret`           | Masks the captured value in chat history                             |
| `pattern`          | Regex for validation + single-match extraction                       |
| `format_guidance`  | Human-readable format note                                           |
| `examples`         | 1–2 example values                                                   |
| `expected_type`    | Optional: `text` (default) or `int`                                  |

## Fallback when no overlay exists

If `lumen/catalog/mcp_overrides/<server_id>.yaml` is missing, Lumen falls
back to `server_config["x-lumen-env"]` if the server config declares it,
then to the bare `env` keys (asking the user without validation).

Overlays are community-extensible — drop a YAML here, add a test fixture
if desired, and the chat-setup flow works for that server immediately.
