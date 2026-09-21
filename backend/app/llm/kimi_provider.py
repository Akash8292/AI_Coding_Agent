"""
Kimi (Moonshot) provider — OpenAI-compatible API.
"""
from app.llm.openai_provider import OpenAIProvider
from app.llm.base import ModelInfo, ProviderNotAvailableError

KIMI_MODELS = {
    "kimi-k2.7-code": {"context": 128_000, "max_out": 16_384},
    "kimi-k2.6": {"context": 128_000, "max_out": 16_384},
    "kimi-k3": {"context": 256_000, "max_out": 32_768},
    "moonshot-v1-8k": {"context": 8_192, "max_out": 4_096},
    "moonshot-v1-32k": {"context": 32_768, "max_out": 4_096},
    "moonshot-v1-128k": {"context": 131_072, "max_out": 4_096},
}


class KimiProvider(OpenAIProvider):
    """Kimi (Moonshot AI) — uses OpenAI-compatible Chat Completions API."""

    def __init__(self, api_key: str, model: str = "kimi-k2.7-code",
                 base_url: str = "https://api.moonshot.ai/v1"):
        super().__init__(api_key=api_key, model=model, base_url=base_url,
                         provider_name_override="kimi", label_override="Kimi (Moonshot)")

    def get_model_info(self) -> ModelInfo:
        info = KIMI_MODELS.get(self._model, {"context": 8_192, "max_out": 4_096})
        return ModelInfo(
            provider="kimi",
            model=self._model,
            label=f"Kimi / {self._model}",
            context_window=info["context"],
            max_output_tokens=info["max_out"],
        )
