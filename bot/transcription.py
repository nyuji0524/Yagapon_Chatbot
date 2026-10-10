"""Gemini 3.5 Transcribeを使う共通文字起こし処理。"""

import asyncio
import io
import logging

from bot.ai_models import log_usage, transcribe_model
from bot.ai_retry import retry_rate_limit

log = logging.getLogger("yagapon.transcription")


async def transcribe_audio(
    client,
    audio_bytes: bytes,
    *,
    mime_type: str,
    display_name: str,
    diarization: bool = False,
    custom_vocabulary: list[str] | None = None,
) -> str:
    """音声を一時アップロードし、必ず削除して文字起こしを返す。"""
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

        response = await retry_rate_limit(
            create_transcription,
            operation_name="transcription_create",
        )
        log_usage(log, "transcription", transcribe_model(), response)
        return (response.output_text or "").strip()
    finally:
        if uploaded is not None:
            try:
                await loop.run_in_executor(
                    None,
                    lambda: client.files.delete(name=uploaded.name),
                )
            except Exception as exc:
                log.warning("Failed to delete uploaded audio %s: %s", uploaded.name, exc)
