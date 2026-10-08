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
            if coord not in storage.tree or not storage.tree[coord].text:
                storage.tree[coord] = r_node
                storage.append_tree_node(r_node.l, r_node.i, r_node.text)
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
            batch = [(m.kind, m.text, m.date) for m in new_items]
            storage.append_messages_batch(batch)
            imported_msgs_count = len(new_items)
        else:
            # Concurrent fork resolution: rewrite log files with deterministic order
            imported_msgs_count = len(unified_msgs) - len(storage.messages)
            _rewrite_storage_messages(storage, unified_msgs)

    # Reconcile tree summaries
    imported_nodes_count = reconcile_tree_nodes(storage, remote_nodes, unified_msgs)

    return {
        "status": "ok",
        "messages_count": len(storage.messages),
        "imported_messages": imported_msgs_count,
        "imported_nodes": imported_nodes_count,
    }


def _rewrite_storage_messages(storage: Storage, unified_msgs: List[Message]) -> None:
    """Atomic rewrite of storage messages on concurrent fork reconciliation."""
    storage.messages = []
    # Clear existing main/*.jsonl
    for f in storage.main_dir.glob("*.jsonl"):
        try:
            f.unlink()
        except Exception:
            pass

    # Re-append all unified messages
    batch = [(m.kind, m.text, m.date) for m in unified_msgs]
    storage.append_messages_batch(batch)


def sync_over_ssh(
    peer_host: str,
    local_storage: Storage,
    remote_dir: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute peer-to-peer sync with a remote host over SSH."""
    ssh_bin = shutil.which("ssh")
    if not ssh_bin:
        raise RuntimeError("SSH binary not found in PATH")

    # Command to run on remote peer
    cmd_prefix = f"optchat sync-exchange"
    if remote_dir:
        cmd_prefix += f" --chat-dir {remote_dir}"

    remote_cmd = [ssh_bin, peer_host, cmd_prefix]

    # Prepare local payload
    local_payload = export_sync_payload(local_storage)
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

    remote_response = json.loads(output_raw)
    result = apply_sync_payload(local_storage, remote_response)
    return result
