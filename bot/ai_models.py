"""AIモデルの用途別設定。

モデル名を環境変数で上書きできるようにし、障害時にコード変更なしで
以前のモデルへ戻せるようにする。
"""

import os
from typing import Any

from google.genai import types

_PROFILE_MODELS = {
    "budget": {
        # Retrieval answers and final minutes are user-facing records, and both
        # must follow long, fail-closed evidence contracts. Lite models caused
        # valid retrievals to be discarded and omitted meeting decisions in
        # production. Keep the budget profile for high-volume/background work,
        # but retain a quality floor for these two critical paths.
        "rag": "gemini-3.8-flash",
        "response": "gemini-3.1-flash-lite",
        "minutes": "gemini-3.8-flash",
        "fast": "gemini-3.1-flash-lite",
        "curation": "gemini-3.1-flash-lite",
        "knowledge": "gemini-3.1-flash-lite",
        "transcribe": "gemini-3.5-transcribe",
        "transcribe_fallback": "gemini-3.5-flash-lite",
        "audio": "gemini-3.1-flash-lite",
    },
    "quality": {
        "rag": "gemini-3.8-flash",
        "response": "gemini-3.8-flash",
        "minutes": "gemini-3.8-flash",
        "fast": "gemini-3.1-flash-lite",
        "curation": "gemini-3.5-flash-lite",
        "knowledge": "gemini-3.8-flash",
        "transcribe": "gemini-3.5-transcribe",
        "transcribe_fallback": "gemini-3.5-flash-lite",
        "audio": "gemini-3.5-flash-lite",
    },
}


def ai_profile() -> str:
    profile = os.environ.get("YAGAPON_AI_PROFILE", "budget").strip().lower()
    if profile not in _PROFILE_MODELS:
        raise ValueError("YAGAPON_AI_PROFILE must be 'budget' or 'quality'")
    return profile


def _configured_model(variable: str, purpose: str) -> str:
    override = os.environ.get(variable, "").strip()
    if override:
        return override
    return _PROFILE_MODELS[ai_profile()][purpose]


def rag_model() -> str:
    return _configured_model("YAGAPON_RAG_MODEL", "rag")


def response_model() -> str:
    return _configured_model("YAGAPON_RESPONSE_MODEL", "response")


def minutes_model() -> str:
    """Long-form final minutes keep a separately tunable quality model."""
    return _configured_model("YAGAPON_MINUTES_MODEL", "minutes")


def fast_model() -> str:
    return _configured_model("YAGAPON_FAST_MODEL", "fast")


def knowledge_curation_model() -> str:
    """Non-urgent Knowledge curation model submitted through the Batch API."""
    return _configured_model("YAGAPON_KNOWLEDGE_CURATION_MODEL", "curation")


def knowledge_model() -> str:
    """One-off Drive and Discord source structuring model."""
    return _configured_model("YAGAPON_KNOWLEDGE_MODEL", "knowledge")


def transcribe_model() -> str:
    return _configured_model("YAGAPON_TRANSCRIBE_MODEL", "transcribe")


def transcribe_fallback_model() -> str:
    """Dedicated STT quota exhaustion and realtime voice fallback."""
    return _configured_model("YAGAPON_TRANSCRIBE_FALLBACK_MODEL", "transcribe_fallback")


def audio_analysis_model() -> str:
    """音声サンプルの音質確認専用。本人識別の権限根拠には使用しない。"""
    return _configured_model("YAGAPON_AUDIO_MODEL", "audio")


def generation_config(model: str, *, thinking_level: str | None = "low", **kwargs):
    """Gemini 3系ではthinking量を明示し、予期しない課金増を防ぐ。"""
    if thinking_level and model.startswith("gemini-3"):
        kwargs["thinking_config"] = types.ThinkingConfig(
            thinking_level=thinking_level,
        )
    return types.GenerateContentConfig(**kwargs)


def log_usage(logger, operation: str, model: str, response: Any) -> None:
    """Generate Content / Interactions両APIのtoken使用量を同じ形式で記録する。"""
    usage = getattr(response, "usage_metadata", None) or getattr(response, "usage", None)
    if usage is None:
        return
    input_tokens = getattr(usage, "prompt_token_count", None)
    if input_tokens is None:
        input_tokens = getattr(usage, "total_input_tokens", None)
    output_tokens = getattr(usage, "candidates_token_count", None)
    if output_tokens is None:
        output_tokens = getattr(usage, "total_output_tokens", None)
    thought_tokens = getattr(usage, "thoughts_token_count", None)
    if thought_tokens is None:
        thought_tokens = getattr(usage, "total_thought_tokens", None)
    total_tokens = getattr(usage, "total_token_count", None)
    if total_tokens is None:
        total_tokens = getattr(usage, "total_tokens", None)
    logger.info(
        "AI usage operation=%s model=%s input_tokens=%s output_tokens=%s thought_tokens=%s total_tokens=%s",
        operation,
        model,
        input_tokens,
        output_tokens,
        thought_tokens,
        total_tokens,
    )
