from types import SimpleNamespace
from unittest.mock import Mock

from bot.ai_models import fast_model, log_usage, rag_model, response_model, transcribe_model


def test_default_models(monkeypatch):
    for name in (
        "YAGAPON_RAG_MODEL",
        "YAGAPON_RESPONSE_MODEL",
        "YAGAPON_FAST_MODEL",
        "YAGAPON_TRANSCRIBE_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    assert rag_model() == "gemini-3.8-flash"
    assert response_model() == "gemini-3.8-flash"
    assert fast_model() == "gemini-3.1-flash-lite"
    assert transcribe_model() == "gemini-3.5-transcribe"


def test_models_can_be_rolled_back_without_code_change(monkeypatch):
    monkeypatch.setenv("YAGAPON_RAG_MODEL", "gemini-fallback")
    monkeypatch.setenv("YAGAPON_TRANSCRIBE_MODEL", "transcribe-fallback")

    assert rag_model() == "gemini-fallback"
    assert transcribe_model() == "transcribe-fallback"


def test_usage_logging_supports_generate_content_metadata():
    logger = Mock()
    response = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            prompt_token_count=100,
            candidates_token_count=20,
            thoughts_token_count=5,
            total_token_count=125,
        )
    )

    log_usage(logger, "rag_query", "gemini-test", response)

    assert logger.info.call_args.args[-4:] == (100, 20, 5, 125)
