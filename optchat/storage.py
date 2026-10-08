"""Storage layer for OptChat (§2).

Append-only streams with fsync durability:
  chat/
    main/YYYY-MM-DD.jsonl   {i, kind, text, size, date}
    tree/YYYY-MM-DD.jsonl   {l, i, text, size}

Single-writer process lock using Unix domain socket semantics.
"""

from __future__ import annotations

import datetime
import json
import logging
import os
from pathlib import Path
import socket
from typing import Any, Dict, List, Optional, Tuple

from optchat.constants import CAP, NODE

logger = logging.getLogger("optchat.storage")


def get_device_name() -> str:
    """Return device identifier for multi-device synchronization.

    Checks:
    1. Environment variable: OPTCHAT_DEVICE_NAME
    2. Configuration file: ~/.optchat/config.json ("device_name")
    3. Hostname fallback: socket.gethostname().split('.')[0]
       (automatically uses 'android' if running in Termux with default localhost)
    """
    dev = os.environ.get("OPTCHAT_DEVICE_NAME")
    if dev and dev.strip():
        return dev.strip()

    cfg_path = Path(os.path.expanduser("~/.optchat/config.json"))
    if cfg_path.is_file():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                val = data.get("device_name")
                if val and str(val).strip():
                    return str(val).strip()
        except Exception:
            pass

    try:
        host = socket.gethostname().split(".")[0].strip()
        if host and host.lower() != "localhost":
            return host
        return "android" if os.path.exists("/data/data/com.termux") else "node"
    except Exception:
        return "node"


class Message:
    __slots__ = ("i", "kind", "text", "size", "date", "key")

    def __init__(self, i: int, kind: str, text: str, size: int, date: str, key: Optional[str] = None):
        self.i = i
        self.kind = kind
        self.text = text
        self.size = size
        self.date = date
        self.key = key or f"{date}#{get_device_name()}"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "i": self.i,
            "kind": self.kind,
            "text": self.text,
            "size": self.size,
            "date": self.date,
            "key": self.key,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Message:
        date_str = str(d["date"])
        key_str = str(d.get("key") or f"{date_str}#{get_device_name()}")
        return cls(
            i=int(d["i"]),
            kind=str(d["kind"]),
            text=str(d["text"]),
            size=int(d.get("size", len(f"{d['kind']}: {d['text']}".encode("utf-8")))),
            date=date_str,
            key=key_str,
        )



class TreeNode:
    __slots__ = ("l", "i", "text", "size")

    def __init__(self, l: int, i: int, text: str, size: int):
        self.l = l
        self.i = i
        self.text = text
        self.size = size

    def to_dict(self) -> Dict[str, Any]:
        return {
            "l": self.l,
            "i": self.i,
            "text": self.text,
            "size": self.size,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> TreeNode:
        text = str(d["text"])
        return cls(
            l=int(d["l"]),
            i=int(d["i"]),
            text=text,
            size=int(d.get("size", len(text.encode("utf-8")))),
        )


def cap_tool_output(text: str, cap: int = CAP) -> str:
    """Cap tool output at `cap` characters, keeping head and tail with cut note."""
    if len(text) <= cap:
        return text
    overhead = 120
    keep_total = max(cap - overhead, 100)
    head_len = keep_total // 2
    tail_len = keep_total - head_len
    cut_count = len(text) - head_len - tail_len
    note = f"\n... [cut {cut_count:,} characters] ...\n"
    return text[:head_len] + note + text[-tail_len:]


class ProcessLock:
    """Unix domain socket single-writer process lock (§2).

    Listens on socket at `path`. A second process connecting to it gets
    rejected/exits. If socket exists but connection is refused, it is stale
    (OS freed it when previous owner died) and is unlinked and taken over.
    """

    def __init__(self, lock_path: Path):
        self.lock_path = lock_path
        self._sock: Optional[socket.socket] = None

    def acquire(self) -> None:
        if self._sock is not None:
            return

        self.lock_path.parent.mkdir(parents=True, exist_ok=True)

        if self.lock_path.exists():
            test_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                test_sock.connect(str(self.lock_path))
                test_sock.close()
                raise RuntimeError(
                    f"Another OptChat process is currently active on {self.lock_path.parent}. Exiting."
                )
            except (ConnectionRefusedError, FileNotFoundError):
                # Stale socket from dead process
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    pass
            finally:
                try:
                    test_sock.close()
                except Exception:
                    pass

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.bind(str(self.lock_path))
            sock.listen(1)
            self._sock = sock
        except Exception as e:
            sock.close()
            raise RuntimeError(f"Failed to acquire single-writer lock at {self.lock_path}: {e}")

    def release(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
            try:
                if self.lock_path.exists():
                    self.lock_path.unlink()
            except Exception:
                pass

    def __enter__(self) -> ProcessLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


class Storage:
    """Durable append-only storage for the log and tree (§2)."""

    def __init__(self, chat_dir: Union[str, Path]):
        self.chat_dir = Path(os.path.expanduser(str(chat_dir))).resolve()
        self.main_dir = self.chat_dir / "main"
        self.tree_dir = self.chat_dir / "tree"
        self.lock = ProcessLock(self.chat_dir / "lock")

        self.messages: List[Message] = []
        self.tree: Dict[Tuple[int, int], TreeNode] = {}

    def open(self, acquire_lock: bool = True) -> None:
        """Open storage, optionally acquiring exclusive writer lock."""
        if acquire_lock:
            self.lock.acquire()
        self.main_dir.mkdir(parents=True, exist_ok=True)
        self.tree_dir.mkdir(parents=True, exist_ok=True)
        self._load_all()

    def reload(self) -> None:
        """Reload all messages and tree nodes from disk."""
        self._load_all()

    def close(self) -> None:
        self.lock.release()

    def _today_filename(self) -> str:
        # File of the local day it was written (§2)
        return datetime.date.today().isoformat() + ".jsonl"

    def _load_stream(self, directory: Path) -> List[Dict[str, Any]]:
        entries: List[Dict[str, Any]] = []
        files = sorted(directory.glob("*.jsonl"))
        for fpath in files:
            needs_newline_fix = False
            with open(fpath, "rb") as f:
                content = f.read()
                if content and not content.endswith(b"\n"):
                    needs_newline_fix = True

            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                for line_no, raw_line in enumerate(f, 1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        entries.append(data)
                    except json.JSONDecodeError as err:
                        logger.warning(
                            "Torn line in %s:%d skipped: %s (error: %s)",
                            fpath.name,
                            line_no,
                            line[:60],
                            err,
                        )

            if needs_newline_fix:
                with open(fpath, "ab") as f:
                    f.write(b"\n")
                    f.flush()
                    os.fsync(f.fileno())

        return entries

    def _load_all(self) -> None:
        self.messages.clear()
        self.tree.clear()

        # Load messages
        msg_entries = self._load_stream(self.main_dir)
        msg_entries.sort(key=lambda d: int(d.get("i", 0)))
        for d in msg_entries:
            msg = Message.from_dict(d)
            self.messages.append(msg)

        # Load tree nodes
        tree_entries = self._load_stream(self.tree_dir)
        for d in tree_entries:
            node = TreeNode.from_dict(d)
            self.tree[(node.l, node.i)] = node

    def append_message(self, kind: str, text: str) -> Message:
        """Append message to log with single write and fsync durability (§2).

        kind: 'user', 'talk', 'tool', 'echo', 'note'
        Thoughts (reasoning) are NEVER passed here.
        """
        if kind == "echo":
            text = cap_tool_output(text, CAP)

        idx = len(self.messages)
        now_iso = datetime.datetime.now().astimezone().isoformat()
        size = len(f"{kind}: {text}".encode("utf-8"))

        msg = Message(i=idx, kind=kind, text=text, size=size, date=now_iso)
        line = json.dumps(msg.to_dict(), ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")

        fpath = self.main_dir / self._today_filename()
        with open(fpath, "ab") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())

        self.messages.append(msg)
        return msg

    def append_messages_batch(
        self,
        items: List[Tuple[str, str, Optional[str]]],
    ) -> List[Message]:
        """Append a batch of messages with durability, grouping by day file.

        Each item is (kind, text, date_iso_optional).
        Thoughts (reasoning) are NEVER passed here.
        """
        if not items:
            return []

        created_msgs: List[Message] = []
        files_to_lines: Dict[Path, List[bytes]] = {}

        now_iso_default = datetime.datetime.now().astimezone().isoformat()
        current_idx = len(self.messages)

        for kind, text, date_iso in items:
            if kind == "echo":
                text = cap_tool_output(text, CAP)

            msg_date = date_iso if date_iso else now_iso_default
            size = len(f"{kind}: {text}".encode("utf-8"))
            msg = Message(i=current_idx, kind=kind, text=text, size=size, date=msg_date)
            current_idx += 1
            created_msgs.append(msg)

            # Determine day filename from date
            day_str = msg_date[:10]
            if len(day_str) == 10 and day_str[4] == "-" and day_str[7] == "-":
                fname = f"{day_str}.jsonl"
            else:
                fname = self._today_filename()

            fpath = self.main_dir / fname
            line_bytes = (json.dumps(msg.to_dict(), ensure_ascii=False) + "\n").encode("utf-8")
            files_to_lines.setdefault(fpath, []).append(line_bytes)

        # Write to disk and fsync once per file
        for fpath, lines in files_to_lines.items():
            with open(fpath, "ab") as f:
                for lb in lines:
                    f.write(lb)
                f.flush()
                os.fsync(f.fileno())

        self.messages.extend(created_msgs)
        return created_msgs

    def save_node(self, l: int, i: int, text: str) -> TreeNode:
        """Save a tree node with fsync durability (§2)."""
        size = len(text.encode("utf-8"))
        node = TreeNode(l=l, i=i, text=text, size=size)
        line = json.dumps(node.to_dict(), ensure_ascii=False) + "\n"
        encoded = line.encode("utf-8")

        fpath = self.tree_dir / self._today_filename()
        with open(fpath, "ab") as f:
            f.write(encoded)
            f.flush()
            os.fsync(f.fileno())

        self.tree[(l, i)] = node
        return node

    def append_tree_node(self, l: int, i: int, text: str) -> TreeNode:
        """Alias for save_node for tree reconciliation and streaming."""
        return self.save_node(l, i, text)

    def get_message(self, i: int) -> Optional[Message]:
        if 0 <= i < len(self.messages):
            return self.messages[i]
        return None

    def get_node(self, l: int, i: int) -> Optional[TreeNode]:
        return self.tree.get((l, i))

    def has_node(self, l: int, i: int) -> bool:
        return (l, i) in self.tree
