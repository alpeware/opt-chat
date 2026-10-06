"""Google Antigravity CLI (agy) Provider for OptChat.

Uses the local `agy` binary to execute model turns and compactor steps without
requiring external API keys.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
from typing import Any, Dict, List, Optional

from optchat.providers.base import BaseLLMProvider, LLMResponse, StreamCallback, ToolCall, ToolDefinition

logger = logging.getLogger("optchat.providers.agy")


class AgyProvider(BaseLLMProvider):
    def __init__(
        self,
        model: str = "gemini-3.8-flash-high",
        agy_path: Optional[str] = None,
        timeout: float = 120.0,
    ):
        self.model = model
        self.agy_path = agy_path or shutil.which("agy") or "/home/simonpure/.local/bin/agy"
        self.timeout = timeout

        if not os.path.isfile(self.agy_path):
            raise FileNotFoundError(f"Antigravity CLI (agy) not found at {self.agy_path}")

    async def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolDefinition]] = None,
        cache_breakpoints: Optional[List[int]] = None,
        stream_callback: Optional[StreamCallback] = None,
    ) -> LLMResponse:
        """Execute chat turn using agy --print."""
        # Compose single prompt with system instructions and conversation
        parts = [f"=== SYSTEM INSTRUCTIONS ===\n{system}\n"]

        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                block_texts = []
                for b in content:
                    if isinstance(b, dict):
                        btype = b.get("type")
                        if btype == "tool_result":
                            block_texts.append(f"[Tool Result]: {b.get('content', '')}")
                        elif btype == "tool_use":
                            block_texts.append(f"[Tool Call]: {b.get('name')}({json.dumps(b.get('input', {}))})")
                        else:
                            block_texts.append(b.get("text", ""))
                    else:
                        block_texts.append(str(b))
                parts.append(f"=== {role.upper()} ===\n" + "\n".join(block_texts))
            else:
                parts.append(f"=== {role.upper()} ===\n{content}")

        full_prompt = "\n\n".join(parts)

        cmd = [
            self.agy_path,
            "--dangerously-skip-permissions",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--print",
            full_prompt,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError(f"agy turn timed out after {self.timeout}s")

        out_text = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"agy process failed (code {proc.returncode}): {err_text}")

        # Parse JSON output from agy
        data = json.loads(out_text)
        response_text = data.get("response", "").strip()
        usage = data.get("usage", {})

        if stream_callback and response_text:
            stream_callback("text", response_text)

        # Inspect if agy output requested a tool call (e.g. zoom(id, n))
        tool_calls: List[ToolCall] = []
        import re

        zoom_match = re.search(r"zoom\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", response_text, re.IGNORECASE)
        if zoom_match:
            tool_calls.append(
                ToolCall(
                    id="agy_zoom_call",
                    name="zoom",
                    arguments={"id": int(zoom_match.group(1)), "n": int(zoom_match.group(2))},
                )
            )

        date_match = re.search(r"date\s*\(\s*(\d+)\s*\)", response_text, re.IGNORECASE)
        if date_match and not zoom_match:
            tool_calls.append(
                ToolCall(
                    id="agy_date_call",
                    name="date",
                    arguments={"id": int(date_match.group(1))},
                )
            )

        return LLMResponse(
            text=response_text,
            reasoning=None,
            tool_calls=tool_calls,
            prompt_tokens=usage.get("input_tokens", 0),
            completion_tokens=usage.get("output_tokens", 0),
            cached_tokens=usage.get("cache_read_tokens", 0),
        )

    async def compact_step(
        self,
        system: str,
        messages: List[Dict[str, Any]],
    ) -> str:
        """Execute compactor step using agy."""
        parts = [f"System:\n{system}\n"]
        for msg in messages:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                block_texts = [b.get("text", "") if isinstance(b, dict) else str(b) for b in content]
                parts.append(f"{role}:\n" + "\n".join(block_texts))
            else:
                parts.append(f"{role}:\n{content}")

        full_prompt = "\n\n".join(parts)

        cmd = [
            self.agy_path,
            "--dangerously-skip-permissions",
            "--model",
            self.model,
            "--output-format",
            "json",
            "--print",
            full_prompt,
        ]

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.timeout)
        except asyncio.TimeoutError:
            proc.kill()
            raise TimeoutError("agy compactor step timed out")

        out_text = stdout.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            err_text = stderr.decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"agy compactor failed (code {proc.returncode}): {err_text}")

        data = json.loads(out_text)
        return data.get("response", "").strip()
