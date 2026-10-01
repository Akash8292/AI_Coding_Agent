import pytest
from app.llm.factory import ProviderFactory
from app.llm.base import ProviderNotAvailableError


def test_provider_factory_all_info(app):
    with app.app_context():
        info = ProviderFactory.get_all_info(app.config)
        assert isinstance(info, dict)
        assert "openai" in info
        assert "anthropic" in info
        assert "gemini" in info
        assert "openrouter" in info
        assert "kimi" in info
        assert "ollama" in info

        # In TestConfig, OPENAI_API_KEY is mock-openai-key, so it should be available
        assert info["openai"]["available"] is True
        # ANTHROPIC_API_KEY is None, so it should not be available
        assert info["anthropic"]["available"] is False


def test_provider_factory_unavailable(app):
    with app.app_context():
        with pytest.raises(ProviderNotAvailableError):
            ProviderFactory.get("anthropic")


def test_provider_factory_unknown(app):
    with app.app_context():
        with pytest.raises(ProviderNotAvailableError):
            ProviderFactory.get("unknown_provider_xyz")


def test_provider_factory_get_openai(app):
    with app.app_context():
        provider = ProviderFactory.get("openai", model="gpt-5.6-terra")
        assert provider is not None
        model_info = provider.get_model_info()
        assert model_info.provider == "openai"
        assert model_info.model == "gpt-5.6-terra"


def test_provider_factory_get_gemini(app):
    with app.app_context():
        # Inject mock gemini key into test app config
        app.config["GEMINI_API_KEY"] = "mock-gemini-key-0123456789"
        provider = ProviderFactory.get("gemini", model="gemini-3.6-flash")
        assert provider is not None
        model_info = provider.get_model_info()
        assert model_info.provider == "gemini"
        assert model_info.model == "gemini-3.6-flash"
