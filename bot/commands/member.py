"""
/member sync - サーバーのメンバー+ロールから自動登録
/member register - 自分の追加情報（担当・学年）を登録
/member list - 登録メンバー一覧
"""

import discord
from discord import app_commands


def register(bot):
    group = app_commands.Group(name="member", description="メンバー管理だぽん！")

    @group.command(name="sync", description="サーバーメンバーをロールから自動登録するぽん！")
    @app_commands.checks.has_permissions(administrator=True)
    async def member_sync(interaction: discord.Interaction):
        await interaction.response.defer()
        guild = interaction.guild

        # botとシステムユーザーを除外
        human_members = [m for m in guild.members if not m.bot]

        members = bot.config.get_members(guild.id)

        registered = 0
        for m in human_members:
            uid = str(m.id)
            # ロールからトップロール（@everyone以外）を取得
            roles = [r for r in m.roles if r.name != "@everyone"]
            top_role = roles[-1].name if roles else "メンバー"

            if uid in members:
                # 既存メンバーはロールだけ更新
                members[uid]["role"] = top_role
                members[uid]["name"] = m.display_name
            else:
                # 新規登録
                members[uid] = {
                    "name": m.display_name,
                    "role": top_role,
                    "tasks": [],
                    "grade": "",
                }
            registered += 1

        await bot.config.set_members(guild.id, members)
        await interaction.followup.send(
            f"✅ **{registered}人** をサーバーロールから登録/更新したぽん！\n"
            f"各メンバーは `/member register` で担当・学年を追加できるぽん。"
        )

    @member_sync.error
    async def sync_error(interaction: discord.Interaction, error):
        if isinstance(error, app_commands.MissingPermissions):
            await interaction.response.send_message(
                "管理者権限が必要だぽん！", ephemeral=True
            )

    @group.command(name="register", description="自分の担当・学年を登録するぽん！")
    async def member_register(interaction: discord.Interaction):
        guild_id = interaction.guild_id
        uid = str(interaction.user.id)
        members = bot.config.get_members(guild_id)

        if uid not in members:
            # 未登録なら基本情報で自動追加
            roles = [r for r in interaction.user.roles if r.name != "@everyone"]
            top_role = roles[-1].name if roles else "メンバー"
            members[uid] = {
                "name": interaction.user.display_name,
                "role": top_role,
                "tasks": [],
                "grade": "",
            }

        # 現在の情報をモーダルのデフォルトに
        current = members[uid]
        modal = MemberInfoModal(
            default_tasks="/".join(current.get("tasks", [])),
            default_grade=current.get("grade", ""),
        )
        await interaction.response.send_modal(modal)

    @group.command(name="list", description="登録メンバー一覧を表示するぽん！")
    async def member_list(interaction: discord.Interaction):
        members = bot.config.get_members(interaction.guild_id)
        if not members:
            await interaction.response.send_message(
                "まだメンバーが登録されてないぽん。\n"
                "管理者が `/member sync` するか、各自 `/member register` してねぽん！"
            )
            return

        embed = discord.Embed(title="登録メンバー一覧", color=discord.Color.blue())
        for uid, info in members.items():
            tasks_str = ", ".join(info.get("tasks", []))
            grade = info.get("grade", "")
            name_line = f"{info['name']}"
            if grade:
                name_line += f" ({grade})"
            embed.add_field(
                name=name_line,
                value=f"役職: {info.get('role', '-')} | 担当: {tasks_str or '-'}",
                inline=False,
            )

        await interaction.response.send_message(embed=embed)

    bot.tree.add_command(group)


class MemberInfoModal(discord.ui.Modal, title="メンバー情報の登録"):
    tasks = discord.ui.TextInput(
        label="担当（複数は / 区切り）",
        placeholder="bot開発 / サーバー管理",
        required=False,
        max_length=200,
    )
    grade = discord.ui.TextInput(
        label="学年",
        placeholder="B3, M1 など",
        required=False,
        max_length=10,
    )

    def __init__(self, default_tasks: str = "", default_grade: str = ""):
        super().__init__()
        self.tasks.default = default_tasks
        self.grade.default = default_grade

    async def on_submit(self, interaction: discord.Interaction):
        bot = interaction.client
        guild_id = interaction.guild_id
        uid = str(interaction.user.id)
        members = bot.config.get_members(guild_id)

        if uid not in members:
            roles = [r for r in interaction.user.roles if r.name != "@everyone"]
            top_role = roles[-1].name if roles else "メンバー"
            members[uid] = {
                "name": interaction.user.display_name,
                "role": top_role,
            }

        members[uid]["tasks"] = [t.strip() for t in str(self.tasks).split("/") if t.strip()]
        members[uid]["grade"] = str(self.grade).strip()

        await bot.config.set_members(guild_id, members)
        await interaction.response.send_message(
            f"✅ 登録完了だぽん！\n"
            f"担当: {', '.join(members[uid]['tasks']) or '-'} / "
            f"学年: {members[uid]['grade'] or '-'}",
            ephemeral=True,
        )
