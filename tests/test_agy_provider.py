"""Tests for AgyProvider stdin-based prompt communication."""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from optchat.providers.agy_provider import AgyProvider


@pytest.mark.asyncio
async def test_compact_step_uses_stdin_not_print_argument():
    provider = AgyProvider(agy_path="/bin/echo")

    huge_text = "x" * 150_000
    captured_stdin = bytearray()
    captured_cmd = []

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    async def fake_communicate(input=None):
        if input:
            captured_stdin.extend(input)
        return json.dumps({"response": "compacted result"}).encode("utf-8"), b""

    mock_proc.communicate = AsyncMock(side_effect=fake_communicate)

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured_cmd.extend(cmd)
        return mock_proc

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
        res = await provider.compact_step(
            system="System prompt",
            messages=[{"role": "user", "content": huge_text}],
        )

    # 1. Assert --print is NOT in CLI args
    assert "--print" not in captured_cmd
    # 2. Assert huge text is NOT in any CLI arg
    for arg in captured_cmd:
        assert len(arg) < 1000

    # 3. Assert huge text WAS piped through stdin
    assert len(captured_stdin) >= 150_000
    assert res == "compacted result"


@pytest.mark.asyncio
async def test_chat_uses_stdin_streaming():
    provider = AgyProvider(agy_path="/bin/echo")

    huge_text = "y" * 140_000
    captured_stdin = bytearray()
    captured_cmd = []

    mock_proc = MagicMock()
    mock_proc.returncode = 0

    mock_stdin = MagicMock()
    mock_stdin.write = MagicMock(side_effect=lambda b: captured_stdin.extend(b))
    mock_stdin.drain = AsyncMock()
    mock_stdin.close = MagicMock()
    mock_stdin.wait_closed = AsyncMock()
    mock_proc.stdin = mock_stdin

    # Mock stdout returning NDJSON events
    lines = [
        json.dumps({"event": "step_update", "step_update": {"text_delta": "Hello "}}).encode("utf-8") + b"\n",
        json.dumps({"event": "step_update", "step_update": {"text_delta": "world!"}}).encode("utf-8") + b"\n",
        json.dumps({"event": "result", "result": {"response": "Hello world!"}}).encode("utf-8") + b"\n",
        b"",
    ]
    mock_stdout = MagicMock()
    mock_stdout.readline = AsyncMock(side_effect=lines)
    mock_proc.stdout = mock_stdout

    mock_stderr = MagicMock()
    mock_stderr.readline = AsyncMock(return_value=b"")
    mock_proc.stderr = mock_stderr

    mock_proc.wait = AsyncMock(return_value=0)

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        captured_cmd.extend(cmd)
        return mock_proc

    deltas = []
    def on_stream(evt, delta):
        deltas.append(delta)

    with patch("asyncio.create_subprocess_exec", side_effect=fake_create_subprocess_exec):
        resp = await provider.chat(
            system="System instructions",
            messages=[{"role": "user", "content": huge_text}],
            stream_callback=on_stream,
        )

    # 1. Assert --print is NOT in CLI args
    assert "--print" not in captured_cmd
    for arg in captured_cmd:
        assert len(arg) < 1000

    # 2. Assert huge text WAS piped through stdin
    assert len(captured_stdin) >= 140_000
    assert resp.text == "Hello world!"
    assert "".join(deltas) == "Hello world!"
