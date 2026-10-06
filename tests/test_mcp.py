"""Tests for OptChat MCP server and agy integration (§9)."""

import asyncio
from pathlib import Path
import pytest

from optchat.mcp_server import OptChatMCPServer


@pytest.mark.asyncio
async def test_mcp_server_tools_and_calls(tmp_path: Path):
    chat_dir = tmp_path / "chat"
    server = OptChatMCPServer(chat_dir)

    tools = server.get_tools_list()
    tool_names = [t["name"] for t in tools]
    assert "optchat_view" in tool_names
    assert "optchat_zoom" in tool_names
    assert "optchat_date" in tool_names
    assert "optchat_log" in tool_names
    assert "optchat_stats" in tool_names
    assert "optchat_browse" in tool_names

    # Test optchat_log
    log_res = await server.handle_tool_call(
        "optchat_log",
        {"kind": "note", "text": "Architectural decision: use OptChat tree memory"},
    )
    assert "Logged message #0" in log_res

    # Test optchat_stats
    stats_res = await server.handle_tool_call("optchat_stats", {})
    assert "Messages: 1" in stats_res

    # Test optchat_view
    view_res = await server.handle_tool_call("optchat_view", {})
    assert "<chat>" in view_res

    # Test optchat_zoom (n=1)
    zoom_res = await server.handle_tool_call("optchat_zoom", {"id": 0, "n": 1})
    assert "0+0|note: Architectural decision: use OptChat tree memory" in zoom_res

    # Test optchat_date
    date_res = await server.handle_tool_call("optchat_date", {"id": 0})
    assert len(date_res) > 5

    # Test optchat_browse
    browse_file = chat_dir / "test_report.html"
    browse_res = await server.handle_tool_call("optchat_browse", {"output_path": str(browse_file)})
    assert browse_file.is_file()
    assert "Report generated at" in browse_res

    server.compactor.stop()
    server.storage.close()
