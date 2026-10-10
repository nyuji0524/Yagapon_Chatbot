"""High-quality, long-form meeting minutes generation."""

import logging
import re

from bot.ai_models import generation_config, log_usage, response_model
from bot.ai_retry import retry_rate_limit

log = logging.getLogger("yagapon.minutes_generation")

TRANSCRIPT_CHUNK_CHARS = 14_000


def normalize_speaker_labels(transcript: str) -> str:
    """Normalize diarization labels without guessing real identities."""
    patterns = (
        r"(?i)\bspk[_ -]?(\d+)\b",
        r"(?i)\bspeaker[_ -]?(\d+)\b",
    )
    normalized = transcript
    for pattern in patterns:
        normalized = re.sub(pattern, lambda match: f"Speaker {int(match.group(1))}", normalized)
    return normalized


def split_transcript(transcript: str, max_chars: int | None = None) -> list[str]:
    """Split at transcript lines while bounding pathological long lines."""
    if not transcript.strip():
        return []
    if max_chars is None:
        max_chars = TRANSCRIPT_CHUNK_CHARS
    chunks: list[str] = []
    current: list[str] = []
    current_size = 0
    for line in transcript.splitlines():
        pieces = [line[index : index + max_chars] for index in range(0, len(line), max_chars)] or [""]
        for piece in pieces:
            piece_size = len(piece) + 1
            if current and current_size + piece_size > max_chars:
                chunks.append("\n".join(current))
                current = []
                current_size = 0
            current.append(piece)
            current_size += piece_size
    if current:
        chunks.append("\n".join(current))
    return chunks


def _minutes_instruction(title: str, glossary_text: str) -> str:
    instruction = (
        "文字起こしまたは区間メモから、事実を補わず詳細な議事録を作成してください。\n"
        "発言量が多くても、少数の話題だけに偏らず全区間の重要事項を反映してください。\n\n"
        "## 出力フォーマット\n"
        f"# {title or '議事録'}\n\n"
        "## 基本情報\n"
        "- 日時: （記録にある場合のみ）\n"
        "- 参加者: Speakerラベルを列挙\n\n"
        "## 議題\n"
        "- 議論されたトピックを漏れなく列挙\n\n"
        "## 議論内容\n"
        "- 時系列と論点が分かるよう、Speakerラベル付きで詳しく記載\n\n"
        "## 決定事項\n"
        "- 決定と、決定に至った条件・理由を記載\n\n"
        "## アクションアイテム\n"
        "- 【担当Speaker】内容（期限。記録にない場合は未定）\n\n"
        "## 未決事項・確認事項\n"
        "- 結論が出ていない論点、曖昧な担当や期限\n\n"
        "## 注意\n"
        "- Speaker 1等のラベルから実名を推測しない\n"
        "- 聞き取れない箇所や根拠のない日時・担当者は不明と記載する\n"
        "- 決定事項と単なる提案を混同しない\n\n"
    )
    if glossary_text:
        instruction += f"## 用語辞書（表記修正の参考）\n{glossary_text}\n\n"
    return instruction


async def _generate(client, *, prompt: str, operation_name: str, max_output_tokens: int) -> str:
    model = response_model()

    async def request():
        return await client.aio.models.generate_content(
            model=model,
            contents=prompt,
            config=generation_config(model, thinking_level="medium", max_output_tokens=max_output_tokens),
        )

    response = await retry_rate_limit(request, operation_name=operation_name)
    log_usage(log, operation_name, model, response)
    return (response.text or "").strip()


async def generate_minutes(
    client,
    transcript: str,
    *,
    title: str = "",
    glossary_text: str = "",
) -> str:
    """Use map/reduce generation so long meetings do not collapse into a weak summary."""
    transcript = normalize_speaker_labels(transcript)
    chunks = split_transcript(transcript)
    if not chunks:
        return ""

    instruction = _minutes_instruction(title, glossary_text)
    if len(chunks) == 1:
        return await _generate(
            client,
            prompt=f"{instruction}\n## 文字起こし\n{chunks[0]}",
            operation_name="minutes_format",
            max_output_tokens=8192,
        )

    notes: list[str] = []
    for index, chunk in enumerate(chunks, start=1):
        note = await _generate(
            client,
            prompt=(
                f"以下は会議全体の第{index}/{len(chunks)}区間です。最終議事録の材料として、情報を圧縮しすぎず抽出してください。\n"
                "必ず、話題、各Speakerの主張、提案、決定、保留、担当、期限、数値、固有名詞を区別して残してください。\n"
                "この区間だけで結論を推測せず、発言順を維持してください。\n\n"
                f"{chunk}"
            ),
            operation_name="minutes_segment",
            max_output_tokens=4096,
        )
        notes.append(f"### 区間 {index}/{len(chunks)}\n{note}")

    log.info("Combining %s transcript segment notes", len(notes))
    return await _generate(
        client,
        prompt=(
            f"{instruction}\n"
            "以下は会議の全区間から抽出したメモです。重複を統合しつつ、後半の議題や少数意見も落とさず、"
            "区間をまたぐ決定の変化が分かるようにまとめてください。\n\n"
            + "\n\n".join(notes)
        ),
        operation_name="minutes_combine",
        max_output_tokens=8192,
    )
