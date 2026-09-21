"""
Ollama provider — OPTIONAL local LLM backend.
Gracefully unavailable if Ollama is not running or OLLAMA_ENABLED=false.
"""
import json
from typing import Iterator

import requests

from app.llm.base import LLMProvider, ModelInfo, GenerationResult, ProviderNotAvailableError


class OllamaProvider(LLMProvider):
    def __init__(self, model: str = "codellama",
                 base_url: str = "http://localhost:11434"):
        self._model = model
        self._base_url = base_url.rstrip("/")

    @property
    def provider_name(self) -> str:
        return "ollama"

    def is_available(self) -> bool:
        """Check if Ollama is reachable."""
        try:
            r = requests.get(f"{self._base_url}/api/tags", timeout=3)
            return r.status_code == 200
        except Exception:
            return False

    def list_models(self) -> list[str]:
        """Return available local models (excluding embedding models)."""
        try:
            r = requests.get(f"{self._base_url}/api/tags", timeout=5)
            r.raise_for_status()
            return [
                m["name"] for m in r.json().get("models", [])
                if "embed" not in m["name"].lower()
            ]
        except Exception:
            return []

    def get_model_info(self) -> ModelInfo:
        available = self.is_available()
        return ModelInfo(
            provider="ollama",
            model=self._model,
            label=f"Ollama / {self._model} (Local)",
            context_window=32_768,
            max_output_tokens=4_096,
            is_local=True,
            available=available,
            error=None if available else "Ollama is not running — start with: ollama serve",
        )

    def stream(self, messages: list[dict], system_prompt: str = "", **kwargs) -> Iterator[str]:
        all_messages = []
        if system_prompt:
            all_messages.append({"role": "system", "content": system_prompt})
        all_messages.extend(messages)

        try:
            with requests.post(
                f"{self._base_url}/api/chat",
                json={"model": self._model, "messages": all_messages, "stream": True},
                stream=True,
                timeout=300,
            ) as r:
                r.raise_for_status()
                for line in r.iter_lines():
                    if not line:
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    piece = chunk.get("message", {}).get("content", "")
                    if piece:
                        yield piece
                    if chunk.get("done"):
                        break
        except requests.ConnectionError:
            yield (
                "\n\n[Ollama is not running. Start it with `ollama serve`, "
                "or switch to a cloud provider in the model selector.]"
            )
        except requests.RequestException as e:
            yield f"\n\n[Ollama error: {e}]"

    def generate(self, messages: list[dict], system_prompt: str = "", **kwargs) -> GenerationResult:
        return self.generate_blocking(messages, system_prompt=system_prompt, **kwargs)
