from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bot.ai_models import (
    ai_profile,
    audio_analysis_model,
    fast_model,
    knowledge_curation_model,
    knowledge_model,
    log_usage,
    minutes_model,
    rag_model,
    response_model,
    transcribe_fallback_model,
    transcribe_model,
)


def test_default_models(monkeypatch):
    for name in (
        "YAGAPON_RAG_MODEL",
        "YAGAPON_RESPONSE_MODEL",
        "YAGAPON_MINUTES_MODEL",
        "YAGAPON_FAST_MODEL",
        "YAGAPON_TRANSCRIBE_MODEL",
        "YAGAPON_TRANSCRIBE_FALLBACK_MODEL",
        "YAGAPON_AUDIO_MODEL",
        "YAGAPON_KNOWLEDGE_CURATION_MODEL",
        "YAGAPON_KNOWLEDGE_MODEL",
        "YAGAPON_AI_PROFILE",
    ):
        monkeypatch.delenv(name, raising=False)

    assert ai_profile() == "budget"
    assert rag_model() == "gemini-3.5-flash-lite"
    assert response_model() == "gemini-3.1-flash-lite"
    assert minutes_model() == "gemini-3.5-flash-lite"
    assert fast_model() == "gemini-3.1-flash-lite"
    assert knowledge_curation_model() == "gemini-3.1-flash-lite"
    assert knowledge_model() == "gemini-3.1-flash-lite"
    assert transcribe_model() == "gemini-3.5-transcribe"
    assert transcribe_fallback_model() == "gemini-3.5-flash-lite"
    assert audio_analysis_model() == "gemini-3.1-flash-lite"


def test_quality_profile_restores_high_quality_models(monkeypatch):
    monkeypatch.setenv("YAGAPON_AI_PROFILE", "quality")
    for name in (
        "YAGAPON_RAG_MODEL",
        "YAGAPON_RESPONSE_MODEL",
        "YAGAPON_MINUTES_MODEL",
        "YAGAPON_KNOWLEDGE_CURATION_MODEL",
        "YAGAPON_KNOWLEDGE_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)

    assert rag_model() == "gemini-3.8-flash"
    assert response_model() == "gemini-3.8-flash"
    assert minutes_model() == "gemini-3.8-flash"
    assert knowledge_curation_model() == "gemini-3.5-flash-lite"
    assert knowledge_model() == "gemini-3.8-flash"


def test_unknown_profile_fails_closed(monkeypatch):
    monkeypatch.setenv("YAGAPON_AI_PROFILE", "expensive-typo")
    monkeypatch.delenv("YAGAPON_RAG_MODEL", raising=False)

    with pytest.raises(ValueError, match="budget.*quality"):
        rag_model()


def test_models_can_be_rolled_back_without_code_change(monkeypatch):
    monkeypatch.setenv("YAGAPON_AI_PROFILE", "budget")
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
