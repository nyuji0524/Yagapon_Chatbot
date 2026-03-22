"""
/status - 現在の設定状況を表示 (pycord)
"""

import discord


def register(bot):
    @bot.slash_command(name="status", description="現在の設定状況を確認するぽん！")
    async def status_cmd(ctx: discord.ApplicationContext):
        gid = ctx.guild_id
        bureau = bot.config.get_bureau(gid)

        if not bureau:
            await ctx.respond(
                "🔴 まだ設定されていないぽん。`/setup` を実行してほしいぽん。",
                silent=True,
            )
            return

        corpus = bot.config.get_corpus(gid) or "未設定"
        members = bot.config.get_members(gid)
        reactions = bot.config.get_reactions(gid)
        github_ch = bot.config.get_github_channel(gid)

        embed = discord.Embed(title="おしゃべりやがぽん - ステータス", color=discord.Color.green())
        embed.add_field(name="局", value=bureau, inline=True)
        embed.add_field(name="コーパス", value=f"`{corpus}`", inline=False)
        embed.add_field(name="登録メンバー数", value=f"{len(members)}人", inline=True)

        if github_ch:
            embed.add_field(name="GitHub通知", value=f"<#{github_ch}>", inline=True)
        else:
            embed.add_field(name="GitHub通知", value="未設定", inline=True)

        react_status = "有効" if reactions.get("enabled") else "無効"
        if reactions.get("enabled"):
            react_status += f" ({reactions['interesting']}/{reactions['surprised']}/{reactions['funny']})"
        embed.add_field(name="リアクション", value=react_status, inline=True)

        # ignore channels
        guild_data = bot.config._guild(gid)
        ignored = guild_data.get("ignore_channels", [])
        if ignored:
            ignore_str = ", ".join(f"<#{cid}>" for cid in ignored[:10])
            embed.add_field(name="学習除外", value=ignore_str, inline=False)

        await ctx.respond(embed=embed, silent=True)
