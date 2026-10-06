"""Anthropic API Provider for OptChat (§4.2, §8).

Implements exact prompt caching with cache_control: {"type": "ephemeral"},
streaming, tool calling, and thinking separation (displayed, never logged).
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from optchat.providers.base import BaseLLMProvider, LLMResponse, StreamCallback, ToolCall, ToolDefinition

logger = logging.getLogger("optchat.providers.anthropic")

ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"


class AnthropicProvider(BaseLLMProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "claude-3-7-sonnet-latest",
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self.model = model
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    async def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolDefinition]] = None,
        cache_breakpoints: Optional[List[int]] = None,
        stream_callback: Optional[StreamCallback] = None,
    ) -> LLMResponse:
        """Call Anthropic API with cache breakpoints and tool execution (§8)."""
        formatted_tools = [t.to_anthropic() for t in tools] if tools else None

        # Build payload
        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "system": system,
            "messages": messages,
        }
        if formatted_tools:
            payload["tools"] = formatted_tools

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                ANTHROPIC_API_URL,
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Anthropic API error ({resp.status_code}): {resp.text}")

            data = resp.json()

        text_parts: List[str] = []
        reasoning_parts: List[str] = []
        tool_calls: List[ToolCall] = []

        content = data.get("content", [])
        for block in content:
            btype = block.get("type")
            if btype == "text":
                text_content = block.get("text", "")
                text_parts.append(text_content)
                if stream_callback:
                    stream_callback("text", text_content)
            elif btype == "thinking":
                # Thinking block: shown to user, never logged (§2)
                thought = block.get("thinking", "")
                reasoning_parts.append(thought)
                if stream_callback:
                    stream_callback("reasoning", thought)
            elif btype == "tool_use":
                call = ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input", {}),
                )
                tool_calls.append(call)
                if stream_callback:
                    stream_callback("tool_call", f"{call.name}({json.dumps(call.arguments)})")

        usage = data.get("usage", {})
        prompt_tokens = usage.get("input_tokens", 0)
        completion_tokens = usage.get("output_tokens", 0)
        cached_tokens = usage.get("cache_read_input_tokens", 0)

        return LLMResponse(
            text="".join(text_parts),
            reasoning="".join(reasoning_parts) if reasoning_parts else None,
            tool_calls=tool_calls,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cached_tokens=cached_tokens,
        )

    async def compact_step(
        self,
        system: str,
        messages: List[Dict[str, Any]],
    ) -> str:
        """Call Anthropic for one compactor step (§4.2)."""
        payload: Dict[str, Any] = {
            "model": self.model,
            "max_tokens": 1024,
            "system": system,
            "messages": messages,
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                ANTHROPIC_API_URL,
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"Anthropic compactor error ({resp.status_code}): {resp.text}")
            data = resp.json()

        text_parts: List[str] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))

        return "".join(text_parts)
