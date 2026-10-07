"""Providers package for OptChat (specifically built for Google Antigravity / agy)."""

from typing import Optional

from optchat.providers.agy_provider import AgyProvider
from optchat.providers.base import BaseLLMProvider, LLMResponse, StreamCallback, ToolCall, ToolDefinition
from optchat.providers.mock import MockLLMProvider


def create_provider(
    provider_name: str = "agy",
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    compact_model: Optional[str] = None,
    timeout: float = 300.0,
    workspace: Optional[str] = None,
    conversation_id: Optional[str] = None,
) -> BaseLLMProvider:
    p = (provider_name or "agy").lower().strip()
    if p == "agy":
        return AgyProvider(
            model=model or "gemini-3.8-flash-medium",
            compact_model=compact_model or "gemini-3.8-flash-low",
            timeout=timeout,
            workspace=workspace,
            conversation_id=conversation_id,
        )
    elif p == "mock":
        return MockLLMProvider()
    else:
        raise ValueError(f"OptChat is specifically built for Google Antigravity ('agy'). Unknown provider: {provider_name}")


__all__ = [
    "BaseLLMProvider",
    "LLMResponse",
    "StreamCallback",
    "ToolCall",
    "ToolDefinition",
    "AgyProvider",
    "MockLLMProvider",
    "create_provider",
]
