"""スマートリアクション - 感情ベース、低感度"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from google import genai

from bot.ai_models import fast_model, generation_config, log_usage
from bot.recording_priority import recording_is_active

log = logging.getLogger("yagapon.reactions")

BATCH_SIZE = 5
BATCH_INTERVAL = 180  # 3分

JUDGE_PROMPT = """大学の学園祭実行委員会のDiscordメッセージを感情判定する。

ルール:
- 選択肢: interesting, surprised, funny, none
- 10個中2〜3個にリアクションがつくくらいの感度でOK。気軽に判定して
- interesting: 新しい情報、意見、提案、進捗報告、技術的な話題
- surprised: 予想外なこと、驚くこと、意外な展開
- funny: ユーモア、ボケ、面白い言い回し、ツッコミ、笑える発言
- none: 挨拶、了解、短い返事、特に感情を動かさない発言
- JSON配列のみ返せ。説明不要。例: ["none", "funny", "interesting", "none", "none"]

メッセージ:
{messages}"""


@dataclass
class PendingMessage:
    message_id: int
    channel_id: int
    guild_id: int
    content: str


_buffer: list[PendingMessage] = []
_last_flush: datetime = datetime.now(timezone.utc)
_client: genai.Client | None = None
_flush_task: asyncio.Task | None = None
_flush_lock = asyncio.Lock()


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        _client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))
    return _client


async def maybe_react(bot, message):
    """メッセージをバッファに追加、条件を満たしたらバッチ判定"""
    reactions_cfg = bot.config.get_reactions(message.guild.id)
    if not reactions_cfg.get("enabled"):
        return

    # フィルタ: 短すぎる、URL only、コマンド
    content = message.content.strip()
    if len(content) < 20:
        return
    if content.startswith(("http://", "https://", "/")):
        return

    _buffer.append(PendingMessage(
        message_id=message.id,
        channel_id=message.channel.id,
        guild_id=message.guild.id,
        content=content[:200],
    ))
    start_reaction_flush_loop(bot)

    global _last_flush
    now = datetime.now(timezone.utc)
    should_flush = (
        len(_buffer) >= BATCH_SIZE
        or (now - _last_flush).total_seconds() >= BATCH_INTERVAL
    )

    if should_flush:
        await _flush_reactions(bot)


def start_reaction_flush_loop(bot) -> None:
    """Start the independent timer that flushes a final partial batch."""
    global _flush_task
    if _flush_task is None or _flush_task.done():
        _flush_task = asyncio.create_task(
            _reaction_flush_loop(bot),
            name="reaction-flush",
        )


async def _reaction_flush_loop(bot) -> None:
    while True:
        await asyncio.sleep(BATCH_INTERVAL)
        await _flush_reactions(bot)


async def shutdown_reactions(bot) -> None:
    """Stop the timer and make a best-effort final flush before shutdown."""
    global _flush_task
    if _flush_task is not None:
        _flush_task.cancel()
        try:
            await _flush_task
        except asyncio.CancelledError:
            pass
        _flush_task = None

    while _buffer:
        if not await _flush_reactions(bot):
            break


async def _flush_reactions(bot) -> bool:
    global _buffer, _last_flush

    async with _flush_lock:
        if not _buffer:
            return True
        if recording_is_active():
            # Reactions are optional. Keep the batch queued and avoid model/API
            # work while voice capture owns the small production VM.
            return False

        batch = _buffer[:BATCH_SIZE]
        _buffer = _buffer[BATCH_SIZE:]
        _last_flush = datetime.now(timezone.utc)

        # Geminiに投げる
        messages_text = "\n".join(
            f"{i+1}: \"{m.content}\"" for i, m in enumerate(batch)
        )
        prompt = JUDGE_PROMPT.format(messages=messages_text)

        try:
            client = _get_client()
            model = fast_model()
            response = await client.aio.models.generate_content(
                model=model,
                contents=prompt,
                # This is a tiny classification task. Thinking regularly used
                # most of the output budget and left truncated JSON, which was
                # then retried forever by the flush loop.
                config=generation_config(model, thinking_level=None, max_output_tokens=128),
            )
            log_usage(log, "reaction_batch", model, response)

            # JSONパース
            import json
            text = response.text.strip()
            # ```json ... ``` を除去
            if text.startswith("```"):
                text = text.split("\n", 1)[1].rsplit("```", 1)[0]
            results = json.loads(text)

            # リアクション付与
            for msg_data, emotion in zip(batch, results):
                if emotion == "none":
                    continue

                guild = bot.get_guild(msg_data.guild_id)
                if not guild:
                    continue

                reactions_cfg = bot.config.get_reactions(msg_data.guild_id)
                emoji = reactions_cfg.get(emotion)
                if not emoji:
                    continue

                channel = guild.get_channel(msg_data.channel_id)
                if not channel:
                    continue

                try:
                    msg = await channel.fetch_message(msg_data.message_id)
                    await msg.add_reaction(emoji)
                except Exception:
                    pass
            return True

        except Exception as e:
            # Preserve the batch for the timer or next message to retry. Smart
            # reactions are optional, but transient AI failures must not drop
            # the final partial batch silently.
            _buffer = batch + _buffer
            log.error(f"Reaction batch error: {e}")
            return False
