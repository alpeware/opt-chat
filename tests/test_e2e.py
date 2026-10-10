"""End-to-end integration tests for OptChat (§1-§10)."""

import asyncio
from pathlib import Path
import pytest

from optchat.agent import TurnAgent
from optchat.compactor import Compactor
from optchat.providers.mock import MockLLMProvider
from optchat.storage import Storage
from optchat.tools import ToolRegistry
from optchat.view import LiveView
from optchat.visualizer import export_html_to_file, generate_html_report


@pytest.mark.asyncio
async def test_full_session_and_restart(tmp_path: Path):
    chat_dir = tmp_path / "chat"

    # --- Session 1 ---
    storage1 = Storage(chat_dir)
    storage1.open()
    view1 = LiveView(storage1, budget=250)
    view1.rebuild()
    provider1 = MockLLMProvider()
    compactor1 = Compactor(storage1, view1, provider1)
    tool_registry1 = ToolRegistry()

    agent1 = TurnAgent(
        storage=storage1,
        view=view1,
        compactor=compactor1,
        provider=provider1,
        tool_registry=tool_registry1,
        git_auto_commit=False,
    )

    # Run 4 conversational turns
    prompts = [
        "First task: initialize database schema",
        "Second task: configure load balancer",
        "Third task: run health check",
        "Fourth task: verify everything",
    ]

    for p in prompts:
        await agent1.submit_user_message(p)
        while agent1.is_running_turn:
            await asyncio.sleep(0.02)

    # Let compactor finish all merges
    compactor1.pump()
    for _ in range(40):
        if view1.is_settled():
            break
        await asyncio.sleep(0.05)

    assert len(storage1.messages) >= 8
    assert len(storage1.tree) >= 4

    # Generate HTML report
    html_file = chat_dir / "browse.html"
    export_html_to_file(storage1, view1, html_file)
    assert html_file.is_file()
    html_text = html_file.read_text(encoding="utf-8")
    assert "OptChat Memory Browser" in html_text
    assert "First task: initialize database schema" in html_text

    compactor1.stop()
    storage1.close()

    # --- Session 2 (Simulating system restart) ---
    storage2 = Storage(chat_dir)
    storage2.open()
    assert len(storage2.messages) >= 8

    view2 = LiveView(storage2, budget=250)
    view2.rebuild()

    # View should match exactly what was in memory in session 1
    assert len(view2.parts) == len(view1.parts)
    for p1, p2 in zip(view1.parts, view2.parts):
        assert p1 == p2

    provider2 = MockLLMProvider()
    compactor2 = Compactor(storage2, view2, provider2)
    tool_registry2 = ToolRegistry()

    agent2 = TurnAgent(
        storage=storage2,
        view=view2,
        compactor=compactor2,
        provider=provider2,
        tool_registry=tool_registry2,
        git_auto_commit=False,
    )

    # Session 2 turn: ask about previous history using zoom
    await agent2.submit_user_message("Please zoom(0, 1) to inspect task 1")
    while agent2.is_running_turn:
        await asyncio.sleep(0.02)

    last_echo = [m for m in storage2.messages if m.kind == "echo"][-1]
    assert "0+0|user" in last_echo.text
    assert "First task: initialize database schema" in last_echo.text

    compactor2.stop()
    storage2.close()
