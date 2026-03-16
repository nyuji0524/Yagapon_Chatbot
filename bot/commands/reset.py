"""コマンド: /reset - サーバー設定をリセット"""

import discord
from discord import app_commands


def register(bot):
    @bot.tree.command(name="reset", description="このサーバーの設定をリセットするぽん")
    @app_commands.checks.has_permissions(administrator=True)
    async def reset_cmd(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        bureau = bot.config.get_bureau(guild_id)

        if not bureau:
            await interaction.response.send_message(
                "このサーバーにはまだ設定がないぽん！", ephemeral=True
            )
            return

        # 確認ボタン
        view = ConfirmResetView(bot, guild_id, bureau)
        await interaction.response.send_message(
            f"**⚠️ 本当にリセットするぽん？**\n\n"
            f"局: **{bureau}**\n"
            f"コーパス・メンバー・リアクション等すべての設定が削除されるぽん。\n"
            f"（コーパスのデータ自体はGemini側に残るぽん）",
            view=view,
            ephemeral=True,
        )

    @reset_cmd.error
    async def reset_error(interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "管理者権限が必要だぽん！", ephemeral=True
            )


class ConfirmResetView(discord.ui.View):
    def __init__(self, bot, guild_id: int, bureau: str):
        super().__init__(timeout=60)
        self.bot = bot
        self.guild_id = guild_id
        self.bureau = bureau

    @discord.ui.button(label="リセットする", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        key = str(self.guild_id)
        if key in self.bot.config._config:
            del self.bot.config._config[key]
            await self.bot.config._save()

        await interaction.response.edit_message(
            content=f"**{self.bureau}** の設定をリセットしたぽん！\n`/setup` で再設定できるぽん。",
            view=None,
        )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(
            content="リセットをキャンセルしたぽん。", view=None
        )
