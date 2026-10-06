"""Google Drive連携設定コマンド"""

import discord

from bot.authorization import require_guild_admin
from bot.gdrive import diagnose_drive, upload_to_drive


def register(bot):
    @bot.slash_command(name="drive_set", description="Google DriveフォルダURLを設定")
    @discord.default_permissions(administrator=True)
    @discord.option("folder_url", description="Google DriveフォルダのURL")
    async def drive_set(ctx: discord.ApplicationContext, folder_url: str):
        if not await require_guild_admin(ctx):
            return
        if not ctx.guild:
            await ctx.respond("サーバーで実行してほしいぽん！", ephemeral=True)
            return

        if "drive.google.com" not in folder_url or "/folders/" not in folder_url:
            await ctx.respond(
                "正しいGoogle DriveフォルダのURLを入力してほしいぽん！\n"
                "例: `https://drive.google.com/drive/folders/xxxxx`",
                ephemeral=True,
            )
            return

        await bot.config.set_drive_folder(ctx.guild.id, folder_url)
        await ctx.respond(
            f"✅ Google Driveフォルダを設定したぽん！\n📁 {folder_url}",
            ephemeral=True,
        )

    @bot.slash_command(name="drive_test", description="Google Drive連携のテスト")
    @discord.default_permissions(administrator=True)
    async def drive_test(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        if not ctx.guild:
            await ctx.respond("サーバーで実行してほしいぽん！", ephemeral=True)
            return

        folder_url = bot.config.get_drive_folder(ctx.guild.id)
        if not folder_url:
            await ctx.respond(
                "Google Driveフォルダが設定されてないぽん！\n`/drive_set` で設定してねぽん。",
                ephemeral=True,
            )
            return

        await ctx.defer(ephemeral=True)

        url = await upload_to_drive(
            folder_url,
            "テスト_おしゃべりやがぽん",
            "# テスト\n\nGoogle Drive連携のテストだぽん！\n\nこのドキュメントは削除してOKです。",
        )

        if url:
            await ctx.followup.send(
                f"✅ テスト成功だぽん！\n📄 [テストドキュメントを確認]({url})",
                ephemeral=True,
            )
        else:
            await ctx.followup.send(
                "❌ テスト失敗だぽん...\n"
                "以下を確認してほしいぽん：\n"
                "- VMのサービスアカウントがフォルダの投稿者以上か\n"
                "- Drive APIとDocs APIが有効か\n"
                "- コンテナでApplication Default Credentialsを取得できるか",
                ephemeral=True,
            )

    @bot.slash_command(name="drive_status", description="Google Drive連携の状態を確認")
    @discord.default_permissions(administrator=True)
    async def drive_status(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        if not ctx.guild:
            await ctx.respond("サーバーで実行してほしいぽん！", ephemeral=True)
            return

        folder_url = bot.config.get_drive_folder(ctx.guild.id)
        if not folder_url:
            await ctx.respond(
                "📁 **Google Drive連携ステータス**\n\n❌ フォルダ未設定（`/drive_set`）",
                ephemeral=True,
            )
            return
        await ctx.defer(ephemeral=True)
        try:
            diagnosis = await diagnose_drive(folder_url)
            folder = diagnosis["folder"]
            access = "✅ 書き込み可能" if diagnosis["can_add_children"] else "❌ 書き込み権限なし"
            await ctx.followup.send(
                "📁 **Google Drive連携ステータス**\n\n"
                f"**認証**: ✅ {diagnosis['credential_source']}\n"
                f"**フォルダ**: ✅ {folder.get('name', folder.get('id'))}\n"
                f"**権限**: {access}",
                ephemeral=True,
            )
        except Exception as exc:
            await ctx.followup.send(
                "📁 **Google Drive連携ステータス**\n\n"
                f"❌ 接続確認に失敗: `{type(exc).__name__}`\n"
                "コンテナログでAPIのエラーコードを確認してほしいぽん。",
                ephemeral=True,
            )
