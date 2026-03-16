"""
/backfill - 過去ログ取り込み (期間指定可)
"""

from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands


def register(bot):
    @bot.tree.command(name="backfill", description="過去ログを取り込むぽん！")
    @app_commands.describe(
        days="何日分遡るか (省略で全量)",
        channel="特定チャンネルのみ取り込む場合に指定（省略で全チャンネル）",
    )
    @app_commands.choices(days=[
        app_commands.Choice(name="全部", value=0),
        app_commands.Choice(name="過去30日", value=30),
        app_commands.Choice(name="過去180日", value=180),
        app_commands.Choice(name="過去365日", value=365),
    ])
    async def backfill_cmd(
        interaction: discord.Interaction,
        days: int = 0,
        channel: discord.TextChannel = None,
    ):
        await interaction.response.defer()

        corpus = bot.config.get_corpus(interaction.guild_id)
        if not corpus:
            await interaction.followup.send("先に `/setup` をしてほしいぽん！")
            return

        after = None
        label = "全期間"
        if days > 0:
            after = datetime.now(timezone.utc) - timedelta(days=days)
            label = f"過去{days}日分"

        if channel:
            channels = [channel]
        else:
            channels = [
                ch for ch in interaction.guild.text_channels
                if ch.permissions_for(interaction.guild.me).read_message_history
                and not bot.config.is_ignored(interaction.guild_id, ch.id)
            ]

        await interaction.followup.send(
            f"📚 {len(channels)}チャンネルの{label}を取り込むぽん！しばらく待ってねぽん..."
        )

        total = 0
        for i, ch in enumerate(channels, 1):
            try:
                async def progress(count, _ch=ch, _i=i):
                    await interaction.followup.send(
                        f"📖 #{_ch.name}: {count:,}件取得中... ({_i}/{len(channels)})"
                    )

                count = await bot.corpus.backfill_channel(
                    ch, corpus, after=after, progress_callback=progress
                )
                total += count
                if count > 0:
                    await interaction.followup.send(f"✅ #{ch.name}: {count:,}件完了")
            except Exception as e:
                await interaction.followup.send(f"⚠️ #{ch.name}: エラー ({e})")

        await interaction.followup.send(f"🎉 取り込み完了！合計 **{total:,}件** 学習したぽん！")
