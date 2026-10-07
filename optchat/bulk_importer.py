"""Bulk importer for past Google Antigravity (agy) chat logs and history into OptChat.

Supports:
- Filtering by workspace (e.g. 'kaggle/gemma-4-developer-agent')
- Idempotent execution using an import manifest
- Option B (Clean dialogue: user requests + agent replies, skipping tool noise)
- Batch writing for maximum I/O performance
- Optional background or synchronous tree compaction
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

from optchat.compactor import Compactor
from optchat.constants import JOBS
from optchat.importer import extract_user_text
from optchat.storage import Message, Storage
from optchat.view import LiveView
from optchat.visualizer import export_html_to_file
from optchat.cli import create_provider

logger = logging.getLogger("optchat.bulk_importer")
console = Console()


class BulkImporter:
    def __init__(
        self,
        storage: Storage,
        workspace_filter: Optional[str] = None,
        skip_tools: bool = True,
        base_gemini_dir: Optional[Path] = None,
        force: bool = False,
    ):
        self.storage = storage
        self.workspace_filter = workspace_filter.strip() if workspace_filter else None
        self.skip_tools = skip_tools
        self.force = force

        self.gemini_dir = Path(base_gemini_dir or (Path.home() / ".gemini")).resolve()
        self.cli_dir = self.gemini_dir / "antigravity-cli"
        self.legacy_agy_dir = self.gemini_dir / "antigravity"

        self.manifest_path = self.storage.chat_dir / "import_manifest.json"
        self.manifest: Dict[str, Any] = self._load_manifest()

    def _load_manifest(self) -> Dict[str, Any]:
        if not self.force and self.manifest_path.is_file():
            try:
                with open(self.manifest_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception as e:
                logger.warning("Could not read import manifest (%s), creating new: %s", self.manifest_path, e)
        return {
            "imported_conversations": {},  # cid -> { "count": int, "last_ts": str }
            "imported_history_timestamps": [],  # [int, ...]
        }

    def _save_manifest(self) -> None:
        try:
            self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.manifest_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.manifest, f, indent=2)
            tmp.replace(self.manifest_path)
        except Exception as e:
            logger.error("Failed to save import manifest: %s", e)

    def _matches_workspace(self, workspace_str: Optional[str]) -> bool:
        if not self.workspace_filter:
            return True
        if not workspace_str:
            return False
        return self.workspace_filter.lower() in workspace_str.lower()

    def discover(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        """Scan databases, transcripts, and history.jsonl to discover matching sessions.

        Returns:
            (conversations_to_import, unattached_history_entries)
        """
        imported_cids: Set[str] = set(self.manifest.get("imported_conversations", {}).keys())
        imported_h_ts: Set[int] = set(self.manifest.get("imported_history_timestamps", []))

        # 1. Query conversation_summaries.db
        matched_cids: Set[str] = set()
        db_summaries: Dict[str, Dict[str, Any]] = {}

        db_path = self.cli_dir / "conversation_summaries.db"
        if db_path.is_file():
            try:
                conn = sqlite3.connect(str(db_path))
                cur = conn.cursor()
                cur.execute(
                    "SELECT conversation_id, title, preview, step_count, last_modified_time, last_user_input_time, workspace_uris FROM conversation_summaries"
                )
                for cid, title, preview, steps, mtime, utime, ws_uris in cur.fetchall():
                    is_match = self._matches_workspace(ws_uris)
                    db_summaries[cid] = {
                        "cid": cid,
                        "title": title or preview,
                        "preview": preview,
                        "steps": steps,
                        "mtime": mtime,
                        "utime": utime,
                        "workspace": ws_uris,
                        "matched_workspace": is_match,
                    }
                    if is_match:
                        matched_cids.add(cid)
                conn.close()
            except Exception as err:
                logger.warning("Could not read conversation_summaries.db: %s", err)

        # 2. Scan history.jsonl
        matched_history: List[Dict[str, Any]] = []
        hist_cids: Set[str] = set()
        hist_path = self.cli_dir / "history.jsonl"
        if hist_path.is_file():
            try:
                with open(hist_path, "r", encoding="utf-8", errors="replace") as f:
                    for line in f:
                        stripped = line.strip()
                        if not stripped:
                            continue
                        try:
                            d = json.loads(stripped)
                        except Exception:
                            continue
                        ws = d.get("workspace", "")
                        if self._matches_workspace(ws):
                            cid = d.get("conversationId")
                            if cid:
                                matched_cids.add(cid)
                                hist_cids.add(cid)
                            matched_history.append(d)
            except Exception as err:
                logger.warning("Could not read history.jsonl: %s", err)

        # 3. Resolve transcript locations across ~/.gemini/*/brain/<cid>/
        brain_dirs = [
            self.cli_dir / "brain",
            self.legacy_agy_dir / "brain",
        ]

        found_transcripts: Dict[str, Path] = {}
        for b_dir in brain_dirs:
            if not b_dir.is_dir():
                continue
            for cdir in b_dir.iterdir():
                if not cdir.is_dir():
                    continue
                cid = cdir.name
                tpath = cdir / ".system_generated" / "logs" / "transcript.jsonl"
                if tpath.is_file() and tpath.stat().st_size > 0:
                    # If already identified as matching workspace:
                    if cid in matched_cids:
                        found_transcripts[cid] = tpath
                    elif not self.workspace_filter:
                        # Include all if no filter
                        matched_cids.add(cid)
                        found_transcripts[cid] = tpath

        # 4. Filter out already imported conversations unless force
        eligible_cids = matched_cids if self.force else (matched_cids - imported_cids)

        conversations: List[Dict[str, Any]] = []
        for cid in eligible_cids:
            tpath = found_transcripts.get(cid)
            summary = db_summaries.get(cid, {})

            # Estimate start timestamp
            start_ts = summary.get("utime") or summary.get("mtime") or ""
            if tpath:
                # Read first line's created_at for highest fidelity timestamp
                try:
                    with open(tpath, "r", encoding="utf-8", errors="replace") as tf:
                        first_line = tf.readline()
                        if first_line:
                            d = json.loads(first_line)
                            start_ts = d.get("created_at") or start_ts
                except Exception:
                    pass

            conversations.append({
                "cid": cid,
                "transcript_path": tpath,
                "summary": summary,
                "start_ts": start_ts,
            })

        # Sort conversations chronologically by start timestamp
        conversations.sort(key=lambda x: str(x.get("start_ts", "")))

        # 5. Extract unattached history items (items whose cid was not in full transcripts)
        unattached_history: List[Dict[str, Any]] = []
        for h in matched_history:
            ts = h.get("timestamp")
            if not ts:
                continue
            if not self.force and ts in imported_h_ts:
                continue
            cid = h.get("conversationId")
            # If this history entry is already represented by an imported full transcript, skip it
            if cid and cid in found_transcripts:
                continue

            unattached_history.append(h)

        unattached_history.sort(key=lambda x: x.get("timestamp", 0))

        return conversations, unattached_history

    def extract_messages_from_transcript(self, tpath: Path) -> List[Tuple[str, str, Optional[str]]]:
        """Extract clean messages from a transcript.jsonl file."""
        messages: List[Tuple[str, str, Optional[str]]] = []
        with open(tpath, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    data = json.loads(stripped)
                except Exception:
                    continue

                stype = data.get("type")
                ts = data.get("created_at")

                if stype == "USER_INPUT":
                    raw_c = data.get("content", "")
                    clean_user = extract_user_text(raw_c)
                    if clean_user:
                        messages.append(("user", clean_user, ts))

                elif stype == "PLANNER_RESPONSE":
                    if not self.skip_tools:
                        tool_calls = data.get("tool_calls") or []
                        for tc in tool_calls:
                            tc_name = tc.get("name", "tool")
                            tc_args = tc.get("args", {})
                            args_str = json.dumps(tc_args, ensure_ascii=False) if isinstance(tc_args, dict) else str(tc_args)
                            messages.append(("tool", f"{tc_name}: {args_str}", ts))

                    reply_text = data.get("content")
                    if reply_text and reply_text.strip():
                        messages.append(("talk", reply_text.strip(), ts))

                elif stype == "GENERIC" and not self.skip_tools:
                    content = data.get("content", "")
                    if content and content.strip():
                        messages.append(("echo", content.strip(), ts))

        return messages

    def import_all(self, dry_run: bool = False) -> Tuple[int, int]:
        """Perform the bulk import.

        Returns:
            (conversations_imported_count, messages_imported_count)
        """
        conversations, unattached_history = self.discover()

        total_conversations = len(conversations)
        console.print(f"[bold cyan]Discovered {total_conversations} conversations and {len(unattached_history)} unattached prompts matching workspace.[/bold cyan]")

        if total_conversations == 0 and len(unattached_history) == 0:
            console.print("[yellow]Nothing to import (already up to date or no matches found).[/yellow]")
            return 0, 0

        # Collect all messages across candidates
        all_messages_to_append: List[Tuple[str, str, Optional[str]]] = []
        cids_imported: Dict[str, Dict[str, Any]] = {}
        history_ts_imported: List[int] = []

        for conv in conversations:
            cid = conv["cid"]
            tpath = conv.get("transcript_path")
            summary = conv.get("summary", {})

            if tpath:
                c_msgs = self.extract_messages_from_transcript(tpath)
                if c_msgs:
                    all_messages_to_append.extend(c_msgs)
                    cids_imported[cid] = {
                        "count": len(c_msgs),
                        "first_ts": c_msgs[0][2],
                        "last_ts": c_msgs[-1][2],
                        "has_transcript": True,
                    }
            elif summary:
                # No transcript on disk, but recorded in summary DB
                title = summary.get("title") or summary.get("preview") or ""
                preview = summary.get("preview") or ""
                mtime = summary.get("mtime")
                if title or preview:
                    note_text = f"[Archived Session {cid[:8]}] {title}: {preview}".strip()
                    all_messages_to_append.append(("note", note_text, mtime))
                    cids_imported[cid] = {
                        "count": 1,
                        "first_ts": mtime,
                        "last_ts": mtime,
                        "has_transcript": False,
                    }

        # Add unattached history entries
        for h in unattached_history:
            display = h.get("display", "").strip()
            ts_ms = h.get("timestamp")
            if not display or not ts_ms:
                continue
            # Convert epoch ms to ISO string
            import datetime
            dt = datetime.datetime.fromtimestamp(ts_ms / 1000.0, datetime.timezone.utc)
            iso_str = dt.isoformat()
            kind = "user" if not display.startswith("/") else "note"
            all_messages_to_append.append((kind, display, iso_str))
            history_ts_imported.append(ts_ms)

        total_msgs = len(all_messages_to_append)
        console.print(f"[bold green]Prepared {total_msgs} messages across {len(cids_imported)} sessions for insertion.[/bold green]")

        if dry_run:
            console.print("[yellow][DRY RUN] No changes written to disk.[/yellow]")
            return len(cids_imported), total_msgs

        # Write messages in batch
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(f"Appending {total_msgs} messages to OptChat...", total=None)
            created_msgs = self.storage.append_messages_batch(all_messages_to_append)
            progress.update(task, description=f"[green]Appended {len(created_msgs)} messages successfully![/green]")

        # Update manifest
        self.manifest.setdefault("imported_conversations", {}).update(cids_imported)
        existing_ts = set(self.manifest.setdefault("imported_history_timestamps", []))
        existing_ts.update(history_ts_imported)
        self.manifest["imported_history_timestamps"] = list(existing_ts)
        self._save_manifest()

        return len(cids_imported), len(created_msgs)


async def run_bulk_compaction(
    storage: Storage,
    view: LiveView,
    provider_name: str = "agy",
    model: Optional[str] = None,
    concurrency: int = JOBS,
    timeout: float = 600.0,
) -> None:
    """Run parallel compaction until the view is fully settled."""
    provider = create_provider(provider_name, model=model)
    compactor = Compactor(storage, view, provider, concurrency=concurrency)
    compactor.start()

    console.print(f"[bold cyan]Compactor started with {concurrency} parallel workers...[/bold cyan]")
    start_time = asyncio.get_running_loop().time()

    try:
        while not view.is_settled():
            elapsed = asyncio.get_running_loop().time() - start_time
            if elapsed > timeout:
                console.print(f"[yellow]Compactor reached timeout ({timeout}s). Remaining nodes will build in background.[/yellow]")
                break

            total_messages = len(storage.messages)
            built_nodes = len(storage.tree)
            view_size = view.compute_size()
            console.print(
                f"  [cyan]Tree nodes built: {built_nodes:,} | View size: {view_size:,}/{view.budget:,} bytes | Elapsed: {elapsed:.1f}s[/cyan]"
            )
            await asyncio.sleep(4.0)

        if view.is_settled():
            console.print("[bold green]View is completely SETTLED! All summary tree nodes built.[/bold green]")
    finally:
        compactor.stop()
