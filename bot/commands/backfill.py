"""/backfill - 差分・再構築に対応したDiscord履歴取り込み。"""

import logging
from datetime import datetime, timedelta, timezone

import discord

from bot.authorization import require_guild_admin
from bot.corpus import calculate_incremental_after

log = logging.getLogger("yagapon.backfill")


def _incremental_after(config, guild_id: int, channel_id: int) -> datetime:
    return calculate_incremental_after(config.get_backfill_cursor(guild_id, channel_id))


def register(bot):
    @bot.slash_command(name="backfill", description="過去ログを安全に取り込むぽん！")
    @discord.default_permissions(administrator=True)
    @discord.option(
        "mode",
        description="差分更新、または指定期間の再構築",
        choices=[
            discord.OptionChoice("差分更新（推奨）", "incremental"),
            discord.OptionChoice("指定期間を再構築", "rebuild"),
        ],
        default="incremental",
    )
    @discord.option(
        "days",
        description="再構築で何日分遡るか",
        choices=[
            discord.OptionChoice("全部", 0),
            discord.OptionChoice("過去30日", 30),
            discord.OptionChoice("過去180日", 180),
            discord.OptionChoice("過去365日", 365),
        ],
        default=30,
    )
    @discord.option(
        "channel",
        description="特定チャンネルのみ取り込む場合に指定",
        type=discord.TextChannel,
        required=False,
        default=None,
    )
    async def backfill_cmd(
        ctx: discord.ApplicationContext,
        mode: str = "incremental",
        days: int = 30,
        channel: discord.TextChannel = None,
    ):
        if not await require_guild_admin(ctx):
            return
        await ctx.defer()

        corpus = bot.config.get_corpus(ctx.guild_id)
        if not corpus:
            await ctx.followup.send("先に `/setup` をしてほしいぽん！", silent=True)
            return
        if not bot.corpus.start_backfill(ctx.guild_id):
            await ctx.followup.send("このサーバーでは別の取り込みが実行中だぽん。完了後に試してねぽん。", silent=True)
            return

        channels = [channel] if channel else [
            ch for ch in ctx.guild.text_channels
            if ch.permissions_for(ctx.guild.me).read_message_history
            and not bot.config.is_ignored(ctx.guild_id, ch.id)
        ]
        label = "差分" if mode == "incremental" else ("全期間" if days == 0 else f"過去{days}日")
        try:
            status_msg = await ctx.followup.send(
                f"📚 {len(channels)}チャンネルの{label}を取り込むぽん。しばらく待ってねぽん...",
                wait=True,
                silent=True,
            )
        except Exception:
            bot.corpus.finish_backfill(ctx.guild_id)
            raise
        total_messages = 0
        total_documents = 0
        total_replaced = 0
        failures = []
        try:
            for i, ch in enumerate(channels, 1):
                after = (
                    _incremental_after(bot.config, ctx.guild_id, ch.id)
                    if mode == "incremental"
                    else (datetime.now(timezone.utc) - timedelta(days=days) if days > 0 else None)
                )
                try:
                    async def progress(count, _ch=ch, _i=i):
                        await status_msg.edit(
                            content=(
                                f"📚 {label}取り込み中... ({_i}/{len(channels)}) "
                                f"#{_ch.name}: {count:,}件確認中 | 索引済み: {total_messages:,}件"
                            )
                        )

                    result = await bot.corpus.backfill_channel(
                        ch,
                        corpus,
                        after=after,
                        progress_callback=progress,
                        replace_existing=True,
                    )
                    if result.latest_message_id and result.latest_message_at:
                        await bot.config.set_backfill_cursor(
                            ctx.guild_id,
                            ch.id,
                            result.latest_message_id,
                            result.latest_message_at,
                        )
                    total_messages += result.messages_indexed
                    total_documents += result.documents_uploaded
                    total_replaced += result.documents_replaced
                    await status_msg.edit(
                        content=(
                            f"📚 {label}取り込み中... ({i}/{len(channels)}) #{ch.name}: "
                            f"{result.messages_indexed:,}件 / {result.documents_uploaded:,}文書"
                        )
                    )
                except Exception as exc:
                    failures.append(ch.name)
                    log.exception("Backfill error #%s: %s", ch.name, exc)
        finally:
            bot.corpus.finish_backfill(ctx.guild_id)

        failure_text = f"\n⚠️ 失敗: {', '.join(f'#{name}' for name in failures)}" if failures else ""
        await status_msg.edit(
            content=(
                f"✅ 取り込み完了: **{total_messages:,}件 / {total_documents:,}文書**\n"
                f"旧文書 **{total_replaced:,}件** を安全に置換したぽん。{failure_text}"
            )
        )
