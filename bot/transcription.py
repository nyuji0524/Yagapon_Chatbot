"""Gemini 3.5 Transcribeを使う共通文字起こし処理。"""

import array
import asyncio
import io
import logging
import re
import sys
import wave
from pathlib import Path

from google.genai import types

from bot.ai_completion import IncompleteModelOutput, require_complete_model_output
from bot.ai_models import (
    generation_config,
    log_usage,
    transcribe_fallback_model,
    transcribe_model,
)
from bot.ai_retry import (
    RateLimitCircuitBreaker,
    RateLimitExceeded,
    retry_rate_limit,
    retry_transient_api,
)

log = logging.getLogger("yagapon.transcription")

# Gemini 3.5 Transcribe has a much smaller daily request quota than the general
# response model. Once it reports 429, keep all voice sessions on the fallback
# for a while rather than making every speaker/session probe the exhausted quota.
_dedicated_transcription_rate_limiter = RateLimitCircuitBreaker()


def _pcm_chunk_has_signal(data: bytes, sample_width: int) -> bool:
    """Conservatively distinguish digital silence from recorded PCM."""
    if not data:
        return False
    if sample_width == 1:
        return any(abs(sample - 128) > 2 for sample in data)
    if sample_width == 2:
        usable = len(data) - (len(data) % 2)
        samples = array.array("h")
        samples.frombytes(data[:usable])
        if sys.byteorder != "little":
            samples.byteswap()
        return any(abs(sample) > 32 for sample in samples)
    if sample_width == 3:
        return any(
            abs(int.from_bytes(data[index : index + 3], "little", signed=True)) > 8192
            for index in range(0, len(data) - 2, 3)
        )
    if sample_width == 4:
        usable = len(data) - (len(data) % 4)
        samples = array.array("i")
        samples.frombytes(data[:usable])
        if sys.byteorder != "little":
            samples.byteswap()
        return any(abs(sample) > 2_097_152 for sample in samples)
    # Unknown PCM layouts are unsafe to call silent when they carry payload.
    return bool(data)


def _audio_has_nontrivial_signal(audio_bytes: bytes, mime_type: str) -> bool:
    """Return true when an empty STT result would risk dropping real audio."""
    if len(audio_bytes) < 1000:
        return False
    if "wav" not in mime_type.casefold() and not audio_bytes.startswith(b"RIFF"):
        return True
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as stream:
            sample_width = stream.getsampwidth()
            while chunk := stream.readframes(8192):
                if _pcm_chunk_has_signal(chunk, sample_width):
                    return True
            return False
    except (EOFError, ValueError, wave.Error):
        # A sizeable malformed/unknown payload is not proof of silence.  Fail
        # closed so callers retain it for recovery rather than advancing the
        # durable offset and deleting the recording.
        return True


def _require_transcription_text(
    text: str,
    *,
    audio_bytes: bytes,
    mime_type: str,
    operation: str,
) -> str:
    normalized = text.strip()
    if not normalized and _audio_has_nontrivial_signal(audio_bytes, mime_type):
        raise IncompleteModelOutput(operation, "EMPTY_OUTPUT_WITH_AUDIO")
    return normalized


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


def _voiceprint_prompt(reference_count: int, custom_vocabulary: list[str] | None) -> str:
    instructions = [
        "最後に示す会議音声を、日本語で逐語的に文字起こししてください。",
        "その前に並ぶ音声は、Registered 1 から順に対応する登録声紋です。",
        "会議音声の声が登録声紋と十分一致するときだけ Registered N: を付けてください。",
        "一致を確信できない話者には Speaker 1:、Speaker 2: のような匿名ラベルを付け、",
        "話者が変わるたびに改行してください。同じ音声内の同じ話者には同じラベルを使ってください。",
        "名前を会話内容から推測したり、似ているだけの声を登録者と断定したりしないでください。",
        "要約、説明、Markdownコードブロック、信頼度は付けず、発話内容だけを返してください。",
        "聞き取れない箇所は推測で補わず、[聞き取り不能] と記載してください。",
        f"登録声紋数: {reference_count}",
    ]
    if custom_vocabulary:
        instructions.append(f"固有名詞・用語の候補: {'、'.join(custom_vocabulary[:100])}")
    return "\n".join(instructions)


def _replace_registered_labels(text: str, labels: dict[int, str]) -> str:
    """Replace opaque model labels with configured display names only at turn starts."""
    pattern = re.compile(r"(?im)^(\s*\[?)registered[ _-]?(\d+)(\]?\s*[:：])")

    def replace(match: re.Match) -> str:
        label = labels.get(int(match.group(2)))
        return f"{match.group(1)}{label}{match.group(3)}" if label else match.group(0)

    return pattern.sub(replace, text)


async def transcribe_audio(
    client,
    audio_bytes: bytes,
    *,
    mime_type: str,
    display_name: str,
    diarization: bool = False,
    custom_vocabulary: list[str] | None = None,
    prefer_dedicated: bool = True,
    voiceprint_paths: dict[str, str] | None = None,
) -> str:
    """音声を一時アップロードし、専用STTまたはFlashで文字起こしする。"""
    uploaded = None
    uploaded_voiceprints = []
    reference_voiceprints = []
    try:
        async def upload():
            return await client.aio.files.upload(
                file=io.BytesIO(audio_bytes),
                config={"mime_type": mime_type, "display_name": display_name},
            )

        uploaded = await retry_rate_limit(
            upload,
            operation_name="transcription_upload",
        )

        # The dedicated transcription endpoint cannot receive enrolled reference
        # clips. When voiceprints exist, use the multimodal model so the samples
        # are actually part of speaker attribution instead of being a UI-only flag.
        use_voiceprints = bool(voiceprint_paths) and diarization
        if prefer_dedicated and not use_voiceprints and _dedicated_transcription_rate_limiter.remaining() <= 0:
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
                response = await retry_transient_api(
                    lambda: retry_rate_limit(
                        create_transcription,
                        operation_name="transcription_create",
                        attempts=1,
                    ),
                    operation_name="transcription_create_transient",
                    attempts=3,
                    initial_delay=2,
                    max_delay=10,
                )
                _dedicated_transcription_rate_limiter.reset()
                model = transcribe_model()
                log_usage(log, "transcription", model, response)
                require_complete_model_output(response, operation="transcription_create")
                if not (response.output_text or "").strip():
                    # Even digital silence gets one fallback interpretation.
                    # The fallback signal check still permits a genuinely
                    # silent recording to complete with an empty transcript.
                    raise IncompleteModelOutput("transcription_create", "EMPTY_OUTPUT")
                return _require_transcription_text(
                    response.output_text or "",
                    audio_bytes=audio_bytes,
                    mime_type=mime_type,
                    operation="transcription_create",
                )
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
            except IncompleteModelOutput as exc:
                # The dedicated endpoint occasionally reports a completed but
                # empty result, or an explicit incomplete status. Discard its
                # partial output and make one independent fallback attempt
                # before declaring the durable PCM unrecoverable.
                log.warning(
                    "Dedicated transcription was incomplete; using fallback operation=%s reason=%s",
                    exc.operation,
                    exc.finish_reason,
                )
        elif prefer_dedicated and not use_voiceprints:
            log.info(
                "Dedicated transcription is cooling down; using fallback remaining=%.0fs",
                _dedicated_transcription_rate_limiter.remaining(),
            )

        fallback_model = transcribe_fallback_model()

        voiceprint_labels: dict[int, str] = {}
        if use_voiceprints:
            valid_voiceprints = []
            for name, path_value in list(voiceprint_paths.items())[:16]:
                path = Path(path_value)
                if not path.is_file() or path.stat().st_size <= 1000:
                    continue
                valid_voiceprints.append((name, path))

            upload_slots = asyncio.Semaphore(4)

            async def upload_voiceprint(index: int, name: str, path: Path):
                async with upload_slots:
                    sample = await retry_rate_limit(
                        lambda: client.aio.files.upload(
                            file=str(path),
                            config={
                                "mime_type": "audio/wav",
                                "display_name": f"voiceprint-{index}",
                            },
                        ),
                        operation_name="voiceprint_upload",
                    )
                    # Register immediately so a sibling upload failure cannot
                    # orphan an already-created remote Files API resource.
                    uploaded_voiceprints.append(sample)
                    return index, name, sample

            # Wait for every upload before propagating a sibling failure so
            # every successfully created remote file is included in cleanup.
            upload_batch = asyncio.gather(
                *(
                    upload_voiceprint(index, name, path)
                    for index, (name, path) in enumerate(valid_voiceprints, start=1)
                ),
                return_exceptions=True,
            )
            try:
                # Let the async upload batch settle so every successfully
                # created remote file is registered for cleanup.
                results = await asyncio.shield(upload_batch)
            except asyncio.CancelledError:
                await upload_batch
                raise

            upload_error = next(
                (result for result in results if isinstance(result, BaseException)),
                None,
            )
            if upload_error is not None:
                raise upload_error
            for index, name, sample in results:
                reference_voiceprints.append(sample)
                voiceprint_labels[index] = name

        async def create_fallback_transcription():
            if reference_voiceprints:
                contents = [_voiceprint_prompt(len(reference_voiceprints), custom_vocabulary)]
                for index, sample in enumerate(reference_voiceprints, start=1):
                    contents.extend([
                        f"登録声紋 Registered {index}:",
                        types.Part.from_uri(file_uri=sample.uri, mime_type="audio/wav"),
                    ])
                contents.extend([
                    "文字起こし対象の会議音声:",
                    types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime_type),
                ])
            else:
                contents = [
                    _fallback_prompt(
                        diarization=diarization,
                        custom_vocabulary=custom_vocabulary,
                    ),
                    types.Part.from_uri(file_uri=uploaded.uri, mime_type=mime_type),
                ]
            return await client.aio.models.generate_content(
                model=fallback_model,
                contents=contents,
                config=generation_config(
                    fallback_model,
                    thinking_level="low",
                    max_output_tokens=8192,
                    temperature=0,
                ),
            )

        fallback_response = await retry_transient_api(
            lambda: retry_rate_limit(
                create_fallback_transcription,
                operation_name="transcription_fallback",
                attempts=2,
                initial_delay=5.0,
                max_delay=15.0,
            ),
            operation_name="transcription_fallback_transient",
            attempts=3,
            initial_delay=2,
            max_delay=10,
        )
        log_usage(log, "transcription_fallback", fallback_model, fallback_response)
        require_complete_model_output(
            fallback_response,
            operation="transcription_fallback",
        )
        text = _require_transcription_text(
            fallback_response.text or "",
            audio_bytes=audio_bytes,
            mime_type=mime_type,
            operation="transcription_fallback",
        )
        return _replace_registered_labels(text, voiceprint_labels)
    finally:
        for temporary in [uploaded, *uploaded_voiceprints]:
            if temporary is None:
                continue
            try:
                await client.aio.files.delete(name=temporary.name)
            except Exception as exc:
                log.warning("Failed to delete uploaded audio %s: %s", temporary.name, exc)
