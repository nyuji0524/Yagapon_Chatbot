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


def can_manage_member(ctx, target) -> bool:
    """Members may manage themselves; only administrators may manage others."""
    author = getattr(ctx, "author", None)
    return bool(author and (target.id == author.id or is_guild_admin(ctx)))
