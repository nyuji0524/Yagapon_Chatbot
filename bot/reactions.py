"""スマートリアクション - 感情ベース、低感度"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

from google import genai
from google.genai import types

log = logging.getLogger("yagapon.reactions")

BATCH_SIZE = 5
BATCH_INTERVAL = 180  # 3分

JUDGE_PROMPT = """以下のDiscordメッセージそれぞれに対し、感情を判定せよ。
選択肢: interesting, surprised, funny, none
ほとんどのメッセージは "none" にすべき。本当に際立つものだけ判定せよ。
JSON配列のみ返せ（説明不要）。例: ["none", "funny", "none"]

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

    global _last_flush
    now = datetime.now(timezone.utc)
    should_flush = (
        len(_buffer) >= BATCH_SIZE
        or (now - _last_flush).total_seconds() >= BATCH_INTERVAL
    )

    if should_flush:
        await _flush_reactions(bot)


async def _flush_reactions(bot):
    global _buffer, _last_flush

    if not _buffer:
        return

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
        response = await client.aio.models.generate_content(
            model="gemini-2.0-flash-lite",
            contents=prompt,
        )

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

    except Exception as e:
        log.error(f"Reaction batch error: {e}")
