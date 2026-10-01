"""
ProviderFactory — single entry point to get an LLMProvider instance.
Application code should NEVER instantiate providers directly.

Usage:
    from app.llm.factory import ProviderFactory
    provider = ProviderFactory.get("openai")
    for chunk in provider.stream(messages, system_prompt="...", cancel_event=ev):
        ...
"""
import threading
import time
from typing import Optional

from flask import current_app

from app.llm.base import LLMProvider, ProviderError, ProviderNotAvailableError, Timeouts
from app.llm.openai_provider import OpenAIProvider
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.kimi_provider import KimiProvider
from app.llm.openrouter_provider import OpenRouterProvider
from app.llm.ollama_provider import OllamaProvider

# Curated fallbacks for the model selector (live lists are used when available)
PROVIDER_MODELS = {
    "openai": ["gpt-5.6-terra", "gpt-5.6-sol", "gpt-5.6-luna", "gpt-4o", "gpt-4o-mini"],
    "anthropic": ["claude-opus-5-5", "claude-sonnet-5", "claude-fable-5-1", "claude-haiku-4-5-20251001"],
    "gemini": ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.1-pro-preview",
               "gemini-2.5-flash", "gemini-2.5-pro"],
    "kimi": ["kimi-k2.7-code", "kimi-k2.6", "kimi-k3", "moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    "openrouter": [
        "google/gemini-3.6-flash", "anthropic/claude-sonnet-5", "openai/gpt-5.6-terra",
        "anthropic/claude-3.5-sonnet", "openai/gpt-4o", "openai/gpt-4o-mini",
        "meta-llama/llama-3.1-70b-instruct",
    ],
    "ollama": [],  # dynamically populated
}

CLOUD_PROVIDERS = {
    # key: (api key setting, model setting, label)
    "gemini": ("GEMINI_API_KEY", "GEMINI_MODEL", "Google Gemini"),
    "openai": ("OPENAI_API_KEY", "OPENAI_MODEL", "OpenAI"),
    "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_MODEL", "Anthropic (Claude)"),
    "openrouter": ("OPENROUTER_API_KEY", "OPENROUTER_MODEL", "OpenRouter"),
    "kimi": ("KIMI_API_KEY", "KIMI_MODEL", "Kimi (Moonshot)"),
}

_PLACEHOLDER_MARKERS = ("...", "your_", "your-", "change-me", "changeme", "xxx", "<", "replace")


def is_real_key(value: Optional[str]) -> bool:
    """Reject empty and obviously-placeholder API keys (e.g. 'sk-...')."""
    if not value:
        return False
    v = value.strip().strip('"').strip("'")
    if len(v) < 16:
        return False
    low = v.lower()
    return not any(m in low for m in _PLACEHOLDER_MARKERS)


# ── Provider health registry ────────────────────────────────────────────────
# Remembers the last hard failure (auth/quota/not_found) per provider so the
# model selector can warn before the user sends another doomed request.
_health_lock = threading.Lock()
_health: dict[str, dict] = {}
HEALTH_TTL_SECONDS = 15 * 60


MODEL_SCOPED_KINDS = {"quota", "not_found"}      # quota & availability are per model
PROVIDER_SCOPED_KINDS = {"auth", "unavailable"}   # a bad key breaks every model


def record_provider_result(provider: str, error: Optional[ProviderError] = None, model: str = "") -> None:
    with _health_lock:
        if error is None:
            _health.pop(provider, None)
            _health.pop(f"{provider}:{model}", None)
        elif error.kind in PROVIDER_SCOPED_KINDS:
            _health[provider] = {"kind": error.kind, "message": error.message, "at": time.time()}
        elif error.kind in MODEL_SCOPED_KINDS and model:
            _health[f"{provider}:{model}"] = {"kind": error.kind, "message": error.message, "at": time.time()}


def model_health(provider: str) -> dict:
    """{model: message} for models of this provider with a recent quota/not-found failure."""
    now = time.time()
    with _health_lock:
        return {k.split(":", 1)[1]: v["message"] for k, v in _health.items()
                if k.startswith(provider + ":") and now - v["at"] <= HEALTH_TTL_SECONDS}


def provider_health(provider: str) -> Optional[dict]:
    with _health_lock:
        h = _health.get(provider)
        if h and time.time() - h["at"] > HEALTH_TTL_SECONDS:
            _health.pop(provider, None)
            return None
        return dict(h) if h else None


# ── Live model list cache ───────────────────────────────────────────────────
_models_lock = threading.Lock()
_models_cache: dict[str, tuple[float, list[str]]] = {}
MODELS_TTL_SECONDS = 10 * 60
_ollama_probe: dict = {"at": 0.0, "available": False, "models": []}


def _cached_models(key: str, loader) -> Optional[list[str]]:
    with _models_lock:
        hit = _models_cache.get(key)
        if hit and time.time() - hit[0] < MODELS_TTL_SECONDS:
            return hit[1]
    try:
        models = loader()
    except Exception:
        models = None
    if models:
        with _models_lock:
            _models_cache[key] = (time.time(), models)
    return models


def _timeouts(cfg) -> Timeouts:
    return Timeouts(
        connect=float(cfg.get("LLM_CONNECT_TIMEOUT", 10)),
        first_token=float(cfg.get("LLM_FIRST_TOKEN_TIMEOUT", 90)),
        idle=float(cfg.get("LLM_IDLE_TIMEOUT", 60)),
        total=float(cfg.get("LLM_TOTAL_TIMEOUT", 300)),
        max_attempts=int(cfg.get("LLM_MAX_ATTEMPTS", 2)),
    )


class ProviderFactory:
    @staticmethod
    def get(provider: str, model: Optional[str] = None, cfg=None) -> LLMProvider:
        """
        Return an LLMProvider instance for the given provider name.
        Raises ProviderNotAvailableError if the provider is not configured.
        """
        cfg = cfg if cfg is not None else current_app.config
        p = (provider or "").lower()
        t = _timeouts(cfg)

        if p in CLOUD_PROVIDERS:
            key_name, model_name, label = CLOUD_PROVIDERS[p]
            key = cfg.get(key_name)
            if not is_real_key(key):
                raise ProviderNotAvailableError(f"{label} is not configured — set {key_name}.")
            chosen = model or cfg.get(model_name) or PROVIDER_MODELS[p][0]
            if p == "openai":
                return OpenAIProvider(api_key=key, model=chosen, timeouts=t,
                                      base_url=cfg.get("OPENAI_BASE_URL", "https://api.openai.com/v1"))
            if p == "anthropic":
                return AnthropicProvider(api_key=key, model=chosen, timeouts=t,
                                         base_url=cfg.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1"))
            if p == "gemini":
                return GeminiProvider(api_key=key, model=chosen, timeouts=t,
                                      base_url=cfg.get("GEMINI_BASE_URL",
                                                       "https://generativelanguage.googleapis.com/v1beta"),
                                      thinking_level=cfg.get("GEMINI_THINKING_LEVEL", ""))
            if p == "kimi":
                return KimiProvider(api_key=key, model=chosen, timeouts=t,
                                    base_url=cfg.get("KIMI_BASE_URL", "https://api.moonshot.ai/v1"))
            if p == "openrouter":
                return OpenRouterProvider(api_key=key, model=chosen, timeouts=t,
                                          base_url=cfg.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"))

        if p == "ollama":
            if not cfg.get("OLLAMA_ENABLED", False):
                raise ProviderNotAvailableError("Ollama is disabled (set OLLAMA_ENABLED=true to use it).")
            return OllamaProvider(
                model=model or cfg.get("OLLAMA_MODEL", "codellama"),
                base_url=cfg.get("OLLAMA_BASE_URL", "http://localhost:11434"),
            )

        raise ProviderNotAvailableError(f"Unknown provider: '{provider}'")

    @staticmethod
    def get_all_info(cfg, live_models: bool = True) -> dict:
        """
        Provider/model info for the model selector UI. Never exposes API keys.
        """
        result = {}
        for provider_key, (key_name, model_name, label) in CLOUD_PROVIDERS.items():
            configured = is_real_key(cfg.get(key_name))
            models = list(PROVIDER_MODELS.get(provider_key, []))
            if configured and live_models and provider_key == "gemini":
                live = _cached_models("gemini", lambda: GeminiProvider(
                    api_key=cfg.get(key_name),
                    base_url=cfg.get("GEMINI_BASE_URL",
                                     "https://generativelanguage.googleapis.com/v1beta")).list_models())
                if live:
                    models = live
            default_model = cfg.get(model_name) or (models[0] if models else "")
            if default_model and default_model not in models:
                models.insert(0, default_model)
            health = provider_health(provider_key) if configured else None
            result[provider_key] = {
                "label": label,
                "models": models,
                "default_model": default_model,
                "available": configured,
                "configured": configured,
                "status": ("error" if health else "ready") if configured else "not_configured",
                "error": (health["message"] if health else None) if configured
                else f"Set {key_name} to enable {label}",
                "is_local": False,
                "model_errors": model_health(provider_key) if configured else {},
            }

        # Ollama — optional, probed with a short timeout and cached briefly
        if cfg.get("OLLAMA_ENABLED", False):
            base_url = cfg.get("OLLAMA_BASE_URL", "http://localhost:11434")
            if time.time() - _ollama_probe["at"] > 15:
                probe = OllamaProvider(base_url=base_url)
                available = probe.is_available()
                _ollama_probe.update(at=time.time(), available=available,
                                     models=probe.list_models() if available else [])
            available = _ollama_probe["available"]
            result["ollama"] = {
                "label": "Ollama (Local)",
                "models": _ollama_probe["models"],
                "default_model": cfg.get("OLLAMA_MODEL", "codellama"),
                "available": available,
                "configured": True,
                "status": "ready" if available else "unreachable",
                "error": None if available else f"Ollama is not running at {base_url}",
                "is_local": True,
            }
        else:
            result["ollama"] = {
                "label": "Ollama (Local)",
                "models": [],
                "default_model": cfg.get("OLLAMA_MODEL", "codellama"),
                "available": False,
                "configured": False,
                "status": "disabled",
                "error": "Optional. Set OLLAMA_ENABLED=true to use a local Ollama server.",
                "is_local": True,
            }
        return result
