"""
/member sync <sheets_url> - スプレッドシートからメンバー情報を同期
/member list - 登録メンバー一覧
"""

import re
import discord
from discord import app_commands


def register(bot):
    group = app_commands.Group(name="member", description="メンバー管理だぽん！")

    @group.command(name="sync", description="スプレッドシートからメンバー情報を同期するぽん！")
    @app_commands.describe(sheets_url="Google SheetsのURL")
    async def member_sync(interaction: discord.Interaction, sheets_url: str):
        await interaction.response.defer()

        # URLを保存
        await bot.config.set_sheets_url(interaction.guild_id, sheets_url)

        # スプレッドシートIDを抽出
        match = re.search(r"/spreadsheets/d/([a-zA-Z0-9_-]+)", sheets_url)
        if not match:
            await interaction.followup.send("❌ 有効なGoogle SheetsのURLじゃないぽん...")
            return

        sheet_id = match.group(1)

        # Google Sheets API (公開シート or CSV export)
        csv_url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv"

        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(csv_url) as resp:
                    if resp.status != 200:
                        await interaction.followup.send(
                            "❌ シートを読み込めなかったぽん。\n"
                            "シートの共有設定を「リンクを知っている全員」にしてほしいぽん！"
                        )
                        return
                    text = await resp.text()

            # CSV解析 (ヘッダー: 名前, Discord ID, 役職, 担当, 学年)
            lines = text.strip().split("\n")
            if len(lines) < 2:
                await interaction.followup.send("❌ データが空っぽいぽん...")
                return

            members = {}
            for line in lines[1:]:  # ヘッダースキップ
                cols = [c.strip().strip('"') for c in line.split(",")]
                if len(cols) < 5:
                    continue
                name, discord_id, role, tasks, grade = cols[:5]
                discord_id = discord_id.strip()
                if not discord_id.isdigit():
                    continue
                members[discord_id] = {
                    "name": name,
                    "role": role,
                    "tasks": [t.strip() for t in tasks.split("/") if t.strip()],
                    "grade": grade,
                }

            await bot.config.set_members(interaction.guild_id, members)
            await interaction.followup.send(
                f"✅ **{len(members)}人** のメンバー情報を同期したぽん！\n"
                f"これでDMでの質問も受け付けられるぽん！"
            )

        except ImportError:
            await interaction.followup.send("❌ aiohttp が必要だぽん。`pip install aiohttp`")
        except Exception as e:
            await interaction.followup.send(f"❌ エラーだぽん: {e}")

    @group.command(name="list", description="登録メンバー一覧を表示するぽん！")
    async def member_list(interaction: discord.Interaction):
        members = bot.config.get_members(interaction.guild_id)
        if not members:
            await interaction.response.send_message("まだメンバーが登録されてないぽん。`/member sync` で同期してねぽん！")
            return

        embed = discord.Embed(title="登録メンバー一覧", color=discord.Color.blue())
        for uid, info in members.items():
            tasks_str = ", ".join(info.get("tasks", []))
            embed.add_field(
                name=f"{info['name']} ({info.get('grade', '')})",
                value=f"役職: {info.get('role', '-')} | 担当: {tasks_str or '-'}",
                inline=False,
            )

        await interaction.response.send_message(embed=embed)

    bot.tree.add_command(group)
