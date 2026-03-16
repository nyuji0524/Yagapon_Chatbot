"""API endpoints - health, status, ask, backfill"""

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


class AskRequest(BaseModel):
    guild_id: int
    query: str


class AskResponse(BaseModel):
    query: str
    response: str
    timestamp: str


class BackfillRequest(BaseModel):
    guild_id: int
    channel_id: int | None = None


@router.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@router.get("/status")
async def status(request: Request):
    bot = request.app.state.bot
    return {
        "bot_connected": not bot.is_closed(),
        "bot_user": str(bot.user) if bot.user else None,
        "guilds": len(bot.guilds),
    }


@router.post("/ask", response_model=AskResponse)
async def ask(request: Request, body: AskRequest):
    bot = request.app.state.bot
    corpus = bot.config.get_corpus(body.guild_id)
    if not corpus:
        raise HTTPException(404, "Guild not configured")

    answer = await bot.corpus.query(body.query, corpus)
    return AskResponse(
        query=body.query,
        response=answer,
        timestamp=datetime.now(timezone.utc).isoformat(),
    )


@router.post("/backfill")
async def backfill(request: Request, body: BackfillRequest):
    bot = request.app.state.bot
    corpus = bot.config.get_corpus(body.guild_id)
    if not corpus:
        raise HTTPException(404, "Guild not configured")

    guild = bot.get_guild(body.guild_id)
    if not guild:
        raise HTTPException(404, "Guild not found")

    if body.channel_id:
        channels = [guild.get_channel(body.channel_id)]
        if not channels[0]:
            raise HTTPException(404, "Channel not found")
    else:
        channels = [
            ch for ch in guild.text_channels
            if ch.permissions_for(guild.me).read_message_history
            and not bot.config.is_ignored(body.guild_id, ch.id)
        ]

    async def run():
        total = 0
        for ch in channels:
            try:
                total += await bot.corpus.backfill_channel(ch, corpus)
            except Exception:
                pass
        return total

    # バックグラウンドで実行
    asyncio.create_task(run())

    return {
        "status": "backfill_started",
        "guild_id": body.guild_id,
        "channels": len(channels),
    }
