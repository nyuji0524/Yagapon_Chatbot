"""Google Drive連携設定コマンド"""

import discord

from bot.gdrive import upload_to_drive


def register(bot):
    @bot.slash_command(name="drive_set", description="Google DriveフォルダURLを設定")
    @discord.option("folder_url", description="Google DriveフォルダのURL")
    async def drive_set(ctx: discord.ApplicationContext, folder_url: str):
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

        bot.config.set_drive_folder(ctx.guild.id, folder_url)
        await ctx.respond(
            f"✅ Google Driveフォルダを設定したぽん！\n📁 {folder_url}",
            ephemeral=True,
        )

    @bot.slash_command(name="drive_test", description="Google Drive連携のテスト")
    async def drive_test(ctx: discord.ApplicationContext):
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
                "- `.env` に `GOOGLE_APPS_SCRIPT_URL` が設定されているか\n"
                "- Google Apps Scriptが正しくデプロイされているか\n"
                "- フォルダへのアクセス権限があるか",
                ephemeral=True,
            )

    @bot.slash_command(name="drive_status", description="Google Drive連携の状態を確認")
    async def drive_status(ctx: discord.ApplicationContext):
        if not ctx.guild:
            await ctx.respond("サーバーで実行してほしいぽん！", ephemeral=True)
            return

        import os
        gas_url = os.environ.get("GOOGLE_APPS_SCRIPT_URL", "")
        folder_url = bot.config.get_drive_folder(ctx.guild.id)

        status_lines = []
        status_lines.append(f"**Apps Script URL**: {'✅ 設定済み' if gas_url else '❌ 未設定（.envに追加が必要）'}")
        status_lines.append(f"**フォルダURL**: {'✅ ' + folder_url if folder_url else '❌ 未設定（/drive_set で設定）'}")

        await ctx.respond(
            "📁 **Google Drive連携ステータス**\n\n" + "\n".join(status_lines),
            ephemeral=True,
        )
