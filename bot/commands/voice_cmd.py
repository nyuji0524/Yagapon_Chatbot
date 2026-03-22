"""
/join listen|meeting|chat - VCに参加
/leave - VCから退出 (議事録生成 → Discord + Google Drive)
pycord版
"""

import io
import discord

from bot.voice import VoiceMode, join_voice, leave_voice, get_session


def register(bot):
    @bot.slash_command(name="join", description="ボイスチャンネルに参加するぽん！")
    @discord.option(
        "mode", description="モードを選ぶぽん",
        choices=[
            discord.OptionChoice("聞き専 (議事録のみ)", "listen"),
            discord.OptionChoice("参加者 (議事録 + 質問対応)", "meeting"),
            discord.OptionChoice("おしゃべり (雑談モード)", "chat"),
        ],
    )
    async def join_cmd(ctx: discord.ApplicationContext, mode: str):
        member = ctx.guild.get_member(ctx.author.id)
        if not member or not member.voice or not member.voice.channel:
            await ctx.respond(
                "先にボイスチャンネルに入ってほしいぽん！", ephemeral=True
            )
            return

        await ctx.defer()

        voice_mode = VoiceMode(mode)
        vc = member.voice.channel

        session = await join_voice(bot, ctx.guild_id, vc, voice_mode)

        mode_desc = {
            VoiceMode.LISTEN: "聞き専モード 🎧\n議事録を作成するぽん。音声も録音してるぽん！終わったら `/leave` で退出してねぽん！",
            VoiceMode.MEETING: "参加者モード 🎤\n議事録を作成しつつ、質問にも答えるぽん！音声も録音してるぽん！",
            VoiceMode.CHAT: "おしゃべりモード 💬\nいっぱい喋るぽん！",
        }

        await ctx.followup.send(f"🔊 {vc.name} に参加したぽん！\n{mode_desc[voice_mode]}")

        if voice_mode == VoiceMode.CHAT:
            await session.speak("こんにちはぽん！おしゃべりやがぽんだぽん！何でも話しかけてねぽん！")

    @bot.slash_command(name="leave", description="ボイスチャンネルから退出するぽん！")
    async def leave_cmd(ctx: discord.ApplicationContext):
        await ctx.defer()

        # セッション情報を退出前に取得
        session = get_session(ctx.guild_id)
        channel_name = session.channel.name if session else "unknown"
        has_audio = bool(session and session.mode in (VoiceMode.LISTEN, VoiceMode.MEETING))

        if has_audio:
            await ctx.followup.send("🎙️ 音声を文字起こし中だぽん...少し待ってねぽん ⏳")

        minutes = await leave_voice(ctx.guild_id)

        if not minutes:
            await ctx.followup.send("退出したぽん！👋")
            return

        # Discord に送信
        embed = discord.Embed(
            title="📝 議事録",
            description=minutes[:4096],
            color=discord.Color.blue(),
        )

        file = discord.File(
            io.BytesIO(minutes.encode("utf-8")),
            filename=f"議事録_{channel_name}.md",
        )
        await ctx.followup.send(embed=embed, file=file)

        # Google Drive にアップロード
        from bot.gdrive import upload_minutes
        drive_url = await upload_minutes(
            bot.config, ctx.guild_id, minutes, channel_name
        )

        if drive_url:
            await ctx.followup.send(f"📁 Google Driveにも保存したぽん！\n{drive_url}")
