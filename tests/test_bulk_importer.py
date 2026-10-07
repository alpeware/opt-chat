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

