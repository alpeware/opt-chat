"""Client for OptChat Daemon IPC Engine (~/.optchat/engine.sock).

Supports both fast synchronous calls (used by CLI hooks) and asynchronous calls
(used by the Web Server and MCP Server).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import socket
from typing import Any, AsyncIterator, Dict, List, Optional


class EngineClient:
    def __init__(self, socket_path: Optional[Path] = None):
        if socket_path:
            self.socket_path = Path(socket_path).resolve()
        else:
            chat_dir = Path(os.path.expanduser("~/.optchat")).resolve()
            self.socket_path = chat_dir / "engine.sock"

    def is_daemon_alive(self, timeout: float = 1.0) -> bool:
        """Check if the daemon socket is alive and responding."""
        if not self.socket_path.exists():
            return False
        try:
            res = self.call("ping", timeout=timeout)
            return res.get("status") == "ok"
        except Exception:
            return False

    async def is_daemon_alive_async(self, timeout: float = 1.0) -> bool:
        """Asynchronously check if daemon socket is alive."""
        if not self.socket_path.exists():
            return False
        try:
            res = await self.call_async("ping", timeout=timeout)
            return res.get("status") == "ok"
        except Exception:
            return False

    def restart(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Request daemon to restart itself and any supervised subprocesses."""
        return self.call("restart", timeout=timeout)

    async def restart_async(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Asynchronously request daemon to restart."""
        return await self.call_async("restart", timeout=timeout)

    def wait_for_daemon(self, timeout: float = 10.0, poll_interval: float = 0.2) -> bool:
        """Poll until daemon is alive and responding, up to timeout seconds."""
        import time
        start = time.time()
        while time.time() - start < timeout:
            if self.is_daemon_alive(timeout=min(poll_interval, 0.5)):
                return True
            time.sleep(poll_interval)
        return False

    def call(self, action: str, timeout: float = 5.0, **kwargs) -> Dict[str, Any]:

        """Synchronous IPC request to daemon (fast, sub-millisecond)."""
        if not self.socket_path.exists():
            raise ConnectionError(f"OptChat daemon is not running (socket not found at {self.socket_path})")

        payload = {"action": action, **kwargs}
        data = (json.dumps(payload) + "\n").encode("utf-8")

        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.connect(str(self.socket_path))
            s.sendall(data)
            chunks: List[bytes] = []
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            raw = b"".join(chunks).decode("utf-8").strip()
            if not raw:
                raise ConnectionError("Empty response from OptChat daemon")
            return json.loads(raw)
        finally:
            s.close()

    async def call_async(self, action: str, timeout: float = 10.0, **kwargs) -> Dict[str, Any]:
        """Asynchronous IPC request to daemon."""
        if not self.socket_path.exists():
            raise ConnectionError(f"OptChat daemon is not running (socket not found at {self.socket_path})")

        payload = {"action": action, **kwargs}
        data = (json.dumps(payload) + "\n").encode("utf-8")

        reader, writer = await asyncio.wait_for(
            asyncio.open_unix_connection(str(self.socket_path), limit=16 * 1024 * 1024),
            timeout=timeout,
        )
        try:
            writer.write(data)
            await writer.drain()
            line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            raw = line.decode("utf-8").strip()
            if not raw:
                raise ConnectionError("Empty response from OptChat daemon")
            return json.loads(raw)
        finally:
            writer.close()
            await writer.wait_closed()

    async def subscribe_events(self) -> AsyncIterator[Dict[str, Any]]:
        """Asynchronously stream events from the daemon event bus."""
        if not self.socket_path.exists():
            raise ConnectionError(f"OptChat daemon is not running (socket not found at {self.socket_path})")

        reader, writer = await asyncio.open_unix_connection(str(self.socket_path), limit=16 * 1024 * 1024)
        try:
            req = {"action": "subscribe_events"}
            writer.write((json.dumps(req) + "\n").encode("utf-8"))
            await writer.drain()

            ack_line = await reader.readline()
            ack = json.loads(ack_line.decode("utf-8"))
            if ack.get("status") != "subscribed":
                raise RuntimeError(f"Subscription failed: {ack}")

            while True:
                line = await reader.readline()
                if not line:
                    break
                raw = line.decode("utf-8").strip()
                if not raw or raw.startswith(":"):
                    continue
                try:
                    evt = json.loads(raw)
                    yield evt
                except Exception:
                    continue
        finally:
            writer.close()
            await writer.wait_closed()

    # Convenience helper methods
    def get_state(self) -> Dict[str, Any]:
        return self.call("get_state")

    async def get_state_async(self) -> Dict[str, Any]:
        return await self.call_async("get_state")

    def get_view(self) -> str:
        res = self.call("get_view")
        return res.get("view", "")

    async def get_view_async(self) -> str:
        res = await self.call_async("get_view")
        return res.get("view", "")

    def append_message(self, kind: str, text: str) -> Dict[str, Any]:
        return self.call("append_message", kind=kind, text=text)

    async def append_message_async(self, kind: str, text: str) -> Dict[str, Any]:
        return await self.call_async("append_message", kind=kind, text=text)

    def zoom(self, id_: int, n: int) -> str:
        res = self.call("zoom", id=id_, n=n)
        return res.get("output", "")

    async def zoom_async(self, id_: int, n: int) -> str:
        res = await self.call_async("zoom", id=id_, n=n)
        return res.get("output", "")

    def date(self, id_: int) -> str:
        res = self.call("date", id=id_)
        return res.get("output", "")

    async def date_async(self, id_: int) -> str:
        res = await self.call_async("date", id=id_)
        return res.get("output", "")

    def get_history(self, limit: int = 30) -> List[Dict[str, Any]]:
        res = self.call("get_history", limit=limit)
        return res.get("messages", [])

    async def get_history_async(self, limit: int = 30) -> List[Dict[str, Any]]:
        res = await self.call_async("get_history", limit=limit)
        return res.get("messages", [])

    def rebuild(self) -> Dict[str, Any]:
        return self.call("rebuild")

    async def rebuild_async(self) -> Dict[str, Any]:
        return await self.call_async("rebuild")

    def sync_export(self, since_idx: int = 0) -> Dict[str, Any]:
        res = self.call("sync_export", timeout=30.0, since_idx=since_idx)
        return res.get("payload", {})

    def sync_apply(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        return self.call("sync_apply", timeout=60.0, payload=payload)


class ProxyStorage:
    """Storage proxy delegating mutations and queries to OptChatDaemon via EngineClient."""

    def __init__(self, client: EngineClient, chat_dir: Path):
        self.client = client
        self.chat_dir = Path(chat_dir)
        self.main_dir = self.chat_dir / "main"
        self.tree_dir = self.chat_dir / "tree"

    def append_message(self, kind: str, text: str) -> Any:
        from optchat.storage import Message
        res = self.client.append_message(kind, text)
        m = res.get("message", {})
        return Message.from_dict(m)

    def get_message(self, i: int) -> Any:
        from optchat.storage import Message
        res = self.client.call("get_message", i=i)
        if res.get("status") == "ok":
            return Message.from_dict(res["message"])
        return None

    @property
    def messages(self) -> List[Any]:
        from optchat.storage import Message
        res = self.client.call("get_history", limit=1000)
        return [Message.from_dict(m) for m in res.get("messages", [])]

    @property
    def tree(self) -> Dict[Any, Any]:
        try:
            st = self.client.get_state()
            count = int(st.get("tree_nodes_count", 0))
            return {i: None for i in range(count)}
        except Exception:
            return {}

    def get_node(self, l: int, i: int) -> Any:
        return None

    def close(self) -> None:
        pass


class ProxyView:
    """LiveView proxy querying OptChatDaemon via EngineClient."""

    def __init__(self, client: EngineClient, budget: int = 128_000):
        self.client = client
        self.budget = budget

    def render(self) -> str:
        return self.client.get_view()

    def render_with_cache_pieces(self) -> List[tuple[str, bool]]:
        return [(self.render(), False)]

    def is_settled(self) -> bool:
        try:
            st = self.client.get_state()
            return bool(st.get("is_settled", True))
        except Exception:
            return True

    def first_unbuilt_message(self) -> int:
        try:
            st = self.client.get_state()
            if st.get("is_settled", True):
                return int(st.get("messages_count", 0))
            return 0
        except Exception:
            return 0

    async def settle(self, abort_event: Optional[asyncio.Event] = None, timeout: float = 3.0) -> bool:
        loop = asyncio.get_event_loop()
        start = loop.time()
        while (loop.time() - start) < timeout:
            if abort_event and abort_event.is_set():
                return False
            try:
                st = await self.client.get_state_async()
                if st.get("is_settled", True):
                    return True
            except Exception:
                return True
            await asyncio.sleep(0.1)
        return False

    def on_new_message(self, i: int) -> None:
        pass  # Handled automatically inside daemon

    def compute_size(self) -> int:
        try:
            st = self.client.get_state()
            return int(st.get("view_size", 0))
        except Exception:
            return 0

    def rebuild(self) -> None:
        self.client.rebuild()


class ProxyCompactor:
    """Compactor proxy delegating pump to OptChatDaemon."""

    def __init__(self, client: EngineClient):
        self.client = client

    def pump(self) -> None:
        try:
            self.client.call("pump", timeout=2.0)
        except Exception:
            pass

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass
