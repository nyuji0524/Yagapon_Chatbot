"""
/setup - 多段階セットアップ
Step 1: 局選択 (使用済みの局はグレーアウト) → コーパス作成
Step 2: 学習除外チャンネル選択
Step 3: GitHub連携 (通知ch + webhook URL案内)
Step 4: リアクション絵文字登録
Step 5: メンバー登録 (CSV形式でメッセージ入力)
Step 6: Google Drive連携 (議事録・レポート保存先)
Step 7: 過去ログ取り込み (全部/30日/180日)
"""

import logging
import os
import discord

log = logging.getLogger("yagapon.setup")
from discord import app_commands
from discord.ui import Select, View, Button, ChannelSelect

BUREAUS = [
    ("IT局", "🩵"), ("総務局", "🩶"), ("装飾局", "💛"), ("ステージ局", "❤️"),
    ("屋外局", "🧡"), ("室内局", "🩵"), ("広報局", "🩷"), ("渉外局", "💜"),
]


# ====== Step 1: 局選択 (使用済みはグレーアウト) ======

class BureauSelect(Select):
    def __init__(self, used_bureaus: set[str]):
        options = []
        for name, emoji in BUREAUS:
            taken = name in used_bureaus
            options.append(discord.SelectOption(
                label=name,
                emoji=emoji,
                description="⛔ 他のサーバーで使用中" if taken else f"{name}のサーバーとして登録",
                default=False,
            ))
        super().__init__(
            placeholder="ここは何局のサーバーだぽん？",
            min_values=1, max_values=1,
            options=options,
        )
        self._used = used_bureaus

    async def callback(self, interaction: discord.Interaction):
        bureau = self.values[0]
        if bureau in self._used:
            await interaction.response.send_message(
                f"❌ 「{bureau}」はすでに他のサーバーで使われてるぽん！別の局を選んでねぽん。",
                ephemeral=True,
            )
            return

        bot = interaction.client

        # 既に局が設定済みなら再実行を防止（コーパス重複防止）
        existing = bot.config.get_bureau(interaction.guild_id)
        if existing:
            await interaction.response.send_message(
                f"既に「**{existing}**」として登録済みだぽん！\n"
                f"変更したい場合は `/reset` でリセットしてねぽん。",
                ephemeral=True,
            )
            return

        await interaction.response.defer()

        try:
            store_name = await bot.corpus.create_corpus(interaction.guild_id, bureau)
            await bot.config.set_bureau(interaction.guild_id, bureau, store_name)
            await interaction.followup.send(
                f"✅ 「**{bureau}**」として登録したぽん！コーパスも作成済みだぽん。\n"
                f"次はこのサーバーで学習してほしくないチャンネルを選ぶぽん！",
                view=Step2IgnoreView(interaction.guild),
            )
        except Exception as e:
            await interaction.followup.send(f"❌ エラーだぽん...: {e}")


class Step1BureauView(View):
    def __init__(self, used_bureaus: set[str]):
        super().__init__(timeout=300)
        self.add_item(BureauSelect(used_bureaus))


# ====== Step 2: 学習除外チャンネル ======

class Step2IgnoreView(View):
    """テキストチャンネル一覧をSelectMenuで表示（カテゴリ横断・25件超対応）"""

    def __init__(self, guild: discord.Guild, page: int = 0):
        super().__init__(timeout=300)
        self.guild = guild
        self.page = page
        text_channels = [ch for ch in guild.text_channels]
        self.all_channels = text_channels
        self.page_size = 25
        self.max_page = max(0, (len(text_channels) - 1) // self.page_size)

        start = page * self.page_size
        page_channels = text_channels[start:start + self.page_size]

        options = []
        for ch in page_channels:
            category = ch.category.name if ch.category else "カテゴリなし"
            options.append(discord.SelectOption(
                label=f"#{ch.name}",
                value=str(ch.id),
                description=category,
            ))

        if options:
            select = Select(
                placeholder=f"学習しないチャンネルを選ぶぽん（{page+1}/{self.max_page+1}ページ）",
                min_values=0,
                max_values=len(options),
                options=options,
            )
            select.callback = self._select_callback
            self.add_item(select)

    async def _select_callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        bot = interaction.client
        selected_ids = interaction.data.get("values", [])

        for ch_id in selected_ids:
            await bot.config.add_ignore_channel(interaction.guild_id, int(ch_id))

        ignored = ", ".join(f"<#{cid}>" for cid in selected_ids) if selected_ids else "なし"
        if self.page < self.max_page:
            await interaction.followup.send(
                f"✅ 除外チャンネル追加: {ignored}\n"
                f"まだチャンネルがあるぽん！次のページも確認してねぽん。",
                view=Step2IgnoreView(self.guild, self.page + 1),
            )
        else:
            await interaction.followup.send(
                f"✅ 除外チャンネル追加: {ignored}\n"
                f"次はGitHub連携の設定だぽん！",
                view=Step3GithubView(),
            )

    @discord.ui.button(label="次のページ", style=discord.ButtonStyle.primary, emoji="➡️", row=1)
    async def next_page(self, interaction: discord.Interaction, button: Button):
        if self.page < self.max_page:
            await interaction.response.edit_message(
                view=Step2IgnoreView(self.guild, self.page + 1),
            )
        else:
            await interaction.response.send_message("最後のページだぽん！", ephemeral=True)

    @discord.ui.button(label="完了 → 次へ", style=discord.ButtonStyle.success, row=1)
    async def done(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "除外チャンネルの設定完了だぽん！\n次はGitHub連携だぽん！",
            view=Step3GithubView(),
        )

    @discord.ui.button(label="スキップ (除外なし)", style=discord.ButtonStyle.secondary, row=1)
    async def skip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "除外チャンネルなしだぽん！\n次はGitHub連携だぽん！",
            view=Step3GithubView(),
        )


# ====== Step 3: GitHub連携 (通知ch + webhook URL案内) ======

class GithubChannelSelect(ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="GitHub通知を送るチャンネルを選ぶぽん",
            min_values=1,
            max_values=1,
            channel_types=[discord.ChannelType.text],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        bot = interaction.client
        ch = self.values[0]
        await bot.config.set_github_channel(interaction.guild_id, ch.id)

        # webhook URL案内
        api_host = os.environ.get("API_HOST", "https://あなたのサーバー")
        webhook_url = f"{api_host}/webhook/github/{interaction.guild_id}"
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        secret_display = f"`{secret}`" if secret else "⚠️ 未設定（.envにGITHUB_WEBHOOK_SECRETを設定してね）"

        await interaction.followup.send(
            f"✅ GitHub通知: <#{ch.id}>\n\n"
            f"📌 **GitHubリポジトリ → Settings → Webhooks → Add webhook**\n"
            f"```\n"
            f"Payload URL: {webhook_url}\n"
            f"Content type: application/json\n"
            f"Secret: {secret}\n"
            f"Events: Just the push event\n"
            f"```\n"
            f"mainブランチへのpushでコードレビューが動くぽん！\n\n"
            f"次はリアクションの設定だぽん！",
            view=Step4ReactionsView(),
        )


class Step3GithubView(View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(GithubChannelSelect())

    @discord.ui.button(label="スキップ", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "GitHub連携はスキップだぽん！\n次はリアクション設定だぽん！",
            view=Step4ReactionsView(),
        )


# ====== Step 4: リアクション絵文字 (リアクションで選択) ======

class Step4ReactionsView(View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="リアクション有効化", style=discord.ButtonStyle.primary, emoji="✨")
    async def enable(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "リアクションに使う絵文字を設定するぽん！\n"
            "下のメッセージにそれぞれ使いたい絵文字でリアクションしてねぽん！\n"
            "（サーバー独自の絵文字もOKだぽん）"
        )
        collector = ReactionEmojiCollector(interaction)
        await collector.start()

    @discord.ui.button(label="リアクション無効", style=discord.ButtonStyle.secondary)
    async def disable(self, interaction: discord.Interaction, button: Button):
        bot = interaction.client
        await bot.config.set_reactions(interaction.guild_id, False, "💡", "😲", "😂")
        await interaction.response.send_message(
            "リアクションは無効にしたぽん！\n次はメンバー登録だぽん！",
            view=Step5MemberView(),
        )


class ReactionEmojiCollector:
    """3つのメッセージを送り、ユーザーのリアクションで絵文字を収集"""

    EMOTIONS = [
        ("interesting", "💡 **興味深い** と思ったときのリアクションを付けてぽん！"),
        ("surprised", "😲 **びっくり** したときのリアクションを付けてぽん！"),
        ("funny", "😂 **笑える** と思ったときのリアクションを付けてぽん！"),
    ]

    def __init__(self, interaction: discord.Interaction):
        self.interaction = interaction
        self.bot = interaction.client
        self.guild_id = interaction.guild_id
        self.user = interaction.user
        self.channel = interaction.channel
        self.emojis = {}

    async def start(self):
        for key, prompt in self.EMOTIONS:
            msg = await self.channel.send(prompt)

            def check(reaction, user):
                return user == self.user and reaction.message.id == msg.id

            try:
                reaction, _ = await self.bot.wait_for("reaction_add", timeout=60, check=check)
                self.emojis[key] = str(reaction.emoji)
                await msg.edit(content=f"✅ {prompt.split('**')[1]}: {reaction.emoji}")
            except Exception:
                # タイムアウト時はデフォルト
                defaults = {"interesting": "💡", "surprised": "😲", "funny": "😂"}
                self.emojis[key] = defaults[key]
                await msg.edit(content=f"⏰ タイムアウト → デフォルト: {defaults[key]}")

        await self.bot.config.set_reactions(
            self.guild_id, True,
            self.emojis["interesting"],
            self.emojis["surprised"],
            self.emojis["funny"],
        )
        await self.channel.send(
            f"✅ リアクション設定完了だぽん！\n"
            f"  興味深い: {self.emojis['interesting']} / "
            f"びっくり: {self.emojis['surprised']} / "
            f"笑える: {self.emojis['funny']}\n\n"
            f"次はメンバー登録だぽん！",
            view=Step5MemberView(),
        )


# ====== Step 5: メンバー登録 (ロール分類 → 自動登録) ======

class Step5MemberView(View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="ロール分類 → メンバー登録", style=discord.ButtonStyle.primary, emoji="👥")
    async def auto_register(self, interaction: discord.Interaction, button: Button):
        guild = interaction.guild
        bot = interaction.client
        roles = [r for r in guild.roles if r.name != "@everyone"]

        if not roles:
            await interaction.response.send_message(
                "ロールがないぽん...先にロールを作ってねぽん。",
                view=Step6DriveView(),
            )
            return

        from bot.commands.member import RoleMappingView, _classify_member_roles

        class SetupRoleMappingView(RoleMappingView):
            """setup用: 保存後にメンバー自動登録 → Step6へ進む"""
            @discord.ui.button(label="保存してメンバー登録", style=discord.ButtonStyle.success, row=4)
            async def save(self, interaction: discord.Interaction, button_: Button):
                await bot.config.set_role_mapping(guild.id, self.mapping)

                # メンバー自動登録
                human_members = [m for m in guild.members if not m.bot]
                members = {}
                for m in human_members:
                    members[str(m.id)] = _classify_member_roles(m, self.mapping)

                await bot.config.set_members(guild.id, members)

                await interaction.response.edit_message(
                    content=f"✅ ロール分類を保存し、**{len(members)}人** を自動登録したぽん！\n"
                    f"`/member register <呼び名>` で呼び名を変更できるぽん。\n\n"
                    f"次はGoogle Drive連携だぽん！",
                    view=Step6DriveView(),
                )

        view = SetupRoleMappingView(bot, guild, roles)
        await interaction.response.send_message(
            "ロールをカテゴリ別に分類するぽん！\n"
            "👑 役職 / 💼 担当 / 🎓 学年 をそれぞれ選んでねぽん。",
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="あとで登録する", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "メンバー登録はあとで `/member roles` → `/member sync` でもできるぽん！\n"
            "次はGoogle Drive連携だぽん！",
            view=Step6DriveView(),
        )


# ====== Step 6: Google Drive連携 ======

class Step6DriveView(View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="Google Driveフォルダを設定", style=discord.ButtonStyle.primary, emoji="📁")
    async def set_drive(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(DriveUrlModal())

    @discord.ui.button(label="スキップ", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "Google Drive連携はスキップだぽん！\n"
            "議事録やレポートはDiscord上にのみ投稿するぽん。\n\n"
            "最後に、過去ログの取り込みだぽん！",
            view=Step7BackfillView(),
        )


class DriveUrlModal(discord.ui.Modal, title="Google Drive連携"):
    drive_url = discord.ui.TextInput(
        label="議事録・レポート保存先のフォルダURL",
        placeholder="https://drive.google.com/drive/folders/xxxx",
        max_length=500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        bot = interaction.client
        url = str(self.drive_url).strip()
        await bot.config.set_drive_folder(interaction.guild_id, url)
        await interaction.response.send_message(
            f"✅ Google Drive連携: 設定済み\n"
            f"議事録やレポートをこのフォルダに保存するぽん！\n\n"
            f"最後に、過去ログの取り込みだぽん！",
            view=Step7BackfillView(),
        )


# ====== Step 7: 過去ログ取り込み ======

async def _run_backfill(interaction: discord.Interaction, days: int | None):
    """days=None で全量"""
    bot = interaction.client
    corpus = bot.config.get_corpus(interaction.guild_id)
    if not corpus:
        await interaction.followup.send("コーパスが見つからないぽん...")
        return

    label = "全期間" if days is None else f"過去{days}日分"
    status_msg = await interaction.followup.send(
        f"📚 {label}の過去ログ取り込みを処理中だぽん...", wait=True, silent=True
    )

    from datetime import datetime, timedelta, timezone
    after = None
    if days:
        after = datetime.now(timezone.utc) - timedelta(days=days)

    total = 0
    channels = [
        ch for ch in interaction.guild.text_channels
        if ch.permissions_for(interaction.guild.me).read_message_history
        and not bot.config.is_ignored(interaction.guild_id, ch.id)
    ]

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

    await status_msg.edit(
        content=f"🎉 セットアップ完了だぽん！\n"
        f"合計 **{total:,}件** のメッセージを学習したぽん！\n"
        f"何でも聞いてねぽん！"
    )


class Step7BackfillView(View):
    def __init__(self):
        super().__init__(timeout=300)
        self._started = False

    async def _disable_buttons(self, interaction: discord.Interaction, label: str):
        """ボタンを無効化して連打防止"""
        if self._started:
            await interaction.response.send_message(
                "すでにバックフィルを実行中だぽん！⏳", ephemeral=True
            )
            return False
        self._started = True
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"📚 {label}の取り込みを開始するぽん！⏳", view=self
        )
        return True

    @discord.ui.button(label="全部取り込む", style=discord.ButtonStyle.success, emoji="📚")
    async def backfill_all(self, interaction: discord.Interaction, button: Button):
        if not await self._disable_buttons(interaction, "全期間"):
            return
        await _run_backfill(interaction, days=None)

    @discord.ui.button(label="過去180日", style=discord.ButtonStyle.primary, emoji="📅")
    async def backfill_180(self, interaction: discord.Interaction, button: Button):
        if not await self._disable_buttons(interaction, "過去180日"):
            return
        await _run_backfill(interaction, days=180)

    @discord.ui.button(label="過去30日", style=discord.ButtonStyle.primary, emoji="📆")
    async def backfill_30(self, interaction: discord.Interaction, button: Button):
        if not await self._disable_buttons(interaction, "過去30日"):
            return
        await _run_backfill(interaction, days=30)

    @discord.ui.button(label="あとでやる", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "🎉 セットアップ完了だぽん！\n"
            "過去ログは後から `/backfill` で取り込めるぽん！"
        )


# ====== register ======

def register(bot):
    @bot.tree.command(name="setup", description="初期設定をするぽん！")
    # @app_commands.checks.has_permissions(administrator=True)  # TODO: テスト後に戻す
    async def setup_cmd(interaction: discord.Interaction):
        # 既にセットアップ済みかチェック
        existing = bot.config.get_bureau(interaction.guild_id)
        if existing:
            await interaction.response.send_message(
                f"このサーバーは既に **{existing}** として設定済みだぽん！\n"
                f"やり直したい場合は先に `/reset` で設定をリセットしてねぽん。",
                ephemeral=True,
            )
            return

        # 使用済みの局を取得
        used_bureaus = set()
        for gid, gdata in bot.config._config.items():
            if gid != str(interaction.guild_id) and "bureau" in gdata:
                used_bureaus.add(gdata["bureau"])

        await interaction.response.send_message(
            "セットアップを始めるぽん！まずは局を選んでねぽん！",
            view=Step1BureauView(used_bureaus),
        )

    @setup_cmd.error
    async def setup_error(interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "管理者権限が必要だぽん！", ephemeral=True
            )
