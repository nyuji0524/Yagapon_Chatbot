"""Gemini 3.5 Transcribeを使う共通文字起こし処理。"""

import asyncio
import io
import logging

from google.genai import types

from bot.ai_models import (
    generation_config,
    log_usage,
    transcribe_fallback_model,
    transcribe_model,
)
from bot.ai_retry import RateLimitCircuitBreaker, RateLimitExceeded, retry_rate_limit

log = logging.getLogger("yagapon.transcription")

# Gemini 3.5 Transcribe has a much smaller daily request quota than the general
# response model. Once it reports 429, keep all voice sessions on the fallback
# for a while rather than making every speaker/session probe the exhausted quota.
_dedicated_transcription_rate_limiter = RateLimitCircuitBreaker()


def _fallback_prompt(*, diarization: bool, custom_vocabulary: list[str] | None) -> str:
    instructions = [
        "この音声を日本語で逐語的に文字起こししてください。",
        "要約や説明、Markdownのコードブロックは付けず、発話内容だけを返してください。",
        "聞き取れない箇所は推測で補わず、[聞き取り不能] と記載してください。",
    ]
    if diarization:
        instructions.append(
            "話者が変わるたびに Speaker 1:、Speaker 2: のような匿名ラベルを付け、"
            "同じ話者には同じ番号を使ってください。実名は推測しないでください。"
        )
    if custom_vocabulary:
        terms = "、".join(custom_vocabulary[:100])
        instructions.append(f"固有名詞・用語の候補: {terms}")
    return "\n".join(instructions)


async def transcribe_audio(
    client,
    audio_bytes: bytes,
    *,
    mime_type: str,
    display_name: str,
    diarization: bool = False,
    custom_vocabulary: list[str] | None = None,
    prefer_dedicated: bool = True,
) -> str:
    """音声を一時アップロードし、専用STTまたはFlashで文字起こしする。"""
    loop = asyncio.get_running_loop()
    uploaded = None
    try:
        async def upload():
            return await loop.run_in_executor(
                None,
                lambda: client.files.upload(
                    file=io.BytesIO(audio_bytes),
                    config={"mime_type": mime_type, "display_name": display_name},
                ),
            )

        uploaded = await retry_rate_limit(
            upload,
            operation_name="transcription_upload",
        )

        if prefer_dedicated and _dedicated_transcription_rate_limiter.remaining() <= 0:
            transcription_options: dict = {"language_codes": ["ja-JP"]}
            if diarization:
                transcription_options["mode"] = {
                    "type": "verbatim",
                    "diarization_mode": "speaker",
                }
            elif custom_vocabulary:
                # diarizationとcustom_vocabularyはAPI上併用できない。
                transcription_options["custom_vocabulary"] = custom_vocabulary[:100]

            async def create_transcription():
                return await client.aio.interactions.create(
                    model=transcribe_model(),
                    input=[
                        {
                            "type": "audio",
                            "uri": uploaded.uri,
                            "mime_type": mime_type,
                        }
                    ],
                    generation_config={"transcription_config": transcription_options},
                )

            try:
                # Quota exhaustion will not recover in a few seconds. Fail over
                # immediately instead of making the voice loop wait and reupload.
                response = await retry_rate_limit(
                    create_transcription,
                    operation_name="transcription_create",
                    attempts=1,
                )
                _dedicated_transcription_rate_limiter.reset()
                model = transcribe_model()
                log_usage(log, "transcription", model, response)
                return (response.output_text or "").strip()
            except RateLimitExceeded as exc:
                cooldown = _dedicated_transcription_rate_limiter.trip(
                    retry_after=exc.retry_after
                )
                log.warning(
                    "Dedicated transcription quota exhausted; using fallback "
                    "cooldown=%.0fs failures=%s diagnostic=%s",
                    cooldown,
                    _dedicated_transcription_rate_limiter.failure_count,
                    exc.diagnostic,
                )
        elif prefer_dedicated:
            log.info(
                "Dedicated transcription is cooling down; using fallback remaining=%.0fs",
                _dedicated_transcription_rate_limiter.remaining(),
            )

        fallback_model = transcribe_fallback_model()

        async def create_fallback_transcription():
            return await client.aio.models.generate_content(
                model=fallback_model,
                contents=[
                    _fallback_prompt(
                        diarization=diarization,
                        custom_vocabulary=custom_vocabulary,
                    ),
                    types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime_type),
                ],
                config=generation_config(
                    fallback_model,
                    thinking_level="low",
                    max_output_tokens=8192,
                    temperature=0,
                ),
            )

        fallback_response = await retry_rate_limit(
            create_fallback_transcription,
            operation_name="transcription_fallback",
            attempts=2,
            initial_delay=5.0,
            max_delay=15.0,
        )
        log_usage(log, "transcription_fallback", fallback_model, fallback_response)
        return (fallback_response.text or "").strip()
    finally:
        if uploaded is not None:
            try:
                await loop.run_in_executor(
                    None,
                    lambda: client.files.delete(name=uploaded.name),
                )
            except Exception as exc:
                log.warning("Failed to delete uploaded audio %s: %s", uploaded.name, exc)
