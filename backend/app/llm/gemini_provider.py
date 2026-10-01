"""
Google Gemini provider — streamGenerateContent API with SSE streaming.
Uses 'model' role instead of 'assistant', and a systemInstruction field.
"""
import json
from typing import Iterator, Optional

import requests

from app.llm.base import (LLMProvider, ModelInfo, ProviderError, ProviderNotAvailableError,
                          Timeouts, Usage)

GEMINI_MODELS = {
    "gemini-3.6-flash": {"context": 1_048_576, "max_out": 65_536},
    "gemini-3.5-flash": {"context": 1_048_576, "max_out": 65_536},
    "gemini-3.5-flash-lite": {"context": 1_048_576, "max_out": 65_536},
    "gemini-3.1-pro-preview": {"context": 1_048_576, "max_out": 65_536},
    "gemini-2.5-flash": {"context": 1_048_576, "max_out": 65_536},
    "gemini-2.5-pro": {"context": 1_048_576, "max_out": 65_536},
}

BLOCK_REASONS = {"SAFETY", "RECITATION", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "OTHER"}


class GeminiProvider(LLMProvider):
    label = "Gemini"

    def __init__(self, api_key: str, model: str = "gemini-3.6-flash",
                 base_url: str = "https://generativelanguage.googleapis.com/v1beta",
                 thinking_level: str = "", timeouts: Optional[Timeouts] = None):
        super().__init__(timeouts)
        if not api_key:
            raise ProviderNotAvailableError("Gemini API key is not configured")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._thinking_level = (thinking_level or "").lower()
        self.last_finish_reason: Optional[str] = None

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

    def _thinking_config(self, level: str) -> Optional[dict]:
        """Gemini 3.x takes thinkingLevel; 2.5 takes a token budget."""
        if not level:
            return None
        if self._model.startswith("gemini-2.5"):
            budgets = {"minimal": 0, "low": 1024, "medium": 4096, "high": -1}
            if "pro" in self._model and level == "minimal":
                return {"thinkingBudget": 128}  # 2.5 Pro cannot disable thinking
            return {"thinkingBudget": budgets.get(level, -1)}
        if self._model.startswith("gemini-3"):
            return {"thinkingLevel": level}
        return None

    def build_payload(self, messages: list[dict], system_prompt: str, json_mode: bool,
                      max_tokens: Optional[int], **kwargs) -> dict:
        contents = [
            {"role": "model" if m["role"] == "assistant" else "user",
             "parts": [{"text": m["content"]}]}
            for m in messages if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        body: dict = {"contents": contents}
        if system_prompt:
            body["systemInstruction"] = {"parts": [{"text": system_prompt}]}

        gen: dict = {}
        if json_mode:
            gen["responseMimeType"] = "application/json"
        if max_tokens:
            gen["maxOutputTokens"] = max_tokens
        if "temperature" in kwargs:
            gen["temperature"] = kwargs["temperature"]
        # Explicit config wins; otherwise a caller "effort" hint maps to a thinking level
        level = kwargs.get("thinking_level") or self._thinking_level or             {"low": "low", "minimal": "minimal"}.get(kwargs.get("effort") or "", "")
        thinking = self._thinking_config(level)
        if thinking is not None:
            gen["thinkingConfig"] = thinking
        if gen:
            body["generationConfig"] = gen
        return body

    def stream(self, messages: list[dict], system_prompt: str = "",
               cancel_event=None, json_mode: bool = False,
               max_tokens: Optional[int] = None, **kwargs) -> Iterator[str]:
        body = self.build_payload(messages, system_prompt, json_mode, max_tokens, **kwargs)
        usage = {"in": None, "out": None}
        output: list[str] = []
        self.last_finish_reason = None

        def parse_line(data: str) -> Iterator[str]:
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                return
            if isinstance(chunk.get("error"), dict):
                raise ProviderError("gemini", "server",
                                    f"Gemini error: {chunk['error'].get('message', 'unknown')}",
                                    model=self._model)
            block = (chunk.get("promptFeedback") or {}).get("blockReason")
            if block:
                raise ProviderError("gemini", "bad_request",
                                    f"Gemini blocked the prompt ({block}).", model=self._model)
            um = chunk.get("usageMetadata")
            if isinstance(um, dict):
                usage["in"] = um.get("promptTokenCount", usage["in"])
                usage["out"] = (um.get("candidatesTokenCount") or 0) + (um.get("thoughtsTokenCount") or 0)
            for cand in chunk.get("candidates") or []:
                if cand.get("finishReason"):
                    self.last_finish_reason = cand["finishReason"]
                for part in (cand.get("content") or {}).get("parts") or []:
                    if part.get("thought"):
                        continue  # never surface model thoughts as answer text
                    piece = part.get("text") or ""
                    if piece:
                        output.append(piece)
                        yield piece

        yield from self._stream_http(
            f"{self._base_url}/models/{self._model}:streamGenerateContent",
            headers={"x-goog-api-key": self._api_key, "Content-Type": "application/json"},
            payload=body, params={"alt": "sse"},
            cancel_event=cancel_event, parse_line=parse_line,
        )

        if not output and self.last_finish_reason in BLOCK_REASONS:
            raise ProviderError("gemini", "bad_request",
                                f"Gemini stopped without output (finishReason={self.last_finish_reason}).",
                                model=self._model)
        if not output and self.last_finish_reason == "MAX_TOKENS":
            raise ProviderError("gemini", "empty",
                                "Gemini used its whole output budget on reasoning and returned no text. "
                                "Try again or pick a model with lower thinking.", model=self._model)
        if usage["in"] is not None:
            self.last_usage = Usage(int(usage["in"] or 0), int(usage["out"] or 0), False)
        else:
            self.last_usage = self.estimate_usage(messages, system_prompt, "".join(output))

    def list_models(self) -> list[str]:
        """Live list of text-generation models available to this key."""
        r = requests.get(f"{self._base_url}/models", params={"pageSize": 200},
                         headers={"x-goog-api-key": self._api_key}, timeout=(5, 10))
        r.raise_for_status()
        skip = ("tts", "image", "transcribe", "robotics", "computer-use", "embedding",
                "deep-research", "customtools", "lyria", "omni", "antigravity", "banana")
        names = []
        for m in r.json().get("models", []):
            name = m.get("name", "").replace("models/", "")
            if ("generateContent" in m.get("supportedGenerationMethods", [])
                    and name.startswith("gemini") and not any(s in name for s in skip)):
                names.append(name)
        return sorted(names, reverse=True)
