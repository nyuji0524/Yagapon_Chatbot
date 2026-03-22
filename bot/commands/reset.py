"""コマンド: /reset - サーバー設定をリセット（コーパス削除オプション付き）pycord版"""

import discord


def register(bot):
    @bot.slash_command(name="reset", description="このサーバーの設定をリセットするぽん")
    @discord.default_permissions(administrator=True)
    async def reset_cmd(ctx: discord.ApplicationContext):
        guild_id = ctx.guild_id
        bureau = bot.config.get_bureau(guild_id)

        if not bureau:
            await ctx.respond(
                "このサーバーにはまだ設定がないぽん！", ephemeral=True
            )
            return

        store_name = bot.config.get_corpus(guild_id)
        view = ConfirmResetView(bot, guild_id, bureau, store_name)
        await ctx.respond(
            f"**⚠️ リセット方法を選んでねぽん**\n\n"
            f"局: **{bureau}**\n"
            f"コーパス: `{store_name or 'なし'}`\n\n"
            f"**設定のみリセット**: 設定を消すけどコーパス（学習データ）は残すぽん\n"
            f"**設定+コーパス削除**: 設定もコーパスも完全に消すぽん（取り消し不可）",
            view=view,
            ephemeral=True,
        )


class ConfirmResetView(discord.ui.View):
    def __init__(self, bot, guild_id: int, bureau: str, store_name: str | None):
        super().__init__(timeout=60)
        self.bot = bot
        self.guild_id = guild_id
        self.bureau = bureau
        self.store_name = store_name

    @discord.ui.button(label="設定のみリセット", style=discord.ButtonStyle.primary)
    async def config_only(self, button: discord.ui.Button, interaction: discord.Interaction):
        key = str(self.guild_id)
        if key in self.bot.config._config:
            setup_count = self.bot.config._config[key].get("setup_count", 0)
            del self.bot.config._config[key]
            # setup_countは保持（再setup時の管理者チェック用）
            self.bot.config._config[key] = {"setup_count": setup_count}
            await self.bot.config._save()

        await interaction.response.edit_message(
            content=f"**{self.bureau}** の設定をリセットしたぽん！\n"
            f"コーパスはGemini側に残っているぽん。\n"
            f"`/setup` で再設定できるぽん。",
            view=None,
        )

    @discord.ui.button(label="設定+コーパス削除", style=discord.ButtonStyle.danger)
    async def config_and_corpus(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content=f"設定とコーパスを削除中だぽん... ⏳", view=None
        )

        # コーパス削除
        if self.store_name:
            try:
                await self.bot.corpus.delete_corpus(self.store_name)
            except Exception as e:
                await interaction.edit_original_response(
                    content=f"コーパス削除に失敗したぽん...: {e}"
                )
                return

        # 設定削除（setup_countは保持）
        key = str(self.guild_id)
        if key in self.bot.config._config:
            setup_count = self.bot.config._config[key].get("setup_count", 0)
            del self.bot.config._config[key]
            self.bot.config._config[key] = {"setup_count": setup_count}
            await self.bot.config._save()

        await interaction.edit_original_response(
            content=f"**{self.bureau}** の設定とコーパスを完全削除したぽん！\n"
            f"`/setup` で再設定できるぽん。",
        )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.edit_message(
            content="リセットをキャンセルしたぽん。", view=None
        )
