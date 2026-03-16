"""
/meigen - 名言bot
過去の会話からGeminiが選んだ面白い・印象的な発言を表示
"""

import random
import discord
from discord import app_commands


def register(bot):
    @bot.tree.command(name="meigen", description="名言を表示するぽん！")
    @app_commands.describe(user="特定ユーザーの名言を見たい場合")
    async def meigen_cmd(interaction: discord.Interaction, user: discord.Member = None):
        await interaction.response.defer()

        corpus = bot.config.get_corpus(interaction.guild_id)
        if not corpus:
            await interaction.followup.send("先に `/setup` をしてほしいぽん！")
            return

        if user:
            query = (
                f"{user.display_name}さんの過去の発言の中から、"
                f"面白い・印象的・名言と呼べるような発言を3つ選んで、"
                f"それぞれ引用形式で紹介してください。"
            )
        else:
            query = (
                "このサーバーの過去の会話の中から、"
                "面白い・印象的・名言と呼べるような発言を3つ選んで、"
                "それぞれ発言者名と一緒に引用形式で紹介してください。"
            )

        answer = await bot.corpus.query(query, corpus)

        embed = discord.Embed(
            title="📜 名言集だぽん！",
            description=answer,
            color=discord.Color.gold(),
        )
        if user:
            embed.set_footer(text=f"{user.display_name}の名言")

        await interaction.followup.send(embed=embed)
