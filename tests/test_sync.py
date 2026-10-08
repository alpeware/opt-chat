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
