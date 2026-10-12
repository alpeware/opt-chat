"""Tests for BulkImporter and Storage batch message operations."""

import json
from pathlib import Path
import sqlite3
import pytest

from optchat.bulk_importer import BulkImporter
from optchat.storage import Storage


def test_append_messages_batch(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()

    items = [
        ("user", "Hello first day", "2026-08-01T12:00:00Z"),
        ("talk", "Hello back", "2026-08-01T12:01:00Z"),
        ("user", "Hello second day", "2026-08-02T09:00:00Z"),
    ]

    msgs = storage.append_messages_batch(items)
    assert len(msgs) == 3
    assert msgs[0].i == 0
    assert msgs[1].i == 1
    assert msgs[2].i == 2

    # Check files created
    day1 = storage.main_dir / "2026-08-01.jsonl"
    day2 = storage.main_dir / "2026-08-02.jsonl"
    assert day1.exists()
    assert day2.exists()

    # Reopen storage to test durability and reloading
    storage.close()

    storage2 = Storage(tmp_path / "chat")
    storage2.open()
    assert len(storage2.messages) == 3
    assert storage2.messages[0].text == "Hello first day"
    assert storage2.messages[2].text == "Hello second day"
    storage2.close()


def test_bulk_importer_idempotency_and_filter(tmp_path: Path):
    # Setup mock gemini directory structure
    gemini_dir = tmp_path / "mock_gemini"
    cli_dir = gemini_dir / "antigravity-cli"
    cli_dir.mkdir(parents=True)

    # 1. Mock conversation_summaries.db
    db_path = cli_dir / "conversation_summaries.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            preview TEXT,
            step_count INTEGER,
            last_modified_time TEXT,
            last_user_input_time TEXT,
            workspace_uris TEXT
        )
    """)
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("conv-1", "Test Task 1", "Doing task 1", 2, "2026-10-01 10:00:00", "2026-10-01 10:00:00", '["file:///home/user/src/target-project"]'),
    )
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("conv-2", "Other Project", "Irrelevant task", 2, "2026-10-01 11:00:00", "2026-10-01 11:00:00", '["file:///home/user/src/other-project"]'),
    )
    conn.commit()
    conn.close()

    # 2. Mock transcript for conv-1
    tpath = cli_dir / "brain" / "conv-1" / ".system_generated" / "logs" / "transcript.jsonl"
    tpath.parent.mkdir(parents=True)
    with open(tpath, "w") as f:
        f.write(json.dumps({
            "type": "USER_INPUT",
            "content": "<USER_REQUEST>\nPlease optimize this loop.\n</USER_REQUEST>",
            "created_at": "2026-10-01T10:00:00Z",
        }) + "\n")
        f.write(json.dumps({
            "type": "PLANNER_RESPONSE",
            "content": "Here is the optimized loop.",
            "created_at": "2026-10-01T10:00:05Z",
        }) + "\n")

    # Target chat dir
    chat_dir = tmp_path / "target_optchat"
    storage = Storage(chat_dir)
    storage.open()

    importer = BulkImporter(
        storage=storage,
        workspace_filter="target-project",
        base_gemini_dir=gemini_dir,
    )

    # First run imports conv-1
    c_count, m_count = importer.import_all()
    assert c_count == 1
    assert m_count == 2
    assert len(storage.messages) == 2
    assert storage.messages[0].text == "Please optimize this loop."
    assert storage.messages[1].text == "Here is the optimized loop."

    # Second run is idempotent (0 imported)
    c_count2, m_count2 = importer.import_all()
    assert c_count2 == 0
    assert m_count2 == 0
    assert len(storage.messages) == 2

    storage.close()


def test_bulk_importer_filters_compaction_sessions(tmp_path: Path):
    gemini_dir = tmp_path / "gemini_compaction_test"
    cli_dir = gemini_dir / "antigravity-cli"
    cli_dir.mkdir(parents=True)

    db_path = cli_dir / "conversation_summaries.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            preview TEXT,
            step_count INTEGER,
            last_modified_time TEXT,
            last_user_input_time TEXT,
            workspace_uris TEXT
        )
    """)
    # Insert normal session
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("conv-user", "Build UI", "Build new interface", 2, "2026-10-01 10:00:00", "2026-10-01 10:00:00", '["file:///home/user/src/opt-chat"]'),
    )
    # Insert compaction session
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("conv-compaction", "Compaction Turn", "CRITICAL REQUIREMENT: Output ONLY the single summary line directly.", 2, "2026-10-01 10:05:00", "2026-10-01 10:05:00", '["file:///home/user/src/opt-chat"]'),
    )
    conn.commit()
    conn.close()

    # Create transcript for both
    tpath_user = cli_dir / "brain" / "conv-user" / ".system_generated" / "logs" / "transcript.jsonl"
    tpath_user.parent.mkdir(parents=True)
    tpath_user.write_text(json.dumps({
        "type": "USER_INPUT",
        "content": "<USER_REQUEST>\nLet us add a button.\n</USER_REQUEST>",
        "created_at": "2026-10-01T10:00:00Z",
    }) + "\n" + json.dumps({
        "type": "PLANNER_RESPONSE",
        "content": "Added the button.",
        "created_at": "2026-10-01T10:00:05Z",
    }) + "\n")

    tpath_comp = cli_dir / "brain" / "conv-compaction" / ".system_generated" / "logs" / "transcript.jsonl"
    tpath_comp.parent.mkdir(parents=True)
    tpath_comp.write_text(json.dumps({
        "type": "USER_INPUT",
        "content": "CRITICAL REQUIREMENT: Output ONLY the single summary line directly.\nSystem:\nYou write the memory of OptChat...",
        "created_at": "2026-10-01T10:05:00Z",
    }) + "\n" + json.dumps({
        "type": "PLANNER_RESPONSE",
        "content": "user: add button; talk: added.",
        "created_at": "2026-10-01T10:05:05Z",
    }) + "\n")

    chat_dir = tmp_path / "chat_storage"
    storage = Storage(chat_dir)
    storage.open()

    importer = BulkImporter(
        storage=storage,
        workspace_filter="opt-chat",
        base_gemini_dir=gemini_dir,
    )

    convs, unattached = importer.discover()
    assert len(convs) == 1
    assert convs[0]["cid"] == "conv-user"

    c_count, m_count = importer.import_all()
    assert c_count == 1
    assert m_count == 2
    assert storage.messages[0].text == "Let us add a button."
    assert storage.messages[1].text == "Added the button."

    storage.close()


def test_bulk_importer_tags_workspace(tmp_path: Path):
    gemini_dir = tmp_path / "gemini_ws_test"
    cli_dir = gemini_dir / "antigravity-cli"
    cli_dir.mkdir(parents=True)

    db_path = cli_dir / "conversation_summaries.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            preview TEXT,
            step_count INTEGER,
            last_modified_time TEXT,
            last_user_input_time TEXT,
            workspace_uris TEXT
        )
    """)
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("conv-ws", "Build feature", "feature preview", 2, "2026-10-01 10:00:00", "2026-10-01 10:00:00", '["file:///home/user/src/alpeware/jacquard"]'),
    )
    conn.commit()
    conn.close()

    tpath = cli_dir / "brain" / "conv-ws" / ".system_generated" / "logs" / "transcript.jsonl"
    tpath.parent.mkdir(parents=True)
    tpath.write_text(json.dumps({
        "type": "USER_INPUT",
        "content": "<USER_REQUEST>\nImplement jacquard feature\n</USER_REQUEST>",
        "created_at": "2026-10-01T10:00:00Z",
    }) + "\n" + json.dumps({
        "type": "PLANNER_RESPONSE",
        "content": "Implemented jacquard feature.",
        "created_at": "2026-10-01T10:00:05Z",
    }) + "\n")

    chat_dir = tmp_path / "chat_storage_ws"
    storage = Storage(chat_dir)
    storage.open()

    importer = BulkImporter(
        storage=storage,
        workspace_filter="alpeware/jacquard",
        base_gemini_dir=gemini_dir,
    )

    c_cnt, m_cnt = importer.import_all()
    assert c_cnt == 1
    assert m_cnt == 2
    assert storage.messages[0].workspace == "jacquard"
    assert storage.messages[1].workspace == "jacquard"

    storage.close()


def test_daemon_ipc_import_actions(tmp_path: Path):
    from optchat.daemon import OptChatDaemon
    from optchat.engine_client import EngineClient

    chat_dir = tmp_path / "daemon_chat"
    sock_path = tmp_path / "engine.sock"

    gemini_dir = tmp_path / "gemini_ipc_test"
    cli_dir = gemini_dir / "antigravity-cli"
    cli_dir.mkdir(parents=True)

    db_path = cli_dir / "conversation_summaries.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            preview TEXT,
            step_count INTEGER,
            last_modified_time TEXT,
            last_user_input_time TEXT,
            workspace_uris TEXT
        )
    """)
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("conv-ipc", "IPC Task", "Task preview", 2, "2026-10-01 10:00:00", "2026-10-01 10:00:00", '["file:///home/user/src/alpeware/jacquard"]'),
    )
    conn.commit()
    conn.close()

    tpath = cli_dir / "brain" / "conv-ipc" / ".system_generated" / "logs" / "transcript.jsonl"
    tpath.parent.mkdir(parents=True)
    tpath.write_text(json.dumps({
        "type": "USER_INPUT",
        "content": "<USER_REQUEST>\nIPC bulk import test\n</USER_REQUEST>",
        "created_at": "2026-10-01T10:00:00Z",
    }) + "\n" + json.dumps({
        "type": "PLANNER_RESPONSE",
        "content": "IPC reply test.",
        "created_at": "2026-10-01T10:00:05Z",
    }) + "\n")

    daemon = OptChatDaemon(
        chat_dir=chat_dir,
        socket_path=sock_path,
        provider_name="mock",
    )
    daemon.start_in_thread()

    client = EngineClient(socket_path=sock_path)
    assert client.is_daemon_alive() is True

    try:
        # 1. Test import_messages via IPC
        msg_res = client.import_messages([("user", "Direct import msg")])
        assert msg_res["status"] == "ok"
        assert msg_res["imported_count"] == 1
        assert msg_res["total_messages"] == 1

        # 2. Test import_agy via IPC
        agy_res = client.import_agy(str(tpath), skip_tools=True)
        assert agy_res["status"] == "ok"
        assert agy_res["imported_count"] == 2
        assert agy_res["total_messages"] == 3

        # 3. Test import_bulk via IPC with dry_run
        # Mock base_gemini_dir by monkeypatching Path.home or testing action directly
        bulk_res = client.import_bulk(workspace="jacquard", dry_run=True)
        assert bulk_res["status"] == "ok"
        assert bulk_res["dry_run"] is True
    finally:
        daemon.stop_in_thread()


def test_bulk_importer_filters_noise(tmp_path: Path):
    gemini_dir = tmp_path / "mock_gemini"
    cli_dir = gemini_dir / "antigravity-cli"
    cli_dir.mkdir(parents=True)

    # 1. DB with 1 clean session and 1 noisy automated session starting with '{'
    db_path = cli_dir / "conversation_summaries.db"
    conn = sqlite3.connect(str(db_path))
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE conversation_summaries (
            conversation_id TEXT PRIMARY KEY,
            title TEXT,
            preview TEXT,
            step_count INTEGER,
            last_modified_time TEXT,
            last_user_input_time TEXT,
            workspace_uris TEXT
        )
    """)
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("conv-clean", "Clean Task", "Real task preview", 4, "2026-10-01 10:00:00", "2026-10-01 10:00:00", '["file:///home/user/src/test-ws"]'),
    )
    cur.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("conv-auto", "", "{\n  \"dailyBar\": 750.4\n}", 4, "2026-10-01 10:00:00", "2026-10-01 10:00:00", '["file:///home/user/src/test-ws"]'),
    )
    conn.commit()
    conn.close()

    # 2. Mock transcript with both real user/talk and intermediate planner chatter
    tpath = cli_dir / "brain" / "conv-clean" / ".system_generated" / "logs" / "transcript.jsonl"
    tpath.parent.mkdir(parents=True)
    with open(tpath, "w") as f:
        f.write(json.dumps({
            "type": "USER_INPUT",
            "content": "<USER_REQUEST>\nLet's implement the design.\n</USER_REQUEST>",
            "created_at": "2026-10-01T10:00:00Z",
        }) + "\n")
        # Intermediate tool-intent planner utterance (should be filtered out)
        f.write(json.dumps({
            "type": "PLANNER_RESPONSE",
            "content": "I will view the src/core.clj file to inspect the engine.",
            "created_at": "2026-10-01T10:00:02Z",
        }) + "\n")
        # Substantive final reply (should be kept)
        f.write(json.dumps({
            "type": "PLANNER_RESPONSE",
            "content": "I have completed the design implementation according to the plan.",
            "created_at": "2026-10-01T10:00:05Z",
        }) + "\n")

    chat_dir = tmp_path / "target_optchat"
    storage = Storage(chat_dir)
    storage.open()

    importer = BulkImporter(
        storage=storage,
        workspace_filter="test-ws",
        base_gemini_dir=gemini_dir,
    )

    convs, unattached = importer.discover()
    # conv-auto should be excluded!
    assert len(convs) == 1
    assert convs[0]["cid"] == "conv-clean"

    c_count, m_count = importer.import_all()
    assert c_count == 1
    # Only 2 messages should be imported: the user prompt and the substantive talk!
    assert m_count == 2
    assert storage.messages[0].text == "Let's implement the design."
    assert storage.messages[1].text == "I have completed the design implementation according to the plan."

    storage.close()



