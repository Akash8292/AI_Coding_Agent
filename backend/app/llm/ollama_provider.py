"""
Ollama provider — OPTIONAL local LLM backend.

CodeSage never requires Ollama. It is only offered when OLLAMA_ENABLED=true,
and every call to it is bounded by short timeouts so an absent Ollama server
shows up as "unavailable" instead of hanging a request.
"""
import json
from typing import Iterator, Optional

import requests

from app.llm.base import LLMProvider, ModelInfo, ProviderError, Timeouts, Usage

PROBE_TIMEOUT = (1.5, 2.5)  # (connect, read) for availability checks


class OllamaProvider(LLMProvider):
    label = "Ollama"

    def __init__(self, model: str = "codellama",
                 base_url: str = "http://localhost:11434",
                 timeouts: Optional[Timeouts] = None):
        # Local models can be slow to load; give the first token more room
        super().__init__(timeouts or Timeouts(connect=3, first_token=180, idle=90, total=600,
                                              max_attempts=1))
        self._model = model
        self._base_url = base_url.rstrip("/")

    @property
    def provider_name(self) -> str:
        return "ollama"

    def is_available(self) -> bool:
        """Check if Ollama is reachable (fast; never blocks more than ~4s)."""
        try:
            r = requests.get(f"{self._base_url}/api/tags", timeout=PROBE_TIMEOUT)
            return r.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        """Return available local models (excluding embedding models)."""
        try:
            r = requests.get(f"{self._base_url}/api/tags", timeout=PROBE_TIMEOUT)
            r.raise_for_status()
            return [
                m["name"] for m in r.json().get("models", [])
                if "embed" not in m["name"].lower()
            ]
        except Exception:
            return []

    def get_model_info(self) -> ModelInfo:
        return ModelInfo(
            provider="ollama",
            model=self._model,
            label=f"Ollama / {self._model} (Local)",
            context_window=32_768,
            max_output_tokens=4_096,
            is_local=True,
        )

    def stream(self, messages: list[dict], system_prompt: str = "",
               cancel_event=None, json_mode: bool = False,
               max_tokens: Optional[int] = None, **kwargs) -> Iterator[str]:
        all_messages = []
        if system_prompt:
            all_messages.append({"role": "system", "content": system_prompt})
        all_messages.extend({"role": m["role"], "content": m["content"]} for m in messages)
        payload: dict = {"model": self._model, "messages": all_messages, "stream": True}
        if json_mode:
            payload["format"] = "json"
        if max_tokens:
            payload["options"] = {"num_predict": max_tokens}

        usage = {"in": None, "out": None}
        output: list[str] = []

        def parse_line(data: str) -> Iterator[str]:
            try:
                chunk = json.loads(data)
            except json.JSONDecodeError:
                return
            if chunk.get("error"):
                raise ProviderError("ollama", "server", f"Ollama error: {chunk['error']}",
                                    model=self._model)
            piece = (chunk.get("message") or {}).get("content") or ""
            if piece:
                output.append(piece)
                yield piece
            if chunk.get("done"):
                usage["in"] = chunk.get("prompt_eval_count")
                usage["out"] = chunk.get("eval_count")

        try:
            yield from self._stream_http(
                f"{self._base_url}/api/chat", headers={"Content-Type": "application/json"},
                payload=payload, params=None, cancel_event=cancel_event,
                parse_line=parse_line, line_prefix=None,
            )
        except ProviderError as e:
            if e.kind == "network":
                raise ProviderError("ollama", "unavailable",
                                    f"Ollama is not reachable at {self._base_url}. Start it with "
                                    "`ollama serve` or choose a cloud provider.", model=self._model)
            if e.kind == "not_found":
                raise ProviderError("ollama", "not_found",
                                    f"Ollama model '{self._model}' is not installed. "
                                    f"Run `ollama pull {self._model}`.", model=self._model)
            raise
        if usage["in"] is not None:
            self.last_usage = Usage(int(usage["in"] or 0), int(usage["out"] or 0), False)
        else:
            self.last_usage = self.estimate_usage(messages, system_prompt, "".join(output))
