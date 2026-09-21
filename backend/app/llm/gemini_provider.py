"""
Google Gemini provider — generateContent API with SSE streaming.
Uses 'model' role instead of 'assistant', systemInstruction field.
"""
import json
import time
from typing import Iterator

import requests

from app.llm.base import LLMProvider, ModelInfo, GenerationResult, ProviderNotAvailableError

GEMINI_MODELS = {
    "gemini-3.6-flash": {"context": 1_000_000, "max_out": 8_192},
    "gemini-3.5-flash-lite": {"context": 1_000_000, "max_out": 8_192},
    "gemini-3.1-pro-preview": {"context": 2_000_000, "max_out": 8_192},
    "gemini-2.5-flash": {"context": 1_000_000, "max_out": 8_192},
    "gemini-2.0-flash": {"context": 1_000_000, "max_out": 8_192},
    "gemini-1.5-flash": {"context": 1_000_000, "max_out": 8_192},
    "gemini-1.5-pro": {"context": 2_000_000, "max_out": 8_192},
}

MAX_ATTEMPTS = 3
RETRYABLE = {429, 500, 502, 503, 504}


class GeminiProvider(LLMProvider):
    def __init__(self, api_key: str, model: str = "gemini-3.6-flash",
                 base_url: str = "https://generativelanguage.googleapis.com/v1beta"):
        if not api_key:
            raise ProviderNotAvailableError("Gemini API key is not configured")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")

    @property
    def provider_name(self) -> str:
        return "gemini"

    def get_model_info(self) -> ModelInfo:
        info = GEMINI_MODELS.get(self._model, {"context": 1_000_000, "max_out": 8_192})
        return ModelInfo(
            provider="gemini",
            model=self._model,
            label=f"Gemini / {self._model}",
            context_window=info["context"],
            max_output_tokens=info["max_out"],
        )

    def stream(self, messages: list[dict], system_prompt: str = "", **kwargs) -> Iterator[str]:
        # Convert role 'assistant' → 'model' for Gemini
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user",
             "parts": [{"text": m["content"]}]}
            for m in messages if m.get("role") in ("user", "assistant") and m.get("content")
        ]

        body = {"contents": contents}
        if system_prompt:
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                with requests.post(
                    f"{self._base_url}/models/{self._model}:streamGenerateContent",
                    params={"alt": "sse"},
                    headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
                    json=body,
                    stream=True,
                    timeout=300,
                ) as r:
                    if r.status_code != 200:
                        if r.status_code in RETRYABLE and attempt < MAX_ATTEMPTS:
                            time.sleep(2 * attempt)
                            continue
                        yield f"\n\n[Gemini API error {r.status_code}: {r.text[:200]}]"
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
                        candidates = chunk.get("candidates", [])
                        if not candidates:
                            continue
                        for part in candidates[0].get("content", {}).get("parts", []):
                            piece = part.get("text", "")
                            if piece:
                                yield piece
                    return

            except requests.RequestException as e:
                if attempt < MAX_ATTEMPTS:
                    time.sleep(2 * attempt)
                    continue
                yield f"\n\n[Network error contacting Gemini: {e}]"
                return

    def generate(self, messages: list[dict], system_prompt: str = "", **kwargs) -> GenerationResult:
        return self.generate_blocking(messages, system_prompt=system_prompt, **kwargs)
