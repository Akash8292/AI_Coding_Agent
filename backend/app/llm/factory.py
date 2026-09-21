"""
ProviderFactory — single entry point to get an LLMProvider instance.
Application code should NEVER instantiate providers directly.

Usage:
    from app.llm.factory import ProviderFactory
    provider = ProviderFactory.get("openai")
    for chunk in provider.stream(messages, system_prompt="..."):
        ...
"""
from flask import current_app
from typing import Optional

from app.llm.base import LLMProvider, ProviderNotAvailableError
from app.llm.openai_provider import OpenAIProvider
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.kimi_provider import KimiProvider
from app.llm.openrouter_provider import OpenRouterProvider
from app.llm.ollama_provider import OllamaProvider

# Model curated lists per provider (for the model selector UI)
PROVIDER_MODELS = {
    "openai": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-4o", "gpt-4o-mini"],
    "anthropic": ["claude-sonnet-5", "claude-opus-4-8", "claude-haiku-4-5-20251001", "claude-3-7-sonnet", "claude-3-5-sonnet-20241022"],
    "gemini": ["gemini-3.6-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview", "gemini-2.5-flash"],
    "kimi": ["kimi-k2.7-code", "kimi-k2.6", "kimi-k3", "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    "openrouter": [
        "google/gemini-3.6-flash", "anthropic/claude-sonnet-5", "openai/gpt-5.6-terra",
        "anthropic/claude-3.5-sonnet", "openai/gpt-4o", "openai/gpt-4o-mini",
        "meta-llama/llama-3.1-70b-instruct",
        "mistralai/mistral-7b-instruct",
    ],
    "ollama": [],  # dynamically populated
}

PROVIDER_LABELS = {
    "openai": "OpenAI",
    "anthropic": "Anthropic (Claude)",
    "gemini": "Google Gemini",
    "kimi": "Kimi (Moonshot)",
    "openrouter": "OpenRouter",
    "ollama": "Ollama (Local)",
}


class ProviderFactory:
    @staticmethod
    def get(provider: str, model: Optional[str] = None) -> LLMProvider:
        """
        Return an LLMProvider instance for the given provider name.
        Raises ProviderNotAvailableError if the provider is not configured.
        """
        cfg = current_app.config
        p = provider.lower()

        if p == "openai":
            key = cfg.get("OPENAI_API_KEY")
            if not key:
                raise ProviderNotAvailableError("OPENAI_API_KEY is not set")
            return OpenAIProvider(
                api_key=key,
                model=model or cfg.get("OPENAI_MODEL", "gpt-5.6-terra"),
                base_url=cfg.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            )

        elif p == "anthropic":
            key = cfg.get("ANTHROPIC_API_KEY")
            if not key:
                raise ProviderNotAvailableError("ANTHROPIC_API_KEY is not set")
            return AnthropicProvider(
                api_key=key,
                model=model or cfg.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
                base_url=cfg.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"),
            )

        elif p == "gemini":
            key = cfg.get("GEMINI_API_KEY")
            if not key:
                raise ProviderNotAvailableError("GEMINI_API_KEY is not set")
            return GeminiProvider(
                api_key=key,
                model=model or cfg.get("GEMINI_MODEL", "gemini-3.6-flash"),
                base_url=cfg.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta"),
            )

        elif p == "kimi":
            key = cfg.get("KIMI_API_KEY")
            if not key:
                raise ProviderNotAvailableError("KIMI_API_KEY is not set")
            return KimiProvider(
                api_key=key,
                model=model or cfg.get("KIMI_MODEL", "kimi-k2.7-code"),
                base_url=cfg.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1"),
            )

        elif p == "openrouter":
            key = cfg.get("OPENROUTER_API_KEY")
            if not key:
                raise ProviderNotAvailableError("OPENROUTER_API_KEY is not set")
            return OpenRouterProvider(
                api_key=key,
                model=model or cfg.get("OPENROUTER_MODEL", "google/gemini-3.6-flash"),
                base_url=cfg.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
            )

        elif p == "ollama":
            if not cfg.get("OLLAMA_ENABLED", True):
                raise ProviderNotAvailableError("Ollama is disabled (OLLAMA_ENABLED=false)")
            return OllamaProvider(
                model=model or cfg.get("OLLAMA_MODEL", "codellama"),
                base_url=cfg.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            )

        else:
            raise ProviderNotAvailableError(f"Unknown provider: '{provider}'")

    @staticmethod
    def get_all_info(cfg) -> dict:
        """
        Return info about all providers for the model selector UI.
        Never exposes API keys.
        """
        result = {}

        cloud_providers = {
            "openai": ("OPENAI_API_KEY", "OPENAI_MODEL", "OpenAI"),
            "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "Anthropic (Claude)"),
            "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL", "Google Gemini"),
            "kimi": ("KIMI_API_KEY", "KIMI_MODEL", "Kimi (Moonshot)"),
            "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OpenRouter"),
        }

        for provider_key, (key_name, model_name, label) in cloud_providers.items():
            api_key = cfg.get(key_name)
            available = bool(api_key)
            result[provider_key] = {
                "label": label,
                "models": PROVIDER_MODELS.get(provider_key, []),
                "default_model": cfg.get(model_name, PROVIDER_MODELS.get(provider_key, [""])[0]),
                "available": available,
                "error": None if available else f"Set {key_name} to enable {label}",
                "is_local": False,
            }

        # Ollama — check live availability
        ollama_enabled = cfg.get("OLLAMA_ENABLED", True)
        if ollama_enabled:
            try:
                from app.llm.ollama_provider import OllamaProvider
                ollama = OllamaProvider(base_url=cfg.get("OLLAMA_BASE_URL", "http://localhost:11434"))
                available = ollama.is_available()
                models = ollama.list_models() if available else []
                result["ollama"] = {
                    "label": "Ollama (Local)",
                    "models": models,
                    "default_model": cfg.get("OLLAMA_MODEL", "codellama"),
                    "available": available,
                    "error": None if available else "Ollama is not running",
                    "is_local": True,
                }
            except Exception as e:
                result["ollama"] = {
                    "label": "Ollama (Local)",
                    "models": [],
                    "default_model": "codellama",
                    "available": False,
                    "error": str(e),
                    "is_local": True,
                }
        else:
            result["ollama"] = {
                "label": "Ollama (Local)",
                "models": [],
                "default_model": "codellama",
                "available": False,
                "error": "Ollama is disabled (OLLAMA_ENABLED=false)",
                "is_local": True,
            }

        return result
