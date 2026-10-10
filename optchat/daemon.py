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
import re
import signal
import sys
from typing import Any, Dict, List, Optional, Set

from optchat.compactor import Compactor
from optchat.constants import VIEW
from optchat.providers import create_provider
from optchat.storage import Message, Storage
from optchat.tree import node_coords
from optchat.view import LiveView
from optchat.visualizer import export_html_to_file

logger = logging.getLogger("optchat.daemon")


def clean_message_text(text: Any) -> str:
    """Strip extraneous escaped JSON quotes or formatting from model reports."""
    if not isinstance(text, str):
        return str(text)
    s = text.strip()
    if s.startswith('"') and s.endswith('"') and len(s) >= 2:
        try:
            return json.loads(s)
        except Exception:
            s = s[1:-1]
    elif s.startswith('"') and not s.endswith('"'):
        s = s[1:]
    return s.strip()


def extract_subagent_report_from_transcript(transcript_path: str) -> Optional[str]:
    """Extract subagent report from its transcript.jsonl if send_message wasn't called."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    report: Optional[str] = None
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    step = json.loads(line)
                    # 1. Prefer send_message tool call
                    for tc in step.get("tool_calls", []):
                        if tc.get("name") == "send_message":
                            msg = tc.get("args", {}).get("Message")
                            if msg:
                                report = msg
                    # 2. Check for final assistant content if no send_message yet
                    if not report and step.get("type") == "PLANNER_RESPONSE" and step.get("content"):
                        report = step.get("content")
                except Exception:
                    continue
    except Exception as e:
        logger.debug("Failed reading subagent transcript %s: %s", transcript_path, e)
    return report


def extract_latest_turn_from_transcript(transcript_path: str) -> Optional[Dict[str, Any]]:
    """Extract the most recent completed turn (step_index, user_text, reply_text) from transcript.jsonl."""
    from optchat.importer import extract_user_text
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    if "agy_isolated" in transcript_path or "compactor_workspace" in transcript_path:
        return None

    last_reply = None
    last_reply_idx = None
    last_user = None

    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()

        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except Exception:
                continue

            stype = data.get("type")
            if last_reply is None and stype == "PLANNER_RESPONSE":
                content = data.get("content")
                if content and content.strip():
                    last_reply = content.strip()
                    last_reply_idx = data.get("step_index", 0)
            elif last_reply is not None and stype == "USER_INPUT":
                raw_u = data.get("content", "")
                if (
                    "CRITICAL REQUIREMENT: Output ONLY" in raw_u
                    or "You write the memory of OptChat" in raw_u
                    or "CRITICAL HOST DIRECTIVE" in raw_u
                ):
                    return None
                last_user = extract_user_text(raw_u)
                break
    except Exception as e:
        logger.debug("Failed reading transcript %s: %s", transcript_path, e)
        return None

    if last_reply is not None and last_user is not None and last_reply_idx is not None:
        return {
            "step_index": last_reply_idx,
            "user": last_user,
            "reply": last_reply,
        }
    return None


def extract_workspace_from_transcript(transcript_path: str) -> Optional[str]:
    """Inspect transcript.jsonl tool calls to detect workspace directory."""
    if not transcript_path or not os.path.isfile(transcript_path):
        return None
    try:
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        for line in reversed(lines):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except Exception:
                continue
            calls = data.get("tool_calls", [])
            for c in calls:
                args = c.get("args", {})
                cwd = args.get("Cwd")
                if cwd and isinstance(cwd, str):
                    clean_cwd = cwd.strip("\"'")
                    if clean_cwd and not clean_cwd.startswith("/tmp") and "agy_isolated" not in clean_cwd:
                        return Path(clean_cwd).name
                tfile = args.get("TargetFile") or args.get("AbsolutePath")
                if tfile and isinstance(tfile, str):
                    clean_file = tfile.strip("\"'")
                    if clean_file and not clean_file.startswith("/tmp") and "agy_isolated" not in clean_file:
                        p = Path(clean_file).parent
                        while p != p.parent:
                            if (p / ".git").exists():
                                return p.name
                            p = p.parent
                        return Path(clean_file).parent.name
    except Exception as e:
        logger.debug("Failed extracting workspace from transcript %s: %s", transcript_path, e)
    return None


class OptChatDaemon:
    def __init__(
        self,
        chat_dir: Optional[Path] = None,
        compactor_model: Optional[str] = None,
        socket_path: Optional[Path] = None,
        provider_name: str = "agy",
        sandbox: str = "none",
        enable_web: bool = False,
        web_port: int = 8765,
        web_host: str = "0.0.0.0",
        workspace: Optional[Path] = None,
    ):
        self.chat_dir = Path(chat_dir or os.path.expanduser("~/.optchat")).resolve()
        self.socket_path = Path(socket_path or (self.chat_dir / "engine.sock")).resolve()
        self.compactor_model = compactor_model
        self.provider_name = provider_name
        self.sandbox_choice = sandbox
        self.enable_web = enable_web
        self.web_port = web_port
        self.web_host = web_host
        self.workspace = Path(workspace).resolve() if workspace else None
        self.web_process: Optional[asyncio.subprocess.Process] = None
        self._web_monitor_task: Optional[asyncio.Task] = None
        self._skip_execv_for_test: bool = False

        from optchat.sandbox import SandboxManager, SandboxBackend
        self.sandbox_backend = SandboxManager.detect_backend(sandbox)
        self.sandbox_config = SandboxManager.create_default_config(
            workspace=Path.cwd(),
            backend=self.sandbox_backend,
            optchat_dir=self.chat_dir,
        )

        self.storage: Optional[Storage] = None
        self.view: Optional[LiveView] = None
        self.compactor: Optional[Compactor] = None
        self.server: Optional[asyncio.Server] = None
        self._running = False
        self._event_subscribers: Set[asyncio.Queue[Dict[str, Any]]] = set()
        self._active_subagents: Dict[str, Dict[str, Any]] = {}
        self._logged_turn_steps: Dict[str, int] = {}

    def broadcast(self, event_type: str, content: str, extra: Optional[Dict[str, Any]] = None) -> None:
        """Broadcast real-time event to all connected subscriber clients (Web UI / CLI)."""
        evt = {"type": event_type, "content": content, "extra": extra or {}}
        for q in list(self._event_subscribers):
            try:
                q.put_nowait(evt)
            except Exception:
                pass

    def _resolve_workspace_name(self, payload: Dict[str, Any], tpath: Optional[str] = None) -> str:
        """Auto-detect real workspace name from caller CWD, payload, transcript, or fallbacks."""
        caller_cwd = payload.get("callerCwd")
        ws_paths = payload.get("workspacePaths") or []
        if caller_cwd:
            p = Path(caller_cwd)
            if p.name not in ("", "tmp", "config") and not str(p).endswith(".gemini/config") and "agy_isolated" not in str(p):
                return p.name
        if ws_paths and ws_paths[0]:
            p = Path(ws_paths[0])
            if p.name not in ("", "tmp", "config") and not str(p).endswith(".gemini/config") and "agy_isolated" not in str(p):
                return p.name
        if tpath:
            from_tpath = extract_workspace_from_transcript(tpath)
            if from_tpath:
                return from_tpath
        ws_target = (str(self.workspace) if self.workspace else None) or os.getcwd()
        return Path(ws_target).name if ws_target else "general"

    async def _start_web_server(self) -> None:
        """Start the responsive web interface as a managed child subprocess."""
        cmd = [
            sys.executable,
            "-m",
            "optchat.cli",
            "web",
            "--host",
            self.web_host,
            "--port",
            str(self.web_port),
            "--chat-dir",
            str(self.chat_dir),
            "--provider",
            self.provider_name,
        ]
        if self.workspace:
            cmd.extend(["--workspace", str(self.workspace)])
        if self.compactor_model:
            cmd.extend(["--compactor-model", str(self.compactor_model)])

        logger.info("Starting managed web server subprocess on %s:%d: %s", self.web_host, self.web_port, " ".join(cmd))
        try:
            self.web_process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=None,
                stderr=None,
            )
            logger.info("Managed web server started with PID %d", self.web_process.pid)
            self._web_monitor_task = asyncio.create_task(self._monitor_web_server())
        except Exception as e:
            logger.error("Failed to start managed web server subprocess: %s", e)

    async def _monitor_web_server(self) -> None:
        """Monitor the web server subprocess and respawn if it unexpectedly exits."""
        while self._running and self.web_process:
            try:
                ret = await self.web_process.wait()
                if not self._running:
                    break
                logger.warning("Managed web server (PID %s) exited unexpectedly with code %s", getattr(self.web_process, "pid", None), ret)
                await asyncio.sleep(2.0)
                if self._running and self.enable_web:
                    logger.info("Respawning managed web server...")
                    await self._start_web_server()
                break
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Error monitoring web server: %s", e)
                break

    async def start(self) -> None:
        """Start the storage, compactor, and IPC socket server."""
        self.chat_dir.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(self.chat_dir)
        self.storage.open()

        self.view = LiveView(self.storage)
        self.view.rebuild()

        from optchat.sandbox import SandboxBackend
        if self.sandbox_backend != SandboxBackend.NONE:
            logger.info("OptChat Daemon active with sandbox: %s", self.sandbox_backend.value)

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
            limit=16 * 1024 * 1024,
        )
        self._running = True
        logger.info("OptChat Daemon listening on %s", self.socket_path)

        if self.enable_web:
            await self._start_web_server()

    async def stop(self) -> None:
        """Clean shutdown of IPC server, compactor, web server, and storage."""
        self._running = False
        if self._web_monitor_task and not self._web_monitor_task.done():
            self._web_monitor_task.cancel()
            try:
                await self._web_monitor_task
            except (asyncio.CancelledError, Exception):
                pass
            self._web_monitor_task = None

        if self.web_process:
            try:
                if self.web_process.returncode is None:
                    logger.info("Stopping managed web server (PID %d)...", self.web_process.pid)
                    self.web_process.terminate()
                    try:
                        await asyncio.wait_for(self.web_process.wait(), timeout=3.0)
                    except asyncio.TimeoutError:
                        logger.warning("Web server did not terminate in time, killing...")
                        self.web_process.kill()
                        await self.web_process.wait()
            except Exception as e:
                logger.warning("Error stopping web server: %s", e)
            finally:
                self.web_process = None

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
                    if req.get("action") == "subscribe_events":
                        await self._handle_subscription(reader, writer)
                        return
                    res = await self._dispatch(req)
                except Exception as e:
                    res = {"status": "error", "error": str(e)}

                payload = json.dumps(res) + "\n"
                writer.write(payload.encode("utf-8"))
                await writer.drain()
        except (asyncio.CancelledError, ConnectionResetError):
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass

    async def _handle_subscription(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Handle long-lived event subscription stream for UI / clients."""
        queue: asyncio.Queue[Dict[str, Any]] = asyncio.Queue()
        self._event_subscribers.add(queue)
        try:
            ack = json.dumps({"status": "subscribed"}) + "\n"
            writer.write(ack.encode("utf-8"))
            await writer.drain()

            while self._running:
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    line = json.dumps(evt) + "\n"
                    writer.write(line.encode("utf-8"))
                    await writer.drain()
                except asyncio.TimeoutError:
                    writer.write(b": keep-alive\n")
                    await writer.drain()
        except (asyncio.CancelledError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            self._event_subscribers.discard(queue)

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
            web_info = None
            if self.enable_web:
                web_info = {
                    "enabled": True,
                    "host": self.web_host,
                    "port": self.web_port,
                    "running": self.web_process is not None and self.web_process.returncode is None,
                    "pid": self.web_process.pid if self.web_process else None,
                }

            # Discover known workspaces from recent messages + current active workspace
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

            return {
                "status": "ok",
                "messages_count": len(self.storage.messages),
                "tree_nodes_count": len(self.storage.tree),
                "view_size": size,
                "view_budget": self.view.budget,
                "is_settled": self.view.is_settled(),
                "web_server": web_info,
                "workspaces": sorted(list(known_workspaces)),
            }

        elif action == "restart":
            logger.info("Daemon received restart request via IPC.")
            try:
                loop = asyncio.get_running_loop()
                loop.call_later(0.1, lambda: asyncio.create_task(self._perform_restart()))
            except Exception as e:
                logger.warning("Failed scheduling restart: %s", e)
            return {"status": "ok", "message": "Daemon restart scheduled."}

        elif action == "get_view":
            ws_filter = req.get("workspace")
            raw_view = self.view.render()
            lines = [part.render(self.storage) for part in self.view.parts]

            if ws_filter and ws_filter.lower() != "all":
                tag = f"[{ws_filter.lower()}]"
                filtered_lines = [l for l in lines if tag in l.lower()]
                if filtered_lines:
                    rendered_view = "<chat>\n" + "\n".join(filtered_lines) + "\n</chat>"
                else:
                    rendered_view = f"<chat>\n(no entries for workspace [{ws_filter}])\n</chat>"
                return {
                    "status": "ok",
                    "view": rendered_view,
                    "is_settled": self.view.is_settled(),
                    "size": len(rendered_view.encode("utf-8")),
                    "lines": filtered_lines,
                }

            return {
                "status": "ok",
                "view": raw_view,
                "is_settled": self.view.is_settled(),
                "size": self.view.compute_size(),
                "lines": lines,
            }

        elif action == "get_history":
            limit = int(req.get("limit", 30))
            ws_filter = req.get("workspace")
            category_filter = req.get("category")
            msgs = self.storage.messages if self.storage.messages else []

            if ws_filter and ws_filter.lower() != "all":
                ws_target = ws_filter.lower()
                msgs = [
                    m for m in msgs
                    if (m.workspace and m.workspace.lower() == ws_target)
                    or (f"[{ws_target}]" in m.text.lower())
                ]

            if category_filter and category_filter.lower() != "all":
                cat = category_filter.lower()
                if cat == "main":
                    msgs = [m for m in msgs if m.kind in ("user", "talk")]
                elif cat == "subagent":
                    msgs = [m for m in msgs if m.kind == "work"]
                elif cat == "note":
                    msgs = [m for m in msgs if m.kind == "note"]

            recent = msgs[-limit:] if msgs else []
            return {
                "status": "ok",
                "messages": [
                    {
                        "i": m.i,
                        "kind": m.kind,
                        "text": m.text,
                        "size": m.size,
                        "date": m.date,
                        "workspace": m.workspace,
                        "device": m.device,
                    }
                    for m in recent
                ],
            }

        elif action == "append_message":
            kind = req.get("kind", "note")
            text = req.get("text", "")
            workspace = req.get("workspace")
            device = req.get("device")
            if not text:
                return {"status": "error", "error": "Text cannot be empty"}
            msg = self.storage.append_message(kind, text, workspace=workspace, device=device)
            self.view.on_new_message(msg.i)
            if self.compactor:
                self.compactor.pump()
            self.broadcast(
                "new_message",
                text,
                extra={
                    "kind": kind,
                    "i": msg.i,
                    "date": msg.date,
                    "workspace": msg.workspace,
                    "device": msg.device,
                },
            )
            return {
                "status": "ok",
                "message": msg.to_dict(),
            }

        elif action == "zoom":
            id_ = int(req.get("id", 0))
            n = int(req.get("n", 1))
            l, i = node_coords(id_, n)
            if n == 1:
                msg = self.storage.get_message(i)
                if msg:
                    meta_parts = []
                    if msg.workspace:
                        meta_parts.append(f"workspace: {msg.workspace}")
                    if msg.device:
                        meta_parts.append(f"device: {msg.device}")
                    meta_parts.append(f"date: {msg.date}")
                    meta_str = f" [{', '.join(meta_parts)}]" if meta_parts else ""
                    out = f"Verbatim Message {id_} ({msg.kind}){meta_str}:\n{msg.text}"
                    msg_dict = {
                        "i": msg.i,
                        "kind": msg.kind,
                        "text": msg.text,
                        "date": msg.date,
                        "workspace": msg.workspace,
                        "device": msg.device,
                    }
                else:
                    out = f"Message {id_} not found."
                    msg_dict = None
                return {"status": "ok", "output": out, "id": id_, "n": 1, "is_leaf": True, "message": msg_dict}

            node = self.storage.get_node(l, i)
            child_n = n // 2
            left_node = self.storage.get_node(l - 1, 2 * i)
            right_node = self.storage.get_node(l - 1, 2 * i + 1)
            left_text = left_node.text if left_node else "(summarizing...)"
            right_text = right_node.text if right_node else "(summarizing...)"
            left_str = f"{id_}+{child_n}|{left_text}"
            right_str = f"{id_ + child_n}+{child_n}|{right_text}"
            out = f"Zoomed {id_}+{n} (Level {l}):\n  {left_str}\n  {right_str}"
            children = [
                {"id": id_, "n": child_n, "text": left_text},
                {"id": id_ + child_n, "n": child_n, "text": right_text},
            ]
            return {
                "status": "ok",
                "output": out,
                "id": id_,
                "n": n,
                "is_leaf": False,
                "children": children,
            }

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

        elif action == "sync_export":
            since_idx = int(req.get("since_idx", 0))
            from optchat.sync import export_sync_payload
            return {"status": "ok", "payload": export_sync_payload(self.storage, since_idx)}

        elif action == "sync_apply":
            sync_payload = req.get("payload", {})
            from optchat.sync import apply_sync_payload
            res = apply_sync_payload(self.storage, sync_payload)
            self.view.rebuild()
            if self.compactor:
                self.compactor.pump()
            self.broadcast(
                "sync_applied",
                f"Sync applied: +{res.get('imported_messages', 0)} messages, +{res.get('imported_nodes', 0)} nodes",
                extra=res,
            )
            return res

        elif action == "exec_sandboxed":
            cmd = req.get("cmd", [])
            ws = Path(req.get("workspace", os.getcwd()))
            from optchat.sandbox import SandboxManager
            cfg = SandboxManager.create_default_config(ws, backend=self.sandbox_backend, optchat_dir=self.chat_dir)
            wrapped = SandboxManager.wrap_command(cmd, cfg)
            env = SandboxManager.filter_env(cfg)
            proc = await asyncio.create_subprocess_exec(
                *wrapped,
                env={**os.environ, **env},
                cwd=str(ws),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            return {
                "status": "ok",
                "returncode": proc.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            }

        elif action == "hook_event":
            event_name = req.get("event", "")
            payload = req.get("payload", {})
            return await self._handle_hook_event(event_name, payload)

        return {"status": "error", "error": f"Unknown action: {action}"}

    async def _handle_hook_event(self, event: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        """Handle events from Antigravity hooks (PreInvocation, PreToolUse, PostToolUse, PostInvocation, Stop)."""
        conv_id = payload.get("conversationId", "unknown")
        parent_id = payload.get("parentConversationId")
        workspace = (payload.get("workspacePaths") or [""])[0]
        agent_name = payload.get("agentName", "")
        tpath = payload.get("transcriptPath", "")

        is_synthetic = (
            "agy_isolated" in str(workspace)
            or "agy_isolated" in str(tpath)
            or "compactor_workspace" in str(workspace)
            or (workspace and "/tmp" in workspace and "pytest" not in workspace and "test" not in workspace)
        )
        if is_synthetic and event != "pre_invocation":
            return {"status": "ok", "ignored": "synthetic_run"}

        if event == "pre_invocation":
            assert self.view is not None
            # Return the latest settled compressed view for injection into the session
            view_content = self.view.render()
            return {
                "status": "ok",
                "view": view_content,
                "is_settled": self.view.is_settled(),
            }

        elif event == "pre_tool":
            tool_call = payload.get("toolCall", {})
            name = tool_call.get("name", "")
            args = tool_call.get("args", {})
            if name == "invoke_subagent":
                subagents = args.get("Subagents", [])
                if isinstance(subagents, str):
                    try:
                        subagents = json.loads(subagents)
                    except Exception:
                        subagents = []
                for sub in subagents:
                    role = sub.get("Role", "Subagent")
                    tname = sub.get("TypeName", "subagent")
                    prompt = sub.get("Prompt", "")
                    summary = f"Spawned subagent: {role} ({tname}) — {prompt[:120]}"
                    self.broadcast("subagent_spawn", summary, extra={"role": role, "typeName": tname, "prompt": prompt})
            return {"status": "ok"}

        elif event == "post_tool":
            tool_call = payload.get("toolCall", {})
            name = tool_call.get("name", "unknown")
            args = tool_call.get("args", {})
            error = payload.get("error")
            result = payload.get("result")

            # 1. If parent agent tool call was invoke_subagent, register spawned conversation IDs
            if name == "invoke_subagent":
                subagents = args.get("Subagents", [])
                if isinstance(subagents, str):
                    try:
                        subagents = json.loads(subagents)
                    except Exception:
                        subagents = []
                spawned_ids = re.findall(r'"conversationId":\s*"([^"]+)"', str(result or ""))
                if spawned_ids:
                    for idx, cid in enumerate(spawned_ids):
                        sub = subagents[idx] if idx < len(subagents) else {}
                        role = sub.get("Role", "Subagent")
                        tname = sub.get("TypeName", "subagent")
                        prompt = sub.get("Prompt", "")
                        self._active_subagents[cid] = {"role": role, "typeName": tname, "prompt": prompt}
                        self.broadcast(
                            "subagent_spawn",
                            f"Spawned subagent: {role} ({tname}) — {prompt[:120]}",
                            extra={"role": role, "typeName": tname, "prompt": prompt, "conversationId": cid},
                        )
                else:
                    for sub in subagents:
                        role = sub.get("Role", "Subagent")
                        tname = sub.get("TypeName", "subagent")
                        prompt = sub.get("Prompt", "")
                        self.broadcast(
                            "subagent_spawn",
                            f"Spawned subagent: {role} ({tname}) — {prompt[:120]}",
                            extra={"role": role, "typeName": tname, "prompt": prompt},
                        )
                return {"status": "ok"}

            # 2. Subagent reporting back via send_message
            if name == "send_message":
                sub_info = self._active_subagents.get(conv_id, {})
                sub_role = sub_info.get("role") or agent_name or "Subagent"
                msg_content = args.get("Message", "")
                if msg_content:
                    clean_msg = clean_message_text(msg_content)
                    already_logged = False
                    if self.storage and self.storage.messages:
                        for m in self.storage.messages[-5:]:
                            if m.kind == "work" and (clean_msg[:60] in m.text or m.text[:60] in clean_msg):
                                already_logged = True
                                break
                    if not already_logged:
                        self.broadcast(
                            "subagent_report",
                            f"[{sub_role}] {clean_msg}",
                            extra={"role": sub_role, "report": clean_msg, "conversationId": conv_id},
                        )
                        if self.storage and self.view:
                            work_msg = self.storage.append_message("work", f"[{sub_role}] {clean_msg}")
                            self.view.on_new_message(work_msg.i)
                            if self.compactor:
                                self.compactor.pump()
                return {"status": "ok"}

            # 3. Intermediate tool execution inside a subagent
            if parent_id or (conv_id in self._active_subagents):
                sub_info = self._active_subagents.get(conv_id, {})
                sub_role = sub_info.get("role") or agent_name or "Subagent"
                args_str = json.dumps(args)
                if len(args_str) > 140:
                    args_str = args_str[:140] + "..."
                tool_desc = f"[{sub_role}] ⚡ {name}: {args_str}"
                if error:
                    tool_desc += f" (error: {error})"
                self.broadcast(
                    "subagent_tool",
                    tool_desc,
                    extra={
                        "role": sub_role,
                        "tool": name,
                        "args": args,
                        "error": error,
                        "result": str(result)[:300] if result else None,
                        "conversationId": conv_id,
                    },
                )
                return {"status": "ok"}

            return {"status": "ok"}

        elif event == "post_invocation":
            return {"status": "ok"}

        elif event == "stop":
            if parent_id or (conv_id in self._active_subagents):
                sub_info = self._active_subagents.pop(conv_id, {})
                sub_role = sub_info.get("role") or agent_name or "Subagent"
                self.broadcast(
                    "subagent_complete",
                    f"Subagent [{sub_role}] completed.",
                    extra={"role": sub_role, "conversationId": conv_id},
                )

                # Check if report needs to be extracted from transcript (e.g. if send_message was not used)
                tpath = payload.get("transcriptPath")
                if tpath and os.path.isfile(tpath):
                    report = extract_subagent_report_from_transcript(tpath)
                    if report:
                        clean_rep = clean_message_text(report)
                        already_logged = False
                        if self.storage and self.storage.messages:
                            for m in self.storage.messages[-5:]:
                                if m.kind == "work" and (clean_rep[:60] in m.text or m.text[:60] in clean_rep):
                                    already_logged = True
                                    break
                        if not already_logged:
                            sub_ws = self._resolve_workspace_name(payload, tpath)
                            self.broadcast(
                                "subagent_report",
                                f"[{sub_role}] {clean_rep}",
                                extra={"role": sub_role, "report": clean_rep, "workspace": sub_ws, "conversationId": conv_id},
                            )
                            if self.storage and self.view:
                                work_msg = self.storage.append_message("work", f"[{sub_role}] {clean_rep}", workspace=sub_ws)
                                self.view.on_new_message(work_msg.i)

                if self.compactor:
                    self.compactor.pump()
                return {"status": "ok"}

            # Main agent turn completed - automatically capture turn into OptChat memory
            tpath = payload.get("transcriptPath")
            if not tpath or not os.path.isfile(tpath):
                if conv_id and conv_id != "unknown":
                    cand = Path.home() / ".gemini" / "antigravity-cli" / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
                    if cand.is_file():
                        tpath = str(cand)

            if tpath and os.path.isfile(tpath):
                turn_info = extract_latest_turn_from_transcript(tpath)
                if turn_info:
                    step_idx = turn_info["step_index"]
                    last_logged = self._logged_turn_steps.get(conv_id, -1)
                    if step_idx > last_logged:
                        clean_user = turn_info["user"].strip()
                        clean_reply = clean_message_text(turn_info["reply"]).strip()

                        ws_name = self._resolve_workspace_name(payload, tpath)
                        if ws_name.startswith("agy_isolated") or "agy_isolated" in ws_name or ws_name == ".compactor_workspace":
                            self._logged_turn_steps[conv_id] = step_idx
                            return {"status": "ok", "ignored": "synthetic_workspace"}

                        # Deduplication check against recent messages in storage
                        already_logged = False
                        if self.storage and self.storage.messages:
                            for m in self.storage.messages[-6:]:
                                if clean_reply[:60] in m.text or m.text[:60] in clean_reply:
                                    already_logged = True
                                    break

                        if not already_logged and self.storage and self.view:
                            msg1 = self.storage.append_message("user", clean_user, workspace=ws_name)
                            self.view.on_new_message(msg1.i)

                            msg2 = self.storage.append_message("talk", clean_reply, workspace=ws_name)
                            self.view.on_new_message(msg2.i)

                            self._logged_turn_steps[conv_id] = step_idx
                            logger.info("Auto-logged turn #%d from [%s] into memory (msgs #%d, #%d)", step_idx, ws_name, msg1.i, msg2.i)

                            self.broadcast(
                                "new_message",
                                clean_user,
                                extra={
                                    "kind": "user",
                                    "i": msg1.i,
                                    "workspace": ws_name,
                                    "device": msg1.device,
                                    "conversationId": conv_id,
                                    "date": msg1.date,
                                },
                            )
                            self.broadcast(
                                "new_message",
                                clean_reply,
                                extra={
                                    "kind": "talk",
                                    "i": msg2.i,
                                    "workspace": ws_name,
                                    "device": msg2.device,
                                    "conversationId": conv_id,
                                    "date": msg2.date,
                                },
                            )
                            self.broadcast(
                                "turn_complete",
                                f"Logged turn #{step_idx} from [{ws_name}]",
                                extra={"workspace": ws_name, "conversationId": conv_id, "user_i": msg1.i, "talk_i": msg2.i},
                            )
                        else:
                            self._logged_turn_steps[conv_id] = step_idx

            # Turn loop finished; trigger compactor pump
            if self.compactor:
                self.compactor.pump()
            return {"status": "ok"}

        return {"status": "ok"}

    async def _perform_restart(self) -> None:
        """Clean shutdown and re-exec the daemon process."""
        logger.info("Performing daemon re-exec restart...")
        try:
            await self.stop()
        except Exception as e:
            logger.warning("Error during stop before restart: %s", e)

        if getattr(self, "_skip_execv_for_test", False):
            logger.info("In test mode: restarting daemon in-process...")
            await asyncio.sleep(0.1)
            await self.start()
            return


        await asyncio.sleep(0.3)
        sys.stdout.flush()
        sys.stderr.flush()
        os.execv(sys.executable, [sys.executable] + sys.argv)


async def run_daemon(
    chat_dir: Optional[Path] = None,
    compactor_model: Optional[str] = None,
    socket_path: Optional[Path] = None,
    provider_name: str = "agy",
    sandbox: str = "none",
    enable_web: bool = False,
    web_host: str = "0.0.0.0",
    web_port: int = 8765,
    workspace: Optional[Path] = None,
) -> None:
    daemon = OptChatDaemon(
        chat_dir=chat_dir,
        compactor_model=compactor_model,
        socket_path=socket_path,
        provider_name=provider_name,
        sandbox=sandbox,
        enable_web=enable_web,
        web_host=web_host,
        web_port=web_port,
        workspace=workspace,
    )
    await daemon.start()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    status_str = f"OptChat Daemon running [chat_dir={daemon.chat_dir}, socket={daemon.socket_path}]"
    if enable_web:
        status_str += f" [web=http://{web_host}:{web_port}]"
    print(status_str)
    try:
        await stop_event.wait()
    finally:
        await daemon.stop()



def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()
