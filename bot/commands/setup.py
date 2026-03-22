"""
/setup - 1メッセージ完結型セットアップ (pycord)
全ステップを同一メッセージの編集で進行。重複登録防止。
"""

import logging
import os
import discord

log = logging.getLogger("yagapon.setup")
from discord.ui import Select, View, Button

BUREAUS = [
    ("IT局", "🩵"), ("総務局", "🩶"), ("装飾局", "💛"), ("ステージ局", "❤️"),
    ("屋外局", "🧡"), ("室内局", "🩵"), ("広報局", "🩷"), ("渉外局", "💜"),
]


class SetupWizard:
    """1つのメッセージでセットアップを進行"""

    def __init__(self, bot, guild: discord.Guild, message: discord.Message):
        self.bot = bot
        self.guild = guild
        self.message = message  # 編集対象の固定メッセージ
        self.step = 0
        self.completed = {
            "bureau": False, "ignore": False, "github": False,
            "reactions": False, "members": False, "drive": False, "backfill": False,
        }

    def _progress_bar(self) -> str:
        steps = ["局選択", "除外CH", "GitHub", "リアクション", "メンバー", "Drive", "過去ログ"]
        parts = []
        for i, name in enumerate(steps):
            if i < self.step:
                parts.append(f"✅ {name}")
            elif i == self.step:
                parts.append(f"▶️ **{name}**")
            else:
                parts.append(f"⬜ {name}")
        return " → ".join(parts)

    async def update(self, content: str, view: View):
        """メッセージを編集して次のステップを表示"""
        full_content = f"**🔧 おしゃべりやがぽん セットアップ**\n{self._progress_bar()}\n\n{content}"
        try:
            await self.message.edit(content=full_content, view=view)
        except Exception as e:
            log.error(f"Setup message edit error: {e}")


# ====== Step 1: 局選択 ======

class Step1View(View):
    def __init__(self, wizard: SetupWizard, used_bureaus: set[str]):
        super().__init__(timeout=600)
        self.wizard = wizard
        options = []
        for name, emoji in BUREAUS:
            taken = name in used_bureaus
            options.append(discord.SelectOption(
                label=name, emoji=emoji,
                description="⛔ 使用中" if taken else f"{name}として登録",
            ))
        select = Select(placeholder="局を選んでねぽん", min_values=1, max_values=1, options=options)
        select.callback = self._select
        self._used = used_bureaus
        self.add_item(select)

    async def _select(self, interaction: discord.Interaction):
        bureau = interaction.data["values"][0]
        if bureau in self._used:
            await interaction.response.send_message(f"❌ 「{bureau}」は使用中だぽん！", ephemeral=True)
            return

        existing = self.wizard.bot.config.get_bureau(self.wizard.guild.id)
        if existing:
            await interaction.response.send_message(
                f"既に「{existing}」として登録済みだぽん！`/reset` してねぽん。", ephemeral=True)
            return

        await interaction.response.defer()
        try:
            store_name = await self.wizard.bot.corpus.create_corpus(self.wizard.guild.id, bureau)
            await self.wizard.bot.config.set_bureau(self.wizard.guild.id, bureau, store_name)
            # セットアップ回数を記録
            g = self.wizard.bot.config._guild(self.wizard.guild.id)
            g["setup_count"] = g.get("setup_count", 0) + 1
            await self.wizard.bot.config._save()
            self.wizard.step = 1
            await show_step2(self.wizard)
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}", ephemeral=True)


# ====== Step 2: 除外チャンネル ======

async def show_step2(wizard: SetupWizard, page: int = 0):
    channels = [ch for ch in wizard.guild.text_channels]
    page_size = 25
    max_page = max(0, (len(channels) - 1) // page_size)
    page_channels = channels[page * page_size:(page + 1) * page_size]

    view = Step2View(wizard, page, max_page)

    options = [
        discord.SelectOption(
            label=f"#{ch.name}", value=str(ch.id),
            description=ch.category.name if ch.category else "カテゴリなし",
        )
        for ch in page_channels
    ]
    if options:
        select = Select(
            placeholder=f"除外チャンネル ({page+1}/{max_page+1})",
            min_values=0, max_values=len(options), options=options,
        )

        async def on_select(interaction: discord.Interaction):
            await interaction.response.defer()
            for ch_id in interaction.data.get("values", []):
                await wizard.bot.config.add_ignore_channel(wizard.guild.id, int(ch_id))

        select.callback = on_select
        view.add_item(select)

    await wizard.update("学習しないチャンネルを選んでねぽん。選ばなくてもOKだぽん。", view)


class Step2View(View):
    def __init__(self, wizard: SetupWizard, page: int, max_page: int):
        super().__init__(timeout=600)
        self.wizard = wizard
        self.page = page
        self.max_page = max_page

    @discord.ui.button(label="次のページ", style=discord.ButtonStyle.primary, emoji="➡️", row=1)
    async def next_page(self, button: Button, interaction: discord.Interaction):
        if self.page < self.max_page:
            await interaction.response.defer()
            await show_step2(self.wizard, self.page + 1)
        else:
            await interaction.response.send_message("最後のページだぽん！", ephemeral=True)

    @discord.ui.button(label="次へ進む", style=discord.ButtonStyle.success, row=1)
    async def done(self, button: Button, interaction: discord.Interaction):
        await interaction.response.defer()
        self.wizard.step = 2
        await show_step3(self.wizard)


# ====== Step 3: GitHub連携 ======

async def show_step3(wizard: SetupWizard):
    channels = [
        ch for ch in wizard.guild.text_channels
        if ch.permissions_for(wizard.guild.me).send_messages
    ]
    view = Step3View(wizard, channels)
    await wizard.update("GitHub通知を送るチャンネルを選んでねぽん。", view)


class Step3View(View):
    def __init__(self, wizard: SetupWizard, channels: list):
        super().__init__(timeout=600)
        self.wizard = wizard

        options = [
            discord.SelectOption(
                label=f"#{ch.name}", value=str(ch.id),
                description=ch.category.name if ch.category else "",
            )
            for ch in channels[:25]
        ]
        if options:
            select = Select(placeholder="GitHub通知チャンネル", min_values=1, max_values=1, options=options)
            select.callback = self._select
            self.add_item(select)

    async def _select(self, interaction: discord.Interaction):
        await interaction.response.defer()
        ch_id = int(interaction.data["values"][0])
        await self.wizard.bot.config.set_github_channel(self.wizard.guild.id, ch_id)

        api_host = os.environ.get("API_HOST", "https://your-server")
        webhook_url = f"{api_host}/webhook/github/{self.wizard.guild.id}"
        secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")

        await interaction.followup.send(
            f"📌 **GitHub Webhook設定情報**\n```\n"
            f"Payload URL: {webhook_url}\n"
            f"Content type: application/json\n"
            f"Secret: {secret}\n"
            f"Events: Just the push event\n```",
            ephemeral=True,
        )
        self.wizard.step = 3
        await show_step4(self.wizard)

    @discord.ui.button(label="スキップ", style=discord.ButtonStyle.secondary, row=1)
    async def skip(self, button: Button, interaction: discord.Interaction):
        await interaction.response.defer()
        self.wizard.step = 3
        await show_step4(self.wizard)


# ====== Step 4: リアクション ======

async def show_step4(wizard: SetupWizard):
    view = Step4View(wizard)
    await wizard.update(
        "スマートリアクションを設定するぽん？\n"
        "有効にすると、面白い発言に自動でリアクションを付けるぽん！",
        view,
    )


class Step4View(View):
    def __init__(self, wizard: SetupWizard):
        super().__init__(timeout=600)
        self.wizard = wizard

    @discord.ui.button(label="有効化（絵文字を設定）", style=discord.ButtonStyle.primary, emoji="✨")
    async def enable(self, button: Button, interaction: discord.Interaction):
        await interaction.response.send_message(
            "リアクションに使う絵文字を設定するぽん！\n"
            "下のメッセージにそれぞれ使いたい絵文字でリアクションしてねぽん！\n"
            "（サーバー独自の絵文字もOKだぽん）",
            ephemeral=True,
        )
        collector = ReactionCollector(self.wizard, interaction)
        await collector.start()

    @discord.ui.button(label="無効のまま", style=discord.ButtonStyle.secondary)
    async def disable(self, button: Button, interaction: discord.Interaction):
        await interaction.response.defer()
        await self.wizard.bot.config.set_reactions(self.wizard.guild.id, False, "💡", "😲", "😂")
        self.wizard.step = 4
        await show_step5(self.wizard)


class ReactionCollector:
    EMOTIONS = [
        ("interesting", "💡 **興味深い** のリアクションを付けてぽん！"),
        ("surprised", "😲 **びっくり** のリアクションを付けてぽん！"),
        ("funny", "😂 **笑える** のリアクションを付けてぽん！"),
    ]

    def __init__(self, wizard: SetupWizard, interaction: discord.Interaction):
        self.wizard = wizard
        self.bot = wizard.bot
        self.user = interaction.user
        self.channel = interaction.channel
        self.emojis = {}

    async def start(self):
        for key, prompt in self.EMOTIONS:
            msg = await self.channel.send(prompt, silent=True)

            def check(reaction, user):
                return user == self.user and reaction.message.id == msg.id

            try:
                reaction, _ = await self.bot.wait_for("reaction_add", timeout=60, check=check)
                self.emojis[key] = str(reaction.emoji)
                await msg.edit(content=f"✅ {prompt.split('**')[1]}: {reaction.emoji}")
            except Exception:
                defaults = {"interesting": "💡", "surprised": "😲", "funny": "😂"}
                self.emojis[key] = defaults[key]
                await msg.edit(content=f"⏰ デフォルト: {defaults[key]}")

        await self.bot.config.set_reactions(
            self.wizard.guild.id, True,
            self.emojis["interesting"], self.emojis["surprised"], self.emojis["funny"],
        )
        self.wizard.step = 4
        await show_step5(self.wizard)


# ====== Step 5: メンバー登録 ======

async def show_step5(wizard: SetupWizard):
    view = Step5View(wizard)
    await wizard.update(
        "メンバーをロールから自動登録するぽん？\n"
        "役職・担当・学年のロールを分類して一括登録できるぽん。",
        view,
    )


class Step5View(View):
    def __init__(self, wizard: SetupWizard):
        super().__init__(timeout=600)
        self.wizard = wizard

    @discord.ui.button(label="ロール分類 → 登録", style=discord.ButtonStyle.primary, emoji="👥")
    async def register(self, button: Button, interaction: discord.Interaction):
        guild = self.wizard.guild
        roles = [r for r in guild.roles if r.name != "@everyone"]
        if not roles:
            await interaction.response.send_message("ロールがないぽん...", ephemeral=True)
            self.wizard.step = 5
            await show_step6(self.wizard)
            return

        from bot.commands.member import RoleMappingView, _classify_member_roles

        class SetupRoleMappingView(RoleMappingView):
            @discord.ui.button(label="保存してメンバー登録", style=discord.ButtonStyle.success, row=4)
            async def save(self_, button_: Button, interaction_: discord.Interaction):
                await self.wizard.bot.config.set_role_mapping(guild.id, self_.mapping)
                human_members = [m for m in guild.members if not m.bot]
                members = {}
                for m in human_members:
                    members[str(m.id)] = _classify_member_roles(m, self_.mapping)
                await self.wizard.bot.config.set_members(guild.id, members)
                await interaction_.response.send_message(
                    f"✅ **{len(members)}人** 登録したぽん！", ephemeral=True
                )
                self.wizard.step = 5
                await show_step6(self.wizard)

        view = SetupRoleMappingView(self.wizard.bot, guild, roles)
        await interaction.response.send_message(
            "ロールを分類してねぽん。", view=view, ephemeral=True
        )

    @discord.ui.button(label="あとで", style=discord.ButtonStyle.secondary)
    async def skip(self, button: Button, interaction: discord.Interaction):
        await interaction.response.defer()
        self.wizard.step = 5
        await show_step6(self.wizard)


# ====== Step 6: Google Drive ======

async def show_step6(wizard: SetupWizard):
    view = Step6View(wizard)
    await wizard.update(
        "議事録・レポートの保存先Google DriveフォルダURLを設定するぽん？",
        view,
    )


class Step6View(View):
    def __init__(self, wizard: SetupWizard):
        super().__init__(timeout=600)
        self.wizard = wizard

    @discord.ui.button(label="フォルダURLを入力", style=discord.ButtonStyle.primary, emoji="📁")
    async def set_drive(self, button: Button, interaction: discord.Interaction):
        await interaction.response.send_modal(DriveModal(self.wizard))

    @discord.ui.button(label="スキップ", style=discord.ButtonStyle.secondary)
    async def skip(self, button: Button, interaction: discord.Interaction):
        await interaction.response.defer()
        self.wizard.step = 6
        await show_step7(self.wizard)


class DriveModal(discord.ui.Modal):
    def __init__(self, wizard: SetupWizard):
        super().__init__(title="Google Drive連携")
        self.wizard = wizard
        self.add_item(discord.ui.InputText(
            label="フォルダURL",
            placeholder="https://drive.google.com/drive/folders/xxxx",
            max_length=500,
        ))

    async def callback(self, interaction: discord.Interaction):
        url = self.children[0].value.strip()
        await self.wizard.bot.config.set_drive_folder(self.wizard.guild.id, url)
        await interaction.response.send_message("✅ Drive連携設定したぽん！", ephemeral=True)
        self.wizard.step = 6
        await show_step7(self.wizard)


# ====== Step 7: 過去ログ取り込み ======

async def show_step7(wizard: SetupWizard):
    view = Step7View(wizard)
    await wizard.update(
        "最後に、過去ログの取り込みだぽん！\n"
        "どのくらい遡って学習するか選んでねぽん。",
        view,
    )


class Step7View(View):
    def __init__(self, wizard: SetupWizard):
        super().__init__(timeout=600)
        self.wizard = wizard
        self._started = False

    async def _start_backfill(self, interaction: discord.Interaction, days: int | None):
        if self._started:
            await interaction.response.send_message("実行中だぽん！", ephemeral=True)
            return
        self._started = True
        for item in self.children:
            item.disabled = True
        label = "全期間" if days is None else f"過去{days}日"
        await interaction.response.defer()
        await self.wizard.update(f"📚 {label}の取り込みを開始するぽん！⏳", self)
        await _run_backfill(self.wizard, days)

    @discord.ui.button(label="全部", style=discord.ButtonStyle.success, emoji="📚")
    async def all(self, button: Button, interaction: discord.Interaction):
        await self._start_backfill(interaction, None)

    @discord.ui.button(label="180日", style=discord.ButtonStyle.primary, emoji="📅")
    async def d180(self, button: Button, interaction: discord.Interaction):
        await self._start_backfill(interaction, 180)

    @discord.ui.button(label="30日", style=discord.ButtonStyle.primary, emoji="📆")
    async def d30(self, button: Button, interaction: discord.Interaction):
        await self._start_backfill(interaction, 30)

    @discord.ui.button(label="あとで", style=discord.ButtonStyle.secondary)
    async def skip(self, button: Button, interaction: discord.Interaction):
        await interaction.response.defer()
        self.wizard.step = 7
        await self.wizard.update(
            "🎉 **セットアップ完了だぽん！**\n"
            "過去ログは `/backfill` で取り込めるぽん。\n"
            "何でも聞いてねぽん！",
            View(),  # 空View（ボタンなし）
        )


async def _run_backfill(wizard: SetupWizard, days: int | None):
    from datetime import datetime, timedelta, timezone

    corpus = wizard.bot.config.get_corpus(wizard.guild.id)
    if not corpus:
        return

    label = "全期間" if days is None else f"過去{days}日"
    after = None
    if days:
        after = datetime.now(timezone.utc) - timedelta(days=days)

    channels = [
        ch for ch in wizard.guild.text_channels
        if ch.permissions_for(wizard.guild.me).read_message_history
        and not wizard.bot.config.is_ignored(wizard.guild.id, ch.id)
    ]

    total = 0
    for i, ch in enumerate(channels, 1):
        try:
            async def progress(c, _ch=ch, _i=i):
                await wizard.update(
                    f"📚 {label}取り込み中... ({_i}/{len(channels)}) #{_ch.name}: {c:,}件 | 合計: {total:,}件",
                    View(),
                )

            count = await wizard.bot.corpus.backfill_channel(ch, corpus, after=after, progress_callback=progress)
            total += count
            await wizard.update(
                f"📚 {label}取り込み中... ({i}/{len(channels)}) #{ch.name}: {count:,}件完了 | 合計: {total:,}件",
                View(),
            )
        except Exception as e:
            log.warning(f"Backfill error #{ch.name}: {e}")

    wizard.step = 7
    await wizard.update(
        f"🎉 **セットアップ完了だぽん！**\n"
        f"合計 **{total:,}件** のメッセージを学習したぽん！\n"
        f"何でも聞いてねぽん！",
        View(),
    )


# ====== register ======

def register(bot):
    @bot.slash_command(name="setup", description="初期設定をするぽん！")
    async def setup_cmd(ctx: discord.ApplicationContext):
        existing = bot.config.get_bureau(ctx.guild_id)
        if existing:
            await ctx.respond(
                f"このサーバーは既に **{existing}** として設定済みだぽん！\n"
                f"`/reset` でリセットしてねぽん。",
                ephemeral=True,
            )
            return

        # 2回目以降のsetup（reset後）は管理者のみ
        setup_count = bot.config._guild(ctx.guild_id).get("setup_count", 0)
        if setup_count > 0 and not ctx.author.guild_permissions.administrator:
            await ctx.respond(
                "再セットアップは管理者のみ実行できるぽん！", ephemeral=True
            )
            return

        used_bureaus = set()
        for gid, gdata in bot.config._config.items():
            if gid != str(ctx.guild_id) and "bureau" in gdata:
                used_bureaus.add(gdata["bureau"])

        # 固定メッセージを作成
        msg = await ctx.respond(
            "**🔧 おしゃべりやがぽん セットアップ**\n準備中...",
            silent=True,
        )
        # InteractionResponseのメッセージを取得
        if hasattr(msg, 'original_response'):
            msg = await msg.original_response()

        wizard = SetupWizard(bot, ctx.guild, msg)
        view = Step1View(wizard, used_bureaus)
        await wizard.update("まずは局を選んでねぽん！", view)
