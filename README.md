# OptChat: an endless chat where the AI remembers everything

OptChat is a production-grade implementation of the endless agent memory architecture specified by [Victor Taelin](https://gist.github.com/VictorTaelin/91837951a5ce5b38f341ec1ba1df6449).

Instead of dropping context when conversation limits are reached, OptChat continuously folds the conversation history into a compressed binary summary tree while maintaining a constant view window. The agent can remember conversations across days, weeks, or years, and zoom into verbatim message history on demand.

---

## Key Concepts

- **One Chat Forever**: Every message (user prompts, agent replies, tool calls, and tool results) is recorded in a durable, append-only JSONL log with `fsync`.
- **Binary Summary Tree**: A background compactor compresses the conversation log into a binary tree of one-line summaries (`<= 512` bytes per node). Level 0 summarizes 1 message; level `l > 0` merges and summarizes its two children.
- **Constant View Size**: Each turn receives a fixed-budget view (`VIEW = 128,000` bytes) of the entire chat history. Recent messages occupy one line each, while older messages are hierarchically summarized.
- **Hierarchical Zooming**: The agent can inspect any past event or retrieve verbatim original messages in $\mathcal{O}(\log n)$ steps using `zoom(id, n)`.
- **Prompt Caching**: Incremental folding preserves the view's prefix across turns, maximizing prompt cache hit rates on providers like Anthropic and OpenAI.
- **Durability & Safety**:
  - Single-writer Unix domain socket lock (`ProcessLock`).
  - Chain-of-thought and thinking tokens are displayed to the user but never logged to disk.
  - Tool outputs are capped at `CAP = 30,000` characters.
  - Settle wait: agent turns wait until all view lines are summarized.

---

## Installation

```bash
# Clone the repository
git clone https://github.com/alpeware/opt-chat.git
cd opt-chat

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install in editable mode with development dependencies
pip install -e ".[dev]"
```

---

## LLM Providers

OptChat supports multiple LLM providers:

| Provider | CLI Flag | Description |
|---|---|---|
| **Google Antigravity CLI** | `--provider agy` *(default)* | Drives local `agy` without requiring external API keys. Pipes prompts safely via `stdin` to handle large view sizes. |
| **Anthropic Claude** | `--provider anthropic` | Direct API integration with Claude 3.5 / 3.7 Sonnet models and prompt caching. |
| **OpenAI** | `--provider openai` | Direct API integration with GPT-4o and compatible endpoints. |
| **Mock** | `--provider mock` | Offline deterministic provider for local testing and CI/CD pipelines. |

### Using the Google Antigravity (agy) Provider

The `agy` provider uses your local `agy` command-line installation. It runs turns without external API key configuration:

```bash
# Start interactive chat using agy (default provider)
optchat chat

# Specify custom main and compactor models
optchat chat --provider agy --model gemini-3.8-flash-high --compactor-model gemini-3.8-flash-low
```

---

## Web Server (`optchat web`)

OptChat includes a built-in, responsive web application designed for both desktop and mobile devices on local networks.

```bash
# Start the web server (defaults to 0.0.0.0:8765)
optchat web

# Custom port and model configuration
optchat web --host 0.0.0.0 --port 8765 --model gemini-3.8-flash-medium
```

### Features:
- **Mobile & Desktop Responsive Design**: Modern dark theme with desktop sidebar, mobile navigation bar, and clean typography.
- **Non-Blocking Asynchronous Submission**: Submit multiple consecutive queries without waiting for pending turns to finish. Requests are queued immediately (`POST /api/chat`) and processed sequentially by the agent.
- **Persistent Server-Sent Events (SSE)**: Real-time token streaming, tool call previews, reasoning inspection, and status updates via `/api/stream`.
- **Interactive Memory Stats**: Live monitor of message count, tree nodes, view size / budget, and compaction settlement state.
- **Markdown & Code Rendering**: Rich rendering for headings, lists, tables, horizontal rules, blockquotes, and code snippets.
- **Slash Commands**:
  - `/view`: Display the current compressed context window.
  - `/stats`: View message count, tree node count, and memory budget usage.
  - `/date <id>`: Show timestamp for a specific message ID.
  - `/note <text>`: Append an architectural note or milestone directly to persistent memory.
  - `/rebuild`: Force rebuild the live view from storage.

---

## Bulk Importer (`optchat import-bulk`)

OptChat can ingest your existing Google Antigravity (`agy`) chat history into persistent memory.

```bash
# Preview what would be imported (dry-run)
optchat import-bulk --workspace kaggle/gemma-4-developer-agent --dry-run

# Import clean dialogue (Option B: user prompts + agent replies, skipping tool noise)
optchat import-bulk --workspace kaggle/gemma-4-developer-agent

# Bulk import, run compactor to build tree nodes, and generate HTML browser
optchat import-bulk --workspace kaggle/gemma-4-developer-agent --compact --browse
```

### Key Capabilities:
- **Workspace Filtering**: Match conversations by workspace path or substring (e.g. `--workspace kaggle/gemma-4-developer-agent`).
- **Idempotent Manifest**: Tracks imported sessions in `import_manifest.json` to prevent duplicate ingestion across repeated runs. Use `--force` to re-import.
- **Clean Dialogue Extraction**: Extracts core conversation turns while stripping noisy tool execution outputs (use `--include-tools` if raw tool outputs are desired).
- **High-Throughput Batch I/O**: Fast sequential ingestion able to import thousands of messages in seconds.

---

## Interactive HTML Memory Browser (`optchat browse`)

Export your entire memory tree into a standalone, interactive HTML visualizer:

```bash
# Generate HTML report
optchat browse --output ./chat/browse.html
```

The browser lets you inspect the full binary tree structure, expand nodes, zoom into summaries, and read verbatim historical conversations.

---

## Model Context Protocol (MCP) Server (`optchat mcp`)

OptChat can run as an MCP server to give external AI agents (including `agy` itself) endless memory tools:

```bash
optchat mcp
```

### Available MCP Tools:
- `optchat_view()`: Retrieve the current compressed tree view.
- `optchat_zoom(id, n)`: Zoom into node `id+n` to reveal finer-grained child summaries or verbatim text when `n=1`.
- `optchat_date(id)`: Look up timestamp of message `id`.
- `optchat_log(kind, text)`: Record an architectural decision or milestone to memory.
- `optchat_stats()`: Return memory statistics and settlement status.
- `optchat_browse()`: Regenerate the HTML memory browser.

---

## Running Tests

OptChat comes with a complete unit and integration test suite:

```bash
# Run all tests
pytest -v

# Run specific test modules
pytest tests/test_agent.py
pytest tests/test_bulk_importer.py
pytest tests/test_web_server.py
```

---

## License

MIT License. See [LICENSE](LICENSE) for details.
