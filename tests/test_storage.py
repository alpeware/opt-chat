"""Tests for Storage layer (§2)."""

import json
from pathlib import Path
import pytest

from optchat.constants import CAP
from optchat.storage import Message, ProcessLock, Storage, TreeNode, cap_tool_output


def test_message_creation_and_size():
    msg = Message(0, "user", "hello world", len("user: hello world".encode("utf-8")), "2026-10-06T13:00:00Z")
    assert msg.i == 0
    assert msg.kind == "user"
    assert msg.size == 17
    d = msg.to_dict()
    assert d["i"] == 0
    loaded = Message.from_dict(d)
    assert loaded.text == "hello world"
    assert loaded.size == 17


def test_tree_node_creation():
    node = TreeNode(1, 0, "merged summary text", len("merged summary text".encode("utf-8")))
    assert node.l == 1
    assert node.i == 0
    d = node.to_dict()
    loaded = TreeNode.from_dict(d)
    assert loaded.text == "merged summary text"
    assert loaded.size == 19


def test_cap_tool_output():
    short_text = "short output"
    assert cap_tool_output(short_text, CAP) == short_text

    long_text = "A" * (CAP + 5000)
    capped = cap_tool_output(long_text, CAP)
    assert len(capped) <= CAP
    assert "... [cut " in capped
    assert capped.startswith("A" * 1000)
    assert capped.endswith("A" * 1000)


def test_storage_append_and_reload(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()

    msg0 = storage.append_message("user", "What is the status?")
    msg1 = storage.append_message("talk", "All systems operational.")

    assert msg0.i == 0
    assert msg1.i == 1
    assert len(storage.messages) == 2

    # Save tree nodes
    node0 = storage.save_node(0, 0, "user: What is the status?")
    assert storage.has_node(0, 0)
    assert storage.get_node(0, 0).text == "user: What is the status?"

    storage.close()

    # Reopen and verify persistence
    storage2 = Storage(tmp_path / "chat")
    storage2.open()
    assert len(storage2.messages) == 2
    assert storage2.messages[0].text == "What is the status?"
    assert storage2.messages[1].text == "All systems operational."
    assert storage2.has_node(0, 0)
    assert storage2.get_node(0, 0).text == "user: What is the status?"
    storage2.close()


def test_torn_lines_handling(tmp_path: Path):
    chat_dir = tmp_path / "chat"
    main_dir = chat_dir / "main"
    main_dir.mkdir(parents=True, exist_ok=True)

    today_file = main_dir / "2026-10-06.jsonl"
    valid_line = json.dumps({"i": 0, "kind": "user", "text": "Valid message", "size": 20, "date": "2026-10-06"})
    torn_line = '{"i": 1, "kind": "talk", "text": "Incomplete json'  # Crash mid-write

    with open(today_file, "w", encoding="utf-8") as f:
        f.write(valid_line + "\n" + torn_line + "\n")

    storage = Storage(chat_dir)
    storage.open()

    # Torn line should be skipped gracefully
    assert len(storage.messages) == 1
    assert storage.messages[0].text == "Valid message"

    # Appending next message works and starts on clean line
    msg = storage.append_message("talk", "Recovered nicely")
    assert msg.i == 1
    storage.close()


def test_single_writer_lock(tmp_path: Path):
    chat_dir = tmp_path / "chat"
    lock1 = ProcessLock(chat_dir / "lock")
    lock1.acquire()

    lock2 = ProcessLock(chat_dir / "lock")
    with pytest.raises(RuntimeError, match="Another OptChat process is currently active"):
        lock2.acquire()

    lock1.release()

    # After release, lock2 can acquire
    lock2.acquire()
    lock2.release()


def test_device_name_resolution(tmp_path: Path, monkeypatch):
    from optchat.storage import get_device_name, get_device_name_info

    # 1. Environment variable override
    monkeypatch.setenv("OPTCHAT_DEVICE_NAME", "custom-node")
    name, source = get_device_name_info()
    assert name == "custom-node"
    assert "environment" in source

    # 2. Config file
    monkeypatch.delenv("OPTCHAT_DEVICE_NAME", raising=False)
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(json.dumps({"device_name": "config-node"}))
    name, source = get_device_name_info(cfg_path=cfg_file)
    assert name == "config-node"
    assert "config file" in source
