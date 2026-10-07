"""Transcript importer for Google Antigravity (agy) into OptChat memory (§10)."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Optional

from optchat.compactor import Compactor
from optchat.storage import Storage
from optchat.view import LiveView


def extract_user_text(raw_content: str) -> str:
    """Extract clean user prompt from agy user request markup."""
    m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", raw_content, re.DOTALL)
    if m:
        return m.group(1).strip()
    return raw_content.strip()


def import_agy_transcript(
    transcript_path: Path,
    storage: Storage,
    view: Optional[LiveView] = None,
    compactor: Optional[Compactor] = None,
    skip_tool_noise: bool = False,
) -> int:
    """Import an agy transcript.jsonl file into OptChat storage.

    Maps:
      - USER_INPUT -> 'user'
      - PLANNER_RESPONSE (text) -> 'talk'
      - PLANNER_RESPONSE (tool_calls) -> 'tool'
      - GENERIC (tool outputs) -> 'echo'
    """
    path = Path(transcript_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Transcript file not found: {path}")

    imported_count = 0

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            try:
                data = json.loads(stripped)
            except Exception:
                continue

            step_type = data.get("type")

            if step_type == "USER_INPUT":
                raw_c = data.get("content", "")
                if "CRITICAL REQUIREMENT: Output ONLY" in raw_c or "You write the memory of OptChat" in raw_c:
                    continue
                user_text = extract_user_text(raw_c)
                if user_text:
                    msg = storage.append_message("user", user_text)
                    if view:
                        view.on_new_message(msg.i)
                    imported_count += 1

            elif step_type == "PLANNER_RESPONSE":
                # Check tool calls
                if not skip_tool_noise:
                    tool_calls = data.get("tool_calls") or []
                    for tc in tool_calls:
                        tc_name = tc.get("name", "tool")
                        tc_args = tc.get("args", {})
                        if isinstance(tc_args, dict):
                            args_str = json.dumps(tc_args, ensure_ascii=False)
                        else:
                            args_str = str(tc_args)
                        msg = storage.append_message("tool", f"{tc_name}: {args_str}")
                        if view:
                            view.on_new_message(msg.i)
                        imported_count += 1

                # Check text reply
                reply_text = data.get("content")
                if reply_text and reply_text.strip():
                    msg = storage.append_message("talk", reply_text.strip())
                    if view:
                        view.on_new_message(msg.i)
                    imported_count += 1

            elif step_type == "GENERIC" and not skip_tool_noise:
                content = data.get("content", "")
                if content and content.strip():
                    msg = storage.append_message("echo", content.strip())
                    if view:
                        view.on_new_message(msg.i)
                    imported_count += 1

    if compactor:
        compactor.pump()

    return imported_count
