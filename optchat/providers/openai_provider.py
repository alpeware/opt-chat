"""OpenAI / OpenAI-compatible API Provider for OptChat."""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

from optchat.providers.base import BaseLLMProvider, LLMResponse, StreamCallback, ToolCall, ToolDefinition

logger = logging.getLogger("optchat.providers.openai")


class OpenAIProvider(BaseLLMProvider):
    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o",
        base_url: str = "https://api.openai.com/v1",
        timeout: float = 60.0,
    ):
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    async def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolDefinition]] = None,
        cache_breakpoints: Optional[List[int]] = None,
        stream_callback: Optional[StreamCallback] = None,
    ) -> LLMResponse:
        formatted_messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            content = msg.get("content")
            # Flatten list of blocks if passed as Anthropic-style blocks
            if isinstance(content, list):
                combined = ""
                for b in content:
                    if isinstance(b, dict):
                        combined += b.get("text", "")
                    else:
                        combined += str(b)
                formatted_messages.append({"role": msg.get("role", "user"), "content": combined})
            else:
                formatted_messages.append(msg)

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": formatted_messages,
        }
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"OpenAI API error ({resp.status_code}): {resp.text}")

            data = resp.json()

        choice = data["choices"][0]["message"]
        text = choice.get("content") or ""
        reasoning = choice.get("reasoning_content")

        if stream_callback:
            if reasoning:
                stream_callback("reasoning", reasoning)
            if text:
                stream_callback("text", text)

        tool_calls: List[ToolCall] = []
        raw_tools = choice.get("tool_calls") or []
        for raw in raw_tools:
            fn = raw.get("function", {})
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except Exception:
                args = {}
            tool_calls.append(ToolCall(id=raw.get("id", ""), name=fn.get("name", ""), arguments=args))
            if stream_callback:
                stream_callback("tool_call", f"{fn.get('name')}({json.dumps(args)})")

        usage = data.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        cached_tokens = (
            usage.get("prompt_tokens_details", {}).get("cached_tokens", 0)
            if "prompt_tokens_details" in usage
            else 0
        )

        return LLMResponse(
            text=text,
            reasoning=reasoning,
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
        formatted_messages: List[Dict[str, Any]] = [{"role": "system", "content": system}]
        for msg in messages:
            content = msg.get("content")
            if isinstance(content, list):
                combined = ""
                for b in content:
                    if isinstance(b, dict):
                        combined += b.get("text", "")
                    else:
                        combined += str(b)
                formatted_messages.append({"role": msg.get("role", "user"), "content": combined})
            else:
                formatted_messages.append(msg)

        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": formatted_messages,
        }

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=payload,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"OpenAI compactor error ({resp.status_code}): {resp.text}")

            data = resp.json()

        choice = data["choices"][0]["message"]
        return choice.get("content") or ""
