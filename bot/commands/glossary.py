"""
/glossary add <用語> <説明> - 語録に追加
/glossary list - 語録一覧
/glossary delete <用語> - 語録から削除
/glossary bulk - 一括登録（モーダル）
/glossary candidates - 文書から抽出した確認待ち候補
/glossary approve <用語> - 候補を確認して正式登録
"""

import discord

from bot.authorization import require_guild_admin
from knowledge_catalog.models import CatalogPatch
from knowledge_catalog.store import CatalogConflictError, CatalogNotFoundError, normalize_label


def register(bot):
    group = bot.create_group("glossary", "語録辞書だぽん！")

    @group.command(name="add", description="語録に用語を追加するぽん！")
    @discord.option("term", description="用語（例: やがサポ）")
    @discord.option("definition", description="説明（例: IT局が開発している運営側アプリ）")
    @discord.option("reading", description="ひらがな読み（例: やがさぽ）", required=False, default="")
    @discord.option("aliases", description="別名（カンマ区切り。例: やがサポート,YagaSupport）", required=False, default="")
    async def glossary_add(ctx: discord.ApplicationContext, term: str, definition: str, reading: str = "", aliases: str = ""):
        if not await require_guild_admin(ctx):
            return
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

        embeds = []
        items = sorted(glossary.items())
        for page_start in range(0, len(items), 25):
            embed = discord.Embed(
                title="📖 語録辞書",
                description=f"登録済み {len(items)}件",
                color=discord.Color.green(),
            )
            for term, entry in items[page_start:page_start + 25]:
                reading = entry.get("reading", "")
                title = f"{term}（{reading}）" if reading else term
                desc = entry["definition"]
                aliases = entry.get("aliases", [])
                if aliases:
                    desc += f"\n_別名: {', '.join(aliases)}_"
                embed.add_field(name=title, value=desc[:1024], inline=False)
            embeds.append(embed)

        await ctx.respond(embeds=embeds[:10], silent=True)

    @group.command(name="candidates", description="文書から抽出した確認待ち用語を表示するぽん")
    async def glossary_candidates(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
        candidates = bot.catalog.list(ctx.guild_id, kind="term", status="candidate")
        if not candidates:
            await ctx.respond("確認待ちの用語候補はないぽん。", ephemeral=True)
            return
        lines = []
        for entry in candidates:
            description = entry.description or "説明未記入"
            aliases = f" / 別名: {', '.join(entry.aliases)}" if entry.aliases else ""
            lines.append(f"**{entry.label}** — {description}{aliases}")
        pages = []
        current = ""
        for line in lines:
            if current and len(current) + len(line) + 1 > 3900:
                pages.append(current)
                current = ""
            current += ("\n" if current else "") + line
        if current:
            pages.append(current)
        embeds = [
            discord.Embed(
                title=f"🔎 確認待ち用語 ({len(candidates)}件)",
                description=page,
                color=discord.Color.gold(),
            )
            for page in pages[:10]
        ]
        embeds[-1].set_footer(text="内容を確認して /glossary approve で正式登録")
        await ctx.respond(embeds=embeds, ephemeral=True)

    @group.command(name="approve", description="確認待ち候補を語録へ正式登録するぽん")
    @discord.option("term", description="候補の用語（完全一致）")
    @discord.option("definition", description="説明を修正する場合のみ入力", required=False, default="")
    @discord.option("reading", description="読みを修正する場合のみ入力", required=False, default="")
    @discord.option("aliases", description="別名を修正する場合のみカンマ区切りで入力", required=False, default="")
    async def glossary_approve(
        ctx: discord.ApplicationContext,
        term: str,
        definition: str = "",
        reading: str = "",
        aliases: str = "",
    ):
        if not await require_guild_admin(ctx):
            return
        matches = [
            entry
            for entry in bot.catalog.list(ctx.guild_id, kind="term", status="candidate")
            if normalize_label(entry.label) == normalize_label(term)
        ]
        if len(matches) != 1:
            await ctx.respond(
                "候補を一意に見つけられなかったぽん。`/glossary candidates` の表記をそのまま指定してねぽん。",
                ephemeral=True,
            )
            return
        candidate = matches[0]
        changes = {"status": "approved", "expected_revision": candidate.revision}
        if definition.strip():
            changes["description"] = definition.strip()
        if reading.strip():
            changes["reading"] = reading.strip()
        if aliases.strip():
            changes["aliases"] = [item.strip() for item in aliases.split(",") if item.strip()]
        try:
            approved = bot.catalog.patch(
                ctx.guild_id,
                candidate.id,
                CatalogPatch(**changes),
            )
        except (CatalogConflictError, CatalogNotFoundError):
            await ctx.respond("候補が更新されたため、一覧を再表示してやり直してねぽん。", ephemeral=True)
            return
        glossary = bot.config.get_glossary(ctx.guild_id)
        glossary[approved.label] = {
            "definition": approved.description or "説明未記入",
            **({"reading": approved.reading} if approved.reading else {}),
            **({"aliases": approved.aliases} if approved.aliases else {}),
        }
        await bot.config.set_glossary(ctx.guild_id, glossary)
        await ctx.respond(
            f"✅ **{approved.label}** を確認済み語録として登録したぽん！\n"
            "文字起こし・議事録・RAG検索で使われるぽん。",
            ephemeral=True,
        )

    @group.command(name="delete", description="語録から用語を削除するぽん")
    @discord.option("term", description="削除する用語")
    async def glossary_delete(ctx: discord.ApplicationContext, term: str):
        if not await require_guild_admin(ctx):
            return
        glossary = bot.config.get_glossary(ctx.guild_id)
        if term in glossary:
            del glossary[term]
            await bot.config.set_glossary(ctx.guild_id, glossary)
            await ctx.respond(f"✅ 「**{term}**」を削除したぽん。", ephemeral=True)
        else:
            await ctx.respond(f"「{term}」は語録にないぽん。", ephemeral=True)

    @group.command(name="bulk", description="語録を一括登録するぽん！")
    async def glossary_bulk(ctx: discord.ApplicationContext):
        if not await require_guild_admin(ctx):
            return
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
