# OptChat: an endless chat where the AI remembers everything

OptChat is an implementation of the endless agent memory architecture specified by Victor Taelin (https://gist.github.com/VictorTaelin/91837951a5ce5b38f341ec1ba1df6449).

## Key Concepts

- **One Chat Forever**: Every message (user prompts, agent replies, tool calls, tool results) is appended to a durable, append-only log with `fsync`.
- **Binary Summary Tree**: Background compactor compresses the log into a binary tree of one-line summaries (`<= 512` bytes per node). Level 0 summarizes 1 message; level `l > 0` merges its two children.
- **Constant View Size**: Each turn starts fresh with a fixed-size view (`VIEW = 128,000` bytes) of the whole chat: recent messages 1 line each, older ones many per line.
- **Hierarchical Zooming**: Agent uses `zoom(id, n)` to inspect any level or verbatim original message in `log2(n)` hops.
- **Prompt Caching**: Incremental folding preserves the view's prefix across turns, yielding high prompt-cache hit rates on Anthropic and OpenAI.
- **Durability & Safety**:
  - Single-writer Unix domain socket lock.
  - Thoughts/reasoning displayed to user, never logged to disk.
  - Tool outputs capped at `CAP = 30,000` characters.
  - Settle wait: agent turn never starts until all view lines are summarized.

## Quickstart

```bash
# Install in development mode
uv pip install -e .

# Run test suite
pytest -v

# Start interactive chat (Mock model for testing)
optchat chat --provider mock

# Start chat with Anthropic Claude
optchat chat --provider anthropic --model claude-3-7-sonnet-latest

# Export memory to an interactive HTML browser
optchat browse --output ./chat/browse.html
```
