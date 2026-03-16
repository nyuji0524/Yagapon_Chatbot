"""
/ignore - 現在のチャンネルの学習を停止
"""

import discord


def register(bot):
    @bot.tree.command(name="ignore", description="このチャンネルの学習を停止するぽん！")
    async def ignore_cmd(interaction: discord.Interaction):
        added = await bot.config.add_ignore_channel(interaction.guild_id, interaction.channel_id)
        if added:
            await interaction.response.send_message(
                f"了解だぽん！<#{interaction.channel_id}> の会話はもう学習しないぽん。"
            )
        else:
            await interaction.response.send_message(
                "すでに除外リストに入ってるぽん。", ephemeral=True
            )
