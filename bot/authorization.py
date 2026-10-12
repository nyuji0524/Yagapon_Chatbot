"""Discord command authorization helpers."""


def is_guild_admin(ctx) -> bool:
    guild = getattr(ctx, "guild", None)
    author = getattr(ctx, "author", None)
    permissions = getattr(author, "guild_permissions", None)
    return bool(guild and permissions and permissions.administrator)


async def require_guild_admin(ctx) -> bool:
    """Return True for guild administrators and respond safely otherwise."""
    if is_guild_admin(ctx):
        return True
    await ctx.respond("この操作はサーバー管理者のみ実行できるぽん。", ephemeral=True)
    return False


async def require_registered_member(ctx, config) -> bool:
    """Allow guild admins and registered members; keep pre-sync guilds usable."""
    guild = getattr(ctx, "guild", None)
    author = getattr(ctx, "author", None)
    if guild is None or author is None:
        await ctx.respond("このコマンドは設定済みのDiscordサーバー内で実行してほしいぽん。", ephemeral=True)
        return False
    if not config.get_corpus(guild.id):
        await ctx.respond("先にサーバー管理者が `/setup` を実行してほしいぽん。", ephemeral=True)
        return False
    members = config.get_members(guild.id)
    if members and str(author.id) not in members and not is_guild_admin(ctx):
        await ctx.respond(
            "この機能は登録メンバー専用だぽん。管理者に `/member sync` をお願いしてねぽん。",
            ephemeral=True,
        )
        return False
    return True


def can_manage_member(ctx, target) -> bool:
    """Members may manage themselves; only administrators may manage others."""
    author = getattr(ctx, "author", None)
    return bool(author and (target.id == author.id or is_guild_admin(ctx)))
