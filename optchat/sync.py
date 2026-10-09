"""OptChat Peer-to-Peer Idempotent Memory Synchronization.

Enables distributed sync across multiple devices (Gentoo Desktop, Gentoo Laptop, Android Termux).
Uses deterministic lexicographical keys:
    key = timestamp#device_name  (e.g. 2026-10-08T15:25:14.123456Z#desktop)

Key properties:
- Deterministic total order across all devices.
- Fully idempotent: Sync(A, B) == Sync(B, A) == Sync(Sync(A, B), B).
- Tree summary reuse: Pre-computed summaries are shared to save mobile battery and LLM tokens.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Set, Tuple

from optchat.storage import Message, Storage, TreeNode, get_device_name
from optchat.view import LiveView

logger = logging.getLogger("optchat.sync")


def reconcile_messages(
    local_msgs: List[Message],
    remote_msgs: List[Message],
) -> Tuple[List[Message], bool]:
    """Merge and deterministically order messages by lexicographical key.

    Returns (unified_messages, has_changes_relative_to_local).
    """
    by_key: Dict[str, Message] = {}

    for m in local_msgs:
        by_key[m.key] = m

    for m in remote_msgs:
        if m.key not in by_key:
            by_key[m.key] = m
        else:
            # If same key, prefer local or whichever has more complete content
            pass

    # Sort strictly lexicographically by key (timestamp#device)
    sorted_msgs = sorted(by_key.values(), key=lambda m: m.key)

    # Re-index continuously 0, 1, 2, ...
    unified: List[Message] = []
    has_changes = len(sorted_msgs) != len(local_msgs)

    for idx, m in enumerate(sorted_msgs):
        if m.i != idx:
            has_changes = True
        unified.append(
            Message(
                i=idx,
                kind=m.kind,
                text=m.text,
                size=m.size,
                date=m.date,
                key=m.key,
            )
        )

    if not has_changes:
        for idx in range(len(local_msgs)):
            if local_msgs[idx].key != unified[idx].key:
                has_changes = True
                break

    return unified, has_changes


def reconcile_tree_nodes(
    storage: Storage,
    remote_nodes: List[TreeNode],
    unified_msgs: List[Message],
    max_valid_idx: Optional[int] = None,
) -> int:
    """Import valid pre-computed tree summaries from remote to save CPU & LLM tokens."""
    imported = 0
    msg_count = len(unified_msgs)

    for r_node in remote_nodes:
        coord = (r_node.l, r_node.i)
        span = 2 ** r_node.l
        start_idx = r_node.i * span
        end_idx = start_idx + span

        # Node must fall within our current message bounds
        if end_idx <= msg_count:
            if max_valid_idx is not None and end_idx > max_valid_idx:
                continue
            if coord not in storage.tree or not storage.tree[coord].text:
                storage.tree[coord] = r_node
                storage.save_node(r_node.l, r_node.i, r_node.text)
                imported += 1

    return imported


def export_sync_payload(storage: Storage, since_idx: int = 0) -> Dict[str, Any]:
    """Export local state into a portable JSON payload for peer exchange."""
    msgs = [m.to_dict() for m in storage.messages[since_idx:]]
    nodes = [n.to_dict() for n in storage.tree.values()]
    return {
        "device": get_device_name(),
        "total_messages": len(storage.messages),
        "messages": msgs,
        "tree": nodes,
    }


def apply_sync_payload(storage: Storage, payload: Dict[str, Any]) -> Dict[str, Any]:
    """Incorporate peer payload into local storage and rebuild live view if necessary."""
    remote_msgs_raw = payload.get("messages", [])
    remote_tree_raw = payload.get("tree", [])

    remote_msgs = [Message.from_dict(d) for d in remote_msgs_raw]
    remote_nodes = [TreeNode.from_dict(d) for d in remote_tree_raw]

    unified_msgs, has_changes = reconcile_messages(storage.messages, remote_msgs)

    if not has_changes and not remote_nodes:
        return {
            "status": "up_to_date",
            "messages_count": len(storage.messages),
            "imported_messages": 0,
            "imported_nodes": 0,
        }

    imported_msgs_count = 0
    max_valid_idx: Optional[int] = None

    if has_changes:
        # Check if local is a prefix and remote only added items
        is_strict_append = (
            len(unified_msgs) > len(storage.messages)
            and all(
                storage.messages[i].key == unified_msgs[i].key
                for i in range(len(storage.messages))
            )
        )

        if is_strict_append:
            new_items = unified_msgs[len(storage.messages):]
            batch = [(m.kind, m.text, m.date, m.key) for m in new_items]
            storage.append_messages_batch(batch)
            imported_msgs_count = len(new_items)
        else:
            # Concurrent fork resolution: find divergence index
            first_diff_idx = 0
            while first_diff_idx < len(storage.messages) and first_diff_idx < len(unified_msgs):
                if storage.messages[first_diff_idx].key != unified_msgs[first_diff_idx].key:
                    break
                first_diff_idx += 1
            max_valid_idx = first_diff_idx
            imported_msgs_count = len(unified_msgs) - len(storage.messages)
            _rewrite_storage_messages(storage, unified_msgs, first_diff_idx=first_diff_idx)

    # Reconcile tree summaries
    imported_nodes_count = reconcile_tree_nodes(
        storage, remote_nodes, unified_msgs, max_valid_idx=max_valid_idx
    )

    return {
        "status": "ok",
        "messages_count": len(storage.messages),
        "imported_messages": imported_msgs_count,
        "imported_nodes": imported_nodes_count,
    }


def _rewrite_storage_messages(
    storage: Storage, unified_msgs: List[Message], first_diff_idx: int = 0
) -> None:
    """Atomic rewrite of storage messages on concurrent fork reconciliation."""
    storage.messages = []
    # Clear existing main/*.jsonl
    for f in storage.main_dir.glob("*.jsonl"):
        try:
            f.unlink()
        except Exception:
            pass

    # Re-append all unified messages
    batch = [(m.kind, m.text, m.date, m.key) for m in unified_msgs]
    storage.append_messages_batch(batch)

    # Invalidate tree nodes that spanned beyond first_diff_idx
    invalid_coords = []
    for coord in list(storage.tree.keys()):
        l, i = coord
        span = 1 << l
        end_idx = (i + 1) * span
        if end_idx > first_diff_idx:
            invalid_coords.append(coord)
            del storage.tree[coord]

    if invalid_coords:
        for f in storage.tree_dir.glob("*.jsonl"):
            try:
                f.unlink()
            except Exception:
                pass
        valid_nodes = list(storage.tree.values())
        storage.tree.clear()
        for node in valid_nodes:
            storage.save_node(node.l, node.i, node.text)


def get_configured_peers(chat_dir: Optional[Path] = None) -> List[str]:
    """Retrieve peer list from ~/.optchat/config.json."""
    if chat_dir is None:
        chat_dir = Path(os.path.expanduser("~/.optchat"))
    cfg_path = chat_dir / "config.json"
    if cfg_path.is_file():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                peers = data.get("peers")
                if isinstance(peers, list):
                    return [str(p).strip() for p in peers if str(p).strip()]
        except Exception:
            pass
    return []


def add_configured_peer(peer_name: str, chat_dir: Optional[Path] = None) -> bool:
    """Add a peer to ~/.optchat/config.json. Returns True if added, False if already present."""
    if chat_dir is None:
        chat_dir = Path(os.path.expanduser("~/.optchat"))
    chat_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = chat_dir / "config.json"
    data: Dict[str, Any] = {}
    if cfg_path.is_file():
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    peers: List[str] = data.get("peers", [])
    if not isinstance(peers, list):
        peers = []
    clean_name = peer_name.strip()
    if not clean_name or clean_name in peers:
        return False
    peers.append(clean_name)
    data["peers"] = peers
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return True


def remove_configured_peer(peer_name: str, chat_dir: Optional[Path] = None) -> bool:
    """Remove a peer from ~/.optchat/config.json. Returns True if removed, False if not found."""
    if chat_dir is None:
        chat_dir = Path(os.path.expanduser("~/.optchat"))
    cfg_path = chat_dir / "config.json"
    if not cfg_path.is_file():
        return False
    try:
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    peers = data.get("peers", [])
    if not isinstance(peers, list):
        return False
    clean_name = peer_name.strip()
    if clean_name not in peers:
        return False
    peers.remove(clean_name)
    data["peers"] = peers
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return True


def get_ssh_config_hosts(ssh_config_path: Optional[Path] = None) -> List[str]:
    """Detect non-wildcard host aliases defined in ~/.ssh/config."""
    if ssh_config_path is None:
        ssh_config_path = Path(os.path.expanduser("~/.ssh/config"))
    if not ssh_config_path.is_file():
        return []
    hosts: List[str] = []
    try:
        with open(ssh_config_path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line.lower().startswith("host "):
                    parts = line.split()[1:]
                    for p in parts:
                        p = p.strip()
                        if p and not any(ch in p for ch in "*?%"):
                            if p not in hosts:
                                hosts.append(p)
    except Exception:
        pass
    return hosts


def sync_payload_over_ssh(
    peer_host: str,
    local_payload: Dict[str, Any],
    remote_dir: Optional[str] = None,
    remote_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """Exchange sync JSON payload with remote peer over SSH stdio."""
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        raise RuntimeError("SSH binary not found in PATH")

    extra_args = f"--chat-dir {remote_dir}" if remote_dir else ""
    if remote_bin:
        remote_script = f"{remote_bin} sync-exchange {extra_args}".strip()
    else:
        # Non-interactive SSH shells often don't source ~/.bashrc where ~/.local/bin is added.
        # We test for optchat in standard locations (PATH, ~/.local/bin, ~/bin, Termux), then fallback.
        remote_script = (
            f'if command -v optchat >/dev/null 2>&1; then exec optchat sync-exchange {extra_args}; '
            f'elif [ -x "$HOME/.local/bin/optchat" ]; then exec "$HOME/.local/bin/optchat" sync-exchange {extra_args}; '
            f'elif [ -x "$HOME/bin/optchat" ]; then exec "$HOME/bin/optchat" sync-exchange {extra_args}; '
            f'elif [ -x "$HOME/src/alpeware/opt-chat/.venv/bin/optchat" ]; then exec "$HOME/src/alpeware/opt-chat/.venv/bin/optchat" sync-exchange {extra_args}; '
            f'elif [ -x "$HOME/.venv/bin/optchat" ]; then exec "$HOME/.venv/bin/optchat" sync-exchange {extra_args}; '
            f'elif [ -x "/data/data/com.termux/files/usr/bin/optchat" ]; then exec /data/data/com.termux/files/usr/bin/optchat sync-exchange {extra_args}; '
            f'else export PATH="$HOME/.local/bin:$HOME/bin:/data/data/com.termux/files/usr/bin:$PATH"; exec optchat sync-exchange {extra_args}; fi'
        )

    remote_cmd = [ssh_bin, peer_host, remote_script]
    input_bytes = (json.dumps(local_payload) + "\n").encode("utf-8")

    proc = subprocess.Popen(
        remote_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = proc.communicate(input=input_bytes)

    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"SSH sync failed (exit {proc.returncode}): {err}")

    output_raw = stdout.decode("utf-8", errors="replace").strip()
    if not output_raw:
        raise RuntimeError("Remote peer returned empty sync response")

    return json.loads(output_raw)


def sync_over_ssh(
    peer_host: str,
    local_storage: Storage,
    remote_dir: Optional[str] = None,
    remote_bin: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute peer-to-peer sync with a remote host over SSH using local storage."""
    local_payload = export_sync_payload(local_storage)
    remote_response = sync_payload_over_ssh(
        peer_host, local_payload, remote_dir=remote_dir, remote_bin=remote_bin
    )
    result = apply_sync_payload(local_storage, remote_response)
    return result


def calculate_scatter_peers(
    peers: List[str],
    last_producer_idx: int,
    successful_peers: Optional[Set[str]] = None,
) -> List[str]:
    """Calculate the minimal subset of peers requiring a Round 2 (Scatter) pass for full convergence.

    In a star topology, if peer at index `last_producer_idx` introduced new messages,
    all peers visited prior to `last_producer_idx` missed those updates.
    Peers visited at or after `last_producer_idx` already received the full state in Round 1.
    """
    if last_producer_idx <= 0:
        return []
    earlier = peers[:last_producer_idx]
    if successful_peers is not None:
        return [p for p in earlier if p in successful_peers]
    return earlier


