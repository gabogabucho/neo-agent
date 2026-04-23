# Structured Output Convention

Lumen supports a convention for emitting structured UI blocks within normal text responses. This allows frontends to render rich components (cards, tables, forms) based on the agent's response.

## How It Works

1. The **personality** instructs the LLM to emit structured blocks using `<agent-ui>` XML tags
2. Lumen passes the response as **plain text** — it does NOT parse or validate these blocks
3. The **frontend** is responsible for parsing the tags and rendering UI components

## Convention Format

Wrap structured JSON in `<agent-ui>` tags within the text response:

```
Here are your options:

<agent-ui>{"version":"v1","cards":[{"title":"Option A","description":"Basic plan"},{"title":"Option B","description":"Pro plan"}]}</agent-ui>

Let me know which one you prefer!
```

## Version Schema

```json
{
  "version": "v1",
  "cards": [
    {
      "title": "string",
      "description": "string",
      "image?": "url",
      "action?": "string"
    }
  ],
  "table?": {
    "headers": ["col1", "col2"],
    "rows": [["val1", "val2"]]
  },
  "form?": {
    "fields": [
      {"name": "email", "type": "email", "label": "Email", "required": true}
    ]
  }
}
```

## Adding to a Personality

In your personality YAML, add a hint:

```yaml
structured_output:
  enabled: true
  tag: agent-ui
  version: v1
  hint: "When presenting options, data comparisons, or form-like inputs, wrap structured data in <agent-ui> tags."
```

## Frontend Parsing

```javascript
function parseAgentUI(text) {
  const regex = /<agent-ui>(.*?)<\/agent-ui>/gs;
  const matches = [];
  let match;
  while ((match = regex.exec(text)) !== null) {
    try {
      matches.push(JSON.parse(match[1]));
    } catch (e) {
      // Invalid JSON — skip
    }
  }
  return { text: text.replace(regex, ''), ui: matches };
}
```

## Important Notes

- **Lumen never parses these tags** — they flow through as plain text
- The personality decides when to emit structured blocks
- The frontend decides how to render them
- Invalid JSON inside tags is the frontend's responsibility to handle
- This is a convention, not a core feature — it works with any personality
