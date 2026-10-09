"""Unit tests for OptChat Self-Updater and Daemon Process Supervisor."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import pytest

from optchat.daemon import OptChatDaemon
from optchat.engine_client import EngineClient
from optchat.updater import (
    check_repo_status,
    find_repo_root,
    get_git_branch,
    get_git_commit,
    restart_daemon_process,
    update_local,
)


def test_find_repo_root():
    root = find_repo_root()
    assert root is not None
    assert (root / ".git").exists()
    assert (root / "optchat").is_dir()


def test_git_metadata_readers():
    root = find_repo_root()
    assert root is not None

    commit = get_git_commit(root)
    assert len(commit) >= 4
    assert commit != "unknown"

    branch = get_git_branch(root)
    assert len(branch) > 0

    status = check_repo_status(root)
    assert isinstance(status, dict)
    assert "clean" in status


def test_daemon_restart_ipc_action(tmp_path: Path):
    chat_dir = tmp_path / "chat"
    sock_path = tmp_path / "engine.sock"

    daemon = OptChatDaemon(
        chat_dir=chat_dir,
        socket_path=sock_path,
        provider_name="mock",
    )
    daemon._skip_execv_for_test = True
    daemon.start_in_thread()

    client = EngineClient(socket_path=sock_path)
    assert client.is_daemon_alive(timeout=1.0) is True

    try:
        # Request restart via client helper
        res = client.restart(timeout=2.0)
        assert res.get("status") == "ok"
        assert "restart" in res.get("message", "").lower()
    finally:
        daemon.stop_in_thread()


def test_daemon_web_supervisor_state(tmp_path: Path):
    chat_dir = tmp_path / "chat"
    sock_path = tmp_path / "engine.sock"

    daemon = OptChatDaemon(
        chat_dir=chat_dir,
        socket_path=sock_path,
        provider_name="mock",
        enable_web=True,
        web_host="127.0.0.1",
        web_port=9999,
    )
    # Don't actually spawn subprocess in quick unit test; test supervisor metadata
    daemon._skip_execv_for_test = True
    daemon.start_in_thread()

    client = EngineClient(socket_path=sock_path)
    assert client.is_daemon_alive(timeout=1.0) is True

    try:
        state = client.get_state()
        assert "web_server" in state
        web_info = state["web_server"]
        assert web_info is not None
        assert web_info["enabled"] is True
        assert web_info["port"] == 9999
        assert web_info["host"] == "127.0.0.1"
    finally:
        daemon.stop_in_thread()


def test_update_local_restart_only(tmp_path: Path):
    chat_dir = tmp_path / "chat"
    sock_path = chat_dir / "engine.sock"

    daemon = OptChatDaemon(
        chat_dir=chat_dir,
        socket_path=sock_path,
        provider_name="mock",
    )
    daemon._skip_execv_for_test = True
    daemon.start_in_thread()

    try:
        res = update_local(chat_dir=chat_dir, restart_only=True)
        assert res["status"] == "ok"
        assert res["updated"] is False
        assert res["restarted"] is True
    finally:
        daemon.stop_in_thread()


def test_update_local_when_daemon_offline(tmp_path: Path):
    chat_dir = tmp_path / "offline_chat"
    chat_dir.mkdir()

    res = update_local(chat_dir=chat_dir, restart_only=True)
    assert res["status"] == "ok"
    assert res["restarted"] is False
    assert "not currently running" in res["message"]
