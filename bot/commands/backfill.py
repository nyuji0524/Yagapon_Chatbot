"""
/backfill - 過去ログ取り込み (期間指定可) - pycord
"""

import logging
from datetime import datetime, timedelta, timezone

import discord

log = logging.getLogger("yagapon.backfill")


def register(bot):
    @bot.slash_command(name="backfill", description="過去ログを取り込むぽん！")
    @discord.option(
        "days", description="何日分遡るか (省略で全量)",
        choices=[
            discord.OptionChoice("全部", 0),
            discord.OptionChoice("過去30日", 30),
            discord.OptionChoice("過去180日", 180),
            discord.OptionChoice("過去365日", 365),
        ],
        default=0,
    )
    @discord.option(
        "channel", description="特定チャンネルのみ取り込む場合に指定",
        type=discord.TextChannel, required=False, default=None,
    )
    async def backfill_cmd(
        ctx: discord.ApplicationContext,
        days: int = 0,
        channel: discord.TextChannel = None,
    ):
        await ctx.defer()

        corpus = bot.config.get_corpus(ctx.guild_id)
        if not corpus:
            await ctx.followup.send("先に `/setup` をしてほしいぽん！")
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
                ch for ch in ctx.guild.text_channels
                if ch.permissions_for(ctx.guild.me).read_message_history
                and not bot.config.is_ignored(ctx.guild_id, ch.id)
            ]

        status_msg = await ctx.followup.send(
            f"📚 {len(channels)}チャンネルの{label}を取り込むぽん！しばらく待ってねぽん...",
            wait=True,
        )

        total = 0
        for i, ch in enumerate(channels, 1):
            try:
                async def progress(c, _ch=ch, _i=i):
                    await status_msg.edit(
                        content=f"📚 {label}取り込み中... ({_i}/{len(channels)}) #{_ch.name}: {c:,}件取得中... | 合計: {total:,}件"
                    )

                count = await bot.corpus.backfill_channel(ch, corpus, after=after, progress_callback=progress)
                total += count
                await status_msg.edit(
                    content=f"📚 {label}取り込み中... ({i}/{len(channels)}) #{ch.name}: {count:,}件完了 | 合計: {total:,}件"
                )
            except Exception as e:
                log.warning(f"Backfill error #{ch.name}: {e}")

        await status_msg.edit(content=f"🎉 取り込み完了！合計 **{total:,}件** 学習したぽん！")
