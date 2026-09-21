"""
Anthropic Claude provider — Messages API with SSE streaming.
System prompt is a first-class field (not a message).
"""
import json
import time
from typing import Iterator

import requests

from app.llm.base import LLMProvider, ModelInfo, GenerationResult, ProviderNotAvailableError

CLAUDE_MODELS = {
    "claude-sonnet-5": {"context": 200_000, "max_out": 16_384},
    "claude-opus-4-8": {"context": 200_000, "max_out": 16_384},
    "claude-haiku-4-5-20251001": {"context": 200_000, "max_out": 8_192},
    "claude-3-7-sonnet": {"context": 200_000, "max_out": 16_384},
    "claude-3-5-sonnet-20241022": {"context": 200_000, "max_out": 8_192},
    "claude-3-5-haiku-20241022": {"context": 200_000, "max_out": 8_192},
    "claude-3-opus-20240229": {"context": 200_000, "max_out": 4_096},
    "claude-3-haiku-20240307": {"context": 200_000, "max_out": 4_096},
}

MAX_ATTEMPTS = 3
RETRYABLE = {429, 500, 502, 503, 504}


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "claude-sonnet-5",
                 base_url: str = "https://api.anthropic.com/v1"):
        if not api_key:
            raise ProviderNotAvailableError("Anthropic API key is not configured")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def get_model_info(self) -> ModelInfo:
        info = CLAUDE_MODELS.get(self._model, {"context": 200_000, "max_out": 4_096})
        return ModelInfo(
            provider="anthropic",
            model=self._model,
            label=f"Claude / {self._model}",
            context_window=info["context"],
            max_output_tokens=info["max_out"],
        )

    def stream(self, messages: list[dict], system_prompt: str = "", **kwargs) -> Iterator[str]:
        # Claude uses role 'assistant' not 'system'; filter to user/assistant only
        chat_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages if m.get("role") in ("user", "assistant") and m.get("content")
        ]

        payload = {
            "model": self._model,
            "messages": chat_messages,
            "max_tokens": kwargs.get("max_tokens", 4096),
            "stream": True,
        }
        if system_prompt:
            payload["system"] = system_prompt

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with requests.post(
                    f"{self._base_url}/messages",
                    headers={
                        "x-api-key": self._api_key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=payload,
                    stream=True,
                    timeout=300,
                ) as r:
                    if r.status_code != 200:
                        if r.status_code in RETRYABLE and attempt < MAX_ATTEMPTS:
                            time.sleep(2 * attempt)
                            continue
                        yield f"\n\n[Claude API error {r.status_code}: {r.text[:200]}]"
                        return

                    for raw_line in r.iter_lines():
                        if not raw_line:
                            continue
                        line = raw_line.decode("utf-8") if isinstance(raw_line, bytes) else raw_line
                        if not line.startswith("data: "):
                            continue
                        try:
                            chunk = json.loads(line[6:])
                        except json.JSONDecodeError:
                            continue
                        if chunk.get("type") == "content_block_delta":
                            piece = chunk.get("delta", {}).get("text", "")
                            if piece:
                                yield piece
                        elif chunk.get("type") == "message_stop":
                            break
                    return

            except requests.RequestException as e:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2 * attempt)
                    continue
                yield f"\n\n[Network error contacting Claude: {e}]"
                return

    def generate(self, messages: list[dict], system_prompt: str = "", **kwargs) -> GenerationResult:
        return self.generate_blocking(messages, system_prompt=system_prompt, **kwargs)
