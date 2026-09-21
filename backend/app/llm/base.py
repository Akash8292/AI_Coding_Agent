"""
LLM Provider abstraction — base class and shared types.
All providers implement this interface.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Iterator, Optional


@dataclass
class ModelInfo:
    provider: str
    model: str
    label: str
    context_window: int
    max_output_tokens: int
    is_local: bool = False
    available: bool = True
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "label": self.label,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "is_local": self.is_local,
            "available": self.available,
            "error": self.error,
        }


@dataclass
class GenerationResult:
    content: str
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    provider: str = ""
    finish_reason: str = "stop"


class ProviderNotAvailableError(Exception):
    """Raised when a provider is not configured or unavailable."""
    pass


class LLMProvider(ABC):
    """Base class for all LLM providers."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Short identifier e.g. 'openai', 'anthropic'."""
        ...

    @abstractmethod
    def stream(self, messages: list[dict], system_prompt: str = "", **kwargs) -> Iterator[str]:
        """
        Stream text chunks. Each yielded value is a string fragment.
        messages: list of {role: 'user'|'assistant', content: str}
        system_prompt: system instruction (handled per-provider)
        """
        ...

    @abstractmethod
    def generate(self, messages: list[dict], system_prompt: str = "", **kwargs) -> GenerationResult:
        """Blocking generation — collects stream and returns full result."""
        ...

    @abstractmethod
    def get_model_info(self) -> ModelInfo:
        """Return metadata about this provider/model."""
        ...

    def count_tokens(self, text: str) -> int:
        """Rough token estimate (4 chars ≈ 1 token). Override for accuracy."""
        return max(1, len(text) // 4)

    def generate_blocking(self, messages: list[dict], system_prompt: str = "", **kwargs) -> GenerationResult:
        """Default implementation: collect stream into a single string."""
        chunks = []
        for chunk in self.stream(messages, system_prompt=system_prompt, **kwargs):
            chunks.append(chunk)
        full = "".join(chunks)
        # Rough token counting
        input_text = system_prompt + " ".join(m.get("content", "") for m in messages)
        return GenerationResult(
            content=full,
            input_tokens=self.count_tokens(input_text),
            output_tokens=self.count_tokens(full),
            model=self.get_model_info().model,
            provider=self.provider_name,
        )
