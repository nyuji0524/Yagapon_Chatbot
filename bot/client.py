"""YagaPon Discord Client - メイン処理 (pycord)"""

import logging

import discord

from bot.config import ConfigManager
from bot.corpus import CorpusManager
from bot.voice import VoiceMode

log = logging.getLogger("yagapon.client")


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
        from bot.commands import setup, status, ignore, backfill, member, meigen, voice_cmd, report, reset, corpus_cmd, voiceprint, glossary
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
                # 通知先の優先順位: GitHub通知チャンネル → システムチャンネル → 最初のテキストチャンネル
                notify_ch = None
                github_ch_id = self.config.get_github_channel(member.guild.id)
                if github_ch_id:
                    notify_ch = member.guild.get_channel(github_ch_id)
                if not notify_ch and member.guild.system_channel:
                    notify_ch = member.guild.system_channel
                if not notify_ch:
                    for ch in member.guild.text_channels:
                        if ch.permissions_for(member.guild.me).send_messages:
                            notify_ch = ch
                            break

                if notify_ch:
                    from bot.gdrive import upload_minutes
                    from bot.commands.voice_cmd import _summarize_minutes

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

        # メンション → RAG回答
        if self.user.mentioned_in(message):
            await self._handle_question(message, corpus, guild_id)
            return

        # 学習 (ignore/短文/コマンドは除外)
        if self.config.is_ignored(guild_id, message.channel.id):
            return
        if len(message.content) < 4 or message.content.startswith("/"):
            return

        self.corpus.add_message(
            guild_id=guild_id,
            channel_id=message.channel.id,
            channel_name=str(message.channel),
            author=message.author.display_name,
            content=message.content,
            timestamp=message.created_at,
            corpus_store_name=corpus,
        )

        # スマートリアクション (別モジュールで処理)
        try:
            from bot.reactions import maybe_react
            await maybe_react(self, message)
        except Exception:
            pass

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
            answer = await self.corpus.query(query, corpus, guild_id=guild_id, members_info=members_info, glossary_text=glossary_text)
            await self.send_split_message(message.channel, answer)

            # VCにいればTTSも
            try:
                from bot.tts import speak_in_vc
                await speak_in_vc(self, message, answer)
            except Exception:
                pass

    async def send_split_message(self, destination, text: str):
        """コードブロックを考慮して2000文字制限で分割送信"""
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

                await destination.send(to_send, silent=True)

                if in_code_block:
                    lang = f" {current_lang}" if current_lang else ""
                    current_chunk = f"```{lang}\n(続き)...\n{line}"
                else:
                    current_chunk = f"(続き)...\n{line}"
            else:
                current_chunk = f"{current_chunk}\n{line}" if current_chunk else line

        if current_chunk:
            await destination.send(current_chunk, silent=True)

    async def close(self):
        log.info("Shutting down, flushing buffers...")
        await self.corpus.shutdown()
        await super().close()


def create_bot() -> YagaPon:
    return YagaPon()
