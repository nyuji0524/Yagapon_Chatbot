import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from bot import voice
from bot.commands.minutes import _generate_minutes_from_file
from bot.recording import PersistentPCMSink, RecoverableRecording
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
async def test_successful_transcription_checkpoints_offsets():
    session = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(), VoiceMode.CHAT)
    session.recording_sink = SimpleNamespace(checkpoint=Mock(), save_transcript=Mock(return_value=None))
    session._transcribe_audio = AsyncMock(return_value="成功")
    session._generate_realtime_response = AsyncMock(return_value=None)

    await session._generate_response_from_audio({20: ("Speaker 1", b"b")}, {20: 200})

    session.recording_sink.checkpoint.assert_called_once()
    assert session.recording_sink.checkpoint.call_args.kwargs["offsets"] == {20: 200}


@pytest.mark.asyncio
async def test_recovery_keeps_failed_429_chunk_for_later(monkeypatch, tmp_path):
    session_dir = tmp_path / "recordings" / "1" / "session"
    sink = PersistentPCMSink(1, "meeting", "chat", session_dir=session_dir, max_bytes=100_000)
    sink.write(b"x" * 2000, 42)
    sink.cleanup()
    candidate = RecoverableRecording(session_dir, sink.read_metadata())
    operations = SimpleNamespace(
        record_event=Mock(), dead_letter=Mock(), resolve_dead_letter=Mock()
    )
    bot = SimpleNamespace(operations=operations)
    monkeypatch.setattr(VoiceSession, "_transcribe_audio", AsyncMock(return_value=None))

    await voice._recover_recording(bot, candidate)

    metadata = sink.read_metadata()
    assert metadata["status"] == "recovery_retry"
    assert metadata.get("offsets", {}) == {}
    assert (session_dir / "user-42.pcm").exists()


@pytest.mark.asyncio
async def test_recovery_batch_stops_after_rate_limit(monkeypatch, tmp_path):
    candidates = [
        RecoverableRecording(tmp_path / "one", {}),
        RecoverableRecording(tmp_path / "two", {}),
    ]
    recover = AsyncMock()

    async def trip_after_first(*_args):
        voice._transcription_rate_limiter.trip(retry_after=60)

    recover.side_effect = trip_after_first
    monkeypatch.setattr(voice, "find_recoverable_recordings", lambda: candidates)
    monkeypatch.setattr(voice, "_recover_recording", recover)
    voice._transcription_rate_limiter.reset()
    try:
        await voice.recover_incomplete_recordings(SimpleNamespace())
    finally:
        voice._transcription_rate_limiter.reset()

    assert recover.await_count == 1


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
        save_transcript=Mock(return_value=None),
    )
    session._transcribe_audio = AsyncMock(side_effect=["最初", "次"])

    await session._drain_recording()

    assert session._last_audio_len == {42: 11}
    assert session.transcript == [
        "[Discord確定: user-42]: 最初",
        "[Discord確定: user-42]: 次",
    ]
    assert session.recording_sink.read_chunks.call_count == 3


def test_failed_transcription_recording_is_retained_after_notification():
    session = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(), VoiceMode.LISTEN)
    session.recording_sink = SimpleNamespace(
        limit_reached=False,
        total_bytes=100,
        mark=Mock(),
        delete_audio=Mock(),
    )
    session._transcription_failed = True

    session.mark_delivered(drive_url="https://docs.example/minutes")

    session.recording_sink.mark.assert_called_once_with(
        "delivered_recording_retained",
        drive_url="https://docs.example/minutes",
        transcript_url="",
        transcript_path="",
        transcription_failed=True,
        storage_limit_reached=False,
        transcript_saved=False,
    )
    session.recording_sink.delete_audio.assert_not_called()


def test_successful_delivery_deletes_audio_after_transcript_is_saved(tmp_path):
    transcript_path = tmp_path / "transcript.md"
    transcript_path.write_text("文字起こし", encoding="utf-8")
    session = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(), VoiceMode.LISTEN)
    session._transcript_path = transcript_path
    session.recording_sink = SimpleNamespace(
        session_dir=tmp_path / "session",
        limit_reached=False,
        total_bytes=100,
        mark=Mock(),
        delete_audio=Mock(return_value=1),
    )

    session.mark_delivered(
        drive_url="https://docs.example/minutes",
        transcript_url="https://docs.example/transcript",
    )

    session.recording_sink.mark.assert_called_once_with(
        "delivered_audio_deleted",
        drive_url="https://docs.example/minutes",
        transcript_url="https://docs.example/transcript",
        transcript_path=str(transcript_path),
    )
    session.recording_sink.delete_audio.assert_called_once_with()


def test_listen_mode_uses_five_minute_large_chunks():
    listen = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(), VoiceMode.LISTEN)
    chat = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(), VoiceMode.CHAT)

    assert voice.REALTIME_INTERVAL_LISTEN == 300
    assert listen._transcription_chunk_bytes() == 50 * 1024 * 1024
    assert chat._transcription_chunk_bytes() == 5 * 1024 * 1024


@pytest.mark.asyncio
async def test_uploaded_minutes_separates_transcription_and_formatting(monkeypatch):
    generated = AsyncMock(return_value=SimpleNamespace(text="# 会議"))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generated)))
    transcribe = AsyncMock(return_value="spk_1: 提案します")
    monkeypatch.setattr("bot.commands.minutes.new_genai_client", lambda _api_key: client)
    monkeypatch.setattr("bot.commands.minutes.transcribe_audio", transcribe)

    result = await _generate_minutes_from_file(
        b"audio",
        "audio/wav",
        "meeting.wav",
        "矢上祭: やがみさい",
        "定例会",
        custom_vocabulary=["矢上祭", "やがみさい", "矢実"],
    )

    assert result == "# 会議"
    assert transcribe.await_args.kwargs["diarization"] is True
    assert transcribe.await_args.kwargs["custom_vocabulary"] == [
        "矢上祭",
        "やがみさい",
        "矢実",
    ]
    assert generated.await_args.kwargs["model"] == "gemini-3.8-flash"
    assert "Speaker 1: 提案します" in generated.await_args.kwargs["contents"]
    prompt = generated.await_args.kwargs["contents"]
    assert "## 今日の議題" in prompt
    assert "Speakerラベルから実名を推測しない" in prompt


@pytest.mark.asyncio
async def test_voice_assigns_stable_discord_id_labels():
    guild = SimpleNamespace(get_member=lambda _user_id: None)
    session = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(guild=guild), VoiceMode.LISTEN)
    session.recording_sink = SimpleNamespace(
        read_chunks=Mock(
            side_effect=[
                {20: (b"first-b", 7), 10: (b"first-a", 7)},
                {10: (b"second-a", 15)},
                {},
            ]
        ),
        mark=Mock(),
        save_transcript=Mock(return_value=None),
    )
    session._transcribe_audio = AsyncMock(side_effect=["Bです", "Aです", "Aの続き"])

    await session._drain_recording()

    assert session.transcript == [
        "[Discord確定: User 20]: Bです",
        "[Discord確定: User 10]: Aです",
        "[Discord確定: User 10]: Aの続き",
    ]


def test_voice_uses_registered_name_when_member_cache_is_unavailable():
    config = SimpleNamespace(
        get_member=lambda _guild_id, user_id: {"name": "毛蟹"} if user_id == 42 else None
    )
    session = VoiceSession(
        SimpleNamespace(config=config),
        1,
        SimpleNamespace(guild=SimpleNamespace(get_member=lambda _user_id: None)),
        VoiceMode.LISTEN,
    )

    assert session._speaker_label(42) == "Discord確定: 毛蟹"


@pytest.mark.asyncio
async def test_final_reprocess_replaces_incremental_transcript_only_after_full_success():
    guild = SimpleNamespace(
        get_member=lambda user_id: SimpleNamespace(display_name=f"member-{user_id}")
    )
    session = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(guild=guild), VoiceMode.LISTEN)
    session.transcript = ["[Discord確定: member-10]: 途中結果"]
    session._transcription_failed = True
    session.recording_sink = SimpleNamespace(
        total_bytes=4000,
        read_chunks=Mock(
            side_effect=[
                {10: (b"first", 5), 20: (b"second", 6)},
                {},
            ]
        ),
        mark=Mock(),
        save_transcript=Mock(return_value=None),
        checkpoint=Mock(),
    )
    session._transcribe_audio = AsyncMock(side_effect=["確定A", "確定B"])

    assert await session._reprocess_complete_recording() is True

    assert session.transcript == [
        "[Discord確定: member-10]: 確定A",
        "[Discord確定: member-20]: 確定B",
    ]
    assert session._last_audio_len == {10: 5, 20: 6}
    assert session._transcription_failed is False
    assert all(
        call.kwargs["prefer_dedicated"] is True
        and call.kwargs["diarization"] is False
        for call in session._transcribe_audio.await_args_list
    )


@pytest.mark.asyncio
async def test_final_reprocess_keeps_incremental_transcript_when_a_chunk_fails():
    session = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(), VoiceMode.LISTEN)
    session.transcript = ["[Discord確定: User 10]: 保存済み"]
    session.recording_sink = SimpleNamespace(
        total_bytes=4000,
        read_chunks=Mock(return_value={10: (b"audio", 5)}),
        mark=Mock(),
    )
    session._transcribe_audio = AsyncMock(return_value=None)

    assert await session._reprocess_complete_recording() is False
    assert session.transcript == ["[Discord確定: User 10]: 保存済み"]
    session.recording_sink.mark.assert_called_with(
        "final_reprocess_deferred",
        final_reprocess_offsets={},
    )


def test_listen_mode_separates_multiple_people_using_one_discord_account():
    session = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(), VoiceMode.LISTEN)

    first = session._format_transcribed_text(
        42,
        "Speaker 1: 司会です\nSpeaker 2: 企画担当です\nSpeaker 1: 次へ進みます",
        diarized=True,
    )
    second = session._format_transcribed_text(
        42,
        "Speaker 2: 補足します",
        diarized=True,
    )

    assert first == (
        "[Speaker 1]: 司会です\n"
        "[Speaker 2]: 企画担当です\n"
        "[Speaker 1]: 次へ進みます"
    )
    # A later STT request restarts its local numbering, so it must not be
    # silently merged with Speaker 2 from the previous chunk.
    assert second == "[Speaker 3]: 補足します"


def test_listen_mode_does_not_merge_chunk_local_speaker_numbers():
    session = VoiceSession(SimpleNamespace(), 1, SimpleNamespace(), VoiceMode.LISTEN)

    first = session._format_transcribed_text(
        42, "Speaker 1: 最初の話者", diarized=True
    )
    second = session._format_transcribed_text(
        42, "Speaker 1: 次のチャンクの話者", diarized=True
    )

    assert first == "[Speaker 1]: 最初の話者"
    assert second == "[Speaker 2]: 次のチャンクの話者"


def test_listen_mode_preserves_only_registered_voiceprint_names(monkeypatch):
    session = VoiceSession(SimpleNamespace(config=SimpleNamespace()), 1, SimpleNamespace(), VoiceMode.LISTEN)
    session._voiceprint_paths = {"中山": "/data/voiceprints/1.wav"}

    result = session._format_transcribed_text(
        42,
        "中山: 登録者です\nSpeaker 1: 未登録者です",
        diarized=True,
    )

    assert result == "[中山]: 登録者です\n[Speaker 1]: 未登録者です"


def test_listen_mode_accepts_full_width_colons_in_speaker_labels():
    session = VoiceSession(SimpleNamespace(config=SimpleNamespace()), 1, SimpleNamespace(), VoiceMode.LISTEN)
    session._voiceprint_paths = {"中山": "/data/voiceprints/1.wav"}

    result = session._format_transcribed_text(
        42,
        "中山： 登録者です\nSpeaker 1： 未登録者です",
        diarized=True,
    )

    assert result == "[中山]: 登録者です\n[Speaker 1]: 未登録者です"


@pytest.mark.asyncio
async def test_uploaded_minutes_retries_without_diarization(monkeypatch):
    generated = AsyncMock(return_value=SimpleNamespace(text="# 会議"))
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generated)))
    transcribe = AsyncMock(side_effect=[RuntimeError("diarization limit"), "文字起こし"])
    monkeypatch.setattr("bot.commands.minutes.new_genai_client", lambda _api_key: client)
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
    monkeypatch.setattr("bot.commands.minutes.new_genai_client", lambda _api_key: client)
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
