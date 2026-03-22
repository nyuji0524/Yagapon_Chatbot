"""
/member sync - サーバーのメンバー+ロールから自動登録
/member register <nickname> - 自分の呼び名を変更
/member list - 登録メンバー一覧
/member roles - ロール分類設定
"""

import discord


def _classify_member_roles(member: discord.Member, role_mapping: dict) -> dict:
    """メンバーのロールをカテゴリ別に分類"""
    position_ids = set(role_mapping.get("position", []))
    task_ids = set(role_mapping.get("task", []))
    grade_ids = set(role_mapping.get("grade", []))

    position = []
    tasks = []
    grade = ""

    for r in member.roles:
        if r.name == "@everyone":
            continue
        if r.id in position_ids:
            position.append(r.name)
        elif r.id in task_ids:
            tasks.append(r.name)
        elif r.id in grade_ids:
            grade = r.name

    return {
        "name": member.display_name,
        "role": ", ".join(position) if position else "メンバー",
        "tasks": tasks,
        "grade": grade,
    }


def register(bot):
    group = bot.create_group("member", "メンバー管理だぽん！")

    @group.command(name="sync", description="サーバーメンバーをロールから自動登録するぽん！")
    # @discord.default_permissions(administrator=True)  # TODO: テスト後に戻す
    async def member_sync(ctx: discord.ApplicationContext):
        await ctx.defer()
        guild = ctx.guild
        role_mapping = bot.config.get_role_mapping(guild.id)

        if not role_mapping:
            await ctx.followup.send(
                "⚠️ ロールの分類がまだ設定されてないぽん！\n"
                "`/member roles` で役職・担当・学年のロールを設定してねぽん。"
            )
            return

        human_members = [m for m in guild.members if not m.bot]
        members = bot.config.get_members(guild.id)

        for m in human_members:
            uid = str(m.id)
            info = _classify_member_roles(m, role_mapping)
            if uid in members:
                # 呼び名は既存を維持
                nickname = members[uid].get("nickname", "")
                info["nickname"] = nickname
            members[uid] = info

        await bot.config.set_members(guild.id, members)
        await ctx.followup.send(
            f"✅ **{len(human_members)}人** をロールから登録/更新したぽん！\n"
            f"`/member register` で呼び名を変更できるぽん。"
        )

    @group.command(name="roles", description="役職・担当・学年のロールを分類するぽん！")
    # @discord.default_permissions(administrator=True)  # TODO: テスト後に戻す
    async def member_roles(ctx: discord.ApplicationContext):
        guild = ctx.guild
        roles = [r for r in guild.roles if r.name != "@everyone"]
        if not roles:
            await ctx.respond("ロールがないぽん...", ephemeral=True)
            return

        view = RoleMappingView(bot, guild, roles)
        await ctx.respond(
            "ロールをカテゴリ別に分類するぽん！\n"
            "それぞれのプルダウンから該当するロールを選んでねぽん。",
            view=view,
            ephemeral=True,
        )

    @group.command(name="register", description="自分の呼び名を登録するぽん！")
    @discord.option("nickname", description="呼ばれたい名前")
    async def member_register(ctx: discord.ApplicationContext, nickname: str):
        guild_id = ctx.guild_id
        uid = str(ctx.author.id)
        members = bot.config.get_members(guild_id)

        if uid not in members:
            # 未登録なら基本情報で追加
            role_mapping = bot.config.get_role_mapping(guild_id)
            info = _classify_member_roles(ctx.author, role_mapping)
            members[uid] = info

        members[uid]["nickname"] = nickname
        await bot.config.set_members(guild_id, members)

        await ctx.respond(
            f"✅ 呼び名を「**{nickname}**」に設定したぽん！",
            ephemeral=True,
        )

    @group.command(name="list", description="登録メンバー一覧を表示するぽん！")
    async def member_list(ctx: discord.ApplicationContext):
        members = bot.config.get_members(ctx.guild_id)
        if not members:
            await ctx.respond(
                "まだメンバーが登録されてないぽん。\n"
                "管理者が `/member roles` → `/member sync` してねぽん！"
            )
            return

        embed = discord.Embed(title="登録メンバー一覧", color=discord.Color.blue())
        for uid, info in members.items():
            nickname = info.get("nickname", "")
            display = nickname if nickname else info.get("name", "不明")
            grade = info.get("grade", "")
            if grade:
                display += f" ({grade})"

            tasks_str = ", ".join(info.get("tasks", []))
            embed.add_field(
                name=display,
                value=f"役職: {info.get('role', '-')} | 担当: {tasks_str or '-'}",
                inline=False,
            )

        await ctx.respond(embed=embed)


class RoleMappingView(discord.ui.View):
    def __init__(self, bot, guild: discord.Guild, roles: list[discord.Role]):
        super().__init__(timeout=300)
        self.bot = bot
        self.guild = guild
        self.mapping = {"position": [], "task": [], "grade": []}

        # ロール数が25個以下なら各セレクトに全部入れる
        options = [
            discord.SelectOption(label=r.name, value=str(r.id))
            for r in roles[:25]
        ]

        self.add_item(RoleCategorySelect(
            "position", "👑 役職ロール（局長・副局長など）", options, self
        ))
        self.add_item(RoleCategorySelect(
            "task", "💼 担当ロール（開発・デザインなど）", options, self
        ))
        self.add_item(RoleCategorySelect(
            "grade", "🎓 学年ロール（B3・M1など）", options, self
        ))

    @discord.ui.button(label="保存する", style=discord.ButtonStyle.success, row=4)
    async def save(self, button: discord.ui.Button, interaction: discord.Interaction):
        await self.bot.config.set_role_mapping(self.guild.id, self.mapping)

        summary = []
        for cat, label in [("position", "役職"), ("task", "担当"), ("grade", "学年")]:
            role_ids = self.mapping.get(cat, [])
            names = []
            for rid in role_ids:
                role = self.guild.get_role(int(rid))
                if role:
                    names.append(role.name)
            if names:
                summary.append(f"**{label}**: {', '.join(names)}")

        await interaction.response.edit_message(
            content=f"✅ ロール分類を保存したぽん！\n" + "\n".join(summary) +
            f"\n\n`/member sync` でメンバーを自動登録できるぽん！",
            view=None,
        )


class RoleCategorySelect(discord.ui.Select):
    def __init__(self, category: str, placeholder: str,
                 options: list[discord.SelectOption], parent: RoleMappingView):
        super().__init__(
            placeholder=placeholder,
            min_values=0,
            max_values=len(options),
            options=options,
        )
        self.category = category
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        self.parent_view.mapping[self.category] = [int(v) for v in self.values]
        await interaction.response.defer()
