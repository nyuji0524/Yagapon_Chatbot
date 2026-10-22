"""TTS - edge-ttsで音声生成 → DiscordのVCで再生"""

import asyncio
import logging
import os
import tempfile

import discord

log = logging.getLogger("yagapon.tts")

VOICE = "ja-JP-NanamiNeural"
# マスコットっぽい可愛い声にするための調整
PITCH = "+80Hz"   # とても高い声（マスコットらしく）
RATE = "-15%"     # ゆっくりめに喋る（聞き取りやすく）


async def speak_in_vc(bot, message: discord.Message, text: str):
    """メッセージ送信者がVCにいれば、そこに入って読み上げる"""
    if not message.guild:
        return

    member = message.guild.get_member(message.author.id)
    if not member or not member.voice or not member.voice.channel:
        return  # VCにいなければスキップ

    vc_channel = member.voice.channel
    # A text answer may only be spoken into the same VoiceChannel's built-in
    # chat. Never relay answers from a separate/private text channel into a VC.
    if getattr(getattr(message, "channel", None), "id", None) != getattr(
        vc_channel, "id", None
    ):
        log.info("Skipping text-answer TTS because text and voice audiences differ")
        return

    # /join owns its VoiceClient for recording and finalization.  Reuse the
    # session's serialized TTS path instead of moving or disconnecting that
    # managed connection from this short-lived text-answer helper.
    from bot.voice import get_session

    session = get_session(message.guild.id)
    if session and session.voice_client and session.voice_client.is_connected():
        if getattr(session.channel, "id", None) != getattr(vc_channel, "id", None):
            log.info(
                "Skipping text-answer TTS because the managed voice session is in another channel"
            )
            return
        await session.speak(text)
        return

    try:
        import edge_tts
    except ImportError:
        log.warning("edge-tts not installed, skipping TTS")
        return

    tmp_path = None
    voice_client = None
    connected_here = False

    try:
        # 音声生成
        communicate = edge_tts.Communicate(text[:500], VOICE, pitch=PITCH, rate=RATE)  # 長すぎる場合は切る
        tmp = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        tmp_path = tmp.name
        tmp.close()
        await communicate.save(tmp_path)

        # A /join can start while edge-tts is generating audio.  Check again
        # before touching a VoiceClient so that the newly managed connection
        # remains owned by VoiceSession.
        session = get_session(message.guild.id)
        if session and session.voice_client and session.voice_client.is_connected():
            if getattr(session.channel, "id", None) == getattr(vc_channel, "id", None):
                await session.speak(text)
            else:
                log.info(
                    "Skipping text-answer TTS because the managed voice session is in another channel"
                )
            return

        # VC接続
        if bot.voice_clients:
            for vc in bot.voice_clients:
                if vc.guild == message.guild:
                    voice_client = vc
                    if vc.channel != vc_channel:
                        # This helper does not own existing connections. Moving
                        # one can interrupt voiceprint capture or another voice
                        # workflow, so skip TTS instead.
                        log.info("Skipping text-answer TTS because an existing voice client is busy")
                        return
                    break

        if voice_client is None:
            voice_client = await vc_channel.connect()
            connected_here = True

        # 再生
        if voice_client.is_playing():
            voice_client.stop()

        audio_source = discord.FFmpegPCMAudio(tmp_path)
        voice_client.play(audio_source)

        # 再生完了を待つ
        while voice_client.is_playing():
            await asyncio.sleep(0.5)

        # Only tear down the connection created by this helper.  A borrowed
        # connection must remain available to its owning workflow.
        if connected_here:
            await voice_client.disconnect()

    except Exception as e:
        log.error(f"TTS error: {e}")
        if connected_here and voice_client and voice_client.is_connected():
            await voice_client.disconnect()
    finally:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
