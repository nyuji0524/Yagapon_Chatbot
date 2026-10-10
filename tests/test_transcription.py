from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot.transcription import transcribe_audio


class RateLimitError(RuntimeError):
    code = 429


@pytest.mark.asyncio
async def test_transcription_uses_dedicated_model_and_deletes_upload(monkeypatch):
    monkeypatch.setenv("YAGAPON_TRANSCRIBE_MODEL", "gemini-3.5-transcribe")
    uploaded = SimpleNamespace(name="files/test", uri="gs://test/audio.wav")
    create = AsyncMock(return_value=SimpleNamespace(output_text="spk_1: テスト"))
    client = SimpleNamespace(
        files=SimpleNamespace(upload=Mock(return_value=uploaded), delete=Mock()),
        aio=SimpleNamespace(interactions=SimpleNamespace(create=create)),
    )

    result = await transcribe_audio(
        client,
        b"RIFFtest",
        mime_type="audio/wav",
        display_name="meeting.wav",
        diarization=True,
    )

    assert result == "spk_1: テスト"
    assert create.await_args.kwargs["model"] == "gemini-3.5-transcribe"
    assert create.await_args.kwargs["generation_config"]["transcription_config"]["mode"] == {
        "type": "verbatim",
        "diarization_mode": "speaker",
    }
    client.files.delete.assert_called_once_with(name="files/test")


@pytest.mark.asyncio
async def test_transcription_deletes_upload_when_generation_fails():
    uploaded = SimpleNamespace(name="files/test", uri="gs://test/audio.wav")
    client = SimpleNamespace(
        files=SimpleNamespace(upload=Mock(return_value=uploaded), delete=Mock()),
        aio=SimpleNamespace(
            interactions=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("failed")))
        ),
    )

    with pytest.raises(RuntimeError, match="failed"):
        await transcribe_audio(
            client,
            b"RIFFtest",
            mime_type="audio/wav",
            display_name="meeting.wav",
        )

    client.files.delete.assert_called_once_with(name="files/test")


@pytest.mark.asyncio
async def test_transcription_retries_429_without_reuploading(monkeypatch):
    uploaded = SimpleNamespace(name="files/test", uri="gs://test/audio.wav")
    create = AsyncMock(
        side_effect=[
            RateLimitError("Too Many Requests"),
            SimpleNamespace(output_text="Speaker 1: 復旧しました"),
        ]
    )
    client = SimpleNamespace(
        files=SimpleNamespace(upload=Mock(return_value=uploaded), delete=Mock()),
        aio=SimpleNamespace(interactions=SimpleNamespace(create=create)),
    )
    sleep = AsyncMock()
    monkeypatch.setattr("bot.ai_retry.asyncio.sleep", sleep)

    result = await transcribe_audio(
        client,
        b"RIFFtest",
        mime_type="audio/wav",
        display_name="meeting.wav",
    )

    assert result == "Speaker 1: 復旧しました"
    assert create.await_count == 2
    client.files.upload.assert_called_once()
    client.files.delete.assert_called_once_with(name="files/test")
