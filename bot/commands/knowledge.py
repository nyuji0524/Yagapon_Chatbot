"""Discord commands for review-gated Knowledge source registration."""

import asyncio
import hashlib
import logging
import os
from pathlib import Path

import discord
from google.auth.exceptions import RefreshError

from bot.authorization import require_guild_admin
from bot.gdrive import DOCS_SCOPE, DRIVE_FILE_SCOPE
from bot.google_credentials import GoogleCredentialScopeError
from bot.knowledge_intake import (
    DiscordMessageSource,
    build_discord_intake_bundle,
    build_drive_intake_bundle,
    parse_discord_message_url,
    parse_drive_file_url,
)
from bot.knowledge_pipeline import KnowledgePipelineBusy
from bot.knowledge_policy import (
    knowledge_guild_allowed as _knowledge_guild_allowed,
)
from bot.knowledge_policy import (
    protected_channel as _protected_channel,
)
from bot.knowledge_publish import (
    KnowledgePublishResult,
    get_knowledge_delivery,
    list_knowledge_deliveries,
    publish_knowledge_bundle,
    retry_knowledge_delivery,
)
from bot.scheduled_backfill import configured_guild_ids, scheduler_enabled
from knowledge_sync.draft import redact_credentials, structure_source
from knowledge_sync.drive import DriveReader

log = logging.getLogger("yagapon.commands.knowledge")


def _curation_state_path() -> Path:
    return Path(
        os.environ.get(
            "YAGAPON_KNOWLEDGE_CURATION_STATE",
            "/data/knowledge-curation.sqlite3",
        )
    )


def _curation_batch_limit() -> int:
    try:
        configured = int(os.environ.get("YAGAPON_KNOWLEDGE_CURATION_BATCH", "3"))
    except ValueError:
        configured = 3
    return max(1, min(configured, 10))


def _run_curation_cycle_sync() -> dict:
    """Run one curation cycle under the same cross-process lock as the worker."""
    from knowledge_curation.__main__ import curate_once, curation_lock

    state_path = _curation_state_path()
    with curation_lock(state_path):
        return asyncio.run(
            curate_once(
                state_path=state_path,
                limit=_curation_batch_limit(),
            )
        )


async def _run_curation_cycle() -> dict:
    return await asyncio.to_thread(_run_curation_cycle_sync)


async def _run_knowledge_refresh(bot, *, mode: str) -> dict:
    """Run the operator-triggered pipeline without bypassing production gates."""
    if mode not in {"full", "historical", "curation"}:
        raise ValueError(f"Unsupported Knowledge refresh mode: {mode}")
    result: dict[str, object] = {"mode": mode}
    if mode in {"full", "historical"}:
        if mode == "historical":
            backfill = await bot.scheduled_backfill.run_once(full_history=True)
        else:
            backfill = await bot.scheduled_backfill.run_once()
        result["backfill"] = backfill
        if backfill.get("status") != "completed":
            result["curation"] = {
                "enabled": True,
                "checkpoint_ready": False,
                "checkpoint_reason": "manual-backfill-not-completed",
                "processed": 0,
                "queued": 0,
                "skipped": 0,
                "failed": 0,
            }
            return result
    result["curation"] = await _run_curation_cycle()
    return result


def _knowledge_refresh_status(result: dict) -> str:
    backfill = result.get("backfill") or {}
    curation = result.get("curation") or {}
    if result.get("error_type"):
        return "failed"
    if backfill and backfill.get("status") != "completed":
        return str(backfill.get("status") or "failed")
    if not curation.get("enabled", False):
        return "disabled"
    if not curation.get("checkpoint_ready", True):
        return "deferred"
    if int(curation.get("failed") or 0) > 0:
        return "partial"
    return "completed"


def _knowledge_refresh_embed(result: dict) -> discord.Embed:
    status = _knowledge_refresh_status(result)
    presentations = {
        "completed": ("✅ Knowledge更新が完了したぽん", discord.Color.green()),
        "partial": ("⚠️ Knowledge更新が一部失敗したぽん", discord.Color.orange()),
        "deferred": ("⏳ Knowledge更新を保留したぽん", discord.Color.orange()),
        "disabled": ("⏸️ Knowledge整理は無効だぽん", discord.Color.light_grey()),
        "busy": ("⏳ 別のKnowledge更新が実行中だぽん", discord.Color.orange()),
        "failed": ("❌ Knowledge更新に失敗したぽん", discord.Color.red()),
    }
    title, color = presentations.get(status, presentations["failed"])
    embed = discord.Embed(title=title, color=color)
    backfill = result.get("backfill") or {}
    if backfill:
        guilds = list((backfill.get("guilds") or {}).values())
        backfill_label = "Discord全期間取得" if result.get("mode") == "historical" else "Discord差分取得"
        embed.add_field(
            name=backfill_label,
            value=(
                f"状態: `{backfill.get('status', 'unknown')}`\n"
                f"チャンネル: {sum(int(item.get('channels_completed') or 0) for item in guilds)} / "
                f"{sum(int(item.get('channels_total') or 0) for item in guilds)}\n"
                f"メッセージ: {sum(int(item.get('messages_indexed') or 0) for item in guilds)}\n"
                f"文書: {sum(int(item.get('documents_uploaded') or 0) for item in guilds)}\n"
                f"置換: {sum(int(item.get('documents_replaced') or 0) for item in guilds)}"
            ),
            inline=False,
        )
        annual: dict[str, dict[str, int]] = {}
        for guild in guilds:
            for festival, values in (guild.get("festivals") or {}).items():
                target = annual.setdefault(
                    str(festival),
                    {"messages_indexed": 0, "documents_uploaded": 0, "documents_replaced": 0},
                )
                for key in target:
                    target[key] += int((values or {}).get(key) or 0)
        if annual:
            def festival_sort(item):
                value = str(item[0]).removesuffix("th")
                return int(value) if value.isdecimal() else 0

            lines = [
                (
                    f"**{festival}** — 発言 {values['messages_indexed']} / "
                    f"文書 {values['documents_uploaded']} / 置換 {values['documents_replaced']}"
                )
                for festival, values in sorted(annual.items(), key=festival_sort)
            ]
            embed.add_field(
                name="年度別結果（10月〜翌9月）",
                value="\n".join(lines)[:1024],
                inline=False,
            )
    curation = result.get("curation") or {}
    checkpoint = str(curation.get("checkpoint_reason") or "ready")
    embed.add_field(
        name="議題別Knowledge整理",
        value=(
            f"checkpoint: `{checkpoint}`\n"
            f"処理: {int(curation.get('processed') or 0)} / "
            f"Draft送信: {int(curation.get('queued') or 0)} / "
            f"見送り: {int(curation.get('skipped') or 0)} / "
            f"失敗: {int(curation.get('failed') or 0)}\n"
            f"再送: {int(curation.get('retried') or 0)} / "
            f"未確認bundle: {int(curation.get('open_bundles') or 0)} / "
            f"PR待ち: {int(curation.get('backpressured') or 0)}"
        ),
        inline=False,
    )
    embed.set_footer(text="自動更新は6時間ごと。生成物はDraft PRで人の確認が必要です。")
    return embed


def _result_message(result: KnowledgePublishResult) -> str:
    if result.pull_request_url:
        return (
            "✅ Knowledgeの**Draft PR**を作成したぽん！\n"
            f"🗂️ [内容を確認する]({result.pull_request_url})\n"
            "原本・公開範囲・個人情報・年度・分類を確認し、問題なければReadyにしてレビューしてほしいぽん。"
        )
    if result.status == "blocked-sensitive-content":
        return (
            "🛑 認証情報らしき内容を検出したため、GitHubへの送信を止めたぽん。\n"
            f"申請ID: `{result.session_id}`\n"
            "サーバー上の候補を管理者が確認・修正するまで再送できないぽん。"
        )
    if result.status == "queued":
        return (
            "📤 Knowledge登録キューへ安全に送ったぽん！\n"
            f"申請ID: `{result.session_id}`\n"
            "KnowledgeリポジトリのパイプラインがDraft PRを作るので、内容をレビューしてほしいぽん。"
        )
    if result.status == "failed":
        return (
            "⚠️ 候補は安全に保存したけれど、Draft PRの作成に失敗したぽん。\n"
            f"申請ID: `{result.session_id}`\n"
            "接続を直したあと `/knowledge retry` で再送できるぽん。"
        )
    return (
        "📥 Knowledge候補を保存したぽん。GitHub送信用のサービス資格情報が未設定なので、"
        "まだDraft PRにはなっていないぽん。\n"
        f"申請ID: `{result.session_id}`\n"
        "設定後に `/knowledge retry` で再送できるぽん。"
    )


async def _require_knowledge_guild(ctx) -> bool:
    if ctx.guild and _knowledge_guild_allowed(ctx.guild.id):
        return True
    await ctx.respond(
        "このサーバーはKnowledge登録先として許可されていないぽん。",
        ephemeral=True,
    )
    return False


async def _publish(bot, ctx, args) -> None:
    bundle_id, repository_root, files, manifest, branch, title, body = args
    result = await publish_knowledge_bundle(
        bundle_id=bundle_id,
        repository_root=repository_root,
        files=files,
        manifest=manifest,
        branch=branch,
        title=title,
        body=body,
    )
    if hasattr(bot, "operations"):
        bot.operations.record_event(
            "knowledge",
            "register_source",
            result.status,
            guild_id=ctx.guild_id,
            item_key=bundle_id,
            details={"source": manifest.get("source"), "pull_request": bool(result.pull_request_url)},
        )
    await ctx.followup.send(_result_message(result), ephemeral=True)


async def _resolve_channel(bot, guild, channel_id: int):
    channel = guild.get_channel(channel_id)
    if channel is None and hasattr(guild, "get_thread"):
        channel = guild.get_thread(channel_id)
    if channel is None:
        channel = await bot.fetch_channel(channel_id)
    return channel


def _permission_channel(channel, parent):
    """Return the channel whose overwrites govern access to ``channel``."""
    return parent if getattr(channel, "parent_id", None) else channel


async def _channel_lineage(bot, guild, channel):
    """Resolve a thread's parent/category even when they are not cached."""
    parent = getattr(channel, "parent", None)
    parent_id = getattr(channel, "parent_id", None)
    if parent is None and parent_id:
        parent = await _resolve_channel(bot, guild, parent_id)

    category = (
        getattr(channel, "category", None)
        or getattr(parent, "category", None)
    )
    category_id = (
        getattr(channel, "category_id", None)
        or getattr(parent, "category_id", None)
    )
    if category is None and category_id:
        category = await _resolve_channel(bot, guild, category_id)
    return parent, category


async def _discord_messages(channel, selected, limit: int) -> list[DiscordMessageSource]:
    fetched = []
    async for message in channel.history(
        limit=max(1, min(limit, 50)),
        around=selected,
    ):
        fetched.append(message)
    if all(message.id != selected.id for message in fetched):
        fetched.append(selected)
    unique = {message.id: message for message in fetched}
    result = []
    for message in sorted(unique.values(), key=lambda item: (item.created_at, item.id)):
        if not str(getattr(message, "content", "") or "").strip() and not getattr(
            message, "attachments", None
        ) and message.id != selected.id:
            continue
        author = getattr(message, "author", None)
        result.append(
            DiscordMessageSource(
                message_id=message.id,
                author_id=getattr(author, "id", 0),
                author_name=(
                    getattr(author, "display_name", None)
                    or getattr(author, "name", None)
                    or "不明"
                ),
                content=str(getattr(message, "content", "") or ""),
                timestamp=message.created_at,
                jump_url=message.jump_url,
                attachment_urls=[attachment.url for attachment in message.attachments],
            )
        )
    return result


def _is_scope_refresh_error(exc: RefreshError) -> bool:
    message = str(exc).casefold()
    return "invalid_scope" in message or "scope" in message


def _read_drive_source(file_id: str):
    """Prefer full read-only access, then retain current drive.file compatibility."""
    credentials_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or None
    try:
        reader = DriveReader(credentials_path, "")
        document = reader.get_document(file_id)
        return document, reader.read_text(document), "drive.readonly"
    except (GoogleCredentialScopeError, RefreshError) as exc:
        if isinstance(exc, RefreshError) and not _is_scope_refresh_error(exc):
            raise
        reader = DriveReader(
            credentials_path,
            "",
            scopes=[DRIVE_FILE_SCOPE, DOCS_SCOPE],
        )
        document = reader.get_document(file_id)
        return document, reader.read_text(document), "drive.file"


def register(bot):
    group = bot.create_group("knowledge", "正式ナレッジへの登録候補を管理するぽん")
    manual_refresh_lock = asyncio.Lock()
    manual_refresh_tasks: set[asyncio.Task] = getattr(
        bot,
        "knowledge_refresh_tasks",
        set(),
    )

    @group.command(name="refresh", description="Discord履歴取得とKnowledge整理を今すぐ実行するぽん")
    @discord.default_permissions(administrator=True)
    @discord.option(
        "mode",
        description="差分・全期間・整理のみのどれを実行するか",
        choices=[
            discord.OptionChoice("Discord差分取得 → Knowledge整理（推奨）", "full"),
            discord.OptionChoice("全期間を年度別に再構築 → Knowledge整理", "historical"),
            discord.OptionChoice("Knowledge整理のみ", "curation"),
        ],
        default="full",
    )
    async def knowledge_refresh(
        ctx: discord.ApplicationContext,
        mode: str = "full",
    ):
        if not await require_guild_admin(ctx):
            return
        if not ctx.guild:
            await ctx.respond("サーバー内で実行してほしいぽん。", ephemeral=True)
            return
        if not await _require_knowledge_guild(ctx):
            return
        if os.environ.get("YAGAPON_KNOWLEDGE_CURATION_ENABLED", "false").lower() != "true":
            await ctx.respond(
                "⏸️ Knowledge自動整理は本番設定でまだ有効になっていないぽん。",
                ephemeral=True,
            )
            return
        if mode in {"full", "historical"} and (
            not scheduler_enabled() or ctx.guild.id not in configured_guild_ids()
        ):
            await ctx.respond(
                "⏸️ このサーバーのDiscord履歴取得が許可されていないぽん。"
                "固定Guild IDの設定を管理者に確認してほしいぽん。",
                ephemeral=True,
            )
            return
        if manual_refresh_lock.locked():
            await ctx.respond(
                "⏳ このBotでは別のKnowledge即時更新が実行中だぽん。完了後に試してほしいぽん。",
                ephemeral=True,
            )
            return
        await manual_refresh_lock.acquire()
        try:
            duration_note = "全期間を年度別に確認するため、かなり時間がかかる場合があるぽん。" if mode == "historical" else ""
            await ctx.respond(
                "🚀 Knowledge更新を開始したぽん。" + duration_note + "長時間かかっても処理を継続し、"
                "完了結果をこのチャンネルへ表示するぽん。",
                ephemeral=True,
            )
        except Exception:
            manual_refresh_lock.release()
            raise
        channel = ctx.channel
        guild_id = ctx.guild.id
        requester = getattr(ctx.author, "display_name", None) or getattr(ctx.author, "name", "管理者")

        async def run_and_report() -> None:
            try:
                try:
                    result = await _run_knowledge_refresh(bot, mode=mode)
                except KnowledgePipelineBusy:
                    result = {
                        "mode": mode,
                        "curation": {
                            "enabled": True,
                            "checkpoint_ready": False,
                            "checkpoint_reason": "knowledge-pipeline-busy",
                        },
                    }
                except RuntimeError as exc:
                    if str(exc) != "Another Knowledge curation run is active":
                        raise
                    result = {
                        "mode": mode,
                        "curation": {
                            "enabled": True,
                            "checkpoint_ready": False,
                            "checkpoint_reason": "curation-already-running",
                        },
                    }
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.exception("Manual Knowledge refresh failed")
                    result = {
                        "mode": mode,
                        "curation": {
                            "enabled": True,
                            "checkpoint_ready": True,
                            "failed": 1,
                        },
                        "error_type": type(exc).__name__,
                    }
                status = _knowledge_refresh_status(result)
                operations = getattr(bot, "operations", None)
                if operations is not None and hasattr(operations, "record_event"):
                    try:
                        curation = result.get("curation") or {}
                        operations.record_event(
                            "knowledge",
                            "manual_refresh",
                            status,
                            guild_id=guild_id,
                            details={
                                "mode": mode,
                                "backfill_status": (result.get("backfill") or {}).get("status"),
                                "checkpoint_ready": curation.get("checkpoint_ready"),
                                "processed": int(curation.get("processed") or 0),
                                "queued": int(curation.get("queued") or 0),
                                "failed": int(curation.get("failed") or 0),
                            },
                        )
                    except Exception:
                        log.exception("Could not record manual Knowledge refresh telemetry")
                try:
                    embed = _knowledge_refresh_embed(result)
                    embed.set_author(name=f"実行者: {requester}")
                    await channel.send(embed=embed, silent=True)
                except Exception:
                    log.exception("Could not publish manual Knowledge refresh result")
            finally:
                manual_refresh_lock.release()

        try:
            task = asyncio.create_task(
                run_and_report(),
                name=f"manual-knowledge-refresh-{guild_id}",
            )
        except Exception:
            manual_refresh_lock.release()
            raise
        manual_refresh_tasks.add(task)
        task.add_done_callback(manual_refresh_tasks.discard)

    @group.command(name="discord", description="Discordメッセージを確認待ち資料として申請するぽん")
    @discord.default_permissions(administrator=True)
    @discord.option("message_url", description="登録したいDiscordメッセージのリンク")
    @discord.option("title", description="資料のタイトル（省略可）", required=False, default="")
    @discord.option(
        "context_messages",
        description="前後を含めて取得する最大メッセージ数（1〜50）",
        required=False,
        default=21,
    )
    async def knowledge_discord(
        ctx: discord.ApplicationContext,
        message_url: str,
        title: str = "",
        context_messages: int = 21,
    ):
        if not await require_guild_admin(ctx):
            return
        if not ctx.guild:
            await ctx.respond("サーバー内で実行してほしいぽん。", ephemeral=True)
            return
        if not await _require_knowledge_guild(ctx):
            return
        try:
            guild_id, channel_id, message_id = parse_discord_message_url(message_url)
        except ValueError as exc:
            await ctx.respond(f"❌ {exc}ぽん。", ephemeral=True)
            return
        if guild_id != ctx.guild.id:
            await ctx.respond("❌ このサーバーのメッセージだけ登録できるぽん。", ephemeral=True)
            return
        context_messages = max(1, min(int(context_messages), 50))
        await ctx.defer(ephemeral=True)
        try:
            channel = await _resolve_channel(bot, ctx.guild, channel_id)
            if getattr(getattr(channel, "guild", None), "id", None) != ctx.guild.id:
                await ctx.followup.send(
                    "❌ このサーバー外のチャンネルは登録できないぽん。",
                    ephemeral=True,
                )
                return
            parent, category = await _channel_lineage(bot, ctx.guild, channel)
            # Discord threads inherit access from their parent.  A thread fetched
            # through the API can have ``parent is None`` in Pycord's cache, and
            # Thread.permissions_for() then dereferences that missing parent.
            permissions = _permission_channel(channel, parent).permissions_for(ctx.author)
            if not permissions.view_channel or not permissions.read_message_history:
                await ctx.followup.send(
                    "❌ 実行者が閲覧できないチャンネルは登録できないぽん。",
                    ephemeral=True,
                )
                return
            parent_id = getattr(channel, "parent_id", None) or getattr(parent, "id", None)
            if bot.config.is_ignored(ctx.guild.id, channel_id) or (
                parent_id and bot.config.is_ignored(ctx.guild.id, parent_id)
            ):
                await ctx.followup.send(
                    "❌ `/ignore` 対象のチャンネルはKnowledgeにも登録しないぽん。",
                    ephemeral=True,
                )
                return
            if _protected_channel(channel, parent=parent, category=category):
                await ctx.followup.send(
                    "❌ このチャンネルは機微情報を扱うためKnowledge登録対象外だぽん。",
                    ephemeral=True,
                )
                return
            selected = await channel.fetch_message(message_id)
            messages = await _discord_messages(channel, selected, context_messages)
            args = build_discord_intake_bundle(
                guild_id=ctx.guild.id,
                channel_id=channel_id,
                channel_name=getattr(channel, "name", str(channel_id)),
                selected_message_id=message_id,
                requested_by=ctx.author.id,
                messages=messages,
                title=title,
            )
            await _publish(bot, ctx, args)
        except discord.Forbidden:
            await ctx.followup.send(
                "❌ やがぽんが対象チャンネルを読めないぽん。閲覧・履歴権限を確認してほしいぽん。",
                ephemeral=True,
            )
        except discord.NotFound:
            await ctx.followup.send("❌ 対象メッセージが見つからないぽん。", ephemeral=True)
        except Exception as exc:
            log.exception("Discord Knowledge registration failed")
            if hasattr(bot, "operations"):
                bot.operations.record_event(
                    "knowledge",
                    "register_source",
                    "failed",
                    guild_id=ctx.guild_id,
                    item_key=str(message_id),
                    details={"source": "discord", "error_type": type(exc).__name__},
                )
            await ctx.followup.send(
                f"❌ 登録候補の作成に失敗したぽん（`{type(exc).__name__}`）。",
                ephemeral=True,
            )

    @group.command(name="drive", description="Google Drive資料を確認待ち資料として申請するぽん")
    @discord.default_permissions(administrator=True)
    @discord.option("drive_url", description="Google Docs・Sheets・Slides・DriveファイルのURL")
    async def knowledge_drive(ctx: discord.ApplicationContext, drive_url: str):
        if not await require_guild_admin(ctx):
            return
        if not ctx.guild:
            await ctx.respond("サーバー内で実行してほしいぽん。", ephemeral=True)
            return
        if not await _require_knowledge_guild(ctx):
            return
        try:
            file_id = parse_drive_file_url(drive_url)
        except ValueError as exc:
            await ctx.respond(f"❌ {exc}ぽん。", ephemeral=True)
            return
        await ctx.defer(ephemeral=True)
        try:
            document, source_text, access_mode = await asyncio.to_thread(
                _read_drive_source,
                file_id,
            )
            if source_text is None:
                await ctx.followup.send(
                    "❌ この資料形式は直接テキスト化できないぽん。"
                    "Google Docs・Sheets・Slides、またはテキスト系ファイルを指定してほしいぽん。",
                    ephemeral=True,
                )
                return
            sanitized, redaction_count = redact_credentials(source_text)
            source_sha256 = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            structured = await asyncio.to_thread(
                structure_source,
                sanitized,
                os.environ.get("YAGAPON_KNOWLEDGE_MODEL", "gemini-3.8-flash"),
            )
            args = build_drive_intake_bundle(
                guild_id=ctx.guild.id,
                requested_by=ctx.author.id,
                document=document,
                source_text=sanitized,
                source_sha256=source_sha256,
                structured=structured,
                redaction_count=redaction_count,
                access_mode=access_mode,
            )
            await _publish(bot, ctx, args)
        except Exception as exc:
            log.exception("Drive Knowledge registration failed")
            if hasattr(bot, "operations"):
                bot.operations.record_event(
                    "knowledge",
                    "register_source",
                    "failed",
                    guild_id=ctx.guild_id,
                    item_key=file_id,
                    details={"source": "drive", "error_type": type(exc).__name__},
                )
            await ctx.followup.send(
                "❌ Drive資料の取得または整理に失敗したぽん。"
                f"やがぽんに資料の閲覧権限があるか確認してほしいぽん（`{type(exc).__name__}`）。",
                ephemeral=True,
            )

    @group.command(name="status", description="最近のKnowledge登録申請を確認するぽん")
    @discord.default_permissions(administrator=True)
    async def knowledge_status(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        if not await _require_knowledge_guild(ctx):
            return
        await ctx.defer(ephemeral=True)
        entries = await asyncio.to_thread(
            list_knowledge_deliveries,
            guild_id=ctx.guild_id,
            limit=10,
        )
        if not entries:
            await ctx.followup.send("このサーバーの登録申請はまだないぽん。", ephemeral=True)
            return
        icons = {
            "published": "✅",
            "pending": "📥",
            "failed": "⚠️",
            "blocked-sensitive-content": "🛑",
        }
        lines = ["**最近のKnowledge登録申請**"]
        for entry in entries:
            status = str(entry.get("status") or "unknown")
            bundle_id = entry.get("bundleId") or entry.get("sessionId") or "不明"
            source = (entry.get("manifest") or {}).get("source") or "unknown"
            line = f"{icons.get(status, '•')} `{bundle_id}` — {source} / {status}"
            if entry.get("pullRequestUrl"):
                line += f" — [Draft PR]({entry['pullRequestUrl']})"
            lines.append(line)
        await ctx.followup.send("\n".join(lines), ephemeral=True)

    @group.command(name="retry", description="保存済みのKnowledge登録申請を再送するぽん")
    @discord.default_permissions(administrator=True)
    @discord.option("bundle_id", description="statusに表示された申請ID")
    async def knowledge_retry(ctx: discord.ApplicationContext, bundle_id: str):
        if not await require_guild_admin(ctx):
            return
        if not await _require_knowledge_guild(ctx):
            return
        entry = await asyncio.to_thread(get_knowledge_delivery, bundle_id)
        if entry is None:
            await ctx.respond("❌ 指定された申請IDが見つからないぽん。", ephemeral=True)
            return
        manifest = (entry or {}).get("manifest") or {}
        if str(manifest.get("guildId") or "") != str(ctx.guild_id):
            await ctx.respond("❌ このサーバーの申請IDではないぽん。", ephemeral=True)
            return
        if entry.get("status") == "blocked-sensitive-content":
            await ctx.respond(
                "🛑 この申請は機密検知で停止中だぽん。サーバー上の候補から該当内容を除去し、"
                "管理者が確認するまで再送できないぽん。",
                ephemeral=True,
            )
            return
        await ctx.defer(ephemeral=True)
        try:
            result = await retry_knowledge_delivery(bundle_id)
        except Exception as exc:
            log.exception("Knowledge delivery retry failed")
            await ctx.followup.send(
                f"❌ 再送できなかったぽん（`{type(exc).__name__}`）。",
                ephemeral=True,
            )
            return
        await ctx.followup.send(_result_message(result), ephemeral=True)
