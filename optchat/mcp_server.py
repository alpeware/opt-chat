"""Model Context Protocol (MCP) server for OptChat (§9).

Exposes OptChat memory tools to Google Antigravity CLI (agy):
  - optchat_view: read live compressed chat view (<chat>...</chat>)
  - optchat_zoom: drill down into summaries or retrieve full verbatim message
  - optchat_date: get timestamp for message id
  - optchat_log: record important decisions/milestones to persistent memory
  - optchat_stats: inspect memory capacity, depth, and settlement
  - optchat_browse: generate interactive HTML visualization

Can be added to agy via:
  agy mcp add optchat /path/to/.venv/bin/python -m optchat.mcp_server
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, Optional

from optchat.compactor import Compactor
from optchat.constants import VIEW
from optchat.providers.agy_provider import AgyProvider
from optchat.storage import Storage
from optchat.tree import execute_date, execute_zoom
from optchat.view import LiveView
from optchat.visualizer import export_html_to_file


class OptChatMCPServer:
    def __init__(self, chat_dir: Path):
        self.chat_dir = Path(chat_dir).resolve()
        self.storage: Optional[Storage] = None
        self.view: Optional[LiveView] = None
        self.compactor: Optional[Compactor] = None

    def initialize_backend(self) -> None:
        if self.storage is None:
            self.storage = Storage(self.chat_dir)
            self.storage.open()
            self.view = LiveView(self.storage)
            self.view.rebuild()
            provider = AgyProvider(model="gemini-3.8-flash-high")
            self.compactor = Compactor(self.storage, self.view, provider)
            self.compactor.start()

    def get_tools_list(self) -> list[Dict[str, Any]]:
        return [
            {
                "name": "optchat_view",
                "description": "Read OptChat's current live view of the entire chat history (one-line summaries inside <chat>...</chat>).",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "optchat_zoom",
                "description": "Drill down into a summary line id+n. When n=1, returns the exact verbatim original message.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "Message id at the start of the line."},
                        "n": {"type": "integer", "description": "Number of messages covered by the line (power of 2)."},
                    },
                    "required": ["id", "n"],
                },
            },
            {
                "name": "optchat_date",
                "description": "Get the local date and time when message id was recorded.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer", "description": "Message id to query."},
                    },
                    "required": ["id"],
                },
            },
            {
                "name": "optchat_log",
                "description": "Append a decision, milestone, or note to OptChat's permanent log and trigger background tree compression.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["user", "talk", "note"],
                            "description": "Kind of entry: 'user', 'talk', or 'note'.",
                            "default": "note",
                        },
                        "text": {"type": "string", "description": "Verbatim content to record."},
                    },
                    "required": ["text"],
                },
            },
            {
                "name": "optchat_stats",
                "description": "Get OptChat memory statistics: message count, tree nodes, current view size, and settlement status.",
                "inputSchema": {
                    "type": "object",
                    "properties": {},
                },
            },
            {
                "name": "optchat_browse",
                "description": "Generate or update an interactive HTML visualization of the whole memory.",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "output_path": {
                            "type": "string",
                            "description": "Optional path for the HTML report (default: ./chat/browse.html).",
                        },
                    },
                },
            },
        ]

    async def handle_tool_call(self, name: str, arguments: Dict[str, Any]) -> str:
        self.initialize_backend()
        assert self.storage is not None and self.view is not None and self.compactor is not None

        if name == "optchat_view":
            return self.view.render()

        elif name == "optchat_zoom":
            id_ = int(arguments.get("id", 0))
            n = int(arguments.get("n", 1))
            return execute_zoom(self.storage, id_, n)

        elif name == "optchat_date":
            id_ = int(arguments.get("id", 0))
            return execute_date(self.storage, id_)

        elif name == "optchat_log":
            kind = arguments.get("kind", "note")
            text = arguments.get("text", "")
            msg = self.storage.append_message(kind, text)
            self.view.on_new_message(msg.i)
            self.compactor.pump()
            return f"Logged message #{msg.i} [{msg.kind}]: {text[:80]}..."

        elif name == "optchat_stats":
            size = self.view.compute_size()
            pct = round((size / VIEW) * 100, 1)
            return (
                f"OptChat Memory Stats:\n"
                f"- Directory: {self.chat_dir}\n"
                f"- Messages: {len(self.storage.messages)}\n"
                f"- Tree Nodes: {len(self.storage.tree)}\n"
                f"- View Size: {size} / {VIEW} bytes ({pct}%)\n"
                f"- Settled: {self.view.is_settled()}"
            )

        elif name == "optchat_browse":
            out_str = arguments.get("output_path")
            out_file = Path(out_str) if out_str else self.chat_dir / "browse.html"
            export_html_to_file(self.storage, self.view, out_file)
            return f"Report generated at {out_file.resolve()}"

        return f"Unknown tool: {name}"

    async def run_stdio(self) -> None:
        """Run MCP JSON-RPC server loop over stdin/stdout."""
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        while True:
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            line = line_bytes.decode("utf-8").strip()
            if not line:
                continue

            try:
                req = json.loads(line)
            except Exception:
                continue

            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})

            if method == "initialize":
                res = {
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "optchat", "version": "0.1.0"},
                    },
                }
                self._send(res)

            elif method == "notifications/initialized":
                pass

            elif method == "ping":
                self._send({"jsonrpc": "2.0", "id": req_id, "result": {}})

            elif method == "tools/list":
                tools = self.get_tools_list()
                res = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
                self._send(res)

            elif method == "tools/call":
                tool_name = params.get("name", "")
                tool_args = params.get("arguments", {})
                try:
                    result_text = await self.handle_tool_call(tool_name, tool_args)
                    res = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": result_text}],
                            "isError": False,
                        },
                    }
                except Exception as e:
                    res = {
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "result": {
                            "content": [{"type": "text", "text": f"Error: {e}"}],
                            "isError": True,
                        },
                    }
                self._send(res)

            else:
                if req_id is not None:
                    self._send({
                        "jsonrpc": "2.0",
                        "id": req_id,
                        "error": {"code": -32601, "message": f"Method not found: {method}"},
                    })

    def _send(self, payload: Dict[str, Any]) -> None:
        out = json.dumps(payload, ensure_ascii=False) + "\n"
        sys.stdout.write(out)
        sys.stdout.flush()


def main() -> None:
    raw_dir = os.environ.get("OPTCHAT_DIR", "~/.optchat")
    chat_dir = Path(os.path.expanduser(raw_dir)).resolve()
    server = OptChatMCPServer(chat_dir)
    asyncio.run(server.run_stdio())


if __name__ == "__main__":
    main()
