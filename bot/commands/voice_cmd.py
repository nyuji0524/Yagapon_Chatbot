"""
/join listen|meeting|chat - VCに参加
/leave - VCから退出 (議事録生成 → Discord + Google Drive)
"""

import io
import discord
from discord import app_commands

from bot.voice import VoiceMode, join_voice, leave_voice, get_session


def register(bot):
    @bot.tree.command(name="join", description="ボイスチャンネルに参加するぽん！")
    @app_commands.describe(mode="モードを選ぶぽん")
    @app_commands.choices(mode=[
        app_commands.Choice(name="聞き専 (議事録のみ)", value="listen"),
        app_commands.Choice(name="参加者 (議事録 + 質問対応)", value="meeting"),
        app_commands.Choice(name="おしゃべり (雑談モード)", value="chat"),
    ])
    async def join_cmd(interaction: discord.Interaction, mode: str):
        member = interaction.guild.get_member(interaction.user.id)
        if not member or not member.voice or not member.voice.channel:
            await interaction.response.send_message(
                "先にボイスチャンネルに入ってほしいぽん！", ephemeral=True
            )
            return

        await interaction.response.defer()

        voice_mode = VoiceMode(mode)
        vc = member.voice.channel

        session = await join_voice(bot, interaction.guild_id, vc, voice_mode)

        mode_desc = {
            VoiceMode.LISTEN: "聞き専モード 🎧\n議事録を作成するぽん。終わったら `/leave` で退出してねぽん！",
            VoiceMode.MEETING: "参加者モード 🎤\n議事録を作成しつつ、質問にも答えるぽん！",
            VoiceMode.CHAT: "おしゃべりモード 💬\nいっぱい喋るぽん！",
        }

        await interaction.followup.send(f"🔊 {vc.name} に参加したぽん！\n{mode_desc[voice_mode]}")

        if voice_mode == VoiceMode.CHAT:
            await session.speak("こんにちはぽん！おしゃべりやがぽんだぽん！何でも話しかけてねぽん！")

    @bot.tree.command(name="leave", description="ボイスチャンネルから退出するぽん！")
    async def leave_cmd(interaction: discord.Interaction):
        await interaction.response.defer()

        # セッション情報を退出前に取得
        session = get_session(interaction.guild_id)
        channel_name = session.channel.name if session else "unknown"

        minutes = await leave_voice(interaction.guild_id)

        if not minutes:
            await interaction.followup.send("退出したぽん！👋")
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
        await interaction.followup.send(embed=embed, file=file)

        # Google Drive にアップロード
        from bot.gdrive import upload_minutes
        drive_url = await upload_minutes(
            bot.config, interaction.guild_id, minutes, channel_name
        )

        if drive_url:
            await interaction.followup.send(f"📁 Google Driveにも保存したぽん！\n{drive_url}")
