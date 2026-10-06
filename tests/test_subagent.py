"""Tests for Subagent and background task management (§9)."""

import asyncio
from pathlib import Path
import pytest

from optchat.compactor import Compactor
from optchat.providers.base import BaseLLMProvider, LLMResponse, ToolCall
from optchat.providers.mock import MockLLMProvider
from optchat.storage import Storage
from optchat.subagent import SubagentManager
from optchat.tools import ToolRegistry, create_subagent_registry
from optchat.tree import check_free_level0
from optchat.view import LiveView


class MockSubagentProvider(BaseLLMProvider):
    def __init__(self, responses: list[LLMResponse]):
        self.responses = list(responses)
        self.call_count = 0
        self.recorded_calls = []

    async def chat(self, system: str, messages: list, tools=None, cache_breakpoints=None, stream_callback=None) -> LLMResponse:
        self.recorded_calls.append((system, messages))
        if self.responses:
            resp = self.responses.pop(0)
            return resp
        return LLMResponse(text="Default subagent reply")

    async def compact_step(self, system: str, messages: list) -> str:
        return "compacted"


@pytest.mark.asyncio
async def test_subagent_spawn_and_report_delivery(tmp_path: Path):
    storage = Storage(tmp_path)
    storage.open()
    view = LiveView(storage)
    view.rebuild()

    reports_received = []

    def on_report(report_text: str):
        reports_received.append(report_text)
        # Log to storage as kind 'user' per §9
        msg = storage.append_message("user", report_text)
        view.on_new_message(msg.i)

    # Provider returning final report
    provider = MockSubagentProvider([
        LLMResponse(text="Found 5 python files in the repository.")
    ])

    tools = ToolRegistry()
    manager = SubagentManager(
        storage=storage,
        view=view,
        provider=provider,
        base_tools=tools,
        on_report=on_report,
    )
    tools.set_subagent_manager(manager)

    # 1. Spawn subagent
    res = manager.spawn(["Scan for python files"])
    assert "Spawned 1 subagent(s): sub-1" in res
    assert "sub-1" in manager.active_tasks

    # Wait for subagent background job to finish
    await asyncio.sleep(0.1)

    assert len(reports_received) == 1
    assert reports_received[0] == "[sub-1] Found 5 python files in the repository."

    # Verify message in storage
    assert len(storage.messages) == 1
    last_msg = storage.messages[0]
    assert last_msg.kind == "user"
    assert last_msg.text.startswith("[sub-1] ")

    # Verify that check_free_level0 tags it as 'work:' per §4.4
    tag = check_free_level0(last_msg)
    assert tag is not None
    assert tag.startswith("work: [sub-1]")

    storage.close()


@pytest.mark.asyncio
async def test_subagent_parallel_group_spawn(tmp_path: Path):
    storage = Storage(tmp_path)
    storage.open()
    view = LiveView(storage)
    view.rebuild()

    delivered_reports = []

    def on_report(text: str):
        delivered_reports.append(text)

    # Two subagents
    provider = MockSubagentProvider([
        LLMResponse(text="Report A"),
        LLMResponse(text="Report B"),
    ])

    tools = ToolRegistry()
    manager = SubagentManager(
        storage=storage,
        view=view,
        provider=provider,
        base_tools=tools,
        on_report=on_report,
    )

    manager.spawn(["Task 1", "Task 2"])
    await asyncio.sleep(0.1)

    assert len(delivered_reports) == 1
    # When all finish, delivered as ONE message formatted as '[id] report' each (§9)
    assert "[sub-1] Report" in delivered_reports[0]
    assert "[sub-2] Report" in delivered_reports[0]

    storage.close()


@pytest.mark.asyncio
async def test_subagent_tool_isolation(tmp_path: Path):
    """Subagent executes operational tools in private session without polluting main log (§9)."""
    storage = Storage(tmp_path)
    storage.open()
    view = LiveView(storage)
    view.rebuild()

    # Create a test file
    test_file = tmp_path / "test.txt"
    test_file.write_text("Hello from subagent test file")

    reports = []
    provider = MockSubagentProvider([
        # Step 1: Subagent calls read_file
        LLMResponse(
            text="Let me inspect the file",
            tool_calls=[ToolCall(id="call-1", name="read_file", arguments={"path": str(test_file)})]
        ),
        # Step 2: Final reply
        LLMResponse(text="File content was verified."),
    ])

    tools = ToolRegistry()
    manager = SubagentManager(
        storage=storage,
        view=view,
        provider=provider,
        base_tools=tools,
        on_report=lambda r: reports.append(r),
    )

    manager.spawn(["Verify file"])
    await asyncio.sleep(0.1)

    assert len(reports) == 1
    assert "File content was verified." in reports[0]

    # Crucial rule (§9): Subagent's intermediate tool calls and echoes are NOT in main storage!
    assert len(storage.messages) == 0

    storage.close()


@pytest.mark.asyncio
async def test_subagent_registry_excludes_spawn_and_tell():
    """Subagents get zoom, date, and ops tools, but not spawn or tell (§9)."""
    reg = create_subagent_registry()
    names = [d.name for d in reg.definitions]
    assert "zoom" in names
    assert "date" in names
    assert "bash" in names
    assert "read_file" in names
    assert "write_file" in names
    assert "edit_file" in names
    assert "spawn" not in names
    assert "tell" not in names
