"""
/glossary add <用語> <説明> - 語録に追加
/glossary list - 語録一覧
/glossary delete <用語> - 語録から削除
/glossary bulk - 一括登録（モーダル）
"""

import discord


def register(bot):
    group = bot.create_group("glossary", "語録辞書だぽん！")

    @group.command(name="add", description="語録に用語を追加するぽん！")
    @discord.option("term", description="用語（例: やがサポ）")
    @discord.option("definition", description="説明（例: IT局が開発している運営側アプリ）")
    @discord.option("reading", description="ひらがな読み（例: やがさぽ）", required=False, default="")
    @discord.option("aliases", description="別名（カンマ区切り。例: やがサポート,YagaSupport）", required=False, default="")
    async def glossary_add(ctx: discord.ApplicationContext, term: str, definition: str, reading: str = "", aliases: str = ""):
        guild_id = ctx.guild_id
        glossary = bot.config.get_glossary(guild_id)

        entry = {"definition": definition}
        if reading:
            entry["reading"] = reading
        if aliases:
            entry["aliases"] = [a.strip() for a in aliases.split(",") if a.strip()]

        glossary[term] = entry
        await bot.config.set_glossary(guild_id, glossary)

        extra = ""
        if reading:
            extra += f"\n読み: {reading}"
        if entry.get("aliases"):
            extra += f"\n別名: {', '.join(entry['aliases'])}"

        await ctx.respond(
            f"✅ 語録に追加したぽん！\n**{term}**: {definition}{extra}",
            ephemeral=True,
        )

    @group.command(name="list", description="語録一覧を表示するぽん！")
    async def glossary_list(ctx: discord.ApplicationContext):
        glossary = bot.config.get_glossary(ctx.guild_id)
        if not glossary:
            await ctx.respond("まだ語録が登録されてないぽん！\n`/glossary add` で追加してねぽん。", silent=True)
            return

        embed = discord.Embed(title="📖 語録辞書", color=discord.Color.green())
        for term, entry in sorted(glossary.items()):
            reading = entry.get("reading", "")
            title = f"{term}（{reading}）" if reading else term
            desc = entry["definition"]
            aliases = entry.get("aliases", [])
            if aliases:
                desc += f"\n_別名: {', '.join(aliases)}_"
            embed.add_field(name=title, value=desc, inline=False)

        await ctx.respond(embed=embed, silent=True)

    @group.command(name="delete", description="語録から用語を削除するぽん")
    @discord.option("term", description="削除する用語")
    async def glossary_delete(ctx: discord.ApplicationContext, term: str):
        glossary = bot.config.get_glossary(ctx.guild_id)
        if term in glossary:
            del glossary[term]
            await bot.config.set_glossary(ctx.guild_id, glossary)
            await ctx.respond(f"✅ 「**{term}**」を削除したぽん。", ephemeral=True)
        else:
            await ctx.respond(f"「{term}」は語録にないぽん。", ephemeral=True)

    @group.command(name="bulk", description="語録を一括登録するぽん！")
    async def glossary_bulk(ctx: discord.ApplicationContext):
        await ctx.send_modal(BulkGlossaryModal(bot))


def _parse_glossary_line(line: str) -> dict | None:
    """1行をパースして {term, reading, aliases, definition} を返す。2つの形式に対応。"""
    line = line.strip()
    if not line:
        return None

    # 形式1: →区切り「やがサポ(やがサポート)[やがさぽ]→IT局が開発しているアプリ」
    if "→" in line:
        term_part, definition = line.split("→", 1)
        term_part = term_part.strip()
        definition = definition.strip()
        aliases = []
        reading = ""
        # [ひらがな] を読みとして解析
        if "[" in term_part and "]" in term_part:
            reading = term_part[term_part.index("[") + 1:term_part.index("]")].strip()
            term_part = term_part[:term_part.index("[")].strip()
        # (別名) を解析
        if "(" in term_part and ")" in term_part:
            main = term_part[:term_part.index("(")].strip()
            alias_str = term_part[term_part.index("(") + 1:term_part.index(")")].strip()
            aliases = [a.strip() for a in alias_str.split(",") if a.strip()]
            term_part = main
        if term_part and definition:
            return {"term": term_part, "reading": reading, "aliases": aliases, "definition": definition}
        return None

    # 形式2: CSV「やがサポ, やがさぽ, やがサポート, IT局が開発しているアプリ」
    # 4カラム: 用語, 読み, 別名, 説明
    # 3カラム: 用語, 別名, 説明
    # 2カラム: 用語, 説明
    parts = [p.strip() for p in line.split(",")]
    if len(parts) >= 4:
        term = parts[0]
        reading = parts[1]
        alias_str = parts[2]
        definition = ",".join(parts[3:])
        aliases = [a.strip() for a in alias_str.split("/") if a.strip()] if alias_str else []
        if term and definition:
            return {"term": term, "reading": reading, "aliases": aliases, "definition": definition}
    elif len(parts) == 3:
        term = parts[0]
        alias_str = parts[1]
        definition = parts[2]
        aliases = [a.strip() for a in alias_str.split("/") if a.strip()] if alias_str else []
        if term and definition:
            return {"term": term, "reading": "", "aliases": aliases, "definition": definition}
    elif len(parts) == 2:
        term, definition = parts
        if term and definition:
            return {"term": term, "reading": "", "aliases": [], "definition": definition}

    return None


class BulkGlossaryModal(discord.ui.Modal):
    def __init__(self, bot):
        super().__init__(title="語録一括登録")
        self.bot = bot
        self.add_item(discord.ui.InputText(
            label="1行1用語。CSV形式 or →形式",
            placeholder="局,きょく,,矢上祭の8つの局のこと\nやがサポ(やがサポート)[やがさぽ]→IT局のアプリ\n総務系申請,そうむけいしんせい,,総務局管轄の申請",
            style=discord.InputTextStyle.long,
            max_length=4000,
        ))

    async def callback(self, interaction: discord.Interaction):
        text = self.children[0].value.strip()
        glossary = self.bot.config.get_glossary(interaction.guild_id)
        added = 0
        errors = []

        for i, line in enumerate(text.split("\n"), 1):
            result = _parse_glossary_line(line)
            if result is None:
                if line.strip():
                    errors.append(f"{i}行目: パースできなかったぽん")
                continue

            entry = {"definition": result["definition"]}
            if result["reading"]:
                entry["reading"] = result["reading"]
            if result["aliases"]:
                entry["aliases"] = result["aliases"]
            glossary[result["term"]] = entry
            added += 1

        await self.bot.config.set_glossary(interaction.guild_id, glossary)

        msg = f"✅ **{added}件** の用語を登録したぽん！\n`/glossary list` で確認できるぽん。"
        if errors:
            msg += f"\n\n⚠️ スキップ: {'; '.join(errors[:5])}"
        await interaction.response.send_message(msg, silent=True)
