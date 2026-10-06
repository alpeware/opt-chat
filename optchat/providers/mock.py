"""Mock LLM Provider for testing and offline development (§4, §7)."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from optchat.constants import NODE
from optchat.providers.base import BaseLLMProvider, LLMResponse, StreamCallback, ToolCall, ToolDefinition
from optchat.tree import flatten_newlines


class MockLLMProvider(BaseLLMProvider):
    """Deterministic mock provider that adheres strictly to OptChat's rules."""

    def __init__(self, force_overshoot_first_try: bool = False):
        self.force_overshoot_first_try = force_overshoot_first_try
        self._overshoot_tracker: Dict[str, int] = {}

    async def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolDefinition]] = None,
        cache_breakpoints: Optional[List[int]] = None,
        stream_callback: Optional[StreamCallback] = None,
    ) -> LLMResponse:
        # Get the latest user message
        last_msg = messages[-1] if messages else {"content": ""}
        content = last_msg.get("content", "")

        # Optional simulated reasoning
        reasoning = "Analyzing conversation view and determining appropriate response."
        if stream_callback:
            stream_callback("reasoning", reasoning)

        # Check for tool invocations or regular answers
        user_text = ""
        if isinstance(content, list):
            # Two blocks: [view, new_message]
            if len(content) > 1 and isinstance(content[1], dict):
                user_text = content[1].get("text", "")
            elif len(content) > 1 and isinstance(content[1], str):
                user_text = content[1]
            elif len(content) == 1 and isinstance(content[0], dict):
                user_text = content[0].get("text", "")
        elif isinstance(content, str):
            user_text = content

        # Check if user asked to zoom or date or run bash
        tool_calls: List[ToolCall] = []
        if "zoom" in user_text.lower():
            match = re.search(r"zoom\s*\(?\s*(\d+)\s*,\s*(\d+)\s*\)?", user_text, re.IGNORECASE)
            if match:
                tool_calls.append(
                    ToolCall(
                        id="call_zoom_1",
                        name="zoom",
                        arguments={"id": int(match.group(1)), "n": int(match.group(2))},
                    )
                )
        elif "date" in user_text.lower():
            match = re.search(r"date\s*\(?\s*(\d+)\s*\)?", user_text, re.IGNORECASE)
            if match:
                tool_calls.append(
                    ToolCall(
                        id="call_date_1",
                        name="date",
                        arguments={"id": int(match.group(1))},
                    )
                )

        if tool_calls:
            text = "I will check the chat memory using the appropriate tool."
        else:
            text = f"I am OptChat. I processed your request: '{user_text.strip()}'."

        if stream_callback:
            stream_callback("text", text)

        return LLMResponse(
            text=text,
            reasoning=reasoning,
            tool_calls=tool_calls,
            prompt_tokens=100,
            completion_tokens=25,
            cached_tokens=80,
        )

    async def compact_step(
        self,
        system: str,
        messages: List[Dict[str, Any]],
    ) -> str:
        last_msg = messages[-1]
        content = last_msg.get("content", "")
        if isinstance(content, list):
            # Find the step block (second block)
            text_blocks = [b.get("text", "") if isinstance(b, dict) else str(b) for b in content]
            step_text = text_blocks[-1] if text_blocks else ""
        else:
            step_text = str(content)

        # Check if this is a retry cut prompt
        if "That line is" in step_text and "| ← LIMIT" in step_text:
            # Extract the cut portion before the limit marker
            m = re.search(r"where it is cut here:\n(.*?)\|\s*←\s*LIMIT", step_text, re.DOTALL)
            if m:
                cut_part = m.group(1).strip()
                # Return shortened line within NODE bytes
                return cut_part[: NODE - 20]
            return "user: retry action; echo: summarized within limit."

        # Check if we should simulate an initial overshoot to test retry logic
        key = step_text[:50]
        tries = self._overshoot_tracker.get(key, 0)
        self._overshoot_tracker[key] = tries + 1

        if self.force_overshoot_first_try and tries == 0:
            # Overshoot 512 bytes on first try
            return (
                "user: this is an intentionally overly verbose summary that exceeds the five hundred twelve byte limit "
                "by repeating details about the task, system settings, configuration values, tool executions, and past conversations "
                "to trigger the conversational retry mechanism described in section 4.3 of the OptChat specification. "
                "Extra padding text: 1234567890 1234567890 1234567890 1234567890 1234567890 1234567890 1234567890 "
                "1234567890 1234567890 1234567890 1234567890 1234567890 1234567890."
            )

        # Normal compaction:
        if "Merge these two lines into one" in step_text:
            lines = [l.strip() for l in step_text.splitlines() if l.strip() and not l.startswith("For scale") and not l.startswith("Merge")]
            combined = "; ".join(lines[-2:]) if len(lines) >= 2 else "merged: summary of previous lines."
            res = f"merge: {flatten_newlines(combined)}"
        else:
            # Single message compression
            m = re.search(r"Compress this message into one line, in at most 512 bytes:\n(.*?)$", step_text, re.DOTALL)
            if m:
                raw_msg = m.group(1).strip()
                res = f"summary: {flatten_newlines(raw_msg)}"
            else:
                res = "user: completed task; echo: operation successful."

        encoded = res.encode("utf-8")
        if len(encoded) > NODE:
            res = encoded[:NODE].decode("utf-8", errors="ignore")
        return res
