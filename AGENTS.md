# OptChat Memory Integration for Antigravity (agy)

This repository uses **OptChat** as its persistent, endless memory engine.

## Guidelines for agy:

1. **Memory as Compressed Tree**:
   - The entire chat history and project decisions are preserved permanently in OptChat's append-only log and compressed binary summary tree.
   - You have access to the `optchat` MCP tools: `optchat_view`, `optchat_zoom`, `optchat_date`, `optchat_log`, `optchat_stats`, and `optchat_browse`.

2. **Retrieving Context**:
   - Check `optchat_view()` when starting a session or when context from earlier sessions is needed.
   - The view renders summaries formatted as `id+n|text` inside `<chat>...</chat>`.
   - **Never guess or act on partial summary information**: If a summary line only mentions something you need (such as a past decision, configuration value, or file location), call `optchat_zoom(id, n)` to open the line into its two child lines, or `optchat_zoom(id, 1)` to retrieve the exact verbatim original message.

3. **Recording History**:
   - Whenever an important architectural decision is finalized, a key requirement is specified by the user, or a major task is completed, record it with `optchat_log(kind="note", text="...")` (or `kind="user"` / `kind="talk"`).
   - This ensures the memory is permanently preserved and summarized up the binary tree for future sessions.
