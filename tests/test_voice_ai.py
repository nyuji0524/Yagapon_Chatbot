import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot import voice
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
async def test_voice_offsets_advance_only_after_successful_transcription():
    bot = SimpleNamespace()
    session = VoiceSession(bot, 1, SimpleNamespace(), VoiceMode.CHAT)
    session._transcribe_audio = AsyncMock(side_effect=[None, "成功"])
    session._generate_realtime_response = AsyncMock(return_value=None)

    await session._generate_response_from_audio(
        {10: ("失敗", b"a"), 20: ("成功", b"b")},
        {10: 100, 20: 200},
    )

    assert 10 not in session._last_audio_len
    assert session._last_audio_len[20] == 200


@pytest.mark.asyncio
async def test_stop_is_idempotent_for_concurrent_leave_paths():
    session = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(), VoiceMode.CHAT)

    async def slow_stop():
        await asyncio.sleep(0.01)
        return "done"

    session._stop_once = AsyncMock(side_effect=slow_stop)

    assert await asyncio.gather(session.stop(), session.stop()) == ["done", "done"]
    session._stop_once.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_leave_releases_guild_session_for_retry():
    session = VoiceSession(SimpleNamespace(), 123, SimpleNamespace(), VoiceMode.LISTEN)
    session._stop_once = AsyncMock(side_effect=RuntimeError("Gemini unavailable"))
    voice._sessions[123] = session

    with pytest.raises(RuntimeError, match="Gemini unavailable"):
        await voice.leave_voice(123)
    await asyncio.sleep(0)

    assert voice.get_session(123) is None
    assert session._stop_completed is True


@pytest.mark.asyncio
async def test_cancelled_leave_caller_does_not_orphan_session():
    started = asyncio.Event()
    finish = asyncio.Event()
    session = VoiceSession(SimpleNamespace(), 124, SimpleNamespace(), VoiceMode.CHAT)

    async def slow_stop():
        started.set()
        await finish.wait()
        return None

    session._stop_once = AsyncMock(side_effect=slow_stop)
    voice._sessions[124] = session
    caller = asyncio.create_task(voice.leave_voice(124))
    await started.wait()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller

    stop_task = session._stop_task
    finish.set()
    await stop_task
    await asyncio.sleep(0)

    assert voice.get_session(124) is None


@pytest.mark.asyncio
async def test_drain_recording_processes_bounded_chunks_until_caught_up():
    guild = SimpleNamespace(get_member=lambda user_id: SimpleNamespace(display_name=f"user-{user_id}"))
    session = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(guild=guild), VoiceMode.LISTEN)
    session.recording_sink = SimpleNamespace(
        read_chunks=Mock(
            side_effect=[
                {42: (b"first", 5)},
                {42: (b"second", 11)},
                {},
            ]
        ),
        mark=Mock(),
    )
    session._transcribe_audio = AsyncMock(side_effect=["最初", "次"])

    await session._drain_recording()

    assert session._last_audio_len == {42: 11}
    assert session.transcript == ["[user-42]: 最初", "[user-42]: 次"]
    assert session.recording_sink.read_chunks.call_count == 3


def test_failed_transcription_recording_is_retained_after_notification():
    session = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(), VoiceMode.LISTEN)
    session.recording_sink = SimpleNamespace(limit_reached=False, mark=Mock())
    session._transcription_failed = True

    session.mark_delivered(drive_url="https://docs.example/minutes")

    session.recording_sink.mark.assert_called_once_with(
        "delivered_recording_retained",
        drive_url="https://docs.example/minutes",
        transcription_failed=True,
        storage_limit_reached=False,
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
