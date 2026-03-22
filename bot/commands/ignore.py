"""
/ignore - 現在のチャンネルの学習を停止 (pycord)
"""

import discord


def register(bot):
    @bot.slash_command(name="ignore", description="このチャンネルの学習を停止するぽん！")
    async def ignore_cmd(ctx: discord.ApplicationContext):
        added = await bot.config.add_ignore_channel(ctx.guild_id, ctx.channel_id)
        if added:
            await ctx.respond(
                f"了解だぽん！<#{ctx.channel_id}> の会話はもう学習しないぽん。"
            )
        else:
            await ctx.respond(
                "すでに除外リストに入ってるぽん。", ephemeral=True
            )
