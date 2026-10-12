"""Authenticated inventory and replay controls for archived recordings."""

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from api.security import require_api_token
from bot.recording_replay import find_recording_artifact, list_recording_artifacts

router = APIRouter(dependencies=[Depends(require_api_token)])


class ReplayRequest(BaseModel):
    session_id: str = Field(min_length=1, max_length=120)
    target_guild_id: int | None = None
    target_guild_name: str | None = Field(default=None, max_length=100)
    target_channel_id: int | None = None
    retranscribe: bool = False


class ReplayLongestRequest(BaseModel):
    target_guild_name: str | None = Field(default=None, max_length=100)
    target_guild_id: int | None = None
    target_channel_id: int | None = None
    retranscribe: bool = False


def _target_guild(bot, payload: ReplayRequest):
    if payload.target_guild_id is not None:
        guild = bot.get_guild(payload.target_guild_id)
        if guild:
            return guild
    if payload.target_guild_name:
        needle = payload.target_guild_name.strip().casefold()
        exact = [
            guild for guild in bot.guilds
            if guild.name.strip().casefold() == needle
        ]
        if len(exact) == 1:
            return exact[0]
        partial = [guild for guild in bot.guilds if needle in guild.name.casefold()]
        if len(partial) == 1:
            return partial[0]
    raise HTTPException(status_code=404, detail="target guild not found or ambiguous")


def _target_channel(guild, channel_id: int | None):
    if channel_id is not None:
        channel = guild.get_channel(channel_id)
        if channel and channel.permissions_for(guild.me).send_messages:
            return channel
        raise HTTPException(status_code=404, detail="writable target channel not found")
    candidates = [guild.system_channel] if guild.system_channel else []
    candidates.extend(guild.text_channels)
    for channel in candidates:
        if channel and channel.permissions_for(guild.me).send_messages:
            return channel
    raise HTTPException(status_code=409, detail="target guild has no writable text channel")


@router.get("/admin/recordings")
async def recordings_inventory(
    limit: int = Query(default=50, ge=1, le=200),
):
    artifacts = await asyncio.to_thread(list_recording_artifacts)
    return {
        "entries": [item.public_dict() for item in artifacts[:limit]],
        "total": len(artifacts),
    }


@router.get("/admin/discord/guilds")
async def discord_guild_inventory(request: Request):
    bot = request.app.state.bot
    entries = []
    for guild in bot.guilds:
        channels = [
            {"id": channel.id, "name": channel.name}
            for channel in guild.text_channels
            if channel.permissions_for(guild.me).send_messages
        ]
        entries.append({"id": guild.id, "name": guild.name, "channels": channels})
    return {"entries": entries, "total": len(entries)}


@router.post("/admin/recordings/replay", status_code=status.HTTP_202_ACCEPTED)
async def start_recording_replay(payload: ReplayRequest, request: Request):
    bot = request.app.state.bot
    guild = _target_guild(bot, payload)
    channel = _target_channel(guild, payload.target_channel_id)
    try:
        artifact = await asyncio.to_thread(
            find_recording_artifact,
            payload.session_id,
        )
        job = bot.recording_replays.start(
            artifact=artifact,
            target_channel=channel,
            retranscribe=payload.retranscribe,
        )
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="recording not found") from exc
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.public_dict()


@router.post("/admin/recordings/replay-longest", status_code=status.HTTP_202_ACCEPTED)
async def start_longest_recording_replay(
    payload: ReplayLongestRequest,
    request: Request,
):
    bot = request.app.state.bot
    artifacts = await asyncio.to_thread(list_recording_artifacts)
    candidates = [
        item for item in artifacts
        if not item.session_id.startswith("replay-")
        and (item.raw_bytes > 0 if payload.retranscribe else item.transcript_bytes > 0)
    ]
    if not candidates:
        kind = "raw recording" if payload.retranscribe else "transcript"
        raise HTTPException(status_code=404, detail=f"no replayable {kind} found")
    if payload.retranscribe:
        selected = max(
            candidates,
            key=lambda item: (item.estimated_seconds, item.raw_bytes),
        )
    else:
        selected = max(candidates, key=lambda item: item.transcript_bytes)
    guild = _target_guild(bot, ReplayRequest(
        session_id=selected.session_id,
        target_guild_id=payload.target_guild_id,
        target_guild_name=payload.target_guild_name,
    ))
    channel = _target_channel(guild, payload.target_channel_id)
    try:
        job = bot.recording_replays.start(
            artifact=selected,
            target_channel=channel,
            retranscribe=payload.retranscribe,
        )
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.public_dict()


@router.get("/admin/recordings/replay/{job_id}")
async def recording_replay_status(job_id: str, request: Request):
    job = request.app.state.bot.recording_replays.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="replay job not found")
    return job.public_dict()
