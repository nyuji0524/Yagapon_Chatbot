"""VC 3モード - listen / meeting / chat + 声紋登録"""

import asyncio
import io
import logging
import os
import tempfile
from enum import Enum

import discord
from google import genai

log = logging.getLogger("yagapon.voice")


class VoiceMode(Enum):
    LISTEN = "listen"    # 聞き専: 議事録作成のみ
    MEETING = "meeting"  # 参加者: 議事録 + 質問対応
    CHAT = "chat"        # おしゃべり: 議事録なし、積極発話


class VoiceSession:
    """1つのVCセッションを管理"""

    def __init__(self, bot, guild_id: int, channel: discord.VoiceChannel, mode: VoiceMode):
        self.bot = bot
        self.guild_id = guild_id
        self.channel = channel
        self.mode = mode
        self.voice_client: discord.VoiceClient | None = None
        self.transcript: list[str] = []
        self.is_active = False

    async def start(self):
        self.voice_client = await self.channel.connect()
        self.is_active = True
        log.info(f"VC joined: {self.channel.name} (mode={self.mode.value})")

        # TODO: 音声受信 + 文字起こし (Gemini Audio API)
        # discord.pyのrecvは別途Sink実装が必要
        # 将来: pyannote で話者分離 + Gemini で文字起こし

    async def stop(self) -> str | None:
        self.is_active = False
        if self.voice_client and self.voice_client.is_connected():
            await self.voice_client.disconnect()

        if self.mode in (VoiceMode.LISTEN, VoiceMode.MEETING) and self.transcript:
            return await self._generate_minutes()
        return None

    async def _generate_minutes(self) -> str:
        """議事録を生成"""
        transcript_text = "\n".join(self.transcript)
        client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY", ""))

        response = await client.aio.models.generate_content(
            model="gemini-2.5-flash",
            contents=(
                "以下の会議の文字起こしから、構造化された議事録を作成してください。\n"
                "形式: 日時、参加者、議題、議論内容、決定事項、アクションアイテム\n\n"
                f"{transcript_text}"
            ),
        )
        return response.text

    async def speak(self, text: str):
        """TTSで発話"""
        if not self.voice_client or not self.voice_client.is_connected():
            return

        from bot.tts import VOICE
        try:
            import edge_tts

            communicate = edge_tts.Communicate(text[:500], VOICE)
            tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            tmp_path = tmp.name
            tmp.close()
            await communicate.save(tmp_path)

            if self.voice_client.is_playing():
                self.voice_client.stop()

            self.voice_client.play(discord.FFmpegPCMAudio(tmp_path))
            while self.voice_client.is_playing():
                await asyncio.sleep(0.5)

            os.remove(tmp_path)
        except Exception as e:
            log.error(f"Voice speak error: {e}")


# グローバルセッション管理
_sessions: dict[int, VoiceSession] = {}  # guild_id -> session


async def join_voice(bot, guild_id: int, channel: discord.VoiceChannel, mode: VoiceMode) -> VoiceSession:
    # 既存セッションがあれば切断
    if guild_id in _sessions:
        await _sessions[guild_id].stop()

    session = VoiceSession(bot, guild_id, channel, mode)
    await session.start()
    _sessions[guild_id] = session
    return session


async def leave_voice(guild_id: int) -> str | None:
    session = _sessions.pop(guild_id, None)
    if session:
        return await session.stop()
    return None


def get_session(guild_id: int) -> VoiceSession | None:
    return _sessions.get(guild_id)
