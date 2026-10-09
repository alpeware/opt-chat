"""OptChat Self-Updater & P2P Cluster Update Orchestrator.

Provides automated git pull, package reinstallation, and graceful daemon & web server
process re-exec. Supports both centralized GitHub updates and decentralized P2P git
syncing across peer devices over SSH.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Tuple

from optchat.engine_client import EngineClient
from optchat.sync import get_configured_peers

logger = logging.getLogger("optchat.updater")


def find_repo_root(start_path: Optional[Path] = None) -> Optional[Path]:
    """Find the root directory of the opt-chat git repository."""
    candidate = (start_path or Path(__file__)).resolve()
    if candidate.is_file():
        candidate = candidate.parent

    for parent in [candidate] + list(candidate.parents):
        git_dir = parent / ".git"
        if git_dir.is_dir() or git_dir.is_file():
            return parent
    return None


def get_git_commit(repo_dir: Path) -> str:
    """Return the current short git commit hash of the repository."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception as e:
        logger.debug("Failed getting git commit: %s", e)
        return "unknown"


def get_git_branch(repo_dir: Path) -> str:
    """Return the current active git branch name."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return res.stdout.strip()
    except Exception:
        return "main"


def check_repo_status(repo_dir: Path) -> Dict[str, Any]:
    """Check for uncommitted or untracked changes in the working tree."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        return {"clean": len(lines) == 0, "uncommitted_files": lines}
    except Exception as e:
        return {"clean": False, "error": str(e), "uncommitted_files": []}


def pull_from_git(
    repo_dir: Path,
    remote_or_url: str = "origin",
    branch: Optional[str] = None,
) -> Tuple[bool, str]:
    """Execute git pull --ff-only from a remote name or SSH URL."""
    target_branch = branch or get_git_branch(repo_dir) or "main"
    cmd = ["git", "-C", str(repo_dir), "pull", "--ff-only", remote_or_url, target_branch]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return True, res.stdout.strip()
        return False, res.stderr.strip() or res.stdout.strip()
    except Exception as e:
        return False, str(e)


def reinstall_editable(repo_dir: Path) -> Tuple[bool, str]:
    """Reinstall package in editable mode using current python environment or uv."""
    uv_bin = shutil.which("uv")
    if uv_bin:
        cmd = [uv_bin, "pip", "install", "-e", str(repo_dir), "--no-deps"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-e", str(repo_dir), "--no-deps"]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0:
            return True, "Reinstalled package successfully."
        return False, res.stderr.strip() or res.stdout.strip()
    except Exception as e:
        return False, str(e)



def restart_daemon_process(chat_dir: Optional[Path] = None, timeout: float = 15.0) -> Tuple[bool, str]:
    """Send IPC restart request to the local daemon and wait for it to return online."""
    target_chat_dir = Path(chat_dir or os.path.expanduser("~/.optchat")).resolve()
    client = EngineClient(socket_path=target_chat_dir / "engine.sock")

    if not client.is_daemon_alive(timeout=1.0):
        return False, "Daemon is not currently running."

    try:
        res = client.restart(timeout=3.0)
        if res.get("status") != "ok":
            return False, res.get("error", "Daemon rejected restart request")
    except Exception as e:
        # A broken connection or disconnect is expected if daemon immediately re-execs
        logger.debug("Restart IPC send notice: %s", e)

    import time
    time.sleep(0.6)

    # Wait for daemon to complete re-exec and respond to ping
    recovered = client.wait_for_daemon(timeout=timeout, poll_interval=0.25)
    if recovered:

        return True, "Daemon successfully restarted and online."
    return False, "Daemon was signaled to restart, but did not respond before timeout."


def update_local(
    repo_dir: Optional[Path] = None,
    chat_dir: Optional[Path] = None,
    from_peer: Optional[str] = None,
    branch: Optional[str] = None,
    no_restart: bool = False,
    restart_only: bool = False,
) -> Dict[str, Any]:
    """Execute complete local update flow: pull -> reinstall -> restart daemon."""
    target_repo = repo_dir or find_repo_root()
    target_chat = Path(chat_dir or os.path.expanduser("~/.optchat")).resolve()

    if not target_repo or not target_repo.is_dir():
        return {
            "status": "error",
            "error": f"Could not determine opt-chat repository root directory from {repo_dir or __file__}",
        }

    old_commit = get_git_commit(target_repo)
    active_branch = branch or get_git_branch(target_repo)

    if restart_only:
        restarted, daemon_msg = restart_daemon_process(target_chat)
        return {
            "status": "ok",
            "repo_dir": str(target_repo),
            "commit": old_commit,
            "updated": False,
            "restarted": restarted,
            "message": daemon_msg,
        }

    # 1. Pull updates
    pull_msg = ""
    pulled_ok = False

    if from_peer:
        # P2P Pull directly from remote peer over SSH
        remote_url = f"ssh://{from_peer}{target_repo}"
        pulled_ok, pull_msg = pull_from_git(target_repo, remote_url, active_branch)
    else:
        # Centralized Pull from origin (GitHub) with P2P fallback
        pulled_ok, pull_msg = pull_from_git(target_repo, "origin", active_branch)
        if not pulled_ok:
            # Check if GitHub is offline; attempt P2P pull from first configured peer
            peers = get_configured_peers(target_chat)
            for p in peers:
                peer_url = f"ssh://{p}{target_repo}"
                peer_ok, peer_res = pull_from_git(target_repo, peer_url, active_branch)
                if peer_ok:
                    pulled_ok = True
                    pull_msg = f"GitHub unreachable; pulled P2P updates from peer '{p}': {peer_res}"
                    break

    if not pulled_ok:
        return {
            "status": "error",
            "repo_dir": str(target_repo),
            "commit": old_commit,
            "error": f"Failed to pull git updates: {pull_msg}",
        }

    new_commit = get_git_commit(target_repo)
    updated = old_commit != new_commit

    # 2. Reinstall editable package
    reinstall_ok, reinstall_msg = reinstall_editable(target_repo)

    # 3. Restart daemon & supervised web server if running
    restarted = False
    daemon_msg = "Daemon not running."
    if not no_restart:
        restarted, daemon_msg = restart_daemon_process(target_chat)

    return {
        "status": "ok",
        "repo_dir": str(target_repo),
        "branch": active_branch,
        "previous_commit": old_commit,
        "commit": new_commit,
        "updated": updated,
        "reinstalled": reinstall_ok,
        "daemon_restarted": restarted,
        "message": f"Updated {old_commit} -> {new_commit}. {daemon_msg}",
    }


def update_remote_peer(
    peer_host: str,
    remote_dir: Optional[str] = None,
    remote_bin: Optional[str] = None,
    from_local: bool = False,
    local_host_alias: Optional[str] = None,
) -> Dict[str, Any]:
    """Execute 'optchat update' on a remote peer over SSH."""
    ssh_bin = shutil.which("ssh") or "ssh"
    extra_args = []
    if remote_dir:
        extra_args.append(f"--chat-dir {remote_dir}")
    if from_local and local_host_alias:
        extra_args.append(f"--from-peer {local_host_alias}")

    arg_str = " ".join(extra_args)

    if remote_bin:
        remote_script = f"{remote_bin} update {arg_str}".strip()
    else:
        remote_script = (
            'bootstrap_peer() { '
            'for r in "$HOME/src/alpeware/opt-chat" "$HOME/opt-chat" "/data/data/com.termux/files/home/src/alpeware/opt-chat"; do '
            'if [ -d "$r/.git" ]; then '
            'cd "$r" && git pull --ff-only 2>&1; '
            'if [ -x "$r/.venv/bin/uv" ]; then "$r/.venv/bin/uv" pip install -e . --no-deps >/dev/null 2>&1; '
            'elif command -v uv >/dev/null 2>&1; then uv pip install -e . --no-deps >/dev/null 2>&1; '
            'elif [ -x "$HOME/.venv/bin/uv" ]; then "$HOME/.venv/bin/uv" pip install -e . --no-deps >/dev/null 2>&1; '
            'elif [ -x "$r/.venv/bin/python" ]; then "$r/.venv/bin/python" -m pip install -e . --no-deps >/dev/null 2>&1 || true; fi; '
            'break; fi; done; }; '
            'export PATH="$HOME/.local/bin:$HOME/bin:$HOME/src/alpeware/opt-chat/.venv/bin:$HOME/.venv/bin:/data/data/com.termux/files/usr/bin:$PATH"; '
            'if ! optchat update --help >/dev/null 2>&1; then bootstrap_peer; fi; '
            f'exec optchat update {arg_str}'
        )

    remote_cmd = [ssh_bin, peer_host, remote_script]

    try:
        proc = subprocess.run(remote_cmd, capture_output=True, text=True, timeout=60.0)
        output = proc.stdout.strip()
        err = proc.stderr.strip()
        if proc.returncode == 0:
            return {"status": "ok", "peer": peer_host, "output": output}
        return {"status": "error", "peer": peer_host, "error": err or output}
    except Exception as e:
        return {"status": "error", "peer": peer_host, "error": str(e)}
