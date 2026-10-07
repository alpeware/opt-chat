"""Providers package for OptChat."""

from typing import Optional

from optchat.providers.agy_provider import AgyProvider
from optchat.providers.anthropic import AnthropicProvider
from optchat.providers.base import BaseLLMProvider, LLMResponse, StreamCallback, ToolCall, ToolDefinition
from optchat.providers.mock import MockLLMProvider
from optchat.providers.openai_provider import OpenAIProvider


def create_provider(
    provider_name: str,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    compact_model: Optional[str] = None,
    timeout: float = 300.0,
) -> BaseLLMProvider:
    p = provider_name.lower().strip()
    if p == "agy":
        return AgyProvider(
            model=model or "gemini-3.8-flash-medium",
            compact_model=compact_model or "gemini-3.8-flash-low",
            timeout=timeout,
        )
    elif p == "anthropic":
        return AnthropicProvider(api_key=api_key, model=model or "claude-3-7-sonnet-latest", timeout=timeout)
    elif p in ("openai", "openrouter"):
        return OpenAIProvider(api_key=api_key, model=model or "gpt-4o", timeout=timeout)
    elif p == "mock":
        return MockLLMProvider()
    else:
        raise ValueError(f"Unknown provider: {provider_name}")


__all__ = [
    "BaseLLMProvider",
    "LLMResponse",
    "StreamCallback",
    "ToolCall",
    "ToolDefinition",
    "AgyProvider",
    "AnthropicProvider",
    "OpenAIProvider",
    "MockLLMProvider",
    "create_provider",
]
