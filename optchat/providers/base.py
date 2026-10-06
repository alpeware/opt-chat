"""Base interfaces for LLM providers in OptChat."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
import json
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ToolDefinition:
    name: str
    description: str
    parameters: Dict[str, Any]

    def to_anthropic(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.parameters,
        }

    def to_openai(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class LLMResponse:
    text: str
    reasoning: Optional[str] = None  # Model thoughts (shown to user, never logged!)
    tool_calls: List[ToolCall] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached_tokens: int = 0


# Stream callback type: (event_type: str, content: str) -> None
# event_type can be "text", "reasoning", "tool_call"
StreamCallback = Callable[[str, str], None]


class BaseLLMProvider(ABC):
    """Abstract base provider for master agent and compactor."""

    @abstractmethod
    async def chat(
        self,
        system: str,
        messages: List[Dict[str, Any]],
        tools: Optional[List[ToolDefinition]] = None,
        cache_breakpoints: Optional[List[int]] = None,
        stream_callback: Optional[StreamCallback] = None,
    ) -> LLMResponse:
        """Execute a chat completion step."""
        pass

    @abstractmethod
    async def compact_step(
        self,
        system: str,
        messages: List[Dict[str, Any]],
    ) -> str:
        """Execute one step of compaction conversation."""
        pass
