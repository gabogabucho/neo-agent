---
name: web-search-provider
description: "Search the web and extract content from URLs"
min_capability: tier-2
requires:
  connectors: [web]
---
# Web Search

You can now search the web and extract content from URLs.

## How to handle search requests

When a user asks to:
- "Search for the latest news about AI"
- "What's the weather in Buenos Aires?"
- "Find information about Python 3.13"
- "Look up the best restaurants in Madrid"

Do the following:
1. Identify the search query from the user's message
2. Use web__search with the query
3. Summarize the results in a clear, concise way
4. If the user wants more detail, use web__extract on a specific URL

## Best practices

- Keep summaries concise — 2-3 paragraphs max
- Cite sources when providing factual information
- If search returns no results, suggest refining the query
- For time-sensitive info (weather, news), note that results may not be real-time

## Limitations

Web search depends on the web connector handler being configured.
For full functionality, consider connecting an MCP server like
@anthropic/mcp-server-fetch or using a search API (Tavily, Brave).
