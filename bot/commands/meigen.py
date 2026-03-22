"""
/meigen - 名言bot (pycord)
過去の会話からGeminiが選んだ面白い・印象的な発言を表示
"""

import discord


def register(bot):
    @bot.slash_command(name="meigen", description="名言を表示するぽん！")
    @discord.option("user", description="特定ユーザーの名言を見たい場合", type=discord.Member, required=False, default=None)
    async def meigen_cmd(ctx: discord.ApplicationContext, user: discord.Member = None):
        await ctx.defer()

        corpus = bot.config.get_corpus(ctx.guild_id)
        if not corpus:
            await ctx.followup.send("先に `/setup` をしてほしいぽん！", silent=True)
            return

        if user:
            query = (
                f"「{user.display_name}」さんの過去の発言を検索し、以下の基準で名言を3つ選んでください。\n"
                f"基準: ① 笑える発言 ② 鋭い指摘・名言 ③ その人らしさが出ている発言\n"
                f"各名言は以下の形式で紹介:\n"
                f"> 「発言内容をそのまま引用」\n"
                f"> — {user.display_name}（#チャンネル名、日付）\n"
                f"※ 発言は改変せず原文のまま引用すること。見つからなければ正直に伝えること。"
            )
        else:
            query = (
                "このサーバーの過去の会話から、名言と呼べる発言を3つ選んでください。\n"
                "基準: ① 笑える発言 ② 鋭い指摘・名言 ③ 意外な発言\n"
                "できるだけ異なるメンバーから選び、以下の形式で紹介:\n"
                "> 「発言内容をそのまま引用」\n"
                "> — 発言者名（#チャンネル名、日付）\n"
                "※ 発言は改変せず原文のまま引用すること。"
            )

        answer = await bot.corpus.query(query, corpus)

        embed = discord.Embed(
            title="📜 名言集だぽん！",
            description=answer,
            color=discord.Color.gold(),
        )
        if user:
            embed.set_footer(text=f"{user.display_name}の名言")

        await ctx.followup.send(embed=embed, silent=True)
