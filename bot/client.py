"""YagaPon Discord Client - メイン処理 (pycord)"""

import logging

import discord

from bot.config import ConfigManager
from bot.corpus import CorpusManager
from bot.voice import VoiceMode

log = logging.getLogger("yagapon.client")

RAG_FEEDBACK_PROMPT = (
    "回答の品質を評価してください：✅ 正しい / ⚠️ 一部不正確 / ❌ 誤り"
    "（対応した絵文字でリアクション）"
)
RAG_FEEDBACK_RATINGS = {"✅": "positive", "⚠️": "partial", "❌": "negative"}


class YagaPon(discord.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = True
        super().__init__(intents=intents)

        self.config = ConfigManager()
        self.corpus = CorpusManager()

        # コマンド登録
        from bot.commands import (
            backfill,
            corpus_cmd,
            drive,
            glossary,
            ignore,
            meigen,
            member,
            minutes,
            report,
            reset,
            setup,
            status,
            voice_cmd,
            voiceprint,
        )
        setup.register(self)
        status.register(self)
        ignore.register(self)
        backfill.register(self)
        member.register(self)
        meigen.register(self)
        voice_cmd.register(self)
        report.register(self)
        reset.register(self)
        corpus_cmd.register(self)
        voiceprint.register(self)
        glossary.register(self)
        minutes.register(self)
        drive.register(self)

    async def on_ready(self):
        self.corpus.start_flush_loop()
        log.info(f"おしゃべりやがぽん起動: {self.user}")

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

    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        """VCから人がいなくなったら自動退出"""
        # 誰かがVCから抜けた場合のみ処理
        if not before.channel or member.id == self.user.id:
            return

        # botがそのVCにいるか確認
        from bot.voice import get_session, leave_voice
        session = get_session(member.guild.id)
        if not session or not session.voice_client or not session.voice_client.is_connected():
            return
        if session.channel.id != before.channel.id:
            return

        # VCに残っているのがbotだけか確認
        humans = [m for m in before.channel.members if not m.bot]
        if len(humans) == 0:
            log.info(f"VC {before.channel.name} に誰もいなくなったので自動退出")

            # 議事録生成がある場合はテキストチャンネルに通知
            has_minutes = session.mode in (VoiceMode.LISTEN, VoiceMode.MEETING)
            minutes = await leave_voice(member.guild.id)

            if minutes and has_minutes:
                # 通知先の優先順位: システムチャンネル → 最初の送信可能チャンネル
                notify_ch = None
                if member.guild.system_channel:
                    notify_ch = member.guild.system_channel
                if not notify_ch:
                    for ch in member.guild.text_channels:
                        if ch.permissions_for(member.guild.me).send_messages:
                            notify_ch = ch
                            break

                if notify_ch:
                    from bot.commands.voice_cmd import _summarize_minutes
                    from bot.gdrive import upload_minutes

                    drive_url = await upload_minutes(self.config, member.guild.id, minutes, session.channel.name)
                    summary = await _summarize_minutes(minutes)

                    embed = discord.Embed(
                        title=f"📝 議事録（自動生成） - {session.channel.name}",
                        description=summary[:4096],
                        color=discord.Color.blue(),
                    )
                    if drive_url:
                        embed.add_field(name="📄 全文", value=f"[Google Docsで見る]({drive_url})", inline=False)

                    await notify_ch.send(embed=embed, silent=True)

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
            if self.user.mentioned_in(message):
                await message.channel.send(
                    "まだ設定がされてないぽん！ `/setup` で設定してほしいぽん！",
                    silent=True,
                )
            return

        # VCセッション中のテキストチャットを参考資料として収集
        from bot.voice import get_session
        vc_session = get_session(guild_id)
        if vc_session and vc_session.is_active:
            # VCのテキストチャンネル or botがいるVCと同じチャンネル
            vc_text_ch = getattr(vc_session.channel, 'id', None)
            msg_ch_id = message.channel.id
            # VCチャンネルのテキストチャット、またはボイスチャンネル名と一致するテキストチャンネル
            is_vc_text = (
                msg_ch_id == vc_text_ch
                or getattr(message.channel, 'name', '') == getattr(vc_session.channel, 'name', '')
                or (hasattr(message.channel, 'category') and hasattr(vc_session.channel, 'category')
                    and message.channel.category == vc_session.channel.category)
            )
            if is_vc_text and (message.content or message.attachments):
                await vc_session.add_reference_from_message(message)

        # メンション → RAG回答
        if self.user.mentioned_in(message):
            await self._handle_question(message, corpus, guild_id)
            return

        # 学習 (ignore/短文/コマンドは除外)
        if self.config.is_ignored(guild_id, message.channel.id):
            return
        learning_content = self.corpus._message_content(message)
        if not learning_content or learning_content.startswith("/"):
            return
        if len(learning_content) < 4 and not message.attachments:
            return

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
        """メンバー情報をテキストに変換"""
        members = self.config.get_members(guild_id)
        if not members:
            return ""
        lines = []
        for uid, info in members.items():
            name = info.get("name", "不明")
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
        return "\n".join(lines)

    async def _handle_question(self, message: discord.Message, corpus: str, guild_id: int = 0):
        """RAGで質問に回答"""
        query = message.content
        # メンション部分を除去
        if self.user:
            query = query.replace(f"<@{self.user.id}>", "").strip()
        if not query:
            return

        members_info = self._build_members_info(guild_id) if guild_id else ""
        glossary_text = self.config.get_glossary_text(guild_id) if guild_id else ""

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
            )
            discord_answer = f"{result.text}\n\n{RAG_FEEDBACK_PROMPT}"
            sent_messages = await self.send_split_message(message.channel, discord_answer)
            if result.query_id and sent_messages:
                self.corpus.rag_store.bind_response_message(
                    result.query_id,
                    guild_id,
                    message.channel.id,
                    sent_messages[-1].id,
                )

            # VCにいればTTSも
            try:
                from bot.tts import speak_in_vc
                await speak_in_vc(self, message, result.text)
            except Exception:
                pass

    async def send_split_message(self, destination, text: str):
        """コードブロックを考慮して2000文字制限で分割送信"""
        sent_messages = []
        lines = text.split("\n")
        current_chunk = ""
        in_code_block = False
        current_lang = ""

        for line in lines:
            if line.strip().startswith("```"):
                if not in_code_block:
                    current_lang = line.strip().replace("```", "").strip()
                in_code_block = not in_code_block

            if len(current_chunk) + len(line) + 10 > 1900:
                to_send = current_chunk
                if in_code_block:
                    to_send += "\n```"

                sent_messages.append(await destination.send(to_send, silent=True))

                if in_code_block:
                    lang = f" {current_lang}" if current_lang else ""
                    current_chunk = f"```{lang}\n(続き)...\n{line}"
                else:
                    current_chunk = f"(続き)...\n{line}"
            else:
                current_chunk = f"{current_chunk}\n{line}" if current_chunk else line

        if current_chunk:
            sent_messages.append(await destination.send(current_chunk, silent=True))
        return sent_messages

    async def close(self):
        log.info("Shutting down, flushing buffers...")
        await self.corpus.shutdown()
        await super().close()


def create_bot() -> YagaPon:
    return YagaPon()
