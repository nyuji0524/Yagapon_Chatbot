"""
/report weekly|monthly - レポート生成 → Discord + Google Drive
"""

import io
import discord
from discord import app_commands


def register(bot):
    group = app_commands.Group(name="report", description="レポート生成だぽん！")

    @group.command(name="weekly", description="週次レポートを生成するぽん！")
    async def report_weekly(interaction: discord.Interaction):
        await interaction.response.defer()

        from bot.reports import generate_weekly_report
        report = await generate_weekly_report(bot, interaction.guild_id)

        embed = discord.Embed(
            title="📊 週次レポート",
            description=report[:4096],
            color=discord.Color.teal(),
        )
        file = discord.File(
            io.BytesIO(report.encode("utf-8")),
            filename="週次レポート.md",
        )
        await interaction.followup.send(embed=embed, file=file)

        # Google Driveにも保存
        from bot.gdrive import upload_report
        drive_url = await upload_report(bot.config, interaction.guild_id, report, "週次レポート")
        if drive_url:
            await interaction.followup.send(f"📁 Google Driveにも保存したぽん！\n{drive_url}")

    @group.command(name="monthly", description="月間報告書を生成するぽん！")
    async def report_monthly(interaction: discord.Interaction):
        await interaction.response.defer()
        await interaction.followup.send("月間報告書を生成中だぽん... (少し時間がかかるぽん) ⏳")

        from bot.reports import generate_monthly_report
        report = await generate_monthly_report(bot, interaction.guild_id)

        embed = discord.Embed(
            title="📋 月間報告書",
            description=report[:2000] + ("\n\n(全文はファイルを参照)" if len(report) > 2000 else ""),
            color=discord.Color.gold(),
        )
        file = discord.File(
            io.BytesIO(report.encode("utf-8")),
            filename="月間報告書.md",
        )
        await interaction.followup.send(embed=embed, file=file)

        # Google Driveにも保存
        from bot.gdrive import upload_report
        drive_url = await upload_report(bot.config, interaction.guild_id, report, "月間報告書")
        if drive_url:
            await interaction.followup.send(f"📁 Google Driveにも保存したぽん！\n{drive_url}")

    bot.tree.add_command(group)
