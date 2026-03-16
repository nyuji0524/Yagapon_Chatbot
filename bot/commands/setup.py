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

import os
import discord
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

        await interaction.response.defer()
        bot = interaction.client

        try:
            store_name = await bot.corpus.create_corpus(interaction.guild_id, bureau)
            await bot.config.set_bureau(interaction.guild_id, bureau, store_name)
            await interaction.followup.send(
                f"✅ 「**{bureau}**」として登録したぽん！コーパスも作成済みだぽん。\n"
                f"次はこのサーバーで学習してほしくないチャンネルを選ぶぽん！",
                view=Step2IgnoreView(),
            )
        except Exception as e:
            await interaction.followup.send(f"❌ エラーだぽん...: {e}")


class Step1BureauView(View):
    def __init__(self, used_bureaus: set[str]):
        super().__init__(timeout=300)
        self.add_item(BureauSelect(used_bureaus))


# ====== Step 2: 学習除外チャンネル ======

class IgnoreChannelSelect(ChannelSelect):
    def __init__(self):
        super().__init__(
            placeholder="学習しないチャンネルを選ぶぽん（複数OK）",
            min_values=0,
            max_values=25,
            channel_types=[discord.ChannelType.text],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        bot = interaction.client

        for ch in self.values:
            await bot.config.add_ignore_channel(interaction.guild_id, ch.id)

        ignored = ", ".join(f"<#{ch.id}>" for ch in self.values) if self.values else "なし"
        await interaction.followup.send(
            f"✅ 除外チャンネル: {ignored}\n"
            f"次はGitHub連携の設定だぽん！",
            view=Step3GithubView(),
        )


class Step2IgnoreView(View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(IgnoreChannelSelect())

    @discord.ui.button(label="スキップ (除外なし)", style=discord.ButtonStyle.secondary)
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
            f"mainブランチへのpushで自動デプロイ＆コードレビューが動くぽん！\n\n"
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


# ====== Step 4: リアクション絵文字 ======

class Step4ReactionsView(View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="リアクション有効化", style=discord.ButtonStyle.primary, emoji="✨")
    async def enable(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(ReactionEmojiModal())

    @discord.ui.button(label="リアクション無効", style=discord.ButtonStyle.secondary)
    async def disable(self, interaction: discord.Interaction, button: Button):
        bot = interaction.client
        await bot.config.set_reactions(interaction.guild_id, False, "💡", "😲", "😂")
        await interaction.response.send_message(
            "リアクションは無効にしたぽん！\n次はメンバー登録だぽん！",
            view=Step5MemberView(),
        )


class ReactionEmojiModal(discord.ui.Modal, title="リアクション絵文字の設定"):
    interesting = discord.ui.TextInput(label="興味深い", default="💡", max_length=5)
    surprised = discord.ui.TextInput(label="びっくり", default="😲", max_length=5)
    funny = discord.ui.TextInput(label="笑える", default="😂", max_length=5)

    async def on_submit(self, interaction: discord.Interaction):
        bot = interaction.client
        await bot.config.set_reactions(
            interaction.guild_id, True,
            str(self.interesting), str(self.surprised), str(self.funny),
        )
        await interaction.response.send_message(
            f"✅ リアクション設定完了だぽん！\n"
            f"  興味深い: {self.interesting} / びっくり: {self.surprised} / 笑える: {self.funny}\n\n"
            f"次はメンバー登録だぽん！",
            view=Step5MemberView(),
        )


# ====== Step 5: メンバー登録 (CSV形式メッセージ入力) ======

class Step5MemberView(View):
    def __init__(self):
        super().__init__(timeout=300)

    @discord.ui.button(label="メンバーを登録する", style=discord.ButtonStyle.primary, emoji="👥")
    async def register_members(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(MemberInputModal())

    @discord.ui.button(label="あとで登録する", style=discord.ButtonStyle.secondary)
    async def skip(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message(
            "メンバー登録はあとで `/member add` でもできるぽん！\n"
            "次はGoogle Drive連携だぽん！",
            view=Step6DriveView(),
        )


class MemberInputModal(discord.ui.Modal, title="メンバー一括登録"):
    members_csv = discord.ui.TextInput(
        label="1行1人: 名前, Discord ID, 役職, 担当, 学年",
        style=discord.TextStyle.paragraph,
        placeholder="中山裕二, 123456789012345678, 局長, bot開発/サーバー管理, M1\n田中太郎, 987654321098765432, 局員, デザイン, B3",
        max_length=4000,
    )

    async def on_submit(self, interaction: discord.Interaction):
        bot = interaction.client
        lines = str(self.members_csv).strip().split("\n")

        members = {}
        errors = []
        for i, line in enumerate(lines, 1):
            cols = [c.strip() for c in line.split(",")]
            if len(cols) < 5:
                errors.append(f"行{i}: カラム不足 (5つ必要)")
                continue
            name, discord_id, role, tasks, grade = cols[:5]
            discord_id = discord_id.strip()
            if not discord_id.isdigit():
                errors.append(f"行{i}: Discord IDが数字じゃないぽん ({discord_id})")
                continue
            members[discord_id] = {
                "name": name,
                "role": role,
                "tasks": [t.strip() for t in tasks.split("/") if t.strip()],
                "grade": grade,
            }

        await bot.config.set_members(interaction.guild_id, members)

        msg = f"✅ **{len(members)}人** 登録したぽん！\n"
        if errors:
            msg += "⚠️ エラー:\n" + "\n".join(errors[:5]) + "\n"
        msg += "\n次はGoogle Drive連携だぽん！"

        await interaction.response.send_message(msg, view=Step6DriveView())


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
    await interaction.followup.send(f"📚 {label}の過去ログ取り込みを処理中だぽん...")

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
            count = await bot.corpus.backfill_channel(
                ch, corpus,
                after=after,
                progress_callback=lambda c, _ch=ch, _i=i:
                    interaction.followup.send(f"📖 #{_ch.name}: {c:,}件取得中... ({_i}/{len(channels)})"),
            )
            total += count
            if count > 0:
                await interaction.followup.send(f"✅ #{ch.name}: {count:,}件")
        except Exception as e:
            await interaction.followup.send(f"⚠️ #{ch.name}: {e}")

    await interaction.followup.send(
        f"🎉 セットアップ完了だぽん！\n"
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
    async def setup_cmd(interaction: discord.Interaction):
        # 使用済みの局を取得
        used_bureaus = set()
        for gid, gdata in bot.config._config.items():
            if gid != str(interaction.guild_id) and "bureau" in gdata:
                used_bureaus.add(gdata["bureau"])

        await interaction.response.send_message(
            "セットアップを始めるぽん！まずは局を選んでねぽん！",
            view=Step1BureauView(used_bureaus),
        )
