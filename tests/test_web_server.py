"""Tests for OptChat Web Server."""

from pathlib import Path
import pytest
from aiohttp.test_utils import TestClient, TestServer

from optchat.storage import Storage
from optchat.web_server import OptChatWebServer


@pytest.mark.asyncio
async def test_web_server_endpoints(tmp_path: Path):
    storage_dir = tmp_path / "chat"
    storage = Storage(storage_dir)
    storage.open()
    msg0 = storage.append_message("user", "Hello world from test")
    msg1 = storage.append_message("talk", "Hello back from agent")
    storage.close()

    server = OptChatWebServer(
        chat_dir=storage_dir,
        provider_name="mock",
    )
    await server.init_engine()

    client = TestClient(TestServer(server.app))
    await client.start_server()

    try:
        # 1. Test index HTML
        res = await client.get("/")
        assert res.status == 200
        text = await res.text()
        assert "OptChat" in text
        assert "<!DOCTYPE html>" in text
        assert "katex.min.css" in text
        assert "katex.min.js" in text
        assert "renderMathToken" in text
        assert "msg-details" in text
        assert "formatTimestamp" in text

        # 2. Test api/state
        res = await client.get("/api/state")
        assert res.status == 200
        state = await res.json()
        assert state["messages_count"] == 2
        assert state["view_budget"] > 0

        # 3. Test api/history
        res = await client.get("/api/history")
        assert res.status == 200
        history = await res.json()
        assert len(history) == 2
        assert history[0]["text"] == "Hello world from test"
        assert history[1]["text"] == "Hello back from agent"

        # 4. Test api/command (/stats)
        res = await client.post("/api/command", json={"command": "/stats"})
        assert res.status == 200
        data = await res.json()
        assert "Messages: 2" in data["output"]

        # 5. Test api/zoom (verbatim leaf message)
        res = await client.post("/api/zoom", json={"id": 0, "n": 1})
        assert res.status == 200
        data = await res.json()
        assert "Hello world from test" in data["output"]

    finally:
        await client.close()
        server.shutdown()
