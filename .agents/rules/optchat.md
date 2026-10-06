---
trigger: always_on
description: OptChat memory system rules for agy
---

# OptChat Memory Integration

When working in this workspace, you have access to the `optchat` MCP server tools:

- `optchat_view()`: Returns the compressed tree view of all past project context.
- `optchat_zoom(id, n)`: Opens line `id+n` into finer-grained summaries (`n/2`), or returns the verbatim message when `n=1`. Always zoom before guessing when a summary mentions relevant context.
- `optchat_date(id)`: Returns the timestamp of message `id`.
- `optchat_log(kind, text)`: Records an architectural decision, user preference, or milestone to persistent memory.
- `optchat_stats()`: Displays memory stats and tree status.
- `optchat_browse()`: Regenerates the interactive HTML visualization at `chat/browse.html`.
