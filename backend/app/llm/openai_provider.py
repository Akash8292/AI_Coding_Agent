"""
OpenAI provider — Chat Completions API with SSE streaming.
Also the base for any OpenAI-compatible endpoint (Kimi, OpenRouter).
"""
import json
from typing import Iterator, Optional

from app.llm.base import (LLMProvider, ModelInfo, ProviderError, ProviderNotAvailableError,
                          Timeouts, Usage)

OPENAI_MODELS = {
    "gpt-5.6-terra": {"context": 256_000, "max_out": 32_768},
    "gpt-5.6-sol": {"context": 256_000, "max_out": 32_768},
    "gpt-5.6-luna": {"context": 256_000, "max_out": 32_768},
    "gpt-4o": {"context": 128_000, "max_out": 16_384},
    "gpt-4o-mini": {"context": 128_000, "max_out": 16_384},
    "gpt-4-turbo": {"context": 128_000, "max_out": 4_096},
    "gpt-3.5-turbo": {"context": 16_385, "max_out": 4_096},
}


class OpenAIProvider(LLMProvider):
    # OpenAI-compatible endpoints differ in a few optional features
    supports_stream_usage = True
    supports_json_mode = True
    max_tokens_param = "max_completion_tokens"

    def __init__(self, api_key: str, model: str = "gpt-5.6-terra",
                 base_url: str = "https://api.openai.com/v1",
                 provider_name_override: str = "openai",
                 label_override: str = "OpenAI",
                 timeouts: Optional[Timeouts] = None):
        super().__init__(timeouts)
        if not api_key:
            raise ProviderNotAvailableError(f"{label_override} API key is not configured")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._provider_name = provider_name_override
        self.label = label_override

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def get_model_info(self) -> ModelInfo:
        info = OPENAI_MODELS.get(self._model, {"context": 128_000, "max_out": 4_096})
        return ModelInfo(
            provider=self._provider_name,
            model=self._model,
            label=f"{self.label} / {self._model}",
            context_window=info["context"],
            max_output_tokens=info["max_out"],
        )

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def build_payload(self, messages: list[dict], system_prompt: str,
                      json_mode: bool, max_tokens: Optional[int], **kwargs) -> dict:
        all_messages = []
        if system_prompt:
            all_messages.append({"role": "system", "content": system_prompt})
        all_messages.extend({"role": m["role"], "content": m["content"]} for m in messages)
        payload = {"model": self._model, "messages": all_messages, "stream": True}
        if self.supports_stream_usage:
            payload["stream_options"] = {"include_usage": True}
        if json_mode and self.supports_json_mode:
            payload["response_format"] = {"type": "json_object"}
        if max_tokens:
            payload[self.max_tokens_param] = max_tokens
        if "temperature" in kwargs:
            payload["temperature"] = kwargs["temperature"]
        return payload

    def stream(self, messages: list[dict], system_prompt: str = "",
               cancel_event=None, json_mode: bool = False,
               max_tokens: Optional[int] = None, **kwargs) -> Iterator[str]:
        payload = self.build_payload(messages, system_prompt, json_mode, max_tokens, **kwargs)
        usage = {"in": None, "out": None}
        output: list[str] = []

        def parse_line(data: str) -> Iterator[str]:
            data = data.strip()
            if not data or data == "[DONE]":
                return
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                return
            if isinstance(chunk.get("error"), dict):  # mid-stream error (OpenRouter)
                err = chunk["error"]
                raise ProviderError(self.provider_name, "server",
                                    f"{self.label} error: {err.get('message', 'unknown')}",
                                    model=self._model)
            u = chunk.get("usage")
            if isinstance(u, dict):
                usage["in"] = u.get("prompt_tokens", usage["in"])
                usage["out"] = u.get("completion_tokens", usage["out"])
            for choice in chunk.get("choices") or []:
                piece = (choice.get("delta") or {}).get("content") or ""
                if piece:
                    output.append(piece)
                    yield piece

        yield from self._stream_http(
            f"{self._base_url}/chat/completions",
            headers=self._build_headers(), payload=payload, params=None,
            cancel_event=cancel_event, parse_line=parse_line,
        )
        if usage["in"] is not None:
            self.last_usage = Usage(int(usage["in"] or 0), int(usage["out"] or 0), False)
        else:
            self.last_usage = self.estimate_usage(messages, system_prompt, "".join(output))
