"""コマンド: /corpus delete - Gemini側コーパスを完全削除"""

import discord
from discord import app_commands


def register(bot):
    group = app_commands.Group(name="corpus", description="コーパス管理コマンドだぽん")

    @group.command(name="delete", description="このサーバーのコーパスを完全削除するぽん")
    # @app_commands.checks.has_permissions(administrator=True)  # TODO: テスト後に戻す
    async def corpus_delete(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        store_name = bot.config.get_corpus(guild_id)
        bureau = bot.config.get_bureau(guild_id) or "不明"

        if not store_name:
            await interaction.response.send_message(
                "このサーバーにはコーパスが設定されてないぽん！", ephemeral=True
            )
            return

        view = ConfirmCorpusDeleteView(bot, guild_id, store_name, bureau)
        await interaction.response.send_message(
            f"**⚠️ コーパスを完全削除するぽん？**\n\n"
            f"局: **{bureau}**\n"
            f"コーパス: `{store_name}`\n\n"
            f"Gemini側の学習データがすべて失われるぽん。この操作は取り消せないぽん！",
            view=view,
            ephemeral=True,
        )

    @corpus_delete.error
    async def corpus_delete_error(interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "管理者権限が必要だぽん！", ephemeral=True
            )

    bot.tree.add_command(group)


class ConfirmCorpusDeleteView(discord.ui.View):
    def __init__(self, bot, guild_id: int, store_name: str, bureau: str):
        super().__init__(timeout=60)
        self.bot = bot
        self.guild_id = guild_id
        self.store_name = store_name
        self.bureau = bureau

    @discord.ui.button(label="完全削除する", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content=f"コーパスを削除中だぽん... ⏳", view=None
        )

        try:
            await self.bot.corpus.delete_corpus(self.store_name)
            # configからコーパス情報を削除
            g = self.bot.config._guild(self.guild_id)
            g.pop("corpus_store_name", None)
            await self.bot.config._save()

            await interaction.edit_original_response(
                content=f"**{self.bureau}** のコーパスを完全削除したぽん！\n"
                f"`/setup` で再作成できるぽん。"
            )
        except Exception as e:
            await interaction.edit_original_response(
                content=f"削除に失敗したぽん...: {e}"
            )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="コーパス削除をキャンセルしたぽん。", view=None
        )
