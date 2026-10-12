from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot import transcription
from bot.transcription import transcribe_audio


class RateLimitError(RuntimeError):
    code = 429


@pytest.fixture(autouse=True)
def reset_dedicated_transcription_circuit():
    transcription._dedicated_transcription_rate_limiter.reset()
    yield
    transcription._dedicated_transcription_rate_limiter.reset()


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
async def test_transcription_falls_back_on_429_without_reuploading(monkeypatch):
    uploaded = SimpleNamespace(name="files/test", uri="gs://test/audio.wav")
    create = AsyncMock(side_effect=RateLimitError("Too Many Requests"))
    fallback = AsyncMock(
        return_value=SimpleNamespace(text="Speaker 1: 復旧しました")
    )
    client = SimpleNamespace(
        files=SimpleNamespace(upload=Mock(return_value=uploaded), delete=Mock()),
        aio=SimpleNamespace(
            interactions=SimpleNamespace(create=create),
            models=SimpleNamespace(generate_content=fallback),
        ),
    )

    result = await transcribe_audio(
        client,
        b"RIFFtest",
        mime_type="audio/wav",
        display_name="meeting.wav",
    )

    assert result == "Speaker 1: 復旧しました"
    create.assert_awaited_once()
    fallback.assert_awaited_once()
    assert fallback.await_args.kwargs["model"] == "gemini-3.8-flash"
    assert transcription._dedicated_transcription_rate_limiter.remaining() > 0
    client.files.upload.assert_called_once()
    client.files.delete.assert_called_once_with(name="files/test")


@pytest.mark.asyncio
async def test_transcription_skips_dedicated_during_shared_cooldown():
    transcription._dedicated_transcription_rate_limiter.trip()
    uploaded = SimpleNamespace(name="files/test", uri="gs://test/audio.wav")
    create = AsyncMock()
    fallback = AsyncMock(return_value=SimpleNamespace(text="フォールバック"))
    client = SimpleNamespace(
        files=SimpleNamespace(upload=Mock(return_value=uploaded), delete=Mock()),
        aio=SimpleNamespace(
            interactions=SimpleNamespace(create=create),
            models=SimpleNamespace(generate_content=fallback),
        ),
    )

    result = await transcribe_audio(
        client,
        b"RIFFtest",
        mime_type="audio/wav",
        display_name="meeting.wav",
    )

    assert result == "フォールバック"
    create.assert_not_awaited()
    fallback.assert_awaited_once()


@pytest.mark.asyncio
async def test_transcription_can_reserve_dedicated_quota_for_listen_mode():
    uploaded = SimpleNamespace(name="files/test", uri="gs://test/audio.wav")
    create = AsyncMock()
    fallback = AsyncMock(return_value=SimpleNamespace(text="短時間モード"))
    client = SimpleNamespace(
        files=SimpleNamespace(upload=Mock(return_value=uploaded), delete=Mock()),
        aio=SimpleNamespace(
            interactions=SimpleNamespace(create=create),
            models=SimpleNamespace(generate_content=fallback),
        ),
    )

    result = await transcribe_audio(
        client,
        b"RIFFtest",
        mime_type="audio/wav",
        display_name="chat.wav",
        prefer_dedicated=False,
        custom_vocabulary=["矢上祭"],
    )

    assert result == "短時間モード"
    create.assert_not_awaited()
    prompt = fallback.await_args.kwargs["contents"][0]
    assert "矢上祭" in prompt


@pytest.mark.asyncio
async def test_transcription_uses_enrolled_voiceprints_for_speaker_names(tmp_path):
    sample = tmp_path / "member.wav"
    sample.write_bytes(b"RIFF" + b"voice" * 300)
    meeting = SimpleNamespace(name="files/meeting", uri="gs://test/meeting.wav")
    reference = SimpleNamespace(name="files/reference", uri="gs://test/reference.wav")
    upload = Mock(side_effect=[meeting, reference])
    create = AsyncMock()
    fallback = AsyncMock(
        return_value=SimpleNamespace(text="Registered 1: 提案します\nSpeaker 1: 賛成です")
    )
    client = SimpleNamespace(
        files=SimpleNamespace(upload=upload, delete=Mock()),
        aio=SimpleNamespace(
            interactions=SimpleNamespace(create=create),
            models=SimpleNamespace(generate_content=fallback),
        ),
    )

    result = await transcribe_audio(
        client,
        b"RIFFmeeting",
        mime_type="audio/wav",
        display_name="meeting.wav",
        diarization=True,
        voiceprint_paths={"中山": str(sample)},
    )

    assert result == "中山: 提案します\nSpeaker 1: 賛成です"
    create.assert_not_awaited()
    contents = fallback.await_args.kwargs["contents"]
    assert "登録声紋" in contents[0]
    assert "Registered 1" in contents[1]
    assert upload.call_count == 2
    assert client.files.delete.call_count == 2
