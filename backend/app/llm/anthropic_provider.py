"""
Anthropic Claude provider — Messages API with SSE streaming.
System prompt is a first-class field (not a message).
"""
import json
from typing import Iterator, Optional

from app.llm.base import (LLMProvider, ModelInfo, ProviderError, ProviderNotAvailableError,
                          Timeouts, Usage)

CLAUDE_MODELS = {
    "claude-opus-5-5": {"context": 200_000, "max_out": 32_000},
    "claude-sonnet-5": {"context": 200_000, "max_out": 64_000},
    "claude-fable-5-1": {"context": 200_000, "max_out": 32_000},
    "claude-haiku-4-5-20251001": {"context": 200_000, "max_out": 64_000},
    "claude-opus-4-8": {"context": 200_000, "max_out": 32_000},
    "claude-3-7-sonnet": {"context": 200_000, "max_out": 16_384},
    "claude-3-5-sonnet-20241022": {"context": 200_000, "max_out": 8_192},
    "claude-3-5-haiku-20241022": {"context": 200_000, "max_out": 8_192},
}

DEFAULT_MAX_TOKENS = 8192


class AnthropicProvider(LLMProvider):
    label = "Claude"

    def __init__(self, api_key: str, model: str = "claude-sonnet-5",
                 base_url: str = "https://api.anthropic.com/v1",
                 timeouts: Optional[Timeouts] = None):
        super().__init__(timeouts)
        if not api_key:
            raise ProviderNotAvailableError("Anthropic API key is not configured")
        self._api_key = api_key
        self._model = model
        self._base_url = base_url.rstrip("/")

    @property
    def provider_name(self) -> str:
        return "anthropic"

    def get_model_info(self) -> ModelInfo:
        info = CLAUDE_MODELS.get(self._model, {"context": 200_000, "max_out": 8_192})
        return ModelInfo(
            provider="anthropic",
            model=self._model,
            label=f"Claude / {self._model}",
            context_window=info["context"],
            max_output_tokens=info["max_out"],
        )

    def build_payload(self, messages: list[dict], system_prompt: str, json_mode: bool,
                      max_tokens: Optional[int], **kwargs) -> dict:
        chat_messages = [
            {"role": m["role"], "content": m["content"]}
            for m in messages if m.get("role") in ("user", "assistant") and m.get("content")
        ]
        info = CLAUDE_MODELS.get(self._model, {"max_out": DEFAULT_MAX_TOKENS})
        payload = {
            "model": self._model,
            "messages": chat_messages,
            "max_tokens": min(max_tokens or DEFAULT_MAX_TOKENS, info["max_out"]),
            "stream": True,
        }
        if system_prompt:
            payload["system"] = system_prompt
        if json_mode:
            # The Messages API has no JSON switch; the system prompt demands a
            # single JSON object and the edit parser tolerates surrounding prose.
            payload["system"] = (payload.get("system", "") +
                                 "\n\nRespond with a single JSON object and nothing else.").strip()
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
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                return
            ctype = chunk.get("type")
            if ctype == "message_start":
                u = (chunk.get("message") or {}).get("usage") or {}
                usage["in"] = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
                    + (u.get("cache_creation_input_tokens") or 0)
                usage["out"] = u.get("output_tokens")
            elif ctype == "content_block_delta":
                delta = chunk.get("delta") or {}
                if delta.get("type", "text_delta") == "text_delta":
                    piece = delta.get("text") or ""
                    if piece:
                        output.append(piece)
                        yield piece
            elif ctype == "message_delta":
                u = chunk.get("usage") or {}
                if "output_tokens" in u:
                    usage["out"] = u["output_tokens"]
            elif ctype == "error":
                err = chunk.get("error") or {}
                kind = "rate_limit" if err.get("type") == "overloaded_error" else "server"
                raise ProviderError("anthropic", kind,
                                    f"Claude error: {err.get('message', err.get('type', 'unknown'))}",
                                    model=self._model)

        yield from self._stream_http(
            f"{self._base_url}/messages",
            headers={
                "x-api-key": self._api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload=payload, params=None,
            cancel_event=cancel_event, parse_line=parse_line,
        )
        if usage["in"] is not None:
            self.last_usage = Usage(int(usage["in"] or 0), int(usage["out"] or 0), False)
        else:
            self.last_usage = self.estimate_usage(messages, system_prompt, "".join(output))
