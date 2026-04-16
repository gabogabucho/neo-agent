---
name: file-manager
description: "Read, write, and list local files"
min_capability: tier-1
requires:
  connectors: [file]
---
# File Manager

You can now read, write, and list local files.

## How to handle file requests

When a user asks to:
- "Read the file config.yaml"
- "Create a file called notes.txt with this content"
- "List files in the data directory"
- "What's in the README?"

Do the following:
1. Identify the operation (read, write, or list)
2. Identify the file path or directory
3. Use the appropriate connector: file__read, file__write, or file__list
4. Present the result clearly

## Safety rules

- NEVER read or write files outside the Neo working directory without asking
- NEVER overwrite files without confirming with the user first
- For large files, summarize content rather than showing everything
- Warn the user if a file path looks sensitive (.env, credentials, keys)

## Examples

User: "Read my config file"
→ file__read(path="config.yaml")
→ Display the content in a code block

User: "Save this as notes.txt: Meeting at 3pm with design team"
→ file__write(path="notes.txt", content="Meeting at 3pm with design team")
→ "Saved to notes.txt"
