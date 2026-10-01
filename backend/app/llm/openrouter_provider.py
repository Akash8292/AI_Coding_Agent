"""
OpenRouter provider — OpenAI-compatible API that proxies many models.
"""
from typing import Optional

from app.llm.openai_provider import OpenAIProvider
from app.llm.base import ModelInfo, Timeouts

OPENROUTER_MODELS = {
    "google/gemini-3.6-flash": {"context": 1_000_000, "max_out": 8_192},
    "anthropic/claude-sonnet-5": {"context": 200_000, "max_out": 16_384},
    "openai/gpt-5.6-terra": {"context": 256_000, "max_out": 32_768},
    "anthropic/claude-3.5-sonnet": {"context": 200_000, "max_out": 8_192},
    "anthropic/claude-3-haiku": {"context": 200_000, "max_out": 4_096},
    "openai/gpt-4o": {"context": 128_000, "max_out": 16_384},
    "openai/gpt-4o-mini": {"context": 128_000, "max_out": 16_384},
    "google/gemini-flash-1.5": {"context": 1_000_000, "max_out": 8_192},
    "meta-llama/llama-3.1-70b-instruct": {"context": 131_072, "max_out": 4_096},
    "mistralai/mistral-7b-instruct": {"context": 32_768, "max_out": 4_096},
}


class OpenRouterProvider(OpenAIProvider):
    """OpenRouter — proxies many models via OpenAI-compatible API."""
    max_tokens_param = "max_tokens"

    def __init__(self, api_key: str, model: str = "google/gemini-3.6-flash",
                 base_url: str = "https://openrouter.ai/api/v1",
                 timeouts: Optional[Timeouts] = None):
        super().__init__(api_key=api_key, model=model, base_url=base_url,
                         provider_name_override="openrouter", label_override="OpenRouter",
                         timeouts=timeouts)

    def _build_headers(self) -> dict:
        headers = super()._build_headers()
        headers["HTTP-Referer"] = "https://codesage.dev"
        headers["X-Title"] = "CodeSage"
        return headers

    def get_model_info(self) -> ModelInfo:
        info = OPENROUTER_MODELS.get(self._model, {"context": 128_000, "max_out": 4_096})
        return ModelInfo(
            provider="openrouter",
            model=self._model,
            label=f"OpenRouter / {self._model}",
            context_window=info["context"],
            max_output_tokens=info["max_out"],
        )
