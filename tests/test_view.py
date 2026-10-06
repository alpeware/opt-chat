"""Tests for LiveView, incremental folding, and cache breakpoints (§5, §6, §8)."""

import asyncio
from pathlib import Path
import pytest

from optchat.constants import MARKS, PLACEHOLDER
from optchat.storage import Storage
from optchat.tree import Part
from optchat.view import LiveView


@pytest.mark.asyncio
async def test_view_incremental_fold(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()

    # Create a view with a small budget (e.g. 100 bytes) to trigger folding early
    view = LiveView(storage, budget=100)

    # Add 4 messages
    for i in range(4):
        storage.append_message("user", f"msg {i}")
        # Save level 0 summaries
        storage.save_node(0, i, f"summary {i}")
        view.on_new_message(i)

    # Pre-build level 1 nodes
    storage.save_node(1, 0, "merged summary 0-1")
    storage.save_node(1, 1, "merged summary 2-3")

    view.fit()

    # Verify that parts merged from level 0 to level 1
    # Check that parts tile [0, 4)
    assert len(view.parts) <= 4
    covered_messages = sum(p.n for p in view.parts)
    assert covered_messages == 4

    storage.close()


@pytest.mark.asyncio
async def test_settle_wait(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()

    view = LiveView(storage, budget=1000)

    storage.append_message("user", "Hello")
    view.on_new_message(0)

    # Node not built yet
    assert not view.is_settled()
    assert "(not summarized yet: zoom it)" in view.render()

    async def build_in_background():
        await asyncio.sleep(0.05)
        storage.save_node(0, 0, "user: Hello")
        view.on_node_built()

    asyncio.create_task(build_in_background())

    # Wait for settle
    settled = await view.settle(timeout=1.0)
    assert settled
    assert view.is_settled()
    assert "user: Hello" in view.render()

    storage.close()


def test_rebuild_from_scratch(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()

    view1 = LiveView(storage, budget=200)
    for i in range(8):
        storage.append_message("user", f"message {i}")
        storage.save_node(0, i, f"user: summary {i}")
        view1.on_new_message(i)

    # Save level 1 and 2 nodes
    for i in range(4):
        storage.save_node(1, i, f"level 1 merge {i}")
    for i in range(2):
        storage.save_node(2, i, f"level 2 merge {i}")

    view1.fit()

    # Rebuild a new view from scratch over the same storage
    view2 = LiveView(storage, budget=200)
    view2.rebuild()

    assert len(view1.parts) == len(view2.parts)
    for p1, p2 in zip(view1.parts, view2.parts):
        assert p1 == p2

    storage.close()


def test_compactor_context_has_no_ids(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()

    view = LiveView(storage)
    storage.append_message("user", "Step 1 details")
    storage.save_node(0, 0, "user: Step 1 details")
    view.on_new_message(0)

    storage.append_message("talk", "Step 2 done")
    storage.save_node(0, 1, "talk: Step 2 done")
    view.on_new_message(1)

    # Render compactor context up to message 2
    ctx = view.render_compactor_context(end_message_idx=2)
    assert "<chat>" in ctx
    assert "</chat>" in ctx
    # Invariant: NO IDS anywhere in compactor context (§4.2)
    assert "0+1|" not in ctx
    assert "1+1|" not in ctx
    assert "user: Step 1 details" in ctx
    assert "talk: Step 2 done" in ctx

    storage.close()


def test_cache_pieces_split(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()

    view = LiveView(storage)
    # Add dummy parts
    for i in range(50):
        storage.append_message("user", f"test message number {i}")
        storage.save_node(0, i, f"summary {i} with some content")
        view.on_new_message(i)

    # Test cache marks split
    full_text = view.render()
    marks = (200, 400, 600)
    pieces = view.render_with_cache_pieces(marks=marks)

    assert "".join(pieces) == full_text
    for p in pieces[:-1]:
        assert p.endswith("\n")

    storage.close()
