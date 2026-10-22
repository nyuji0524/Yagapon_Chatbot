"""
/report weekly|monthly - レポート生成 → Discord + Google Drive (pycord)
"""

import io

import discord

from bot.authorization import require_guild_admin


def register(bot):
    group = bot.create_group("report", "レポート生成だぽん！")

    @group.command(name="weekly", description="週次レポートを生成するぽん！")
    @discord.default_permissions(administrator=True)
    async def report_weekly(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        await ctx.defer(ephemeral=True)

        from bot.reports import generate_weekly_report
        report = await generate_weekly_report(
            bot,
            ctx.guild_id,
            allow_unrestricted_conversations=True,
        )

        embed = discord.Embed(
            title="📊 週次レポート",
            description=report[:4096],
            color=discord.Color.teal(),
        )
        file = discord.File(
            io.BytesIO(report.encode("utf-8")),
            filename="週次レポート.md",
        )
        await ctx.followup.send(embed=embed, file=file, ephemeral=True)

        # Google Driveにも保存
        from bot.gdrive import upload_report
        drive_url = await upload_report(bot.config, ctx.guild_id, report, "週次レポート")
        if drive_url:
            await ctx.followup.send(
                f"📁 Google Driveにも保存したぽん！\n{drive_url}",
                ephemeral=True,
            )

    @group.command(name="monthly", description="月間報告書を生成するぽん！")
    @discord.default_permissions(administrator=True)
    async def report_monthly(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        await ctx.defer(ephemeral=True)
        await ctx.followup.send(
            "月間報告書を生成中だぽん... (少し時間がかかるぽん) ⏳",
            ephemeral=True,
        )

        from bot.reports import generate_monthly_report
        report = await generate_monthly_report(
            bot,
            ctx.guild_id,
            allow_unrestricted_conversations=True,
        )

        embed = discord.Embed(
            title="📋 月間報告書",
            description=report[:2000] + ("\n\n(全文はファイルを参照)" if len(report) > 2000 else ""),
            color=discord.Color.gold(),
        )
        file = discord.File(
            io.BytesIO(report.encode("utf-8")),
            filename="月間報告書.md",
        )
        await ctx.followup.send(embed=embed, file=file, ephemeral=True)

        # Google Driveにも保存
        from bot.gdrive import upload_report
        drive_url = await upload_report(bot.config, ctx.guild_id, report, "月間報告書")
        if drive_url:
            await ctx.followup.send(
                f"📁 Google Driveにも保存したぽん！\n{drive_url}",
                ephemeral=True,
            )
