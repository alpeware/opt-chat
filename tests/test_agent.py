"""Tests for TurnAgent and turn loop (§7)."""

import asyncio
from pathlib import Path
import pytest

from optchat.agent import TurnAgent
from optchat.compactor import Compactor
from optchat.providers.mock import MockLLMProvider
from optchat.storage import Storage
from optchat.tools import ToolRegistry
from optchat.view import LiveView


@pytest.mark.asyncio
async def test_agent_turn_execution(tmp_path: Path):
    storage = Storage(tmp_path / "chat")
    storage.open()
    view = LiveView(storage)
    provider = MockLLMProvider()
    compactor = Compactor(storage, view, provider)
    tool_registry = ToolRegistry()

    ui_events = []

    def ui_callback(event_type: str, content: str) -> None:
        ui_events.append((event_type, content))

    agent = TurnAgent(
        storage=storage,
        view=view,
        compactor=compactor,
        provider=provider,
        tool_registry=tool_registry,
        git_auto_commit=False,
        ui_callback=ui_callback,
    )

    # First turn
    await agent.submit_user_message("Hello OptChat")
    while agent.is_running_turn:
        await asyncio.sleep(0.02)

    # Check logged messages
    assert len(storage.messages) == 2
    assert storage.messages[0].kind == "user"
    assert storage.messages[0].text == "Hello OptChat"
    assert storage.messages[1].kind == "talk"
    assert "OptChat" in storage.messages[1].text

    # Verify thoughts were emitted to UI but NEVER logged to storage (§2)
    reasoning_events = [c for evt, c in ui_events if evt == "reasoning"]
    assert len(reasoning_events) > 0
    for msg in storage.messages:
        assert "Analyzing conversation view" not in msg.text

    # Second turn with a tool call (zoom)
    ui_events.clear()
    await agent.submit_user_message("Please zoom(0, 1)")
    while agent.is_running_turn:
        await asyncio.sleep(0.02)

    # Storage should have: user, talk (turn 1), user, tool, echo, talk (turn 2)
    kinds = [m.kind for m in storage.messages]
    assert kinds == ["user", "talk", "user", "tool", "echo", "talk"]

    tool_msg = storage.messages[3]
    assert tool_msg.kind == "tool"
    assert "zoom" in tool_msg.text

    echo_msg = storage.messages[4]
    assert echo_msg.kind == "echo"
    assert "0+0|user" in echo_msg.text
    assert "Hello OptChat" in echo_msg.text

    compactor.stop()
    storage.close()
