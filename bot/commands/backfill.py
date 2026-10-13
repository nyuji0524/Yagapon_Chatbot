"""/backfill - 差分・再構築に対応したDiscord履歴取り込み。"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord

from bot.authorization import require_guild_admin
from bot.corpus import (
    backfill_channel_concurrency,
    calculate_resumable_incremental_after,
    run_backfill_channels,
)

log = logging.getLogger("yagapon.backfill")


class BackfillStatus:
    """長時間処理からDiscordの期限付きWebhookを切り離す進捗表示。"""

    def __init__(self, channel, message=None):
        self.channel = channel
        self.message = message
        self._lock = asyncio.Lock()

    async def update(self, content: str) -> bool:
        async with self._lock:
            if self.message is None:
                return False
            try:
                await self.message.edit(content=content)
                return True
            except Exception as exc:
                log.warning("Backfill status update disabled: %s", exc)
                self.message = None
                return False

    async def finish(self, content: str) -> None:
        if await self.update(content):
            return
        try:
            self.message = await self.channel.send(content, silent=True)
        except Exception as exc:
            log.exception("Could not publish final backfill status: %s", exc)


def _incremental_after(config, guild_id: int, channel_id: int) -> datetime | None:
    return calculate_resumable_incremental_after(config, guild_id, channel_id)


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
        "dry_run",
        description="更新せず対象件数だけ確認する",
        type=bool,
        default=False,
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
        dry_run: bool = False,
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
        concurrency = min(len(channels), backfill_channel_concurrency())
        initial_status = (
            f"📚 {len(channels)}チャンネルの{label}を最大{concurrency}チャンネル並列で取り込むぽん。"
            "しばらく待ってねぽん..."
        )
        status_msg = None
        try:
            # Followup messages depend on a short-lived interaction webhook.
            # A normal bot-authored message remains editable throughout a long backfill.
            status_msg = await ctx.channel.send(initial_status, silent=True)
        except Exception as exc:
            log.warning("Could not create durable backfill status message: %s", exc)
            try:
                status_msg = await ctx.followup.send(initial_status, wait=True, silent=True)
            except Exception as fallback_exc:
                log.warning("Backfill will continue without a status message: %s", fallback_exc)
        status = BackfillStatus(ctx.channel, status_msg)
        total_messages = 0
        total_documents = 0
        total_replaced = 0
        failures = []
        completed_channels = 0
        state_lock = asyncio.Lock()

        async def process_channel(ch):
            nonlocal total_messages, total_documents, total_replaced, completed_channels
            after = (
                _incremental_after(bot.config, ctx.guild_id, ch.id)
                if mode == "incremental"
                else (datetime.now(timezone.utc) - timedelta(days=days) if days > 0 else None)
            )
            try:
                async def progress(count):
                    async with state_lock:
                        indexed = total_messages
                        completed = completed_channels
                    await status.update(
                        f"📚 {label}取り込み中... ({completed}/{len(channels)}完了) "
                        f"#{ch.name}: {count:,}件確認中 | 索引済み: {indexed:,}件"
                    )

                result = await bot.corpus.backfill_channel(
                    ch,
                    corpus,
                    after=after,
                    progress_callback=progress,
                    replace_existing=True,
                    dry_run=dry_run,
                )
                if not dry_run and result.latest_message_id and result.latest_message_at:
                    await bot.config.set_backfill_cursor(
                        ctx.guild_id,
                        ch.id,
                        result.latest_message_id,
                        result.latest_message_at,
                    )
                async with state_lock:
                    total_messages += result.messages_indexed
                    total_documents += result.documents_uploaded
                    total_replaced += result.documents_replaced
                    completed_channels += 1
                    update_text = (
                        f"📚 {label}取り込み中... ({completed_channels}/{len(channels)}完了) #{ch.name}: "
                        f"{result.messages_indexed:,}件 / {result.documents_uploaded:,}文書 | "
                        f"合計: {total_messages:,}件"
                    )
                await status.update(update_text)
            except Exception as exc:
                async with state_lock:
                    failures.append(ch.name)
                    completed_channels += 1
                    update_text = (
                        f"⚠️ {label}取り込み中... ({completed_channels}/{len(channels)}完了) "
                        f"#{ch.name} は失敗。ほかのチャンネルは継続中だぽん。"
                    )
                log.exception("Backfill error #%s: %s", ch.name, exc)
                await status.update(update_text)

        try:
            await run_backfill_channels(channels, process_channel, concurrency=concurrency)
        finally:
            bot.corpus.finish_backfill(ctx.guild_id)

        failure_text = f"\n⚠️ 失敗: {', '.join(f'#{name}' for name in failures)}" if failures else ""
        await status.finish(
            f"✅ {'事前確認' if dry_run else '取り込み'}完了: "
            f"**{total_messages:,}件 / {total_documents:,}文書**\n"
            f"旧文書 **{total_replaced:,}件** を"
            f"{'置換対象として確認した' if dry_run else '安全に置換した'}ぽん。{failure_text}"
        )
