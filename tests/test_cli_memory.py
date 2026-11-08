"""Tests for CLI memory commands: view, zoom, date, log, stats, history."""

import json
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
    storage.append_message("user", "Hello world from test", device="laptop")
    storage.append_message("note", "Key decision: testing offline CLI commands", device="desktop")
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

    # 5b. history --json
    res_json = run_cli("history", "--json", "--limit", "1", "--chat-dir", str(chat_dir))
    assert res_json.returncode == 0
    data = json.loads(res_json.stdout)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["kind"] == "note"
    assert data[0]["text"] == "Key decision: testing offline CLI commands"
    assert "device" in data[0]

    # 5c. history --json when empty
    res_empty = run_cli("history", "--json", "--workspace", "nonexistent", "--chat-dir", str(chat_dir))
    assert res_empty.returncode == 0
    assert json.loads(res_empty.stdout) == []

    # 5d. history --device filter (offline)
    res_dev = run_cli("history", "--device", "laptop", "--json", "--chat-dir", str(chat_dir))
    assert res_dev.returncode == 0
    dev_data = json.loads(res_dev.stdout)
    assert len(dev_data) == 1
    assert dev_data[0]["device"] == "laptop"
    assert dev_data[0]["text"] == "Hello world from test"

    # 5e. history --since filter (offline: message ID, relative duration, keyword)
    res_since_id = run_cli("history", "--since", "1", "--json", "--chat-dir", str(chat_dir))
    assert res_since_id.returncode == 0
    since_id_data = json.loads(res_since_id.stdout)
    assert len(since_id_data) == 1
    assert since_id_data[0]["i"] == 1

    res_since_rel = run_cli("history", "--since", "1h", "--json", "--chat-dir", str(chat_dir))
    assert res_since_rel.returncode == 0
    assert len(json.loads(res_since_rel.stdout)) == 2

    res_since_future = run_cli("history", "--since", "2099-01-01", "--json", "--chat-dir", str(chat_dir))
    assert res_since_future.returncode == 0
    assert json.loads(res_since_future.stdout) == []

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

        # 7. workspace filtering test
        run_cli("log", "[opt-chat] Fix bug in parser", "--kind", "talk", "--chat-dir", str(chat_dir))
        run_cli("log", "[website] Update landing page hero", "--kind", "note", "--chat-dir", str(chat_dir))

        res_ws = run_cli("history", "--workspace", "opt-chat", "--chat-dir", str(chat_dir))
        assert res_ws.returncode == 0
        assert "Fix bug in parser" in res_ws.stdout
        assert "Update landing page hero" not in res_ws.stdout

        # 8. history --json with workspace and kind filter via daemon
        res_ws_json = run_cli("history", "--workspace", "opt-chat", "--kind", "talk", "--json", "--chat-dir", str(chat_dir))
        assert res_ws_json.returncode == 0
        ws_data = json.loads(res_ws_json.stdout)
        assert isinstance(ws_data, list)
        assert len(ws_data) == 1
        assert ws_data[0]["kind"] == "talk"
        assert "Fix bug in parser" in ws_data[0]["text"]

        # 9. history --device filter via daemon
        from optchat.engine_client import EngineClient
        client = EngineClient(socket_path=sock_path)
        client.append_message("note", "Note from remote phone", workspace="opt-chat", device="phone")
        res_dev_daemon = run_cli("history", "--device", "phone", "--json", "--chat-dir", str(chat_dir))
        assert res_dev_daemon.returncode == 0
        dev_daemon_data = json.loads(res_dev_daemon.stdout)
        assert len(dev_daemon_data) == 1
        assert dev_daemon_data[0]["device"] == "phone"
        assert dev_daemon_data[0]["text"] == "Note from remote phone"

        # 10. history --since filter via daemon
        res_since_daemon = run_cli("history", "--since", "#3", "--json", "--chat-dir", str(chat_dir))
        assert res_since_daemon.returncode == 0
        since_daemon_data = json.loads(res_since_daemon.stdout)
        assert len(since_daemon_data) == 1
        assert since_daemon_data[0]["i"] == 3
        assert since_daemon_data[0]["text"] == "Note from remote phone"

        res_view_ws = run_cli("view", "--workspace", "opt-chat", "--chat-dir", str(chat_dir))
        assert res_view_ws.returncode == 0
        assert "Fix bug in parser" in res_view_ws.stdout
    finally:
        daemon.stop_in_thread()
