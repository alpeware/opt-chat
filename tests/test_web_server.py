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
        # 1. Test index HTML & static assets
        res = await client.get("/")
        assert res.status == 200
        text = await res.text()
        assert "OptChat" in text
        assert "<!DOCTYPE html>" in text
        assert "katex.min.css" in text
        assert "katex.min.js" in text
        assert "/static/style.css" in text
        assert "/static/app.js" in text

        # Test static assets are served
        res_css = await client.get("/static/style.css")
        assert res_css.status == 200
        css_text = await res_css.text()
        assert "msg-details" in css_text

        res_js = await client.get("/static/app.js")
        assert res_js.status == 200
        js_text = await res_js.text()
        assert "renderMathToken" in js_text
        assert "formatTimestamp" in js_text

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

        # 6. Test /mcp GET (endpoint descriptor)
        res = await client.get("/mcp")
        assert res.status == 200
        mcp_meta = await res.json()
        assert mcp_meta["service"] == "optchat-mcp-server"
        assert len(mcp_meta["tools"]) >= 6

        # 7. Test /mcp POST (JSON-RPC tools/call)
        res = await client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "optchat_zoom", "arguments": {"id": 0, "n": 1}},
            },
        )
        assert res.status == 200
        rpc_res = await res.json()
        assert "Hello world from test" in rpc_res["result"]["content"][0]["text"]

    finally:
        await client.close()
        server.shutdown()
