"""TTS - edge-ttsで音声生成 → DiscordのVCで再生"""

import asyncio
import logging
import tempfile
import os

import discord

log = logging.getLogger("yagapon.tts")

VOICE = "ja-JP-NanamiNeural"
# マスコットっぽい可愛い声にするための調整
PITCH = "+60Hz"   # かなり高い声
RATE = "-10%"     # ゆっくり喋る


async def speak_in_vc(bot, message: discord.Message, text: str):
    """メッセージ送信者がVCにいれば、そこに入って読み上げる"""
    if not message.guild:
        return

    member = message.guild.get_member(message.author.id)
    if not member or not member.voice or not member.voice.channel:
        return  # VCにいなければスキップ

    vc_channel = member.voice.channel

    try:
        import edge_tts
    except ImportError:
        log.warning("edge-tts not installed, skipping TTS")
        return

    tmp_path = None
    voice_client = None

    try:
        # 音声生成
        communicate = edge_tts.Communicate(text[:500], VOICE, pitch=PITCH, rate=RATE)  # 長すぎる場合は切る
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_path = tmp.name
        tmp.close()
        await communicate.save(tmp_path)

        # VC接続
        if bot.voice_clients:
            for vc in bot.voice_clients:
                if vc.guild == message.guild:
                    voice_client = vc
                    if vc.channel != vc_channel:
                        await vc.move_to(vc_channel)
                    break

        if voice_client is None:
            voice_client = await vc_channel.connect()

        # 再生
        if voice_client.is_playing():
            voice_client.stop()

        audio_source = discord.FFmpegPCMAudio(tmp_path)
        voice_client.play(audio_source)

        # 再生完了を待つ
        while voice_client.is_playing():
            await asyncio.sleep(0.5)

        # 切断
        await voice_client.disconnect()

    except Exception as e:
        log.error(f"TTS error: {e}")
        if voice_client and voice_client.is_connected():
            await voice_client.disconnect()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
