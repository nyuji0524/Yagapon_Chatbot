"""YagaPon Discord Client - メイン処理 (pycord)"""

import asyncio
import io
import logging
import re

import discord

from bot.config import ConfigManager
from bot.corpus import CorpusManager
from bot.key_management import KeyManagementManager
from bot.operations import OperationalStore
from bot.recording import cleanup_old_recordings
from bot.recording_replay import RecordingReplayManager
from bot.scheduled_backfill import ScheduledBackfillManager
from bot.support_directory import SupportDirectory
from knowledge_catalog.store import CatalogStore, normalize_label

log = logging.getLogger("yagapon.client")

RAG_FEEDBACK_PROMPT = "回答の品質を評価してください：✅ 正しい / ⚠️ 一部不正確 / ❌ 誤り（対応した絵文字でリアクション）"
RAG_FEEDBACK_RATINGS = {"✅": "positive", "⚠️": "partial", "❌": "negative"}


def _clean_rag_query(
    content: str,
    bot_user_id: int | None,
    bot_role_ids: tuple[int, ...] = (),
) -> str:
    """Remove addressing helpers while preserving the user's question structure."""
    query = content.replace("\r\n", "\n").replace("\r", "\n")
    if bot_user_id:
        query = query.replace(f"<@{bot_user_id}>", "").replace(f"<@!{bot_user_id}>", "")
    for role_id in bot_role_ids:
        query = query.replace(f"<@&{role_id}>", "")
    query = re.sub(r"(?i)(?<!\S)@silent(?!\S)", "", query)
    normalized_lines = []
    previous_blank = True
    for raw_line in query.split("\n"):
        line = re.sub(r"[\t\f\v ]+", " ", raw_line).strip()
        if not line:
            if not previous_blank:
                normalized_lines.append("")
            previous_blank = True
            continue
        normalized_lines.append(line)
        previous_blank = False
    while normalized_lines and not normalized_lines[-1]:
        normalized_lines.pop()
    return "\n".join(normalized_lines)


def _mentioned_bot_role_ids(message: discord.Message, bot_user) -> tuple[int, ...]:
    """Return mentioned roles assigned to the bot and named after the bot.

    Discord's composer can prefer a role over a user when both have the same
    display name.  Treat that role mention as an invocation only when the role
    is actually assigned to this bot, so an unrelated same-name role cannot
    trigger RAG answers.
    """
    guild = getattr(message, "guild", None)
    bot_member = getattr(guild, "me", None) if guild else None
    if not bot_user or not bot_member:
        return ()

    bot_names = {
        str(name).strip().casefold()
        for name in (
            getattr(bot_user, "name", ""),
            getattr(bot_user, "display_name", ""),
            getattr(bot_member, "display_name", ""),
        )
        if str(name).strip()
    }
    assigned_role_ids = {getattr(role, "id", None) for role in getattr(bot_member, "roles", ())}
    return tuple(
        role.id
        for role in getattr(message, "role_mentions", ())
        if getattr(role, "id", None) in assigned_role_ids
        and str(getattr(role, "name", "")).strip().casefold() in bot_names
    )


def _is_rag_invocation(message: discord.Message, bot_user) -> bool:
    """Whether a Discord message explicitly addresses this bot."""
    return bool(bot_user and (bot_user.mentioned_in(message) or _mentioned_bot_role_ids(message, bot_user)))


def _is_voice_reference_channel(message_channel, voice_channel) -> bool:
    """Accept only the active VC's own chat or a thread explicitly under it.

    Channel names and categories are not identities: unrelated text channels
    commonly share both.  Collecting those messages as meeting material can
    silently contaminate minutes and lets bystanders inject instructions.
    """
    voice_channel_id = getattr(voice_channel, "id", None)
    if voice_channel_id is None:
        return False
    return (
        getattr(message_channel, "id", None) == voice_channel_id
        or getattr(message_channel, "parent_id", None) == voice_channel_id
    )


def _is_ignored_with_parent(config, guild_id: int, channel) -> bool:
    """Apply /ignore to a channel and every thread below it."""
    identifiers = {
        value
        for value in (
            getattr(channel, "id", None),
            getattr(channel, "parent_id", None),
            getattr(getattr(channel, "parent", None), "id", None),
        )
        if value is not None
    }
    return any(config.is_ignored(guild_id, int(channel_id)) for channel_id in identifiers)


def _discord_channel_lineage(channel) -> dict[str, object]:
    """Return stable thread/parent/category identities for RAG access checks."""
    parent = getattr(channel, "parent", None)
    parent_id = getattr(channel, "parent_id", None) or getattr(parent, "id", None)
    parent_name = str(getattr(parent, "name", "") or "")
    category = getattr(channel, "category", None) or getattr(parent, "category", None)
    category_id = (
        getattr(channel, "category_id", None)
        or getattr(category, "id", None)
        or getattr(parent, "category_id", None)
    )
    return {
        "parent_id": int(parent_id) if parent_id is not None else None,
        "parent_name": parent_name,
        "category_id": int(category_id) if category_id is not None else None,
        "category_name": str(getattr(category, "name", "") or ""),
    }


def _guild_yagapon_emojis(guild) -> dict[str, str]:
    """Return custom YagaPon emojis which the bot can use in this guild."""
    emojis: dict[str, str] = {}
    for emoji in getattr(guild, "emojis", ()):
        name = getattr(emoji, "name", "")
        if not name.startswith("yagapon_") or getattr(emoji, "available", True) is False:
            continue
        is_usable = getattr(emoji, "is_usable", None)
        if callable(is_usable) and not is_usable():
            continue
        emojis[name] = str(emoji)
    return emojis


def _decorate_rag_answer(answer: str, guild, *, no_answer: bool) -> str:
    """Use guild YagaPon emojis without emitting broken ``:name:`` placeholders."""
    available = _guild_yagapon_emojis(guild)
    preferred = (
        ("yagapon_cry", "yagapon_bow", "yagapon_relaxing")
        if no_answer
        else ("yagapon_happy", "yagapon_glad", "yagapon_bow", "yagapon_kansha")
    )
    selected = [available[name] for name in preferred if name in available and available[name] not in answer][:2]
    if not selected:
        return answer
    if len(selected) == 1:
        return f"{selected[0]} {answer}"
    return f"{selected[0]} {answer}\n\n{selected[1]}"


def _split_discord_text(text: str, *, limit: int = 1900) -> list[str]:
    """Split arbitrary text without dropping oversized lines or breaking fences.

    Discord accepts at most 2,000 characters.  Reserve a few characters so an
    open Markdown code block can be closed in one message and reopened in the
    next.  Unlike the former line-only splitter, this also makes progress when
    one generated paragraph or code line is larger than the whole message.
    """
    if not text:
        return []
    if limit < 32:
        raise ValueError("Discord chunk limit is too small")

    content_limit = limit - 8
    chunks: list[str] = []
    current = ""
    in_code_block = False
    current_language = ""

    def flush() -> None:
        nonlocal current
        if not current:
            return
        payload = current
        if in_code_block and not payload.rstrip().endswith("```"):
            payload += "\n```"
        if payload:
            chunks.append(payload)
        current = f"```{current_language}\n" if in_code_block else ""

    for logical_line in text.splitlines(keepends=True):
        stripped = logical_line.strip()
        is_fence = stripped.startswith("```")
        # Never split the fence marker itself.  If it does not fit, finish the
        # current message first (closing/reopening an existing block as needed).
        if is_fence and current and len(current) + len(logical_line) > content_limit:
            flush()

        remaining = logical_line
        while remaining:
            available = content_limit - len(current)
            if available <= 0:
                flush()
                available = content_limit - len(current)
            current += remaining[:available]
            remaining = remaining[available:]
            if remaining:
                flush()

        if is_fence:
            if in_code_block:
                in_code_block = False
                current_language = ""
            else:
                in_code_block = True
                current_language = stripped[3:].strip()

    # ``splitlines(keepends=True)`` returns nothing for an empty string only;
    # a normal final line (with or without newline) is already in ``current``.
    flush()
    return chunks


class YagaPon(discord.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True
        super().__init__(intents=intents)

        self.config = ConfigManager()
        self.corpus = CorpusManager()
        self.operations = OperationalStore()
        self.key_management = KeyManagementManager(self)
        self.catalog = CatalogStore()
        self.recording_replays = RecordingReplayManager(self)
        self.scheduled_backfill = ScheduledBackfillManager(self)
        self.knowledge_refresh_tasks: set[asyncio.Task] = set()
        self.support_directory = SupportDirectory()
        self._recording_recovery_task: asyncio.Task | None = None
        self._close_lock = asyncio.Lock()
        self._close_completed = False

        # コマンド登録
        from bot.commands import (
            backfill,
            committee_server,
            corpus_cmd,
            drive,
            glossary,
            ignore,
            knowledge,
            meigen,
            member,
            minutes,
            recording,
            report,
            reset,
            setup,
            status,
            voice_cmd,
            voiceprint,
        )

        setup.register(self)
        committee_server.register_persistent_views(self)
        committee_server.register(self)
        status.register(self)
        ignore.register(self)
        backfill.register(self)
        member.register(self)
        meigen.register(self)
        voice_cmd.register(self)
        report.register(self)
        recording.register(self)
        reset.register(self)
        corpus_cmd.register(self)
        voiceprint.register(self)
        glossary.register(self)
        minutes.register(self)
        drive.register(self)
        knowledge.register(self)

    async def on_ready(self):
        self.corpus.start_flush_loop()
        self.key_management.start()
        self.scheduled_backfill.start()
        removed = await asyncio.to_thread(cleanup_old_recordings)
        if removed:
            log.info("Removed %s expired delivered recording(s)", removed)
        if self._recording_recovery_task is None or self._recording_recovery_task.done():
            from bot.voice import recording_recovery_loop

            self._recording_recovery_task = asyncio.create_task(
                recording_recovery_loop(self),
                name="recording-recovery",
            )
        log.info(f"おしゃべりやがぽん起動: {self.user}")

    async def on_application_command_error(self, ctx, error):
        """Always acknowledge failed slash commands without leaking internals."""
        root_error = getattr(error, "original", error)
        command = getattr(ctx, "command", None)
        command_name = getattr(command, "qualified_name", None) or getattr(command, "name", None) or "unknown"
        guild_id = getattr(ctx, "guild_id", None)
        if guild_id is None:
            guild_id = getattr(getattr(ctx, "guild", None), "id", None)
        interaction = getattr(ctx, "interaction", None)
        response = getattr(interaction, "response", None)
        responded = bool(response and response.is_done())

        log.error(
            "Application command failed command=%s guild=%s error_type=%s",
            command_name,
            guild_id,
            type(root_error).__name__,
            exc_info=(
                type(root_error),
                root_error,
                root_error.__traceback__,
            ),
        )
        try:
            self.operations.record_event(
                "discord_command",
                "application_command",
                "failed",
                guild_id=guild_id,
                item_key=command_name,
                details={
                    "error_type": type(root_error).__name__,
                    "responded_before_error": responded,
                },
            )
        except Exception:
            log.exception("Could not persist application-command failure metadata")

        message = (
            "処理中に問題が発生したぽん。少し待ってからもう一度試してねぽん。"
            "\n解決しない場合は、このコマンド名を管理者に伝えてほしいぽん。"
        )
        try:
            if responded:
                await ctx.followup.send(message, ephemeral=True)
            else:
                try:
                    await ctx.respond(message, ephemeral=True)
                except discord.InteractionResponded:
                    await ctx.followup.send(message, ephemeral=True)
        except Exception:
            # An expired/deleted interaction cannot be acknowledged.  The
            # original exception and safe metadata have already been logged.
            log.exception(
                "Could not acknowledge failed application command command=%s guild=%s",
                command_name,
                guild_id,
            )

    async def on_guild_join(self, guild: discord.Guild):
        for channel in guild.text_channels:
            if channel.permissions_for(guild.me).send_messages:
                await channel.send(
                    f"こんにちはぽん！矢上祭実行委員会「**{self.user.name}**」だぽん！\n"
                    f"このサーバーの会話を学習して、みんなの役に立ちたいぽん！\n"
                    f"まずは `/setup` で初期設定をしてほしいぽん！",
                    silent=True,
                )
                break

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ):
        """VCから人がいなくなったら自動退出"""
        # 誰かがVCから抜けた場合のみ処理。Discord側からbot自身が
        # 切断された場合も、セッションを取り残さず同じ回収経路へ送る。
        if not before.channel:
            return
        bot_departed_source = member.id == self.user.id and (
            after.channel is None or after.channel.id != before.channel.id
        )
        if member.id == self.user.id and not bot_departed_source:
            return

        # botがそのVCにいるか確認
        from bot.voice import VoiceMode, get_session, leave_voice

        session = get_session(member.guild.id)
        if not session or not session.voice_client:
            return
        if not bot_departed_source and not session.voice_client.is_connected():
            return
        if session.channel.id != before.channel.id:
            return

        if bot_departed_source:
            if getattr(session, "_intentional_stop", False):
                return
            session._capture_interrupted = True
            session._transcription_failed = True
            session.is_active = False
            if session.recording_sink:
                session.recording_sink.mark(
                    "capture_interrupted",
                    reason="bot-moved" if after.channel is not None else "bot-disconnected",
                )

        # VCに残っているのがbotだけか確認
        humans = [] if bot_departed_source else [m for m in before.channel.members if not m.bot]
        if len(humans) == 0:
            log.info(f"VC {before.channel.name} に誰もいなくなったので自動退出")

            if not session.claim_delivery():
                return
            try:
                minutes = await leave_voice(member.guild.id)
            except Exception as exc:
                if session.recording_sink:
                    from bot.recording import RecordingLimitExceeded

                    session.recording_sink.mark(
                        "capture_interrupted"
                        if getattr(session, "_capture_interrupted", False)
                        else (
                            "recording_truncated"
                            if isinstance(exc, RecordingLimitExceeded)
                            else "automatic_leave_failed"
                        ),
                        error=type(exc).__name__,
                    )
                log.exception("Automatic voice leave failed for guild %s", member.guild.id)
                return

            # Durable delivery must not depend on whether Discord has a
            # writable notification channel.  Any failure before
            # mark_delivered keeps the PCM recoverable for the retry worker.
            try:
                from bot.gdrive import upload_minutes, upload_transcript
                from bot.meeting_index import index_bot_meeting

                export_decision = session.external_export_decision()
                session.checkpoint_delivery_progress(
                    external_export_expected=export_decision.allowed,
                    external_export_reason=export_decision.reason,
                )
                if not export_decision.allowed:
                    log.info(
                        "External meeting export skipped guild=%s channel=%s reason=%s",
                        member.guild.id,
                        session.channel.id,
                        export_decision.reason,
                    )
                transcript_url = (
                    await upload_transcript(
                        self.config,
                        member.guild.id,
                        session.transcript_text,
                        session.channel.name,
                        meeting_at=session.started_at,
                        session_id=session.session_id,
                    )
                    if export_decision.allowed
                    else None
                )
                session.checkpoint_delivery_progress(
                    transcript_url=transcript_url or "",
                    transcript_path=str(getattr(session, "transcript_path", "") or ""),
                )

                if not minutes and session.mode in (VoiceMode.LISTEN, VoiceMode.MEETING):
                    raise RuntimeError("minutes generation returned no content")

                if not minutes:
                    await session.publish_knowledge()
                    await index_bot_meeting(
                        self,
                        guild_id=member.guild.id,
                        channel_id=session.channel.id,
                        parent_channel_id=getattr(session.channel, "parent_id", None),
                        session_id=session.session_id,
                        channel_name=session.channel.name,
                        category_name=getattr(getattr(session.channel, "category", None), "name", ""),
                        started_at=session.started_at,
                        transcript=session.transcript_text,
                        transcript_url=transcript_url,
                    )
                    session.mark_delivered(transcript_url=transcript_url)
                    return

                from bot.commands.voice_cmd import _summarize_minutes

                drive_url = (
                    await upload_minutes(
                        self.config,
                        member.guild.id,
                        minutes,
                        session.channel.name,
                        meeting_at=session.started_at,
                        session_id=session.session_id,
                    )
                    if export_decision.allowed
                    else None
                )
                session.checkpoint_delivery_progress(
                    transcript_url=transcript_url or "",
                    minutes_url=drive_url or "",
                    transcript_path=str(getattr(session, "transcript_path", "") or ""),
                )
                summary = await _summarize_minutes(minutes)
                knowledge_url = await session.publish_knowledge(
                    summary=summary,
                    minutes=minutes,
                )
                await index_bot_meeting(
                    self,
                    guild_id=member.guild.id,
                    channel_id=session.channel.id,
                    parent_channel_id=getattr(session.channel, "parent_id", None),
                    session_id=session.session_id,
                    channel_name=session.channel.name,
                    category_name=getattr(getattr(session.channel, "category", None), "name", ""),
                    started_at=session.started_at,
                    transcript=session.transcript_text,
                    minutes=minutes,
                    transcript_url=transcript_url,
                    minutes_url=drive_url,
                )
                session.mark_delivered(
                    drive_url=drive_url,
                    transcript_url=transcript_url,
                )
            except Exception as exc:
                if session.recording_sink:
                    session.recording_sink.mark(
                        "automatic_leave_failed",
                        stage="delivery",
                        error=type(exc).__name__,
                    )
                log.exception(
                    "Automatic voice delivery failed for guild %s; recording retained",
                    member.guild.id,
                )
                return

            # Notification is best-effort and happens only after every durable
            # output has been saved and indexed.
            from bot.discord_visibility import resolve_minutes_notification_channel

            notify_ch = resolve_minutes_notification_channel(
                self.config,
                member.guild,
                session.channel,
            )
            if not notify_ch:
                log.info(
                    "Automatic voice delivery completed without a notification channel guild=%s",
                    member.guild.id,
                )
                return

            embed = discord.Embed(
                title=f"📝 議事録（自動生成） - {session.channel.name}",
                description=summary[:4096],
                color=discord.Color.blue(),
            )
            if drive_url:
                embed.add_field(
                    name="📄 全文",
                    value=f"[Google Docsで見る]({drive_url})",
                    inline=False,
                )
            if transcript_url:
                embed.add_field(
                    name="📜 逐語文字起こし",
                    value=f"[Google Docsで見る]({transcript_url})",
                    inline=False,
                )
            if knowledge_url:
                embed.add_field(
                    name="🗂️ Knowledge",
                    value=f"[Draft PRを確認する]({knowledge_url})",
                    inline=False,
                )

            import io

            files = []
            if not drive_url:
                files.append(
                    discord.File(
                        io.BytesIO(minutes.encode("utf-8")),
                        filename=f"議事録_{session.channel.name}.md",
                    )
                )
                if export_decision.allowed:
                    embed.set_footer(text="⚠️ Google Driveへの保存に失敗したため、ファイルを添付します")
                else:
                    embed.set_footer(text="🔒 限定チャンネルのため、外部Driveへの保存を行わず添付します")
            if session.transcript_text and not transcript_url:
                files.append(
                    discord.File(
                        io.BytesIO(session.transcript_text.encode("utf-8")),
                        filename=f"文字起こし_{session.channel.name}.md",
                    )
                )

            send_kwargs = {"embed": embed, "silent": True}
            if files:
                send_kwargs["files"] = files
            try:
                await notify_ch.send(**send_kwargs)
            except Exception:
                log.exception(
                    "Automatic voice delivery completed but notification failed guild=%s",
                    member.guild.id,
                )

    async def on_message(self, message: discord.Message):
        if message.author == self.user or message.author.bot:
            return

        # ----- DM処理 -----
        if not message.guild:
            await self._handle_dm(message)
            return

        guild_id = message.guild.id
        corpus = self.config.get_corpus(guild_id)

        # 未設定
        if not corpus:
            if _is_rag_invocation(message, self.user):
                await message.channel.send(
                    "まだ設定がされてないぽん！ `/setup` で設定してほしいぽん！",
                    silent=True,
                )
            return

        channel_ignored = _is_ignored_with_parent(self.config, guild_id, message.channel)

        # VCセッション中のテキストチャットを参考資料として収集
        from bot.voice import get_session

        vc_session = get_session(guild_id)
        if vc_session and vc_session.is_active and not channel_ignored:
            if _is_voice_reference_channel(
                message.channel,
                vc_session.channel,
            ) and (message.content or message.attachments):
                await vc_session.add_reference_from_message(message)

        # メンション → RAG回答
        if _is_rag_invocation(message, self.user):
            await self._handle_question(message, corpus, guild_id)
            return

        # 学習 (ignore/短文/コマンドは除外)
        if channel_ignored:
            return
        learning_content = self.corpus._message_content(message)
        if not learning_content or learning_content.startswith("/"):
            return
        if len(learning_content) < 4 and not message.attachments:
            return

        lineage = _discord_channel_lineage(message.channel)
        self.corpus.add_message(
            guild_id=guild_id,
            channel_id=message.channel.id,
            channel_name=str(message.channel),
            author=message.author.display_name,
            content=learning_content,
            timestamp=message.created_at,
            corpus_store_name=corpus,
            message_id=message.id,
            source_url=getattr(message, "jump_url", ""),
            **lineage,
        )

        # スマートリアクション (別モジュールで処理)
        try:
            from bot.reactions import maybe_react

            await maybe_react(self, message)
        except Exception:
            pass

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        """Persist explicit quality feedback attached to a RAG answer."""
        if self.user and payload.user_id == self.user.id:
            return
        emoji = str(payload.emoji)
        rating = RAG_FEEDBACK_RATINGS.get(emoji)
        if rating is None:
            return
        try:
            recorded = self.corpus.rag_store.record_feedback(
                payload.message_id,
                payload.user_id,
                emoji,
                rating,
            )
            if recorded:
                log.info("RAG feedback recorded: message=%s rating=%s", payload.message_id, rating)
        except Exception as exc:
            log.warning("Failed to record RAG feedback: %s", exc)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        """Keep quality metrics consistent when a user retracts a reaction."""
        emoji = str(payload.emoji)
        if emoji not in RAG_FEEDBACK_RATINGS:
            return
        try:
            self.corpus.rag_store.remove_feedback(
                payload.message_id,
                payload.user_id,
                emoji,
            )
        except Exception as exc:
            log.warning("Failed to remove RAG feedback: %s", exc)

    async def _handle_dm(self, message: discord.Message):
        """DM: 登録済みメンバーのみ回答"""
        guild_id = self.config.find_guild_for_user(message.author.id)
        if guild_id is None:
            await message.channel.send(
                "ボクに質問できるのは、局に登録されたメンバーだけだぽん！\n"
                "サーバーの管理者に `/member sync` で登録してもらってねぽん。",
                silent=True,
            )
            return

        corpus = self.config.get_corpus(guild_id)
        if not corpus:
            await message.channel.send("サーバーの設定がまだ完了してないぽん...", silent=True)
            return

        await self._handle_question(message, corpus, guild_id)

    def _build_members_info(self, guild_id: int) -> str:
        """Build speaker candidates from Discord members and approved Knowledge people."""
        members = self.config.get_members(guild_id)
        lines = []
        known = set()
        for uid, info in members.items():
            name = info.get("name", "不明")
            known.add(normalize_label(name))
            role = info.get("role", "")
            tasks = ", ".join(info.get("tasks", []))
            grade = info.get("grade", "")
            parts = [name]
            if role:
                parts.append(f"役職:{role}")
            if tasks:
                parts.append(f"担当:{tasks}")
            if grade:
                parts.append(f"学年:{grade}")
            lines.append(" | ".join(parts))
        for person in self.catalog.list(guild_id, kind="person", status="approved"):
            if normalize_label(person.label) in known:
                continue
            parts = [person.label]
            if person.aliases:
                parts.append(f"別名:{', '.join(person.aliases)}")
            if person.roles:
                parts.append(f"役職:{', '.join(person.roles)}")
            if person.description:
                parts.append(f"説明:{person.description}")
            lines.append(" | ".join(parts))
        return "\n".join(lines)

    async def _handle_question(self, message: discord.Message, corpus: str, guild_id: int = 0):
        """RAGで質問に回答"""
        query = _clean_rag_query(
            message.content,
            self.user.id if self.user else None,
            _mentioned_bot_role_ids(message, self.user),
        )
        if not query:
            return

        members_info = self._build_members_info(guild_id) if guild_id else ""
        glossary_text = self.config.get_glossary_text(guild_id) if guild_id else ""
        from bot.discord_visibility import (
            has_guildwide_rag_access,
            readable_channel_ids,
        )

        guild = message.guild or self.get_guild(guild_id)
        member = message.author if message.guild else (guild.get_member(message.author.id) if guild else None)
        if message.guild:
            # A normal message is visible to everyone who can read its output
            # channel. Restrict raw evidence to that same channel so a member
            # cannot relay a private channel into a broader audience.
            visible_channels = readable_channel_ids(
                guild,
                member,
                current_channel=message.channel,
                limit=1,
            )
            guildwide_access = False
        else:
            # A DM response is visible only to the requester, so it may use the
            # requester's current Discord read permissions across the guild.
            visible_channels = readable_channel_ids(guild, member)
            guildwide_access = has_guildwide_rag_access(guild, member)

        async with message.channel.typing():
            result = await self.corpus.query_with_trace(
                query,
                corpus,
                guild_id=guild_id,
                actor_id=message.author.id,
                channel_id=message.channel.id,
                members_info=members_info,
                glossary_text=glossary_text,
                glossary=self.config.get_glossary(guild_id),
                allowed_channel_ids=visible_channels,
                allow_unrestricted_conversations=guildwide_access,
            )
            decorated_answer = _decorate_rag_answer(
                result.text,
                message.guild,
                no_answer=result.no_answer,
            )
            discord_answer = f"{decorated_answer}\n\n{RAG_FEEDBACK_PROMPT}"
            sent_messages = await self.send_split_message(message.channel, discord_answer)
            if result.query_id and sent_messages:
                self.corpus.rag_store.bind_response_message(
                    result.query_id,
                    guild_id,
                    message.channel.id,
                    sent_messages[-1].id,
                )

            # Text-channel RAG answers are deliberately not relayed into a VC.
            # The two audiences can differ (for example an officer-only text
            # channel while the requester is in a public VC), which would leak
            # channel-scoped evidence. Voice mode has its own in-session TTS.

    async def send_split_message(self, destination, text: str):
        """Send short answers inline and preserve long answers as one Markdown file."""
        chunks = _split_discord_text(text)
        if len(chunks) > 4:
            attachment_text = text
            if text.endswith(RAG_FEEDBACK_PROMPT):
                attachment_text = text[: -len(RAG_FEEDBACK_PROMPT)]
                if attachment_text.endswith("\n\n"):
                    attachment_text = attachment_text[:-2]
            message = await destination.send(
                "回答が長いため、全文をMarkdownファイルにまとめたぽん。"
                f"\n\n{RAG_FEEDBACK_PROMPT}",
                file=discord.File(
                    io.BytesIO(attachment_text.encode("utf-8")),
                    filename="yagapon-answer.md",
                ),
                silent=True,
            )
            return [message]
        sent_messages = []
        for chunk in chunks:
            sent_messages.append(await destination.send(chunk, silent=True))
        return sent_messages

    async def close(self):
        async with self._close_lock:
            if self._close_completed:
                return
            log.info("Shutting down, checkpointing voice sessions...")
            from bot.voice import checkpoint_voice_sessions_for_shutdown

            try:
                await checkpoint_voice_sessions_for_shutdown()
            except Exception:
                log.exception("Voice session checkpoint failed during shutdown; continuing cleanup")
            try:
                from bot.reactions import shutdown_reactions

                await asyncio.wait_for(shutdown_reactions(self), timeout=20)
            except Exception:
                log.exception("Smart-reaction flush failed during shutdown; continuing cleanup")
            try:
                await self.recording_replays.shutdown(timeout=20)
            except Exception:
                log.exception("Recording replay shutdown failed; continuing cleanup")
            try:
                await self.scheduled_backfill.stop()
            except Exception:
                log.exception("Scheduled backfill shutdown failed; continuing cleanup")
            pending_refreshes = [
                task for task in self.knowledge_refresh_tasks if not task.done()
            ]
            for task in pending_refreshes:
                task.cancel()
            if pending_refreshes:
                await asyncio.gather(*pending_refreshes, return_exceptions=True)
            if self._recording_recovery_task and not self._recording_recovery_task.done():
                self._recording_recovery_task.cancel()
                try:
                    await self._recording_recovery_task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    log.exception("Recording recovery task failed during shutdown; continuing cleanup")
            # Close the Discord connection before the slower corpus flush so
            # Pycord cannot start a reconnect against an already-closing HTTP
            # session during deployments.
            try:
                await super().close()
            finally:
                # A transport error while Discord is closing must not discard
                # buffered knowledge waiting for its durable upload.
                log.info("Discord closing finished; flushing corpus buffers...")
                await self.corpus.shutdown()
                self._close_completed = True


def create_bot() -> YagaPon:
    return YagaPon()
