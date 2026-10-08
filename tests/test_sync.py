"""Unit tests for OptChat P2P Idempotent Memory Synchronization."""

from pathlib import Path

from optchat.storage import Message, Storage, TreeNode
from optchat.sync import (
    apply_sync_payload,
    export_sync_payload,
    reconcile_messages,
    reconcile_tree_nodes,
)


def test_reconcile_messages_idempotent():
    # Device Desktop
    m1 = Message(0, "user", "Hello from desktop", 20, "2026-10-08T10:00:00.000000Z", "2026-10-08T10:00:00.000000Z#desktop")
    m2 = Message(1, "talk", "Reply on desktop", 20, "2026-10-08T10:01:00.000000Z", "2026-10-08T10:01:00.000000Z#desktop")

    # Device Phone (Termux)
    m3 = Message(0, "user", "Hello from desktop", 20, "2026-10-08T10:00:00.000000Z", "2026-10-08T10:00:00.000000Z#desktop")
    m4 = Message(1, "user", "Mobile query", 15, "2026-10-08T10:00:30.000000Z", "2026-10-08T10:00:30.000000Z#phone")

    # Merge Desktop + Phone
    unified_1, has_changes_1 = reconcile_messages([m1, m2], [m3, m4])
    assert has_changes_1 is True
    assert len(unified_1) == 3

    # Check order: m1 (10:00), m4 (10:00:30), m2 (10:01)
    assert unified_1[0].key == "2026-10-08T10:00:00.000000Z#desktop"
    assert unified_1[1].key == "2026-10-08T10:00:30.000000Z#phone"
    assert unified_1[2].key == "2026-10-08T10:01:00.000000Z#desktop"
    assert [m.i for m in unified_1] == [0, 1, 2]

    # Verify Idempotence: Merge result with Phone again -> no changes
    unified_2, has_changes_2 = reconcile_messages(unified_1, [m3, m4])
    assert has_changes_2 is False
    assert [m.key for m in unified_2] == [m.key for m in unified_1]

    # Verify Idempotence: Merge result with Desktop again -> no changes
    unified_3, has_changes_3 = reconcile_messages(unified_1, [m1, m2])
    assert has_changes_3 is False
    assert [m.key for m in unified_3] == [m.key for m in unified_1]


def test_reconcile_identical_timestamps_tie_breaker():
    # If two devices produced messages at the exact same microsecond, device_name breaks tie
    m_desk = Message(0, "note", "A", 1, "2026-10-08T12:00:00.000000Z", "2026-10-08T12:00:00.000000Z#desktop")
    m_phone = Message(0, "note", "B", 1, "2026-10-08T12:00:00.000000Z", "2026-10-08T12:00:00.000000Z#phone")

    # 'desktop' < 'phone' in ASCII
    res1, _ = reconcile_messages([m_desk], [m_phone])
    res2, _ = reconcile_messages([m_phone], [m_desk])

    assert [m.key for m in res1] == [m_desk.key, m_phone.key]
    assert [m.key for m in res2] == [m_desk.key, m_phone.key]
    assert res1[0].text == "A"
    assert res1[1].text == "B"


def test_sync_payload_application(tmp_path: Path):
    store_a = Storage(tmp_path / "peer_a")
    store_a.open()
    m_a1 = store_a.append_message("user", "Hello from A")
    m_a2 = store_a.append_message("talk", "Reply from A")

    store_b = Storage(tmp_path / "peer_b")
    store_b.open()
    m_b1 = store_b.append_message("user", "Hello from B")

    # Export from A, apply to B
    payload_a = export_sync_payload(store_a)
    result_b = apply_sync_payload(store_b, payload_a)

    assert result_b["status"] == "ok"
    assert len(store_b.messages) == 3

    # Now export from B, apply back to A
    payload_b = export_sync_payload(store_b)
    result_a = apply_sync_payload(store_a, payload_b)

    assert result_a["status"] == "ok"
    assert len(store_a.messages) == 3

    # Both stores now have the exact same message keys and order
    keys_a = [m.key for m in store_a.messages]
    keys_b = [m.key for m in store_b.messages]
    assert keys_a == keys_b

    store_a.close()
    store_b.close()


def test_fork_reconciliation_tree_invalidation(tmp_path: Path):
    store = Storage(tmp_path / "fork_store")
    store.open()

    # Pre-populate 4 messages
    m0 = store.append_message("user", "Msg 0")
    m1 = store.append_message("talk", "Msg 1")
    m2 = store.append_message("user", "Msg 2")
    m3 = store.append_message("talk", "Msg 3")

    # Add tree nodes: (1, 0) covers 0..1, (1, 1) covers 2..3
    store.save_node(1, 0, "Summary of 0 and 1")
    store.save_node(1, 1, "Summary of 2 and 3")
    assert store.has_node(1, 0)
    assert store.has_node(1, 1)

    # Remote payload with an offline message inserted right before m2
    # m2 has date, let's create a message with earlier date than m2
    offline_msg = Message(
        i=99,
        kind="note",
        text="Offline note from phone",
        size=25,
        date=m1.date + "_interleaved",  # Interleaves between m1 and m2
        key=m1.date + "_interleaved#phone",
    )

    remote_payload = {
        "device": "phone",
        "total_messages": 1,
        "messages": [offline_msg.to_dict()],
        "tree": [],
    }

    res = apply_sync_payload(store, remote_payload)
    assert res["status"] == "ok"
    assert len(store.messages) == 5

    # Node (1, 0) covers [0, 2) which was unchanged prefix <= first_diff_idx -> preserved!
    assert store.has_node(1, 0)
    assert store.get_node(1, 0).text == "Summary of 0 and 1"

    # Node (1, 1) spanned into the rewritten message zone -> invalidated!
    assert not store.has_node(1, 1)

    store.close()


def test_peer_discovery_and_ssh_hosts(tmp_path: Path, monkeypatch):
    import json
    from optchat.sync import get_configured_peers, get_ssh_config_hosts

    # Test config.json peers
    chat_dir = tmp_path / ".optchat"
    chat_dir.mkdir(parents=True)
    cfg_file = chat_dir / "config.json"
    cfg_file.write_text(json.dumps({"peers": ["desktop", "phone", "laptop"]}))

    peers = get_configured_peers(chat_dir)
    assert peers == ["desktop", "phone", "laptop"]

    # Test add_configured_peer
    from optchat.sync import add_configured_peer, remove_configured_peer
    assert add_configured_peer("tablet", chat_dir) is True
    assert add_configured_peer("tablet", chat_dir) is False  # Duplicate
    assert "tablet" in get_configured_peers(chat_dir)

    # Test remove_configured_peer
    assert remove_configured_peer("phone", chat_dir) is True
    assert remove_configured_peer("phone", chat_dir) is False  # Already removed
    assert "phone" not in get_configured_peers(chat_dir)

    # Test ssh config parser
    fake_ssh_dir = tmp_path / ".ssh"
    fake_ssh_dir.mkdir()
    ssh_cfg = fake_ssh_dir / "config"
    ssh_cfg.write_text(
        "Host desktop\n"
        "    HostName 192.168.1.10\n"
        "\n"
        "Host phone laptop\n"
        "    Port 2222\n"
        "\n"
        "Host *\n"
        "    ServerAliveInterval 60\n"
    )

    hosts = get_ssh_config_hosts(ssh_cfg)
    assert "desktop" in hosts
    assert "phone" in hosts
    assert "laptop" in hosts
    assert "*" not in hosts
