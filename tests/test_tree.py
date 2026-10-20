"""Tests for Tree coordinates, free node detection, and zoom/date (§3, §7.1)."""

from pathlib import Path
import pytest

from optchat.constants import NODE
from optchat.storage import Message, Storage
from optchat.tree import (
    Part,
    check_free_level0,
    check_free_merge,
    execute_date,
    execute_zoom,
    is_power_of_two,
    node_address,
    node_coords,
    node_covers,
    slot,
    unslot,
)


def test_slot_and_unslot():
    # Test level 0: 0, 2, 4, 6...
    assert slot(0, 0) == 0
    assert slot(0, 1) == 2
    assert slot(0, 2) == 4
    assert unslot(0) == (0, 0)
    assert unslot(2) == (0, 1)
    assert unslot(4) == (0, 2)

    # Test level 1: 1, 5, 9, 13...
    assert slot(1, 0) == 1
    assert slot(1, 1) == 5
    assert slot(1, 2) == 9
    assert unslot(1) == (1, 0)
    assert unslot(5) == (1, 1)
    assert unslot(9) == (1, 2)

    # Test level 2: 3, 11, 19...
    assert slot(2, 0) == 3
    assert slot(2, 1) == 11
    assert slot(2, 2) == 19
    assert unslot(3) == (2, 0)
    assert unslot(11) == (2, 1)
    assert unslot(19) == (2, 2)

    # Comprehensive round-trip check
    for l in range(10):
        for i in range(20):
            s = slot(l, i)
            assert unslot(s) == (l, i)

    with pytest.raises(ValueError):
        unslot(-1)


def test_binary_coordinates():
    assert is_power_of_two(1)
    assert is_power_of_two(2)
    assert is_power_of_two(16)
    assert not is_power_of_two(3)
    assert not is_power_of_two(0)

    # Level 0
    assert node_address(0, 5) == (5, 1)
    assert node_coords(5, 1) == (0, 5)
    assert node_covers(0, 5) == (5, 6)

    # Level 3 (n = 8)
    assert node_address(3, 2) == (16, 8)
    assert node_coords(16, 8) == (3, 2)
    assert node_covers(3, 2) == (16, 24)

    with pytest.raises(ValueError):
        node_coords(15, 8)  # 15 not multiple of 8


def test_free_node_level0():
    short_msg = Message(0, "user", "Deploy to staging", 23, "2026-10-06")
    free_txt = check_free_level0(short_msg)
    assert free_txt == "user: Deploy to staging"

    long_msg = Message(1, "user", "X" * 600, 606, "2026-10-06")
    assert check_free_level0(long_msg) is None


def test_free_node_merge():
    a = "user: deploy staging; echo: success"
    b = "user: check health; echo: 200 OK"
    merged = check_free_merge(a, b)
    assert merged == f"{a}\n{b}"

    huge = "Y" * 300
    assert check_free_merge(huge, huge) is None


def test_zoom_and_date(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()

    storage.append_message("user", "Hello world")
    storage.append_message("talk", "Hi there, how can I help?")

    # Free level 0 nodes
    storage.save_node(0, 0, "user: Hello world")
    storage.save_node(0, 1, "talk: Hi there, how can I help?")

    # Level 1 node
    storage.save_node(1, 0, "conversation between user and agent")

    # zoom(id=0, n=1) -> verbatim message id+0|kind [metadata]:\ntext
    res1 = execute_zoom(storage, 0, 1)
    assert res1.startswith("0+0|user")
    assert "Hello world" in res1
    assert "date:" in res1

    res2 = execute_zoom(storage, 1, 1)
    assert res2.startswith("1+0|talk")
    assert "Hi there, how can I help?" in res2

    # zoom(id=0, n=2) -> opens into children
    res_pair = execute_zoom(storage, 0, 2)
    lines = res_pair.splitlines()
    assert len(lines) == 2
    assert lines[0] == "0+1|user: Hello world"
    assert lines[1] == "1+1|talk: Hi there, how can I help?"

    # date(id)
    d = execute_date(storage, 0)
    assert d == storage.messages[0].date

    # Invalid zoom
    assert execute_zoom(storage, 5, 2) == "No line 5+2."
    assert execute_date(storage, 99) == "No message 99."

    storage.close()
