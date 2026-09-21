"""
OpenAI provider — Chat Completions API with SSE streaming.
Also used as the base for any OpenAI-compatible endpoint.
"""
import json
import time
from typing import Iterator

import requests

from app.llm.base import LLMProvider, ModelInfo, GenerationResult, ProviderNotAvailableError

OPENAI_MODELS = {
    "gpt-5.6-terra": {"context": 256_000, "max_out": 32_768},
    "gpt-5.6-sol": {"context": 256_000, "max_out": 32_768},
    "gpt-5.6-luna": {"context": 256_000, "max_out": 32_768},
    "gpt-4o": {"context": 128_000, "max_out": 16_384},
    "gpt-4o-mini": {"context": 128_000, "max_out": 16_384},
    "gpt-4-turbo": {"context": 128_000, "max_out": 4_096},
    "gpt-3.5-turbo": {"context": 16_385, "max_out": 4_096},
}

MAX_ATTEMPTS = 3
RETRYABLE = {429, 500, 502, 503, 504}


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gpt-5.6-terra",
                 base_url: str = "https://api.openai.com/v1",
                 provider_name_override: str = "openai",
                 label_override: str = "OpenAI"):
        if not api_key:
            raise ProviderNotAvailableError("OpenAI API key is not configured")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._provider_name = provider_name_override
        self._label = label_override

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def get_model_info(self) -> ModelInfo:
        info = OPENAI_MODELS.get(self._model, {"context": 128_000, "max_out": 4_096})
        return ModelInfo(
            provider=self._provider_name,
            model=self._model,
            label=f"{self._label} / {self._model}",
            context_window=info["context"],
            max_output_tokens=info["max_out"],
        )

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def stream(self, messages: list[dict], system_prompt: str = "", **kwargs) -> Iterator[str]:
        all_messages = []
        if system_prompt:
            all_messages.append({"role": "system", "content": system_prompt})
        all_messages.extend(messages)

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with requests.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._build_headers(),
                    json={"model": self._model, "messages": all_messages, "stream": True,
                          **{k: v for k, v in kwargs.items() if k in ("temperature", "max_tokens")}},
                    stream=True,
                    timeout=300,
                ) as r:
                    if r.status_code != 200:
                        if r.status_code in RETRYABLE and attempt < MAX_ATTEMPTS:
                            time.sleep(2 * attempt)
                            continue
                        error_msg = self._parse_error(r)
                        yield f"\n\n[{self._label} error {r.status_code}: {error_msg}]"
                        return

                    for line in r.iter_lines():
                        if not line:
                            continue
                        line = line.decode("utf-8") if isinstance(line, bytes) else line
                        if not line.startswith("data: "):
                            continue
                        payload = line[6:]
                        if payload.strip() == "[DONE]":
                            break
                        try:
                            chunk = json.loads(payload)
                            delta = chunk.get("choices", [{}])[0].get("delta", {})
                            piece = delta.get("content", "")
                            if piece:
                                yield piece
                        except json.JSONDecodeError:
                            continue
                    return

            except requests.RequestException as e:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2 * attempt)
                    continue
                yield f"\n\n[Network error contacting {self._label}: {e}]"
                return

    def generate(self, messages: list[dict], system_prompt: str = "", **kwargs) -> GenerationResult:
        return self.generate_blocking(messages, system_prompt=system_prompt, **kwargs)

    def _parse_error(self, r) -> str:
        try:
            data = r.json()
            return data.get("error", {}).get("message", r.text[:200])
        except Exception:
            return r.text[:200]
