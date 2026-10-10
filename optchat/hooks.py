"""Antigravity Lifecycle Hooks Integration for OptChat.

Handles:
- `optchat hook pre-invocation`: Injects compressed memory view before model runs.
- `optchat hook post-tool`: Records tool actions in memory.
- `optchat hook post-invocation`: Handles turn steps.
- `optchat hook stop`: Finalizes turn and triggers background compactor pump.
- `optchat install-hooks`: Registers OptChat hook bridge in ~/.gemini/config/hooks.json.
- `optchat uninstall-hooks`: Removes OptChat hook bridge.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import sys
from typing import Any, Dict, Optional

from optchat.engine_client import EngineClient

logger = logging.getLogger("optchat.hooks")


def handle_hook_command(event_name: str) -> None:
    """Execute lifecycle hook command invoked by Antigravity CLI."""
    raw_input = sys.stdin.read()
    payload: Dict[str, Any] = {}
    if raw_input.strip():
        try:
            payload = json.loads(raw_input)
        except Exception:
            pass

    event_normalized = event_name.lower().replace("-", "_")

    # Ignore internal OptChat compactor turns and synthetic benchmark/eval runs
    is_compactor = os.environ.get("OPTCHAT_IS_COMPACTOR") == "1"
    is_synthetic = False

    if payload:
        ws = (payload.get("workspacePaths") or [""])[0]
        tpath_str = payload.get("transcriptPath", "")
        last_input = payload.get("lastUserInput", "")

        if (
            "agy_isolated" in ws
            or "agy_isolated" in str(tpath_str)
            or "compactor_workspace" in ws
            or (ws and "/tmp" in ws and "pytest" not in ws and "test" not in ws)
            or "CRITICAL HOST DIRECTIVE" in last_input
        ):
            is_synthetic = True

    if not is_compactor and not is_synthetic and payload:
        last_input = payload.get("lastUserInput", "")
        if "CRITICAL REQUIREMENT: Output ONLY the single summary line directly" in last_input or "You write the memory of OptChat" in last_input:
            is_compactor = True
        elif not last_input:
            tpath_str = payload.get("transcriptPath")
            if tpath_str and os.path.isfile(tpath_str):
                try:
                    with open(tpath_str, "r", encoding="utf-8", errors="ignore") as f:
                        first_line = f.readline()
                        if "CRITICAL REQUIREMENT" in first_line or "You write the memory of OptChat" in first_line or "CRITICAL HOST DIRECTIVE" in first_line:
                            is_compactor = True
                except Exception:
                    pass

    if is_compactor or is_synthetic:
        if event_normalized == "pre_invocation":
            sys.stdout.write(json.dumps({"injectSteps": []}) + "\n")
        elif event_normalized in ("pre_tool", "stop"):
            sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        else:
            sys.stdout.write("{}\n")
        sys.stdout.flush()
        return

    client = EngineClient()

    if event_normalized == "pre_invocation":
        # PreInvocation hook: inject latest compressed memory view into agent prompt
        response: Dict[str, Any] = {"injectSteps": []}
        try:
            if client.is_daemon_alive(timeout=0.5):
                view_res = client.call("get_view", timeout=1.0)
                view_text = view_res.get("view", "").strip()
                if view_text:
                    memory_notice = (
                        "=== OPTCHAT ENDLESS MEMORY TREE ===\n"
                        f"{view_text}\n"
                        "=== END MEMORY (Use optchat MCP tools to zoom or date) ==="
                    )
                    response["injectSteps"].append({"ephemeralMessage": memory_notice})
        except Exception as e:
            logger.debug("Failed to query daemon in pre-invocation hook: %s", e)

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()

    elif event_normalized == "pre_tool":
        # PreToolUse hook (fires for invoke_subagent)
        try:
            if client.is_daemon_alive(timeout=0.5):
                client.call("hook_event", event="pre_tool", payload=payload, timeout=1.0)
        except Exception:
            pass

        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        sys.stdout.flush()

    elif event_normalized == "post_tool":
        # PostToolUse hook: stream tool execution to engine
        try:
            if client.is_daemon_alive(timeout=0.5):
                client.call("hook_event", event="post_tool", payload=payload, timeout=1.0)
        except Exception:
            pass

        # PostToolUse expects empty JSON object {}
        sys.stdout.write("{}\n")
        sys.stdout.flush()

    elif event_normalized == "post_invocation":
        # PostInvocation hook
        try:
            if client.is_daemon_alive(timeout=0.5):
                client.call("hook_event", event="post_invocation", payload=payload, timeout=1.0)
        except Exception:
            pass

        sys.stdout.write("{}\n")
        sys.stdout.flush()

    elif event_normalized == "stop":
        # Stop hook: trigger compactor pump
        try:
            if client.is_daemon_alive(timeout=0.5):
                client.call("hook_event", event="stop", payload=payload, timeout=1.0)
        except Exception:
            pass

        # Allow agent turn loop to finish normally
        sys.stdout.write(json.dumps({"decision": "allow"}) + "\n")
        sys.stdout.flush()

    else:
        sys.stdout.write("{}\n")
        sys.stdout.flush()


def install_hooks(workspace_dir: Optional[Path] = None, global_config: bool = True) -> Path:
    """Install OptChat memory bridge into Antigravity hooks.json."""
    if global_config:
        hooks_dir = Path.home() / ".gemini" / "config"
    else:
        hooks_dir = (workspace_dir or Path.cwd()) / ".agents"

    hooks_dir.mkdir(parents=True, exist_ok=True)
    hooks_file = hooks_dir / "hooks.json"

    # Find optchat executable path
    optchat_bin = shutil.which("optchat")
    if not optchat_bin:
        venv_bin = Path(sys.executable).parent / "optchat"
        if venv_bin.is_file():
            optchat_bin = str(venv_bin)
        else:
            optchat_bin = "optchat"

    existing_config: Dict[str, Any] = {}
    if hooks_file.is_file():
        try:
            with open(hooks_file, "r", encoding="utf-8") as f:
                existing_config = json.load(f)
        except Exception:
            existing_config = {}

    optchat_hook_spec = {
        "enabled": True,
        "PreInvocation": [
            {
                "type": "command",
                "command": f"{optchat_bin} hook pre-invocation",
                "timeout": 5,
            }
        ],
        "PreToolUse": [
            {
                "matcher": "invoke_subagent",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{optchat_bin} hook pre-tool",
                        "timeout": 5,
                    }
                ],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [
                    {
                        "type": "command",
                        "command": f"{optchat_bin} hook post-tool",
                        "timeout": 5,
                    }
                ],
            }
        ],
        "PostInvocation": [
            {
                "type": "command",
                "command": f"{optchat_bin} hook post-invocation",
                "timeout": 5,
            }
        ],
        "Stop": [
            {
                "type": "command",
                "command": f"{optchat_bin} hook stop",
                "timeout": 5,
            }
        ],
    }

    # Fix Python boolean for JSON dump
    optchat_hook_spec["enabled"] = True

    existing_config["optchat-memory-bridge"] = optchat_hook_spec

    with open(hooks_file, "w", encoding="utf-8") as f:
        json.dump(existing_config, f, indent=2)

    return hooks_file


def uninstall_hooks(workspace_dir: Optional[Path] = None, global_config: bool = True) -> Path:
    """Remove OptChat memory bridge from hooks.json."""
    if global_config:
        hooks_dir = Path.home() / ".gemini" / "config"
    else:
        hooks_dir = (workspace_dir or Path.cwd()) / ".agents"

    hooks_file = hooks_dir / "hooks.json"
    if not hooks_file.is_file():
        return hooks_file

    try:
        with open(hooks_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        if "optchat-memory-bridge" in data:
            del data["optchat-memory-bridge"]
            with open(hooks_file, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
    except Exception:
        pass

    return hooks_file
