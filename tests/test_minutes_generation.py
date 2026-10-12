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


@pytest.mark.asyncio
async def test_long_minutes_reuses_completed_segment_checkpoints(tmp_path, monkeypatch):
    first_generated = AsyncMock(
        side_effect=[
            SimpleNamespace(text="区間1"),
            SimpleNamespace(text="区間2"),
            RuntimeError("combine failed"),
        ]
    )
    first_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=first_generated))
    )
    monkeypatch.setattr("bot.minutes_generation.TRANSCRIPT_CHUNK_CHARS", 40)
    transcript = "[Speaker 1]: 最初の議題について詳しく話します\n[Speaker 2]: 次の議題について詳しく話します"

    with pytest.raises(RuntimeError, match="combine failed"):
        await generate_minutes(first_client, transcript, checkpoint_dir=tmp_path)

    assert len(list(tmp_path.glob("segment-*.json"))) == 2
    resumed_generate = AsyncMock(return_value=SimpleNamespace(text="# 復旧した議事録"))
    resumed_client = SimpleNamespace(
        aio=SimpleNamespace(models=SimpleNamespace(generate_content=resumed_generate))
    )

    result = await generate_minutes(resumed_client, transcript, checkpoint_dir=tmp_path)

    assert result == "# 復旧した議事録"
    assert resumed_generate.await_count == 1
    assert "区間1" in resumed_generate.await_args.kwargs["contents"]
