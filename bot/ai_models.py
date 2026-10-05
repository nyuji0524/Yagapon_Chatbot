"""AIモデルの用途別設定。

モデル名を環境変数で上書きできるようにし、障害時にコード変更なしで
以前のモデルへ戻せるようにする。
"""

import os
from typing import Any

from google.genai import types


def rag_model() -> str:
    return os.environ.get("YAGAPON_RAG_MODEL", "gemini-3.8-flash")


def response_model() -> str:
    return os.environ.get("YAGAPON_RESPONSE_MODEL", "gemini-3.8-flash")


def fast_model() -> str:
    return os.environ.get("YAGAPON_FAST_MODEL", "gemini-3.1-flash-lite")


def transcribe_model() -> str:
    return os.environ.get("YAGAPON_TRANSCRIBE_MODEL", "gemini-3.5-transcribe")


def audio_analysis_model() -> str:
    """音声サンプルの音質確認専用。本人識別の権限根拠には使用しない。"""
    return os.environ.get("YAGAPON_AUDIO_MODEL", "gemini-3.8-flash")


def review_model() -> str:
    return os.environ.get("YAGAPON_REVIEW_MODEL", "gemini-3.8-flash")


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
