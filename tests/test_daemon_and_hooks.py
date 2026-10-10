"""Tests for OptChat Daemon, EngineClient, Hooks, and Session Scoping."""

from __future__ import annotations

import asyncio
import io
import json
from pathlib import Path
import sys
from typing import Any
import pytest
from aiohttp.test_utils import TestClient, TestServer

from optchat.daemon import OptChatDaemon
from optchat.engine_client import EngineClient, ProxyCompactor, ProxyStorage, ProxyView
from optchat.hooks import handle_hook_command, install_hooks, uninstall_hooks
from optchat.storage import Storage
from optchat.web_server import OptChatWebServer


@pytest.mark.asyncio
async def test_daemon_and_client_roundtrip(tmp_path: Path):
    chat_dir = tmp_path / "chat"
    sock_path = tmp_path / "engine.sock"

    daemon = OptChatDaemon(
        chat_dir=chat_dir,
        socket_path=sock_path,
        provider_name="mock",
    )
    daemon.start_in_thread()

    client = EngineClient(socket_path=sock_path)
    assert client.is_daemon_alive(timeout=1.0) is True

    try:
        # 1. Ping
        res = client.call("ping")
        assert res.get("status") == "ok"
        assert res.get("service") == "optchat-daemon"

        # 2. Append message
        app_res = client.append_message("user", "Hello daemon from client")
        assert app_res.get("status") == "ok"
        assert app_res["message"]["i"] == 0

        app_res2 = client.append_message("talk", "Hello back from daemon")
        assert app_res2.get("status") == "ok"
        assert app_res2["message"]["i"] == 1

        # 3. Get state
        st = client.get_state()
        assert st["messages_count"] == 2
        assert st["view_budget"] > 0

        # 4. Get view
        view_text = client.get_view()
        assert "<chat>" in view_text
        assert "</chat>" in view_text

        # 5. Zoom & Date
        zoom_res = client.zoom(0, 1)
        assert "Hello daemon from client" in zoom_res

        date_res = client.date(0)
        assert "Message 0:" in date_res

        # 6. ProxyStorage & ProxyView tests
        proxy_storage = ProxyStorage(client, chat_dir)
        proxy_view = ProxyView(client)
        proxy_compactor = ProxyCompactor(client)

        assert len(proxy_storage.messages) == 2
        assert proxy_storage.get_message(0).text == "Hello daemon from client"
        assert "<chat>" in proxy_view.render()
        assert proxy_view.compute_size() > 0

        proxy_compactor.pump()

    finally:
        daemon.stop_in_thread()


@pytest.mark.asyncio
async def test_web_server_with_daemon(tmp_path: Path):
    chat_dir = tmp_path / "chat"
    sock_path = chat_dir / "engine.sock"

    daemon = OptChatDaemon(
        chat_dir=chat_dir,
        socket_path=sock_path,
        provider_name="mock",
    )
    daemon.start_in_thread()

    server = OptChatWebServer(
        chat_dir=chat_dir,
        provider_name="mock",
        workspace=tmp_path,
    )
    await server.init_engine()

    assert server.is_daemon_connected is True
    assert isinstance(server.storage, ProxyStorage)
    assert isinstance(server.view, ProxyView)

    client = TestClient(TestServer(server.app))
    await client.start_server()

    try:
        # Test session GET
        res = await client.get("/api/session")
        assert res.status == 200
        session_data = await res.json()
        assert session_data["is_daemon_connected"] is True
        assert session_data["workspace"] == str(tmp_path)

        # Test session POST (switch workspace)
        new_ws = tmp_path / "sub_ws"
        new_ws.mkdir()
        res = await client.post("/api/session", json={"workspace": str(new_ws), "conversation_id": "test-conv-123"})
        assert res.status == 200
        post_data = await res.json()
        assert post_data["workspace"] == str(new_ws)
        assert post_data["conversation_id"] == "test-conv-123"

        # Test state
        res = await client.get("/api/state")
        assert res.status == 200
        st = await res.json()
        assert st["daemon_connected"] is True
        assert st["workspace"] == str(new_ws)

        # Test /api/chat asynchronous submission
        res = await client.post("/api/chat", json={"message": "Hello from web UI"})
        assert res.status == 200
        chat_res = await res.json()
        assert chat_res["status"] == "queued"

    finally:
        await client.close()
        server.shutdown()
        daemon.stop_in_thread()


def test_hooks_install_and_uninstall(tmp_path: Path):
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()

    # Install hooks in workspace
    hooks_file = install_hooks(workspace_dir=ws_dir, global_config=False)
    assert hooks_file.is_file()

    with open(hooks_file, "r", encoding="utf-8") as f:
        config = json.load(f)

    assert "optchat-memory-bridge" in config
    bridge = config["optchat-memory-bridge"]
    assert bridge["enabled"] is True
    assert "PreInvocation" in bridge
    assert "PostToolUse" in bridge
    assert "PostInvocation" in bridge
    assert "Stop" in bridge

    # Uninstall hooks
    uninstall_hooks(workspace_dir=ws_dir, global_config=False)
    with open(hooks_file, "r", encoding="utf-8") as f:
        config_after = json.load(f)
    assert "optchat-memory-bridge" not in config_after


def test_hook_command_execution(monkeypatch, tmp_path: Path):
    # Test pre-invocation output when daemon is not alive (graceful fallback)
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    stdout_buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout_buf)

    handle_hook_command("pre-invocation")
    out = json.loads(stdout_buf.getvalue())
    assert "injectSteps" in out

    # Test stop hook output
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    stdout_buf_stop = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout_buf_stop)

    handle_hook_command("stop")
    out_stop = json.loads(stdout_buf_stop.getvalue())
    assert out_stop.get("decision") == "allow"


def test_hook_ignores_compactor_via_env(monkeypatch):
    monkeypatch.setenv("OPTCHAT_IS_COMPACTOR", "1")
    monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))
    stdout_buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout_buf)

    handle_hook_command("pre-invocation")
    out = json.loads(stdout_buf.getvalue())
    assert out == {"injectSteps": []}

    # Test stop hook with compactor env
    stdout_buf_stop = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout_buf_stop)
    handle_hook_command("stop")
    out_stop = json.loads(stdout_buf_stop.getvalue())
    assert out_stop == {"decision": "allow"}


def test_hook_ignores_compactor_via_payload(monkeypatch):
    monkeypatch.delenv("OPTCHAT_IS_COMPACTOR", raising=False)
    payload = {
        "lastUserInput": "CRITICAL REQUIREMENT: Output ONLY the single summary line directly."
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    stdout_buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout_buf)

    handle_hook_command("pre-invocation")
    out = json.loads(stdout_buf.getvalue())
    assert out == {"injectSteps": []}


def test_hook_ignores_compactor_via_transcript(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("OPTCHAT_IS_COMPACTOR", raising=False)
    tpath = tmp_path / "transcript.jsonl"
    tpath.write_text(json.dumps({
        "step_index": 0,
        "type": "USER_INPUT",
        "content": "CRITICAL REQUIREMENT: Output ONLY the single summary line directly."
    }))

    payload = {"transcriptPath": str(tpath)}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    stdout_buf = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout_buf)

    handle_hook_command("pre-invocation")
    out = json.loads(stdout_buf.getvalue())
    assert out == {"injectSteps": []}


@pytest.mark.asyncio
async def test_daemon_subagent_events_and_report_recording(tmp_path: Path):
    sock_path = tmp_path / "engine.sock"
    daemon = OptChatDaemon(
        chat_dir=tmp_path,
        socket_path=sock_path,
        provider_name="mock",
    )
    daemon.start_in_thread()
    try:
        client = EngineClient(socket_path=sock_path)
        assert client.is_daemon_alive(timeout=2.0)

        received_events = []
        stop_sub = asyncio.Event()

        async def sub_worker():
            async for evt in client.subscribe_events():
                received_events.append(evt)
                if stop_sub.is_set():
                    break

        sub_task = asyncio.create_task(sub_worker())
        await asyncio.sleep(0.1)

        # 1. Test subagent spawn event via pre_tool
        spawn_payload = {
            "conversationId": "parent-123",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "Subagents": [
                        {"Role": "Research Specialist", "TypeName": "research", "Prompt": "Search for latest news"}
                    ]
                }
            }
        }
        res1 = await client.call_async("hook_event", event="pre_tool", payload=spawn_payload)
        assert res1.get("status") == "ok"
        await asyncio.sleep(0.1)
        assert any(e.get("type") == "subagent_spawn" and "Research Specialist" in e.get("content", "") for e in received_events)

        # 2. Test subagent tool execution via post_tool
        tool_payload = {
            "conversationId": "subagent-456",
            "parentConversationId": "parent-123",
            "agentName": "Research Specialist",
            "toolCall": {
                "name": "run_command",
                "args": {"CommandLine": "curl https://example.com"}
            }
        }
        res2 = await client.call_async("hook_event", event="post_tool", payload=tool_payload)
        assert res2.get("status") == "ok"
        await asyncio.sleep(0.1)
        assert any(e.get("type") == "subagent_tool" and "curl https://example.com" in e.get("content", "") for e in received_events)

        # 3. Test subagent report delivery via send_message
        report_payload = {
            "conversationId": "subagent-456",
            "parentConversationId": "parent-123",
            "agentName": "Research Specialist",
            "toolCall": {
                "name": "send_message",
                "args": {"Recipient": "parent-123", "Message": "Analysis complete: all systems nominal."}
            }
        }
        res3 = await client.call_async("hook_event", event="post_tool", payload=report_payload)
        assert res3.get("status") == "ok"
        await asyncio.sleep(0.1)
        assert any(e.get("type") == "subagent_report" and "all systems nominal" in e.get("content", "") for e in received_events)

        # Verify recorded into memory storage as kind="work"
        hist = await client.call_async("get_history", limit=10)
        msgs = hist.get("messages", [])
        work_msgs = [m for m in msgs if m.get("kind") == "work"]
        assert len(work_msgs) == 1
        assert "Analysis complete: all systems nominal." in work_msgs[0].get("text")

        # 4. Test subagent completion via stop hook
        stop_payload = {
            "conversationId": "subagent-456",
            "parentConversationId": "parent-123",
            "agentName": "Research Specialist"
        }
        res4 = await client.call_async("hook_event", event="stop", payload=stop_payload)
        assert res4.get("status") == "ok"
        await asyncio.sleep(0.1)
        assert any(e.get("type") == "subagent_complete" and "Research Specialist" in e.get("content", "") for e in received_events)

        stop_sub.set()
        sub_task.cancel()
        try:
            await sub_task
        except asyncio.CancelledError:
            pass

    finally:
        daemon.stop_in_thread()


@pytest.mark.asyncio
async def test_subagent_lifecycle_without_parent_conversation_id(tmp_path: Path):
    """Verify subagent tracking works when agy omits parentConversationId."""
    chat_dir = tmp_path / "chat"
    sock_path = tmp_path / "engine.sock"

    daemon = OptChatDaemon(
        chat_dir=chat_dir,
        socket_path=sock_path,
        provider_name="mock",
    )
    daemon.start_in_thread()

    client = EngineClient(socket_path=sock_path)
    assert client.is_daemon_alive(timeout=1.0) is True

    try:
        received_events = []
        stop_sub = asyncio.Event()

        async def sub_worker():
            async for evt in client.subscribe_events():
                received_events.append(evt)
                if stop_sub.is_set():
                    break

        sub_task = asyncio.create_task(sub_worker())
        await asyncio.sleep(0.1)

        # 1. Parent executes invoke_subagent and returns result with conversationId
        subagent_cid = "sub-no-parent-999"
        invoke_result_payload = {
            "conversationId": "parent-root-111",
            "toolCall": {
                "name": "invoke_subagent",
                "args": {
                    "Subagents": [
                        {"Role": "Autonomous Coder", "TypeName": "code-writer", "Prompt": "Write unit tests"}
                    ]
                }
            },
            "result": f'Created subagent:\n{{\n  "conversationId": "{subagent_cid}"\n}}'
        }
        res1 = await client.call_async("hook_event", event="post_tool", payload=invoke_result_payload)
        assert res1.get("status") == "ok"
        await asyncio.sleep(0.1)
        assert any(e.get("type") == "subagent_spawn" and "Autonomous Coder" in e.get("content", "") for e in received_events)

        # 2. Subagent executes intermediate tool WITHOUT parentConversationId
        tool_payload = {
            "conversationId": subagent_cid,
            "toolCall": {
                "name": "view_file",
                "args": {"AbsolutePath": "/tmp/test.py"}
            }
        }
        res2 = await client.call_async("hook_event", event="post_tool", payload=tool_payload)
        assert res2.get("status") == "ok"
        await asyncio.sleep(0.1)
        assert any(e.get("type") == "subagent_tool" and "view_file" in e.get("content", "") for e in received_events)

        # 3. Subagent sends report back via send_message WITHOUT parentConversationId
        msg_payload = {
            "conversationId": subagent_cid,
            "toolCall": {
                "name": "send_message",
                "args": {"Recipient": "parent-root-111", "Message": "Completed writing tests with 100% pass."}
            }
        }
        res3 = await client.call_async("hook_event", event="post_tool", payload=msg_payload)
        assert res3.get("status") == "ok"
        await asyncio.sleep(0.1)
        assert any(e.get("type") == "subagent_report" and "100% pass" in e.get("content", "") for e in received_events)

        # Verify persisted as work
        hist = await client.call_async("get_history", limit=5)
        work_msgs = [m for m in hist.get("messages", []) if m.get("kind") == "work"]
        assert len(work_msgs) == 1
        assert "[Autonomous Coder]" in work_msgs[0].get("text")
        assert "Completed writing tests with 100% pass." in work_msgs[0].get("text")

        # 4. Subagent stops WITHOUT parentConversationId
        stop_payload = {
            "conversationId": subagent_cid,
        }
        res4 = await client.call_async("hook_event", event="stop", payload=stop_payload)
        assert res4.get("status") == "ok"
        await asyncio.sleep(0.1)
        assert any(e.get("type") == "subagent_complete" and "Autonomous Coder" in e.get("content", "") for e in received_events)

        stop_sub.set()
        sub_task.cancel()
        try:
            await sub_task
        except asyncio.CancelledError:
            pass

    finally:
        daemon.stop_in_thread()


@pytest.mark.asyncio
async def test_main_turn_auto_capture_on_stop(tmp_path: Path):
    """Verify that when a main agy turn completes, the Stop hook captures user prompt and reply with workspace tag and deduplicates."""
    chat_dir = tmp_path / "chat"
    sock_path = tmp_path / "engine.sock"
    ws_dir = tmp_path / "super-app"
    ws_dir.mkdir()

    tpath = tmp_path / "transcript.jsonl"
    with open(tpath, "w", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 1, "type": "USER_INPUT", "content": "<USER_REQUEST>Implement authentication flow</USER_REQUEST>"}) + "\n")
        f.write(json.dumps({"step_index": 2, "type": "PLANNER_RESPONSE", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "cargo build"}}]}) + "\n")
        f.write(json.dumps({"step_index": 3, "type": "GENERIC", "content": "Compiling auth v0.1.0..."}) + "\n")
        f.write(json.dumps({"step_index": 4, "type": "PLANNER_RESPONSE", "content": "I have completed implementing the auth flow with JWT tokens."}) + "\n")

    daemon = OptChatDaemon(
        chat_dir=chat_dir,
        socket_path=sock_path,
        provider_name="mock",
    )
    daemon.start_in_thread()

    try:
        client = EngineClient(socket_path=sock_path)
        assert client.is_daemon_alive(timeout=2.0)

        # 1. Fire Stop hook for main conversation
        stop_payload = {
            "conversationId": "main-conv-001",
            "transcriptPath": str(tpath),
            "workspacePaths": [str(ws_dir)],
        }
        res = await client.call_async("hook_event", event="stop", payload=stop_payload)
        assert res.get("status") == "ok"

        # 2. Check history in memory (pure text + metadata)
        msgs = client.get_history(limit=10)
        assert len(msgs) == 2
        assert msgs[0]["kind"] == "user"
        assert msgs[0]["text"] == "Implement authentication flow"
        assert msgs[0]["workspace"] == "super-app"
        assert msgs[1]["kind"] == "talk"
        assert msgs[1]["text"] == "I have completed implementing the auth flow with JWT tokens."
        assert msgs[1]["workspace"] == "super-app"

        # 3. Test workspace and category filtering in get_history and get_view
        filtered_msgs = client.get_history(limit=10, workspace="super-app")
        assert len(filtered_msgs) == 2

        other_msgs = client.get_history(limit=10, workspace="other-repo")
        assert len(other_msgs) == 0

        main_msgs = client.get_history(limit=10, category="main")
        assert len(main_msgs) == 2

        subagent_msgs = client.get_history(limit=10, category="subagent")
        assert len(subagent_msgs) == 0

        ws_view = client.get_view(workspace="super-app")
        assert "<chat>" in ws_view
        assert "[super-app]" in ws_view

        other_view = client.get_view(workspace="other-repo")
        assert "no entries for workspace [other-repo]" in other_view

        # 4. Check get_state workspaces list
        st = client.get_state()
        assert "super-app" in st.get("workspaces", [])

        # 5. Idempotence test: Fire Stop hook again with identical step_index
        res2 = await client.call_async("hook_event", event="stop", payload=stop_payload)
        assert res2.get("status") == "ok"
        msgs_after = client.get_history(limit=10)
        assert len(msgs_after) == 2  # NOT duplicated!

    finally:
        daemon.stop_in_thread()


@pytest.mark.asyncio
async def test_daemon_captures_timer_progress_and_deduplicates(tmp_path: Path):
    from optchat.daemon import parse_system_message_prompt, extract_latest_turn_from_transcript

    # 1. Test parse_system_message_prompt
    raw_timer = (
        "<SYSTEM_MESSAGE>\n"
        "[Message] timestamp=2026-10-10T14:48:47Z sender=.../task-123 priority=MESSAGE_PRIORITY_HIGH "
        "content=Check progress on Kaggle GPU kernel runner v38 and local frontier traces v2\n"
        "</SYSTEM_MESSAGE>"
    )
    prompt = parse_system_message_prompt(raw_timer)
    assert prompt == "Check progress on Kaggle GPU kernel runner v38 and local frontier traces v2"

    raw_bg_task = (
        "<SYSTEM_MESSAGE>\n"
        "Task id \"conv/task-3690\" finished with result:\nThe command exited with code 0.\n"
        "</SYSTEM_MESSAGE>"
    )
    task_prompt = parse_system_message_prompt(raw_bg_task)
    assert task_prompt == "Background task task-3690 finished"

    # 2. Test transcript extraction with SYSTEM_MESSAGE
    tpath = tmp_path / "transcript.jsonl"
    with open(tpath, "w", encoding="utf-8") as f:
        f.write(json.dumps({"step_index": 10, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "content": raw_timer}) + "\n")
        f.write(json.dumps({"step_index": 11, "source": "MODEL", "type": "PLANNER_RESPONSE", "content": "### Progress Check: Kaggle GPU Runner v38\nfastapi_14978 (Task 4): Executing"}) + "\n")

    turn = extract_latest_turn_from_transcript(str(tpath))
    assert turn is not None
    assert turn["step_index"] == 11
    assert "⏱️ Check progress on Kaggle GPU kernel runner" in turn["user"]
    assert "Task 4" in turn["reply"]

    # 3. Test daemon capturing successive progress updates with identical header prefixes
    chat_dir = tmp_path / "chat"
    sock_path = chat_dir / "engine.sock"
    ws_dir = tmp_path / "gemma-4-developer-agent"
    ws_dir.mkdir()

    daemon = OptChatDaemon(chat_dir=chat_dir, socket_path=sock_path, provider_name="mock")
    daemon.start_in_thread()

    try:
        client = EngineClient(socket_path=sock_path)
        assert client.is_daemon_alive(timeout=2.0)

        # First progress check
        stop_payload = {
            "conversationId": "gemma-conv",
            "transcriptPath": str(tpath),
            "workspacePaths": [str(ws_dir)],
        }
        res1 = await client.call_async("hook_event", event="stop", payload=stop_payload)
        assert res1.get("status") == "ok"

        # Append second progress check (steps 12 and 13) sharing same header prefix
        with open(tpath, "a", encoding="utf-8") as f:
            f.write(json.dumps({"step_index": 12, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "content": raw_timer}) + "\n")
            f.write(json.dumps({
                "step_index": 13,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "content": "### Progress Check: Kaggle GPU Runner v38\nfastapi_14482 (Task 5): Currently executing (Pydantic v2 TypeAdapter arbitrary types)",
            }) + "\n")

        res2 = await client.call_async("hook_event", event="stop", payload=stop_payload)
        assert res2.get("status") == "ok"

        # Both updates should be recorded (2 user + 2 talk = 4 messages)
        msgs = client.get_history(limit=10)
        assert len(msgs) == 4
        assert "Task 4" in msgs[1]["text"]
        assert "Task 5" in msgs[3]["text"]
        assert msgs[3]["workspace"] == "gemma-4-developer-agent"

        # Exact duplicate check: firing hook again with no new steps should NOT add messages
        res3 = await client.call_async("hook_event", event="stop", payload=stop_payload)
        assert res3.get("status") == "ok"
        msgs_after = client.get_history(limit=10)
        assert len(msgs_after) == 4

    finally:
        daemon.stop_in_thread()



