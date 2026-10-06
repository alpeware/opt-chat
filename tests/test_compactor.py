"""Tests for Compactor worker (§4)."""

import asyncio
from pathlib import Path
import pytest

from optchat.compactor import Compactor
from optchat.constants import NODE
from optchat.providers.mock import MockLLMProvider
from optchat.storage import Storage
from optchat.view import LiveView


@pytest.mark.asyncio
async def test_compactor_free_nodes(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()
    view = LiveView(storage)
    provider = MockLLMProvider()
    compactor = Compactor(storage, view, provider)

    # Message 0: short (fits in 512 bytes)
    msg0 = storage.append_message("user", "Short message")
    view.on_new_message(msg0.i)

    # Message 1: short
    msg1 = storage.append_message("talk", "Short reply")
    view.on_new_message(msg1.i)

    compactor.pump()
    await asyncio.sleep(0.05)

    # Both level 0 nodes should be built as free nodes
    assert storage.has_node(0, 0)
    assert storage.get_node(0, 0).text == "user: Short message"
    assert storage.has_node(0, 1)
    assert storage.get_node(0, 1).text == "talk: Short reply"

    # Merge node (1, 0) should also be built as free node
    compactor.pump()
    await asyncio.sleep(0.05)
    assert storage.has_node(1, 0)
    assert storage.get_node(1, 0).text == "user: Short message\ntalk: Short reply"

    compactor.stop()
    storage.close()


@pytest.mark.asyncio
async def test_compactor_retry_on_overshoot(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()
    view = LiveView(storage)
    # Configure mock provider to force overshoot on first try
    provider = MockLLMProvider(force_overshoot_first_try=True)
    compactor = Compactor(storage, view, provider)

    # Large message requiring model compression
    large_text = "Detailed explanation: " + ("database migration step and validation; " * 30)
    msg = storage.append_message("user", large_text)
    view.on_new_message(msg.i)

    compactor.pump()
    # Wait for compactor and its retry loop
    for _ in range(20):
        if storage.has_node(0, 0):
            break
        await asyncio.sleep(0.05)

    assert storage.has_node(0, 0)
    node = storage.get_node(0, 0)
    assert len(node.text.encode("utf-8")) <= NODE

    compactor.stop()
    storage.close()


@pytest.mark.asyncio
async def test_compactor_in_order_compression(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()
    view = LiveView(storage)
    provider = MockLLMProvider()
    compactor = Compactor(storage, view, provider)

    # Append 4 large messages
    for i in range(4):
        msg = storage.append_message("user", f"Large message {i}: " + ("x" * 600))
        view.on_new_message(msg.i)

    # Rule 3 says node 0 must build first before node 1 can start
    assert view.first_unbuilt_message() == 0

    compactor.pump()
    for _ in range(30):
        if storage.has_node(0, 3):
            break
        await asyncio.sleep(0.05)

    for i in range(4):
        assert storage.has_node(0, i)

    compactor.stop()
    storage.close()
