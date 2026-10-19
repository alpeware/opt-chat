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
import shutil
import socket
import sys
import time
from typing import Any, Dict, List, Optional, Set

from aiohttp import web

from optchat.agent import TurnAgent
from optchat.compactor import Compactor
from optchat.constants import VIEW
from optchat.engine_client import EngineClient, ProxyCompactor, ProxyStorage, ProxyView
from optchat.providers import create_provider
from optchat.providers.agy_provider import AgyProvider
from optchat.providers.base import BaseLLMProvider
from optchat.storage import Message, Storage, is_valid_workspace_name
from optchat.telemetry import TelemetryStore, estimate_cost_usd
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
        timeout: float = 600.0,
        workspace: Optional[Path] = None,
        conversation_id: Optional[str] = None,
    ):
        self.chat_dir = Path(chat_dir).resolve()
        self.provider_name = provider_name
        self.model = model
        self.compactor_model = compactor_model
        self.timeout = timeout

        default_ws = Path.home() / "desk"
        self.default_workspace = (Path(workspace).resolve() if workspace else (default_ws if default_ws.exists() else Path.cwd())).resolve()
        self.workspace = self.default_workspace
        self.conversation_id: Optional[str] = conversation_id
        self.agy_path = shutil.which("agy") or "/home/simonpure/.local/bin/agy"

        self.engine_client: Optional[EngineClient] = None
        self.is_daemon_connected: bool = False
        self.storage: Optional[Any] = None
        self.view: Optional[Any] = None
        self.compactor: Optional[Any] = None
        self.agent: Optional[TurnAgent] = None
        self.main_provider: Optional[BaseLLMProvider] = None
        self.telemetry = TelemetryStore(chat_dir=self.chat_dir)

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
            telemetry=self.telemetry,
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
        self.app.router.add_get("/api/models", self.handle_api_models)
        self.app.router.add_get("/api/history", self.handle_api_history)
        self.app.router.add_get("/api/view", self.handle_api_view)
        self.app.router.add_get("/api/stream", self.handle_api_stream)
        self.app.router.add_post("/api/chat", self.handle_api_chat)
        self.app.router.add_post("/api/command", self.handle_api_command)
        self.app.router.add_post("/api/zoom", self.handle_api_zoom)
        self.app.router.add_post("/api/voice", self.handle_api_voice)
        self.app.router.add_get("/api/file", self.handle_api_file)
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

    async def handle_api_file(self, request: web.Request) -> web.Response:
        """Serve file or directory content over HTTP for mobile/web browsing."""
        import urllib.parse
        import html as html_lib

        raw_path = request.query.get("path", "").strip()
        if not raw_path:
            return web.Response(text="Missing ?path= parameter", status=400)

        clean_path_str = urllib.parse.unquote(raw_path)
        if clean_path_str.startswith("file://"):
            clean_path_str = clean_path_str[7:]

        target = Path(clean_path_str).expanduser().resolve()

        # Security check: Ensure target exists and is within allowed roots ($HOME, /tmp)
        home = Path.home().resolve()
        if not str(target).startswith(str(home)) and not str(target).startswith("/tmp"):
            return web.Response(text="Access denied: path outside user home", status=403)

        if not target.exists():
            return web.Response(
                text=f"<!DOCTYPE html><html><head><title>404 Not Found</title><meta name='viewport' content='width=device-width, initial-scale=1'><style>body{{font-family:sans-serif;background:#0f172a;color:#f8fafc;padding:2rem;}}a{{color:#38bdf8;}}</style></head><body><h2>File or Directory Not Found</h2><p><code>{target}</code> does not exist.</p><p><a href='/'>← Back to OptChat</a></p></body></html>",
                content_type="text/html",
                status=404,
            )

        # Directory listing
        if target.is_dir():
            try:
                entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except Exception as e:
                return web.Response(text=f"Cannot list directory: {e}", status=500)

            # Build breadcrumbs
            parts = target.parts
            crumbs = []
            accum = Path(parts[0])
            for p in parts[1:]:
                accum = accum / p
                crumbs.append(f"<a href='/api/file?path={urllib.parse.quote(str(accum))}' style='color:#38bdf8;text-decoration:none;'>{p}</a>")
            crumb_html = " / ".join(crumbs)

            parent_link = ""
            if target != target.parent and str(target.parent).startswith(str(home)):
                parent_link = f"<div style='margin-bottom:12px;'><a href='/api/file?path={urllib.parse.quote(str(target.parent))}' style='color:#94a3b8;text-decoration:none;'>📁 <b>.. (Parent Directory)</b></a></div>"

            items_html = []
            for item in entries:
                try:
                    is_d = item.is_dir()
                    name = item.name + ("/" if is_d else "")
                    icon = "📁" if is_d else "📄"
                    color = "#38bdf8" if is_d else "#e2e8f0"
                    size_str = ""
                    if not is_d:
                        sz = item.stat().st_size
                        if sz > 1024 * 1024:
                            size_str = f"{sz / (1024*1024):.1f} MB"
                        elif sz > 1024:
                            size_str = f"{sz / 1024:.1f} KB"
                        else:
                            size_str = f"{sz} B"
                    link = f"/api/file?path={urllib.parse.quote(str(item))}"
                    items_html.append(
                        f"<li style='padding:8px 0;display:flex;justify-content:space-between;border-bottom:1px solid rgba(255,255,255,0.06);'>"
                        f"  <a href='{link}' style='color:{color};text-decoration:none;word-break:break-all;'>{icon} {name}</a>"
                        f"  <span style='color:#64748b;font-size:0.85rem;margin-left:12px;white-space:nowrap;'>{size_str}</span>"
                        f"</li>"
                    )
                except Exception:
                    continue

            html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{target.name}/ - OptChat File Browser</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: #0f172a;
      color: #f8fafc;
      margin: 0;
      padding: 1.25rem;
      line-height: 1.5;
    }}
    .container {{
      max-width: 900px;
      margin: 0 auto;
      background: #1e293b;
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 10px;
      padding: 1.25rem;
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
      border-bottom: 1px solid rgba(255,255,255,0.1);
      padding-bottom: 0.75rem;
    }}
    .back-btn {{
      background: #334155;
      color: #f8fafc;
      padding: 6px 12px;
      border-radius: 6px;
      text-decoration: none;
      font-size: 0.85rem;
    }}
    ul {{ list-style: none; padding: 0; margin: 0; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div style="word-break: break-all;"><b>Directory:</b> /{crumb_html}</div>
      <a href="/" class="back-btn">💬 Back to Chat</a>
    </div>
    {parent_link}
    <ul>
      {"".join(items_html) if items_html else "<li style='color:#64748b;padding:12px 0;'>Directory is empty</li>"}
    </ul>
  </div>
</body>
</html>"""
            return web.Response(text=html, content_type="text/html")

        # It's a file
        if request.query.get("raw") == "1":
            return web.FileResponse(target)

        # Image extensions
        img_exts = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico"}
        if target.suffix.lower() in img_exts:
            return web.FileResponse(target)

        # Text / code file preview
        try:
            stat = target.stat()
            if stat.st_size > 5 * 1024 * 1024:
                return web.FileResponse(target)
            content = target.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return web.FileResponse(target)

        escaped_content = html_lib.escape(content)
        raw_url = f"/api/file?path={urllib.parse.quote(str(target))}&raw=1"
        parent_url = f"/api/file?path={urllib.parse.quote(str(target.parent))}"

        file_html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{target.name} - OptChat File Viewer</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <style>
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: #0f172a;
      color: #f8fafc;
      margin: 0;
      padding: 1rem;
      line-height: 1.4;
    }}
    .container {{
      max-width: 1000px;
      margin: 0 auto;
      background: #1e293b;
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 10px;
      overflow: hidden;
    }}
    .header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: #0f172a;
      padding: 0.75rem 1rem;
      border-bottom: 1px solid rgba(255,255,255,0.1);
      flex-wrap: wrap;
      gap: 8px;
    }}
    .btn {{
      background: #334155;
      color: #f8fafc;
      padding: 5px 10px;
      border-radius: 5px;
      text-decoration: none;
      font-size: 0.8rem;
    }}
    .btn:hover {{ background: #475569; }}
    pre {{
      margin: 0;
      padding: 1rem;
      overflow-x: auto;
      font-family: "JetBrains Mono", Menlo, Consolas, Monaco, monospace;
      font-size: 0.88rem;
      line-height: 1.5;
      color: #e2e8f0;
      background: #1e293b;
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header">
      <div style="font-weight: 600; color: #38bdf8; word-break: break-all;">📄 {target.name}</div>
      <div style="display: flex; gap: 8px;">
        <a href="{parent_url}" class="btn">📁 Folder</a>
        <a href="{raw_url}" class="btn" download>⬇ Raw</a>
        <a href="/" class="btn">💬 Chat</a>
      </div>
    </div>
    <pre><code>{escaped_content}</code></pre>
  </div>
</body>
</html>"""
        return web.Response(text=file_html, content_type="text/html")

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

        if "model" in data and data["model"]:
            self.model = data["model"].strip()
            if self.main_provider:
                self.main_provider.model = self.model

        return web.json_response({
            "status": "ok",
            "workspace": str(self.workspace),
            "conversation_id": self.conversation_id,
            "model": self.model,
        })

    async def handle_api_models(self, request: web.Request) -> web.Response:
        models = [
            {"id": "gemini-3.8-flash-high", "name": "Gemini 3.8 Flash (High)", "provider": "google"},
            {"id": "gemini-3.8-flash-medium", "name": "Gemini 3.8 Flash (Medium)", "provider": "google"},
            {"id": "gemini-3.8-flash-low", "name": "Gemini 3.8 Flash (Low)", "provider": "google"},
            {"id": "gemini-4-argon-high", "name": "Gemini 4 Argon (High)", "provider": "google"},
            {"id": "gemini-4-argon-medium", "name": "Gemini 4 Argon (Medium)", "provider": "google"},
            {"id": "gemini-4-argon-low", "name": "Gemini 4 Argon (Low)", "provider": "google"},
            {"id": "claude-sonnet-5-5-high", "name": "Claude Sonnet 5.5 (High)", "provider": "anthropic"},
            {"id": "claude-sonnet-5-5-medium", "name": "Claude Sonnet 5.5 (Medium)", "provider": "anthropic"},
            {"id": "claude-sonnet-5-5-low", "name": "Claude Sonnet 5.5 (Low)", "provider": "anthropic"},
            {"id": "claude-opus-5-5-high", "name": "Claude Opus 5.5 (High)", "provider": "anthropic"},
            {"id": "claude-opus-5-5-medium", "name": "Claude Opus 5.5 (Medium)", "provider": "anthropic"},
            {"id": "claude-opus-5-5-low", "name": "Claude Opus 5.5 (Low)", "provider": "anthropic"},
            {"id": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)", "provider": "openai"},
        ]
        return web.json_response({
            "models": models,
            "current_model": self.model or "gemini-3.8-flash-high",
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
                "model": self.model or "gemini-3.8-flash-high",
                "workspaces": st.get("workspaces", []),
            })

        assert self.storage is not None and self.view is not None
        # Discover known workspaces from offline storage
        known_workspaces = set()
        if self.workspace and is_valid_workspace_name(self.workspace.name):
            known_workspaces.add(self.workspace.name)
        elif os.getcwd():
            cwd_name = Path.cwd().name
            if is_valid_workspace_name(cwd_name):
                known_workspaces.add(cwd_name)

        if self.storage and self.storage.messages:
            for m in self.storage.messages:
                if m.workspace and is_valid_workspace_name(m.workspace):
                    known_workspaces.add(m.workspace)
                elif m.text and m.text.startswith("["):
                    mat = re.match(r"^\[([a-zA-Z0-9_\-]+)\](?:\s|$)", m.text)
                    if mat:
                        lbl = mat.group(1)
                        if is_valid_workspace_name(lbl):
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
            "model": self.model or "gemini-3.8-flash-high",
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
                or (not m.workspace and m.text.strip().lower().startswith(f"[{ws_target}]"))
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
            ws_target = ws_filter.lower()
            ws_pat = re.compile(r"\|\w+:\s*\[" + re.escape(ws_target) + r"\]", re.IGNORECASE)
            lines = [l for l in lines if ws_pat.search(l)]
        return web.json_response({
            "size": self.view.compute_size(),
            "budget": self.view.budget,
            "lines": lines,
        })

    def broadcast_stream_event(self, event_type: str, content: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """Broadcast an event to all connected /api/stream clients."""
        event_obj = {"type": event_type, "content": content}
        if extra:
            event_obj.update(extra)
        for q in list(self._active_stream_queues):
            try:
                q.put_nowait(event_obj)
            except Exception:
                pass

    def resolve_workspace_dir(self, ws_name: str) -> Path:
        """Resolve workspace name to its directory path on disk."""
        ws_clean = ws_name.strip()
        if not ws_clean or ws_clean in ("all", "desk"):
            return self.workspace

        # 1. Exact path if provided
        p = Path(ws_clean).expanduser().resolve()
        if p.is_dir():
            return p

        # 2. Check if matches current workspace
        if self.workspace and self.workspace.name.lower() == ws_clean.lower() and self.workspace.is_dir():
            return self.workspace

        # 3. Search common project directories
        home = Path.home()
        candidates = [
            home / ws_clean,
            home / "src" / ws_clean,
            home / "src" / "alpeware" / ws_clean,
            home / "src" / "kaggle" / ws_clean,
        ]
        for c in candidates:
            if c and c.is_dir():
                return c.resolve()

        # 4. Search under ~/src/ (one level deep)
        src_dir = home / "src"
        if src_dir.is_dir():
            try:
                for sub in src_dir.iterdir():
                    if sub.is_dir():
                        target = sub / ws_clean
                        if target.is_dir():
                            return target.resolve()
            except Exception:
                pass

        logger.warning("Could not resolve workspace path for '%s'; falling back to %s", ws_name, self.workspace)
        return self.workspace

    async def run_workspace_agy_turn(
        self,
        ws_dir: Path,
        ws_name: str,
        message: str,
        req_id: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        """Execute a turn directly in a specific workspace using agy -c simulating CLI behavior."""
        logger.info("Executing agy -c in workspace '%s' (%s) [req_id=%s, model=%s]", ws_name, ws_dir, req_id, model)

        extra_info: Dict[str, Any] = {"workspace": ws_name}
        if req_id:
            extra_info["req_id"] = req_id

        self.broadcast_stream_event("settling", f"Connecting to {ws_name} session...", extra=extra_info)

        agy_bin = shutil.which("agy") or "/home/simonpure/.local/bin/agy"
        cmd = [
            agy_bin,
            "-c",
            "--dangerously-skip-permissions",
            "--disable-slash-commands",
            "--output-format",
            "stream-json",
        ]
        selected_model = model or self.model
        if selected_model:
            cmd.extend(["--model", selected_model])

        env = {**os.environ}

        full_response_chunks: List[str] = []
        final_response_text = ""
        usage_data: Dict[str, Any] = {}
        t0 = time.monotonic()
        is_error = False
        is_timeout = False

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                cwd=str(ws_dir),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            async def write_stdin():
                assert proc.stdin is not None
                try:
                    proc.stdin.write(message.encode("utf-8"))
                    await proc.stdin.drain()
                finally:
                    proc.stdin.close()
                    await proc.stdin.wait_closed()

            async def read_stdout():
                nonlocal final_response_text, usage_data
                assert proc.stdout is not None
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    raw = line.decode("utf-8", errors="replace").strip()
                    if not raw:
                        continue
                    try:
                        event_data = json.loads(raw)
                        event_type = event_data.get("event")
                        if event_type == "step_update":
                            su = event_data.get("step_update", {})
                            delta = su.get("text_delta")
                            if delta:
                                full_response_chunks.append(delta)
                                self.broadcast_stream_event("text", delta, extra=extra_info)
                        elif event_type == "result":
                            res = event_data.get("result", {})
                            final_response_text = res.get("response", "")
                            usage = res.get("usage") or event_data.get("usage") or {}
                            if usage:
                                usage_data = usage
                    except Exception:
                        pass

            async def read_stderr():
                assert proc.stderr is not None
                while True:
                    err_line = await proc.stderr.readline()
                    if not err_line:
                        break

            await asyncio.wait_for(
                asyncio.gather(write_stdin(), read_stdout(), read_stderr()),
                timeout=self.timeout,
            )
            await proc.wait()

        except asyncio.TimeoutError:
            is_error = True
            is_timeout = True
            logger.error("Turn in workspace '%s' timed out after %ss [req_id=%s]", ws_name, self.timeout, req_id)
            if 'proc' in locals() and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            self.broadcast_stream_event("error", f"Request in {ws_name} timed out after {int(self.timeout)}s", extra=extra_info)
        except Exception as e:
            is_error = True
            logger.error("Error executing agy turn in workspace '%s': %s", ws_name, e)
            if 'proc' in locals() and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            self.broadcast_stream_event("error", f"Error in {ws_name}: {e}", extra=extra_info)

        duration_ms = int((time.monotonic() - t0) * 1000)
        in_tokens = int(usage_data.get("input_tokens") or usage_data.get("prompt_tokens") or 0)
        out_tokens = int(usage_data.get("output_tokens") or usage_data.get("completion_tokens") or 0)
        cache_tokens = int(usage_data.get("cache_read_tokens") or usage_data.get("cached_tokens") or 0)

        # Fallback to transcript if 0 tokens captured
        if in_tokens == 0 and out_tokens == 0:
            cand_tpath = None
            try:
                brain_dir = Path.home() / ".gemini" / "antigravity-cli" / "brain"
                if brain_dir.is_dir():
                    subdirs = sorted(
                        [d for d in brain_dir.iterdir() if d.is_dir()],
                        key=lambda d: d.stat().st_mtime,
                        reverse=True,
                    )
                    for d in subdirs[:3]:
                        tp = d / ".system_generated" / "logs" / "transcript.jsonl"
                        if tp.is_file():
                            cand_tpath = tp
                            break
            except Exception:
                pass
            if cand_tpath:
                try:
                    with open(cand_tpath, "r", encoding="utf-8", errors="replace") as f:
                        for line in reversed(f.readlines()[-30:]):
                            try:
                                d = json.loads(line)
                                if d.get("type") == "PLANNER_RESPONSE":
                                    in_tokens += d.get("input_tokens", 0)
                                    out_tokens += d.get("output_tokens", 0)
                                    cache_tokens += d.get("cache_read_tokens", 0)
                                    if d.get("content"):
                                        break
                            except Exception:
                                continue
                except Exception:
                    pass

        turn_status = "timeout" if is_timeout else ("error" if is_error else "ok")
        if getattr(self, "telemetry", None):
            try:
                self.telemetry.record_turn(
                    workspace=ws_name,
                    model=selected_model or "default",
                    input_tokens=in_tokens,
                    output_tokens=out_tokens,
                    cache_read_tokens=cache_tokens,
                    req_id=req_id,
                    source="web",
                    role="main",
                    provider="agy",
                    duration_ms=duration_ms,
                    status=turn_status,
                )
            except Exception as e:
                logger.error("Failed recording web turn telemetry: %s", e)

        reply = final_response_text.strip() or "".join(full_response_chunks).strip()
        if reply:
            self.broadcast_stream_event("log_talk", reply, extra=extra_info)

        if in_tokens > 0 or out_tokens > 0:
            extra_info["tokens"] = {
                "input": in_tokens,
                "output": out_tokens,
                "cache_read": cache_tokens,
                "total": in_tokens + out_tokens,
                "cost_usd": estimate_cost_usd(selected_model or "default", in_tokens, out_tokens, cache_tokens),
            }

        self.broadcast_stream_event("turn_complete", "Turn finished.", extra=extra_info)

    async def handle_api_chat(self, request: web.Request) -> web.Response:
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON body"}, status=400)

        message = data.get("message", "").strip()
        if not message:
            return web.json_response({"error": "Message cannot be empty"}, status=400)

        ws_req = (data.get("workspace") or "all").strip().lower()
        req_id = data.get("req_id")
        req_model = data.get("model")
        if req_model:
            self.model = req_model

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

            if ws_req in ("all", "desk"):
                assert self.agent is not None
                if req_model and self.main_provider:
                    self.main_provider.model = req_model
                turn_task = asyncio.create_task(self.agent.submit_user_message(message))
            else:
                ws_dir = self.resolve_workspace_dir(ws_req)
                turn_task = asyncio.create_task(
                    self.run_workspace_agy_turn(ws_dir, ws_req, message, req_id=req_id, model=req_model or self.model)
                )

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
        if ws_req in ("all", "desk"):
            assert self.agent is not None
            if req_model and self.main_provider:
                self.main_provider.model = req_model
            asyncio.create_task(self.agent.submit_user_message(message))
        else:
            ws_dir = self.resolve_workspace_dir(ws_req)
            asyncio.create_task(
                self.run_workspace_agy_turn(ws_dir, ws_req, message, req_id=req_id, model=req_model or self.model)
            )

        return web.json_response({
            "status": "queued",
            "message": message,
            "workspace": ws_req,
            "req_id": req_id,
            "model": req_model or self.model,
        })

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
