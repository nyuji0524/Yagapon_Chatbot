from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.minutes_generation import generate_minutes, normalize_speaker_labels, split_transcript


def test_normalize_speaker_labels():
    transcript = "spk_1: こんにちは\nSPEAKER-2: 賛成\nSpeaker 1: 続けます"

    assert normalize_speaker_labels(transcript) == (
        "Speaker 1: こんにちは\nSpeaker 2: 賛成\nSpeaker 1: 続けます"
    )


def test_split_transcript_preserves_all_text():
    transcript = "\n".join(f"[Speaker 1]: 発言{i}" for i in range(20))
    chunks = split_transcript(transcript, max_chars=80)

    assert len(chunks) > 1
    assert "\n".join(chunks) == transcript


@pytest.mark.asyncio
async def test_long_minutes_uses_segment_notes_and_combines(monkeypatch):
    generated = AsyncMock(
        side_effect=[
            SimpleNamespace(text="区間1"),
            SimpleNamespace(text="区間2"),
            SimpleNamespace(text="# 統合議事録"),
        ]
    )
    client = SimpleNamespace(aio=SimpleNamespace(models=SimpleNamespace(generate_content=generated)))
    monkeypatch.setattr("bot.minutes_generation.TRANSCRIPT_CHUNK_CHARS", 40)

    result = await generate_minutes(
        client,
        "[spk_1]: 最初の議題について詳しく話します\n[spk_2]: 次の議題について詳しく話します",
    )

    assert result == "# 統合議事録"
    assert generated.await_count == 3
    final_prompt = generated.await_args.kwargs["contents"]
    assert "区間1" in final_prompt
    assert "区間2" in final_prompt
    assert "後半の議題や少数意見も落とさず" in final_prompt
