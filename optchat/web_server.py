"""Responsive Web Interface for OptChat (Mobile & Desktop).

Provides a local intranet web version of the CLI with:
- Real-time token streaming via Server-Sent Events (SSE)
- Mobile-first responsive UI (touch friendly, dynamic viewport)
- Interactive <chat> live view with Zoom & Date line actions
- Direct access to browse.html tree visualization
- Built-in slash commands (/view, /stats, /browse, /zoom, /date)
"""

from __future__ import annotations

import asyncio
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import re
import socket
import sys
from typing import Any, Dict, List, Optional, Set

from aiohttp import web

from optchat.agent import TurnAgent
from optchat.compactor import Compactor
from optchat.constants import VIEW
from optchat.engine_client import EngineClient, ProxyCompactor, ProxyStorage, ProxyView
from optchat.providers import create_provider
from optchat.providers.agy_provider import AgyProvider
from optchat.providers.base import BaseLLMProvider
from optchat.storage import Storage
from optchat.tools import ToolRegistry
from optchat.tree import node_coords
from optchat.view import LiveView
from optchat.visualizer import export_html_to_file

logger = logging.getLogger("optchat.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"


class OptChatWebServer:
    def __init__(
        self,
        chat_dir: Path,
        provider_name: str = "agy",
        model: Optional[str] = None,
        compactor_model: Optional[str] = None,
        timeout: float = 300.0,
        workspace: Optional[Path] = None,
        conversation_id: Optional[str] = None,
    ):
        self.chat_dir = Path(chat_dir).resolve()
        self.provider_name = provider_name
        self.model = model
        self.compactor_model = compactor_model
        self.timeout = timeout

        default_ws = Path("/home/simonpure/src/alpeware/opt-chat")
        self.workspace = (Path(workspace).resolve() if workspace else (default_ws if default_ws.exists() else Path.cwd())).resolve()
        self.conversation_id: Optional[str] = conversation_id

        self.engine_client: Optional[EngineClient] = None
        self.is_daemon_connected: bool = False
        self.storage: Optional[Any] = None
        self.view: Optional[Any] = None
        self.compactor: Optional[Any] = None
        self.agent: Optional[TurnAgent] = None
        self.main_provider: Optional[BaseLLMProvider] = None

        @web.middleware
        async def static_cache_control(request: web.Request, handler: Any) -> web.StreamResponse:
            response = await handler(request)
            if request.path.startswith("/static/"):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
                response.headers["Pragma"] = "no-cache"
                response.headers["Expires"] = "0"
            return response

        self.app = web.Application(middlewares=[static_cache_control])

        self._active_stream_queues: Set[asyncio.Queue[Dict[str, Any]]] = set()
        self._daemon_event_task: Optional[asyncio.Task] = None

    async def init_engine(self) -> None:
        client = EngineClient(socket_path=self.chat_dir / "engine.sock")
        if await client.is_daemon_alive_async(timeout=0.5):
            logger.info("OptChat Web connected to background daemon via %s", client.socket_path)
            self.engine_client = client
            self.is_daemon_connected = True
            self.storage = ProxyStorage(client, self.chat_dir)
            self.view = ProxyView(client)
            self.compactor = ProxyCompactor(client)
            self._daemon_event_task = asyncio.create_task(self._listen_to_daemon_events())
        else:
            logger.info("OptChat Daemon not running. Using embedded Storage & Compactor.")
            self.engine_client = None
            self.is_daemon_connected = False
            self.storage = Storage(self.chat_dir)
            self.storage.open()

            self.view = LiveView(self.storage)
            self.view.rebuild()

            comp_provider = (
                create_provider(self.provider_name, model=self.compactor_model, timeout=self.timeout)
                if self.compactor_model
                else None
            )
            self.compactor = Compactor(self.storage, self.view, comp_provider)
            self.compactor.start()

        self.main_provider = create_provider(
            self.provider_name,
            model=self.model,
            timeout=self.timeout,
            workspace=str(self.workspace),
            conversation_id=self.conversation_id,
        )

        tool_registry = ToolRegistry()

        def ui_listener(event_type: str, content: str) -> None:
            # Sync conversation_id if AgyProvider captured one
            if isinstance(self.main_provider, AgyProvider) and self.main_provider.conversation_id:
                self.conversation_id = self.main_provider.conversation_id

            # Broadcast to active SSE listeners
            event_obj = {"type": event_type, "content": content}
            for q in list(self._active_stream_queues):
                try:
                    q.put_nowait(event_obj)
                except Exception:
                    pass

        agents_md = self.workspace / "AGENTS.md"
        if not agents_md.exists():
            agents_md = self.chat_dir.parent / "AGENTS.md"

        self.agent = TurnAgent(
            storage=self.storage,
            view=self.view,
            compactor=self.compactor,
            provider=self.main_provider,
            tool_registry=tool_registry,
            agents_md_path=agents_md if agents_md.exists() else None,
            git_auto_commit=False,
            ui_callback=ui_listener,
        )

        self._setup_routes()

    async def _listen_to_daemon_events(self) -> None:
        """Stream real-time daemon events (including agy subagent activities) to connected web clients."""
        while self.is_daemon_connected:
            try:
                assert self.engine_client is not None
                async for evt in self.engine_client.subscribe_events():
                    for q in list(self._active_stream_queues):
                        try:
                            q.put_nowait(evt)
                        except Exception:
                            pass
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.debug("Daemon event subscription retry: %s", e)
                await asyncio.sleep(2.0)

    def _setup_routes(self) -> None:
        self.app.router.add_static("/static", path=STATIC_DIR)
        self.app.router.add_get("/", self.handle_index)
        self.app.router.add_get("/browse", self.handle_browse)
        self.app.router.add_get("/api/state", self.handle_api_state)
        self.app.router.add_get("/api/session", self.handle_api_session_get)
        self.app.router.add_post("/api/session", self.handle_api_session_post)
        self.app.router.add_get("/api/history", self.handle_api_history)
        self.app.router.add_get("/api/view", self.handle_api_view)
        self.app.router.add_get("/api/stream", self.handle_api_stream)
        self.app.router.add_post("/api/chat", self.handle_api_chat)
        self.app.router.add_post("/api/command", self.handle_api_command)
        self.app.router.add_post("/api/zoom", self.handle_api_zoom)
        self.app.router.add_post("/api/voice", self.handle_api_voice)
        self.app.router.add_get("/mcp", self.handle_mcp_get)
        self.app.router.add_post("/mcp", self.handle_mcp_post)

    async def handle_api_stream(self, request: web.Request) -> web.StreamResponse:

        response = web.StreamResponse(
            status=200,
            reason="OK",
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)

        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._active_stream_queues.add(queue)

        try:
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    payload = f"data: {json.dumps(event)}\n\n"
                    await response.write(payload.encode("utf-8"))
                except asyncio.TimeoutError:
                    await response.write(b": keep-alive\n\n")
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            self._active_stream_queues.discard(queue)
            try:
                await response.write_eof()
            except Exception:
                pass

        return response

    async def handle_index(self, request: web.Request) -> web.Response:
        index_file = STATIC_DIR / "index.html"
        if index_file.exists():
            content = index_file.read_text(encoding="utf-8")
        else:
            content = "<h1>OptChat</h1><p>index.html not found</p>"
        return web.Response(
            text=content,
            content_type="text/html",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    async def handle_browse(self, request: web.Request) -> web.Response:
        browse_file = self.chat_dir / "browse.html"
        if self.is_daemon_connected and self.engine_client:
            try:
                await self.engine_client.call_async("export_browse")
            except Exception:
                pass
        elif self.storage is not None and self.view is not None:
            if not browse_file.exists():
                export_html_to_file(self.storage, self.view, browse_file)
        if browse_file.exists():
            return web.FileResponse(browse_file)
        return web.Response(text="browse.html not available yet", status=404)

    async def handle_api_session_get(self, request: web.Request) -> web.Response:
        return web.json_response({
            "workspace": str(self.workspace),
            "conversation_id": self.conversation_id,
            "is_daemon_connected": self.is_daemon_connected,
            "provider": self.provider_name,
            "model": self.model,
        })

    async def handle_api_session_post(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        if "workspace" in data:
            ws_path = Path(data["workspace"]).expanduser().resolve()
            if not ws_path.is_dir():
                return web.json_response({"error": f"Directory not found: {ws_path}"}, status=400)
            self.workspace = ws_path
            if isinstance(self.main_provider, AgyProvider):
                self.main_provider.workspace = str(self.workspace)
            agents_md = self.workspace / "AGENTS.md"
            if self.agent:
                self.agent.agents_md_path = agents_md if agents_md.exists() else None

        if "conversation_id" in data:
            conv_id = data["conversation_id"]
            self.conversation_id = conv_id if conv_id else None
            if isinstance(self.main_provider, AgyProvider):
                self.main_provider.conversation_id = self.conversation_id
                self.main_provider.last_conversation_id = self.conversation_id

        return web.json_response({
            "status": "ok",
            "workspace": str(self.workspace),
            "conversation_id": self.conversation_id,
        })

    async def handle_api_state(self, request: web.Request) -> web.Response:
        if self.is_daemon_connected and self.engine_client:
            st = await self.engine_client.get_state_async()
            return web.json_response({
                "messages_count": st.get("messages_count", 0),
                "tree_nodes_count": st.get("tree_nodes_count", 0),
                "view_size": st.get("view_size", 0),
                "view_budget": st.get("view_budget", 128000),
                "is_settled": st.get("is_settled", True),
                "daemon_connected": True,
                "workspace": str(self.workspace),
                "conversation_id": self.conversation_id,
                "workspaces": st.get("workspaces", []),
            })

        assert self.storage is not None and self.view is not None
        # Discover known workspaces from offline storage
        known_workspaces = set()
        if self.workspace:
            known_workspaces.add(self.workspace.name)
        elif os.getcwd():
            known_workspaces.add(Path.cwd().name)

        ignored_labels = {"subagent", "subagents", "agent", "note", "talk", "user", "work", "tool", "echo", "none"}
        if self.storage and self.storage.messages:
            for m in self.storage.messages[-500:]:
                if m.workspace and m.workspace.lower() not in ignored_labels:
                    known_workspaces.add(m.workspace)
                elif m.text:
                    mat = re.match(r"^\[([a-zA-Z0-9_\-\.]+)\]", m.text)
                    if mat:
                        lbl = mat.group(1)
                        if lbl.lower() not in ignored_labels and not lbl.startswith("http"):
                            known_workspaces.add(lbl)

        return web.json_response({
            "messages_count": len(self.storage.messages),
            "tree_nodes_count": len(self.storage.tree),
            "view_size": self.view.compute_size(),
            "view_budget": self.view.budget,
            "is_settled": self.view.is_settled(),
            "daemon_connected": False,
            "workspace": str(self.workspace),
            "conversation_id": self.conversation_id,
            "workspaces": sorted(list(known_workspaces)),
        })

    async def handle_api_history(self, request: web.Request) -> web.Response:
        limit = int(request.query.get("limit", "50"))
        ws_filter = request.query.get("workspace")
        category_filter = request.query.get("category")
        if self.is_daemon_connected and self.engine_client:
            kwargs: Dict[str, Any] = {"limit": limit}
            if ws_filter:
                kwargs["workspace"] = ws_filter
            if category_filter:
                kwargs["category"] = category_filter
            res = await self.engine_client.call_async("get_history", **kwargs)
            return web.json_response(res.get("messages", []))

        assert self.storage is not None
        storage_msgs = self.storage.messages
        if ws_filter and ws_filter.lower() != "all":
            ws_target = ws_filter.lower()
            storage_msgs = [
                m for m in storage_msgs
                if (m.workspace and m.workspace.lower() == ws_target)
                or (f"[{ws_target}]" in m.text.lower())
            ]
        if category_filter and category_filter.lower() != "all":
            cat = category_filter.lower()
            if cat == "main":
                storage_msgs = [m for m in storage_msgs if m.kind in ("user", "talk")]
            elif cat == "subagent":
                storage_msgs = [m for m in storage_msgs if m.kind == "work"]
            elif cat == "note":
                storage_msgs = [m for m in storage_msgs if m.kind == "note"]

        msgs = storage_msgs[-limit:] if limit > 0 else storage_msgs
        result = [
            {
                "i": m.i,
                "kind": m.kind,
                "text": m.text,
                "size": m.size,
                "date": m.date,
                "workspace": m.workspace,
                "device": m.device,
            }
            for m in msgs
            if m.kind in ("user", "talk", "note", "work")
        ]
        return web.json_response(result)

    async def handle_api_view(self, request: web.Request) -> web.Response:
        ws_filter = request.query.get("workspace")
        if self.is_daemon_connected and self.engine_client:
            kwargs = {}
            if ws_filter:
                kwargs["workspace"] = ws_filter
            res = await self.engine_client.call_async("get_view", **kwargs)
            return web.json_response({
                "size": res.get("size", 0),
                "budget": 128000,
                "lines": res.get("lines", []),
            })

        assert self.storage is not None and self.view is not None
        lines = [part.render(self.storage) for part in self.view.parts]
        if ws_filter and ws_filter.lower() != "all":
            tag = f"[{ws_filter.lower()}]"
            lines = [l for l in lines if tag in l.lower()]
        return web.json_response({
            "size": self.view.compute_size(),
            "budget": self.view.budget,
            "lines": lines,
        })

    async def handle_api_chat(self, request: web.Request) -> web.Response:
        assert self.agent is not None
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        message = data.get("message", "").strip()
        if not message:
            return web.json_response({"error": "Message cannot be empty"}, status=400)

        accept = request.headers.get("Accept", "")
        if "text/event-stream" in accept:
            response = web.StreamResponse(
                status=200,
                reason="OK",
                headers={
                    "Content-Type": "text/event-stream",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )
            await response.prepare(request)

            queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
            self._active_stream_queues.add(queue)

            turn_task = asyncio.create_task(self.agent.submit_user_message(message))

            try:
                while not turn_task.done() or not queue.empty():
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=0.2)
                        payload = f"data: {json.dumps(event)}\n\n"
                        await response.write(payload.encode("utf-8"))
                        if event.get("type") in ("turn_complete", "done"):
                            break
                    except asyncio.TimeoutError:
                        continue
            finally:
                self._active_stream_queues.discard(queue)
                await response.write_eof()

            return response

        # Asynchronous submission: queue task immediately and return status
        asyncio.create_task(self.agent.submit_user_message(message))
        return web.json_response({"status": "queued", "message": message})

    async def handle_api_command(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        cmd = data.get("command", "").strip()

        if cmd == "/view":
            if self.is_daemon_connected and self.engine_client:
                v = await self.engine_client.get_view_async()
                return web.json_response({"output": v})
            assert self.view is not None
            return web.json_response({"output": self.view.render()})

        elif cmd == "/stats":
            if self.is_daemon_connected and self.engine_client:
                st = await self.engine_client.get_state_async()
                output = (
                    f"Messages: {st.get('messages_count', 0):,}\n"
                    f"Tree nodes: {st.get('tree_nodes_count', 0):,}\n"
                    f"View size: {st.get('view_size', 0):,} / {st.get('view_budget', 128000):,} bytes "
                    f"({(st.get('view_size', 0) / max(1, st.get('view_budget', 128000))) * 100:.1f}%)\n"
                    f"View settled: {st.get('is_settled', True)}\n"
                    f"Engine Mode: Daemon IPC ({self.engine_client.socket_path})\n"
                    f"Workspace: {self.workspace}\n"
                    f"Conversation: {self.conversation_id or 'none (fresh)'}"
                )
                return web.json_response({"output": output})

            assert self.storage is not None and self.view is not None
            output = (
                f"Messages: {len(self.storage.messages):,}\n"
                f"Tree nodes: {len(self.storage.tree):,}\n"
                f"View size: {self.view.compute_size():,} / {self.view.budget:,} bytes "
                f"({(self.view.compute_size() / self.view.budget) * 100:.1f}%)\n"
                f"View settled: {self.view.is_settled()}\n"
                f"Engine Mode: Embedded Storage\n"
                f"Workspace: {self.workspace}\n"
                f"Conversation: {self.conversation_id or 'none (fresh)'}"
            )
            return web.json_response({"output": output})

        elif cmd.startswith("/date"):
            parts = cmd.split()
            if len(parts) >= 2 and parts[1].isdigit():
                idx = int(parts[1])
                if self.is_daemon_connected and self.engine_client:
                    out = await self.engine_client.date_async(idx)
                    return web.json_response({"output": out})
                assert self.storage is not None
                msg = self.storage.get_message(idx)
                if msg:
                    return web.json_response({"output": f"Message {idx}: {msg.date}"})
                return web.json_response({"output": f"Message {idx} not found."})
            return web.json_response({"output": "Usage: /date <id>"})

        elif cmd.startswith("/note"):
            text = cmd[5:].strip()
            if text:
                if self.is_daemon_connected and self.engine_client:
                    res = await self.engine_client.append_message_async("note", text)
                    m = res.get("message", {})
                    return web.json_response({"output": f"Logged note #{m.get('i')}: {text}"})
                assert self.storage is not None and self.view is not None
                msg = self.storage.append_message("note", text)
                self.view.on_new_message(msg.i)
                if self.compactor:
                    self.compactor.pump()
                return web.json_response({"output": f"Logged note #{msg.i}: {text}"})
            return web.json_response({"output": "Usage: /note <text>"})

        elif cmd.startswith("/workspace"):
            parts = cmd.split(maxsplit=1)
            if len(parts) == 2:
                new_path = Path(parts[1].strip()).expanduser().resolve()
                if new_path.is_dir():
                    self.workspace = new_path
                    if isinstance(self.main_provider, AgyProvider):
                        self.main_provider.workspace = str(self.workspace)
                    agents_md = self.workspace / "AGENTS.md"
                    if self.agent:
                        self.agent.agents_md_path = agents_md if agents_md.exists() else None
                    return web.json_response({"output": f"Workspace switched to: {self.workspace}"})
                return web.json_response({"output": f"Directory not found: {new_path}"})
            return web.json_response({"output": f"Current workspace: {self.workspace}"})

        elif cmd == "/rebuild":
            if self.is_daemon_connected and self.engine_client:
                res = await self.engine_client.rebuild_async()
                return web.json_response({"output": f"View rebuilt. Size: {res.get('size', 0):,} bytes."})
            assert self.view is not None
            self.view.rebuild()
            return web.json_response({"output": f"View rebuilt. Size: {self.view.compute_size():,} bytes."})

        return web.json_response({"output": f"Unknown command: {cmd}"})

    async def handle_api_zoom(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
            id_ = int(data.get("id"))
            n = int(data.get("n"))
        except Exception:
            return web.json_response({"error": "Invalid id or n"}, status=400)

        if self.is_daemon_connected and self.engine_client:
            res = await self.engine_client.call_async("zoom", id=id_, n=n)
            return web.json_response(res)

        assert self.storage is not None
        l, i = node_coords(id_, n)

        if n == 1:
            msg = self.storage.get_message(i)
            if msg:
                out = f"Verbatim Message {id_} ({msg.kind}):\n{msg.text}"
                msg_dict = {"i": msg.i, "kind": msg.kind, "text": msg.text, "date": msg.date}
            else:
                out = f"Message {id_} not found."
                msg_dict = None
            return web.json_response({
                "status": "ok",
                "output": out,
                "id": id_,
                "n": 1,
                "is_leaf": True,
                "message": msg_dict,
            })

        # Higher level zoom (§7.1)
        child_a = self.storage.get_node(l - 1, 2 * i)
        child_b = self.storage.get_node(l - 1, 2 * i + 1)

        half = n // 2
        a_text = child_a.text if child_a else "(not summarized yet: zoom it)"
        b_text = child_b.text if child_b else "(not summarized yet: zoom it)"

        out = (
            f"Zoomed line {id_}+{n} into span {half}:\n\n"
            f"{id_}+{half}|{a_text}\n"
            f"{id_ + half}+{half}|{b_text}"
        )
        children = [
            {"id": id_, "n": half, "text": a_text},
            {"id": id_ + half, "n": half, "text": b_text},
        ]
        return web.json_response({
            "status": "ok",
            "output": out,
            "id": id_,
            "n": n,
            "is_leaf": False,
            "children": children,
        })

    async def handle_api_voice(self, request: web.Request) -> web.Response:
        """Handle voice recording transcription upload endpoint."""
        try:
            transcript = ""
            if request.content_type == "application/json":
                data = await request.json()
                transcript = data.get("text", "")
            return web.json_response({
                "status": "ok",
                "text": transcript,
                "message": "Voice dictation received",
            })
        except Exception as e:
            return web.json_response({"error": str(e)}, status=400)

    async def handle_mcp_get(self, request: web.Request) -> web.Response:
        """Return MCP service descriptor and tools list."""
        from optchat.mcp_server import OptChatMCPServer
        mcp = OptChatMCPServer(self.chat_dir)
        return web.json_response({
            "service": "optchat-mcp-server",
            "version": "0.1.0",
            "transport": "HTTP-POST-JSON-RPC",
            "endpoint": "/mcp",
            "tools": mcp.get_tools_list(),
        })

    async def handle_mcp_post(self, request: web.Request) -> web.Response:
        """Handle Model Context Protocol JSON-RPC requests over HTTP."""
        try:
            req = await request.json()
        except Exception:
            return web.json_response({"jsonrpc": "2.0", "error": {"code": -32700, "message": "Parse error"}}, status=400)

        from optchat.mcp_server import OptChatMCPServer
        mcp = OptChatMCPServer(self.chat_dir)
        mcp.initialize_backend()

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
        elif method in ("ping", "notifications/initialized"):
            res = {"jsonrpc": "2.0", "id": req_id, "result": {}}
        elif method == "tools/list":
            tools = mcp.get_tools_list()
            res = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
        elif method == "tools/call":
            tool_name = params.get("name", "")
            tool_args = params.get("arguments", {})
            try:
                result_text = await mcp.handle_tool_call(tool_name, tool_args)
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
        else:
            res = {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": -32601, "message": f"Method not found: {method}"},
            }
        return web.json_response(res)

    def shutdown(self) -> None:

        if self._daemon_event_task and not self._daemon_event_task.done():
            self._daemon_event_task.cancel()
        if not self.is_daemon_connected:
            if self.compactor:
                self.compactor.stop()
            if self.storage:
                self.storage.close()


def get_lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


def run_web_app(
    host: str = "0.0.0.0",
    port: int = 8765,
    chat_dir: Optional[Path] = None,
    provider_name: str = "agy",
    model: Optional[str] = None,
    compactor_model: Optional[str] = None,
    workspace: Optional[Path] = None,
    conversation_id: Optional[str] = None,
) -> None:
    target_dir = Path(chat_dir or (Path.home() / ".optchat")).resolve()
    server = OptChatWebServer(
        chat_dir=target_dir,
        provider_name=provider_name,
        model=model,
        compactor_model=compactor_model,
        workspace=workspace,
        conversation_id=conversation_id,
    )

    async def start():
        await server.init_engine()
        lan_ip = get_lan_ip()
        print("\n" + "=" * 55)
        print("  OptChat Web Interface Running")
        print("=" * 55)
        print(f"  • Local URL:   http://localhost:{port}")
        print(f"  • Network URL: http://{lan_ip}:{port}  (Open on Phone)")
        print(f"  • Chat Dir:    {target_dir}")
        print("=" * 55 + "\n")

        runner = web.AppRunner(server.app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        await site.start()

        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            server.shutdown()
            await runner.cleanup()

    try:
        asyncio.run(start())
    except KeyboardInterrupt:
        print("\nOptChat web server stopped.")
