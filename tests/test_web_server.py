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
        assert "model" in state

        # 2b. Test api/models and api/session model switching
        res_models = await client.get("/api/models")
        assert res_models.status == 200
        models_data = await res_models.json()
        assert len(models_data["models"]) >= 10
        assert "current_model" in models_data

        res_sess = await client.post("/api/session", json={"model": "gemini-4-argon-high"})
        assert res_sess.status == 200
        sess_data = await res_sess.json()
        assert sess_data["model"] == "gemini-4-argon-high"

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
        # 8. Test /api/file endpoint (directory and file viewing)
        test_dir = tmp_path / "sample_workspace"
        test_dir.mkdir()
        sample_file = test_dir / "summary.txt"
        sample_file.write_text("Execution complete: 10/10 tasks evaluated.")

        # 8a. Directory view
        res_dir = await client.get(f"/api/file?path={test_dir}")
        assert res_dir.status == 200
        dir_html = await res_dir.text()
        assert "sample_workspace" in dir_html
        assert "summary.txt" in dir_html

        # 8b. File preview view
        res_file = await client.get(f"/api/file?path={sample_file}")
        assert res_file.status == 200
        file_html = await res_file.text()
        assert "summary.txt" in file_html
        assert "10/10 tasks evaluated" in file_html

        # 8c. Raw file download
        res_raw = await client.get(f"/api/file?path={sample_file}&raw=1")
        assert res_raw.status == 200
        raw_text = await res_raw.text()
        assert raw_text == "Execution complete: 10/10 tasks evaluated."

        # 8d. 404 for missing path
        res_404 = await client.get("/api/file?path=/tmp/non_existent_optchat_path_xyz")
        assert res_404.status == 404

        # 8e. 403 for unauthorized path
        res_403 = await client.get("/api/file?path=/etc/shadow")
        assert res_403.status == 403

        # 9. Test workspace resolution
        assert server.resolve_workspace_dir("all") == server.workspace
        assert server.resolve_workspace_dir("desk") == server.workspace
        assert server.resolve_workspace_dir("opt-chat").name == "opt-chat"
        assert server.resolve_workspace_dir("gemma-4-developer-agent").name == "gemma-4-developer-agent"

        # 10. Test /api/chat with workspace parameters
        res_chat_all = await client.post("/api/chat", json={"message": "Hello desk", "workspace": "all"})
        assert res_chat_all.status == 200
        data_all = await res_chat_all.json()
        assert data_all["status"] == "queued"
        assert data_all["workspace"] == "all"

        res_chat_ws = await client.post("/api/chat", json={"message": "Hello opt-chat", "workspace": "opt-chat"})
        assert res_chat_ws.status == 200
        data_ws = await res_chat_ws.json()
        assert data_ws["status"] == "queued"
        assert data_ws["workspace"] == "opt-chat"

    finally:
        await client.close()
        server.shutdown()


@pytest.mark.asyncio
async def test_web_server_workspace_discovery(tmp_path: Path):
    storage_dir = tmp_path / "chat_discovery"
    storage = Storage(storage_dir)
    storage.open()
    # Add messages with older and newer workspaces beyond 500 threshold
    storage.append_message("user", "Jacquard design", workspace="jacquard")
    for _ in range(505):
        storage.append_message("note", "Intermediate note", workspace="desk")
    storage.append_message("user", "Gsuite tasks", workspace="gsuite-cli")
    storage.append_message("talk", "Gemma runner", workspace="gemma-4-developer-agent")
    storage.append_message("user", "OptChat web", workspace="opt-chat")
    storage.close()

    server = OptChatWebServer(chat_dir=storage_dir, provider_name="mock")
    await server.init_engine()

    client = TestClient(TestServer(server.app))
    await client.start_server()

    try:
        res = await client.get("/api/state")
        assert res.status == 200
        state = await res.json()
        workspaces = state.get("workspaces", [])
        # All workspaces should be discovered even if older than 500 messages
        assert "jacquard" in workspaces
        assert "gsuite-cli" in workspaces
        assert "gemma-4-developer-agent" in workspaces
        assert "opt-chat" in workspaces
        assert "desk" in workspaces

        # Test workspace filtering
        res_ws = await client.get("/api/history?workspace=jacquard")
        assert res_ws.status == 200
        history_ws = await res_ws.json()
        assert len(history_ws) == 1
        assert history_ws[0]["workspace"] == "jacquard"
    finally:
        await client.close()
        server.shutdown()


@pytest.mark.asyncio
async def test_web_server_telemetry_endpoints(tmp_path: Path):
    storage_dir = tmp_path / "chat"
    storage = Storage(storage_dir)
    storage.open()
    storage.append_message("user", "Hello telemetry")
    storage.close()

    server = OptChatWebServer(
        chat_dir=storage_dir,
        provider_name="mock",
    )
    await server.init_engine()

    # Pre-populate telemetry entries
    server.telemetry.record_turn(
        req_id="req_test_1",
        conversation_id="conv_1",
        workspace="jacquard",
        model="gemini-4-argon-medium",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=500,
        duration_ms=1200,
        role="assistant",
        source="turn_agent",
    )
    server.telemetry.record_turn(
        req_id="req_test_2",
        conversation_id="conv_2",
        workspace="opt-chat",
        model="gemini-4-argon-high",
        input_tokens=2000,
        output_tokens=400,
        cache_read_tokens=0,
        duration_ms=2500,
        role="assistant",
        source="turn_agent",
    )

    client = TestClient(TestServer(server.app))
    await client.start_server()

    try:
        # 1. Test GET /api/telemetry default
        res = await client.get("/api/telemetry")
        assert res.status == 200
        data = await res.json()
        assert data["status"] == "ok"
        assert data["summary"]["total_requests"] == 2
        assert data["summary"]["total_input_tokens"] == 3000
        assert data["summary"]["total_output_tokens"] == 600
        assert data["summary"]["total_tokens"] == 3600
        assert data["summary"]["total_cost_usd"] > 0
        assert len(data["by_workspace"]) == 2
        assert len(data["by_model"]) == 2
        assert len(data["recent"]) == 2

        # 2. Test GET /api/telemetry with workspace filter
        res_ws = await client.get("/api/telemetry?workspace=jacquard")
        assert res_ws.status == 200
        data_ws = await res_ws.json()
        assert data_ws["summary"]["total_requests"] == 1
        assert data_ws["summary"]["total_tokens"] == 1200
        assert data_ws["summary"]["total_input_tokens"] == 1000

        # 3. Test GET /api/telemetry with model filter
        res_m = await client.get("/api/telemetry?model=gemini-4-argon-high")
        assert res_m.status == 200
        data_m = await res_m.json()
        assert data_m["summary"]["total_requests"] == 1
        assert data_m["summary"]["total_tokens"] == 2400

        # 4. Test GET /api/telemetry/request/{req_id} found
        res_req = await client.get("/api/telemetry/request/req_test_1")
        assert res_req.status == 200
        data_req = await res_req.json()
        assert data_req["status"] == "ok"
        assert data_req["request"]["req_id"] == "req_test_1"
        assert data_req["request"]["workspace"] == "jacquard"
        assert data_req["request"]["total_tokens"] == 1200

        # 5. Test GET /api/telemetry/request/{req_id} not found
        res_nf = await client.get("/api/telemetry/request/non_existent_req")
        assert res_nf.status == 404
        data_nf = await res_nf.json()
        assert data_nf["status"] == "not_found"
    finally:
        await client.close()
        server.shutdown()


@pytest.mark.asyncio
async def test_all_session_continuation_and_delta_injection(tmp_path: Path):
    import time
    storage_dir = tmp_path / "chat"
    storage = Storage(storage_dir)
    storage.open()
    storage.append_message("user", "Initial turn", workspace="desk")
    storage.close()

    server = OptChatWebServer(
        chat_dir=storage_dir,
        provider_name="mock",
    )
    await server.init_engine()

    try:
        # 1. Initially, no conversation ID -> should_continue_all_session is False
        assert server.should_continue_all_session() is False

        # Set conversation ID
        server.all_conversation_id = "conv-desk-1"
        server.all_session_last_ts = time.time()
        server.all_session_turn_count = 1
        server.all_session_anchor_batch_count = 0
        server.all_session_anchor_msg_count = server.get_latest_msg_id()
        assert server.should_continue_all_session() is True

        # Invalidation trigger: Sawtooth batch coarsening (batch_count increases)
        server.view.batch_count = 1
        assert server.should_continue_all_session() is False
        server.view.batch_count = 0
        assert server.should_continue_all_session() is True

        # Invalidation trigger: Turn ceiling (>= 20)
        server.all_session_turn_count = 20
        assert server.should_continue_all_session() is False
        server.all_session_turn_count = 5
        assert server.should_continue_all_session() is True

        # Invalidation trigger: Inactivity (> 2 hours)
        server.all_session_last_ts = time.time() - 7205
        assert server.should_continue_all_session() is False
        server.all_session_last_ts = time.time()
        assert server.should_continue_all_session() is True

        # 2. Test cross-workspace delta generation
        assert server.get_cross_workspace_delta() is None

        # Add message in 'desk' -> delta still None (same workspace)
        server.storage.append_message("talk", "Internal desk talk", workspace="desk")
        assert server.get_cross_workspace_delta() is None

        # Add messages in external workspaces
        server.storage.append_message("note", "Completed SPY backtest: Sharpe 2.4", workspace="jacquard")
        server.storage.append_message("user", "Submitted Kaggle kernel v54", workspace="gemma-4")

        delta = server.get_cross_workspace_delta()
        assert delta is not None
        assert "<optchat_updates>" in delta
        assert "[jacquard] note: Completed SPY backtest: Sharpe 2.4" in delta
        assert "[gemma-4] user: Submitted Kaggle kernel v54" in delta
        assert "</optchat_updates>" in delta

        # 3. Test session reset via /api/session
        client = TestClient(TestServer(server.app))
        await client.start_server()
        try:
            res_reset = await client.post("/api/session", json={"conversation_id": ""})
            assert res_reset.status == 200
            assert server.all_conversation_id is None
            assert server.should_continue_all_session() is False
        finally:
            await client.close()
    finally:
        server.shutdown()



