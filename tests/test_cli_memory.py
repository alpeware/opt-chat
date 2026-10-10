"""Tests for CLI memory commands: view, zoom, date, log, stats, history."""

from pathlib import Path
import subprocess
import sys
import pytest

from optchat.storage import Storage
from optchat.view import LiveView


def run_cli(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "optchat.cli", *args],
        capture_output=True,
        text=True,
    )


def test_cli_memory_offline(tmp_path: Path):
    chat_dir = tmp_path / "chat"
    storage = Storage(chat_dir)
    storage.open()
    storage.append_message("user", "Hello world from test")
    storage.append_message("note", "Key decision: testing offline CLI commands")
    view = LiveView(storage)
    view.rebuild()
    storage.close()

    # 1. stats
    res = run_cli("stats", "--chat-dir", str(chat_dir))
    assert res.returncode == 0
    assert "Messages:    2" in res.stdout
    assert "Offline Storage" in res.stdout

    # 2. view
    res = run_cli("view", "--chat-dir", str(chat_dir))
    assert res.returncode == 0
    assert "<chat>" in res.stdout
    assert "</chat>" in res.stdout
    assert "0+1|" in res.stdout

    # 3. zoom (n=1)
    res = run_cli("zoom", "0", "1", "--chat-dir", str(chat_dir))
    assert res.returncode == 0
    assert "Hello world from test" in res.stdout

    # 4. date
    res = run_cli("date", "0", "--chat-dir", str(chat_dir))
    assert res.returncode == 0
    assert "Message 0:" in res.stdout

    # 5. history
    res = run_cli("history", "--chat-dir", str(chat_dir))
    assert res.returncode == 0
    assert "Recent Messages" in res.stdout
    assert "Hello world from test" in res.stdout
    assert "Key decision: testing offline CLI commands" in res.stdout

    # 6. log
    res = run_cli("log", "Third message via CLI", "--kind", "talk", "--chat-dir", str(chat_dir))
    assert res.returncode == 0
    assert "Logged message #2" in res.stdout

    # Check that new message is reflected in stats
    res = run_cli("stats", "--chat-dir", str(chat_dir))
    assert res.returncode == 0
    assert "Messages:    3" in res.stdout


def test_cli_memory_with_daemon(tmp_path: Path):
    from optchat.daemon import OptChatDaemon
    chat_dir = tmp_path / "chat"
    sock_path = chat_dir / "engine.sock"

    daemon = OptChatDaemon(
        chat_dir=chat_dir,
        socket_path=sock_path,
        provider_name="mock",
    )
    daemon.start_in_thread()

    try:
        # 1. log via daemon
        res = run_cli("log", "Daemon test message", "--kind", "user", "--chat-dir", str(chat_dir))
        assert res.returncode == 0
        assert "Logged message #0" in res.stdout

        # 2. stats via daemon
        res = run_cli("stats", "--chat-dir", str(chat_dir))
        assert res.returncode == 0
        assert "via Daemon" in res.stdout
        assert "Messages:    1" in res.stdout

        # 3. view via daemon
        res = run_cli("view", "--chat-dir", str(chat_dir))
        assert res.returncode == 0
        assert "<chat>" in res.stdout

        # 4. zoom via daemon
        res = run_cli("zoom", "0", "1", "--chat-dir", str(chat_dir))
        assert res.returncode == 0
        assert "Daemon test message" in res.stdout

        # 5. date via daemon
        res = run_cli("date", "0", "--chat-dir", str(chat_dir))
        assert res.returncode == 0
        assert "Message 0:" in res.stdout

        # 6. history via daemon
        res = run_cli("history", "--chat-dir", str(chat_dir))
        assert res.returncode == 0
        assert "Daemon test message" in res.stdout
    finally:
        daemon.stop_in_thread()
