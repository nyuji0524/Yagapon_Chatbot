"""
/status - 現在の設定状況を表示 (pycord)
"""

import discord

from bot.authorization import require_guild_admin


def register(bot):
    @bot.slash_command(name="status", description="現在の設定状況を確認するぽん！")
    @discord.default_permissions(administrator=True)
    async def status_cmd(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        gid = ctx.guild_id
        bureau = bot.config.get_bureau(gid)

        if not bureau:
            embed = discord.Embed(
                title="🔴 初期設定が必要です",
                description="管理者が `/setup` を実行すると、検索・議事録・メンバー設定を使えるようになります。",
                color=discord.Color.red(),
            )
            await ctx.respond(embed=embed, ephemeral=True)
            return

        corpus = bot.config.get_corpus(gid) or "未設定"
        members = bot.config.get_members(gid)
        reactions = bot.config.get_reactions(gid)

        embed = discord.Embed(
            title="🟢 おしゃべりやがぽん 稼働ステータス",
            description="このサーバーのナレッジ・議事録設定をまとめて確認できます。",
            color=discord.Color.green(),
        )
        embed.add_field(name="🏮 対象", value=f"**{bureau}**", inline=True)
        embed.add_field(name="👥 登録メンバー", value=f"**{len(members)}人**", inline=True)

        react_status = "有効" if reactions.get("enabled") else "無効"
        if reactions.get("enabled"):
            react_status += f" ({reactions['interesting']}/{reactions['surprised']}/{reactions['funny']})"
        embed.add_field(name="✨ スマートリアクション", value=react_status, inline=True)

        drive_status = "✅ 設定済み" if bot.config.get_drive_folder(gid) else "⏸️ 未設定"
        minutes_channel_id = bot.config.get_minutes_channel(gid)
        minutes_status = f"<#{minutes_channel_id}>" if minutes_channel_id else "未設定"
        embed.add_field(name="📁 Google Drive", value=drive_status, inline=True)
        embed.add_field(name="📝 議事録の投稿先", value=minutes_status, inline=True)
        embed.add_field(name="🧠 File Search", value=f"`{corpus}`", inline=False)

        # ignore channels
        guild_data = bot.config._guild(gid)
        ignored = guild_data.get("ignore_channels", [])
        if ignored:
            ignore_str = " ".join(f"<#{cid}>" for cid in ignored)
            if len(ignore_str) > 950:
                ignore_str = f"{ignore_str[:950]}…"
            embed.add_field(
                name=f"🔒 学習除外（{len(ignored)}件）",
                value=ignore_str,
                inline=False,
            )
        else:
            embed.add_field(name="🔒 学習除外", value="なし", inline=False)

        embed.set_footer(text="設定変更後は /status を再実行して確認してください")
        await ctx.respond(embed=embed, ephemeral=True)
