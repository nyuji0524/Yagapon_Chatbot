from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.commands.minutes import _generate_minutes_from_file
from bot.voice import VoiceMode, VoiceSession


@pytest.mark.asyncio
async def test_voice_audio_is_transcribed_before_response_generation():
    bot = SimpleNamespace()
    session = VoiceSession(bot, 1, SimpleNamespace(), VoiceMode.CHAT)
    session._transcribe_audio = AsyncMock(side_effect=["こんにちは", "質問です"])
    session._generate_realtime_response = AsyncMock(return_value="返答だぽん")

    result = await session._generate_response_from_audio(
        {
            10: ("A", b"audio-a"),
            20: ("B", b"audio-b"),
        }
    )

    assert result == ("[A]: こんにちは\n[B]: 質問です", "返答だぽん")
    session._generate_realtime_response.assert_awaited_once_with(
        [
            {"speaker": "A", "text": "こんにちは"},
            {"speaker": "B", "text": "質問です"},
        ]
    )


@pytest.mark.asyncio
async def test_uploaded_minutes_separates_transcription_and_formatting(monkeypatch):
    generated = AsyncMock(return_value=SimpleNamespace(text="# 会議"))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generated)))
    transcribe = AsyncMock(return_value="spk_1: 提案します")
    monkeypatch.setattr("bot.commands.minutes.genai.Client", lambda **kwargs: client)
    monkeypatch.setattr("bot.commands.minutes.transcribe_audio", transcribe)

    result = await _generate_minutes_from_file(
        b"audio",
        "audio/wav",
        "meeting.wav",
        "矢上祭: やがみさい",
        "定例会",
    )

    assert result == "# 会議"
    assert transcribe.await_args.kwargs["diarization"] is True
    assert generated.await_args.kwargs["model"] == "gemini-3.8-flash"
    assert "spk_1: 提案します" in generated.await_args.kwargs["contents"]
    assert "実名だと推測しない" in generated.await_args.kwargs["contents"]


@pytest.mark.asyncio
async def test_uploaded_minutes_retries_without_diarization(monkeypatch):
    generated = AsyncMock(return_value=SimpleNamespace(text="# 会議"))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generated)))
    transcribe = AsyncMock(side_effect=[RuntimeError("diarization limit"), "文字起こし"])
    monkeypatch.setattr("bot.commands.minutes.genai.Client", lambda **kwargs: client)
    monkeypatch.setattr("bot.commands.minutes.transcribe_audio", transcribe)

    result = await _generate_minutes_from_file(
        b"audio",
        "audio/wav",
        "long-meeting.wav",
        "",
        "",
    )

    assert result == "# 会議"
    assert transcribe.await_count == 2
    assert "diarization" not in transcribe.await_args.kwargs


@pytest.mark.asyncio
async def test_uploaded_minutes_does_not_retry_quota_errors(monkeypatch):
    client = SimpleNamespace()
    transcribe = AsyncMock(side_effect=RuntimeError("quota exceeded"))
    monkeypatch.setattr("bot.commands.minutes.genai.Client", lambda **kwargs: client)
    monkeypatch.setattr("bot.commands.minutes.transcribe_audio", transcribe)

    result = await _generate_minutes_from_file(
        b"audio",
        "audio/wav",
        "meeting.wav",
        "",
        "",
    )

    assert result is None
    assert transcribe.await_count == 1
