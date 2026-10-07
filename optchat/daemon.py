"""OptChat Memory Daemon & IPC Engine.

Manages persistent Storage, LiveView, Compactor, and provides an IPC Unix domain
socket server (~/.optchat/engine.sock) for concurrent clients (Web Server, CLI,
Antigravity Hooks, MCP).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import signal
import sys
from typing import Any, Dict, List, Optional

from optchat.compactor import Compactor
from optchat.constants import VIEW
from optchat.providers import create_provider
from optchat.storage import Message, Storage
from optchat.tree import node_coords
from optchat.view import LiveView
from optchat.visualizer import export_html_to_file

logger = logging.getLogger("optchat.daemon")


class OptChatDaemon:
    def __init__(
        self,
        chat_dir: Optional[Path] = None,
        compactor_model: Optional[str] = None,
        socket_path: Optional[Path] = None,
        provider_name: str = "agy",
    ):
        self.chat_dir = Path(chat_dir or os.path.expanduser("~/.optchat")).resolve()
        self.socket_path = Path(socket_path or (self.chat_dir / "engine.sock")).resolve()
        self.compactor_model = compactor_model
        self.provider_name = provider_name

        self.storage: Optional[Storage] = None
        self.view: Optional[LiveView] = None
        self.compactor: Optional[Compactor] = None
        self.server: Optional[asyncio.Server] = None
        self._running = False

    async def start(self) -> None:
        """Start the storage, compactor, and IPC socket server."""
        self.chat_dir.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(self.chat_dir)
        self.storage.open()

        self.view = LiveView(self.storage)
        self.view.rebuild()

        comp_provider = create_provider(
            self.provider_name,
            model=self.compactor_model or "gemini-3.8-flash-low",
            compact_model=self.compactor_model or "gemini-3.8-flash-low",
        )
        self.compactor = Compactor(self.storage, self.view, comp_provider)
        self.compactor.start()

        # Remove stale socket if present
        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass

        self.server = await asyncio.start_unix_server(
            self._handle_client,
            path=str(self.socket_path),
        )
        self._running = True
        logger.info("OptChat Daemon listening on %s", self.socket_path)

    async def stop(self) -> None:
        """Clean shutdown of IPC server, compactor, and storage."""
        self._running = False
        if self.server:
            self.server.close()
            await self.server.wait_closed()
            self.server = None

        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass

        if self.compactor:
            self.compactor.stop()
            self.compactor = None

        if self.storage:
            self.storage.close()
            self.storage = None

        logger.info("OptChat Daemon stopped.")

    def start_in_thread(self) -> None:
        """Start daemon in a background thread (ideal for in-process testing and embedding)."""
        import threading
        self._thread_ready = threading.Event()
        self._thread_loop: Optional[asyncio.AbstractEventLoop] = None

        def runner():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._thread_loop = loop
            loop.run_until_complete(self.start())
            self._thread_ready.set()
            try:
                loop.run_forever()
            finally:
                loop.run_until_complete(self.stop())
                loop.close()

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()
        self._thread_ready.wait(timeout=5.0)

    def stop_in_thread(self) -> None:
        """Stop background thread daemon."""
        if hasattr(self, "_thread_loop") and self._thread_loop and self._thread_loop.is_running():
            self._thread_loop.call_soon_threadsafe(self._thread_loop.stop)
            if hasattr(self, "_thread") and self._thread:
                self._thread.join(timeout=3.0)

    async def _handle_client(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle incoming JSON requests from IPC clients."""
        try:
            while self._running:
                line = await reader.readline()
                if not line:
                    break
                raw = line.decode("utf-8").strip()
                if not raw:
                    continue

                try:
                    req = json.loads(raw)
                    res = await self._dispatch(req)
                except Exception as e:
                    res = {"status": "error", "error": str(e)}

                payload = json.dumps(res) + "\n"
                writer.write(payload.encode("utf-8"))
                await writer.drain()
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, req: Dict[str, Any]) -> Dict[str, Any]:
        assert self.storage is not None and self.view is not None
        action = req.get("action", "")

        if action == "ping":
            return {"status": "ok", "service": "optchat-daemon"}

        elif action == "pump":
            if self.compactor:
                self.compactor.pump()
            return {"status": "ok"}

        elif action == "get_message":
            idx = int(req.get("i", 0))
            msg = self.storage.get_message(idx)
            if msg:
                return {"status": "ok", "message": msg.to_dict()}
            return {"status": "error", "error": f"Message {idx} not found"}

        elif action == "get_state":
            size = self.view.compute_size()
            return {
                "status": "ok",
                "messages_count": len(self.storage.messages),
                "tree_nodes_count": len(self.storage.tree),
                "view_size": size,
                "view_budget": self.view.budget,
                "is_settled": self.view.is_settled(),
            }

        elif action == "get_view":
            return {
                "status": "ok",
                "view": self.view.render(),
                "is_settled": self.view.is_settled(),
                "size": self.view.compute_size(),
                "lines": [part.render(self.storage) for part in self.view.parts],
            }

        elif action == "get_history":
            limit = int(req.get("limit", 30))
            msgs = self.storage.messages[-limit:] if self.storage.messages else []
            return {
                "status": "ok",
                "messages": [
                    {
                        "i": m.i,
                        "kind": m.kind,
                        "text": m.text,
                        "size": m.size,
                        "date": m.date,
                    }
                    for m in msgs
                ],
            }

        elif action == "append_message":
            kind = req.get("kind", "note")
            text = req.get("text", "")
            if not text:
                return {"status": "error", "error": "Text cannot be empty"}
            msg = self.storage.append_message(kind, text)
            self.view.on_new_message(msg.i)
            if self.compactor:
                self.compactor.pump()
            return {
                "status": "ok",
                "message": {
                    "i": msg.i,
                    "kind": msg.kind,
                    "text": msg.text,
                    "size": msg.size,
                    "date": msg.date,
                },
            }

        elif action == "zoom":
            id_ = int(req.get("id", 0))
            n = int(req.get("n", 1))
            l, i = node_coords(id_, n)
            if n == 1:
                msg = self.storage.get_message(i)
                if msg:
                    out = f"Verbatim Message {id_} ({msg.kind}):\n{msg.text}"
                else:
                    out = f"Message {id_} not found."
                return {"status": "ok", "output": out}

            node = self.storage.get_node(l, i)
            child_n = n // 2
            left_node = self.storage.get_node(l - 1, 2 * i)
            right_node = self.storage.get_node(l - 1, 2 * i + 1)
            left_str = f"{id_}+{child_n}|{left_node.text}" if left_node else f"{id_}+{child_n}|(summarizing...)"
            right_str = f"{id_ + child_n}+{child_n}|{right_node.text}" if right_node else f"{id_ + child_n}+{child_n}|(summarizing...)"
            out = f"Zoomed {id_}+{n} (Level {l}):\n  {left_str}\n  {right_str}"
            return {"status": "ok", "output": out}

        elif action == "date":
            id_ = int(req.get("id", 0))
            msg = self.storage.get_message(id_)
            if msg:
                return {"status": "ok", "output": f"Message {id_}: {msg.date}"}
            return {"status": "ok", "output": f"Message {id_} not found."}

        elif action == "rebuild":
            self.view.rebuild()
            return {"status": "ok", "size": self.view.compute_size()}

        elif action == "export_browse":
            target = self.storage.chat_dir / "browse.html"
            export_html_to_file(self.storage, self.view, target)
            return {"status": "ok", "path": str(target)}

        elif action == "hook_event":
            event_name = req.get("event", "")
            payload = req.get("payload", {})
            return await self._handle_hook_event(event_name, payload)

        return {"status": "error", "error": f"Unknown action: {action}"}

    async def _handle_hook_event(self, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle events from Antigravity hooks (PreInvocation, PostToolUse, PostInvocation, Stop)."""
        conv_id = payload.get("conversationId", "unknown")
        workspace = (payload.get("workspacePaths") or [""])[0]

        if event == "pre_invocation":
            assert self.view is not None
            # Return the latest settled compressed view for injection into the session
            view_content = self.view.render()
            return {
                "status": "ok",
                "view": view_content,
                "is_settled": self.view.is_settled(),
            }

        elif event == "post_tool":
            tool_call = payload.get("toolCall", {})
            name = tool_call.get("name", "unknown")
            args = tool_call.get("args", {})
            error = payload.get("error")
            text = f"Tool '{name}' in session {conv_id[:8]} (workspace: {workspace}): args={json.dumps(args)}"
            if error:
                text += f" | error={error}"
            # Optionally log significant tool calls
            return {"status": "ok"}

        elif event == "post_invocation":
            return {"status": "ok"}

        elif event == "stop":
            # Turn loop finished; trigger compactor pump
            if self.compactor:
                self.compactor.pump()
            return {"status": "ok"}

        return {"status": "ok"}


async def run_daemon(
    chat_dir: Optional[Path] = None,
    compactor_model: Optional[str] = None,
    socket_path: Optional[Path] = None,
    provider_name: str = "agy",
) -> None:
    daemon = OptChatDaemon(
        chat_dir=chat_dir,
        compactor_model=compactor_model,
        socket_path=socket_path,
        provider_name=provider_name,
    )
    await daemon.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    print(f"OptChat Daemon running [chat_dir={daemon.chat_dir}, socket={daemon.socket_path}]")
    try:
        await stop_event.wait()
    finally:
        await daemon.stop()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()
