"""Authenticated catalog API intended for the future admin UI."""

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from api.security import require_api_token
from knowledge_catalog.models import CatalogEntry, CatalogPatch
from knowledge_catalog.store import (
    CatalogConflictError,
    CatalogNotFoundError,
    catalog_entry_id,
)

router = APIRouter(prefix="/admin/catalog", dependencies=[Depends(require_api_token)])


class CatalogCreate(BaseModel):
    guild_id: int
    kind: Literal["term", "person"]
    label: str = Field(min_length=1, max_length=200)
    reading: str = Field(default="", max_length=200)
    aliases: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)
    roles: list[str] = Field(default_factory=list)


def _require_guild(request: Request, guild_id: int):
    if not request.app.state.bot.config.get_corpus(guild_id):
        raise HTTPException(404, "Guild not configured")


async def _sync_approved_term(bot, previous: CatalogEntry | None, updated: CatalogEntry):
    if updated.kind != "term":
        return
    glossary = bot.config.get_glossary(updated.guild_id)
    if previous and previous.status == "approved" and previous.label != updated.label:
        glossary.pop(previous.label, None)
    if updated.status == "approved":
        glossary[updated.label] = {
            "definition": updated.description or "説明未記入",
            **({"reading": updated.reading} if updated.reading else {}),
            **({"aliases": updated.aliases} if updated.aliases else {}),
        }
    elif previous and previous.status == "approved":
        glossary.pop(previous.label, None)
    await bot.config.set_glossary(updated.guild_id, glossary)


@router.get("")
async def list_catalog(
    request: Request,
    guild_id: int,
    kind: Literal["term", "person"] | None = None,
    status: Literal["candidate", "approved", "rejected"] | None = None,
    query: str = Query(default="", max_length=200),
):
    _require_guild(request, guild_id)
    entries = request.app.state.catalog.list(guild_id, kind=kind, status=status, query=query)
    return {"entries": entries, "total": len(entries)}


@router.post("", response_model=CatalogEntry, status_code=201)
async def create_catalog_entry(request: Request, body: CatalogCreate):
    _require_guild(request, body.guild_id)
    entry = CatalogEntry(
        id=catalog_entry_id(body.guild_id, body.kind, body.label),
        guild_id=body.guild_id,
        kind=body.kind,
        label=body.label,
        reading=body.reading,
        aliases=body.aliases,
        description=body.description,
        roles=body.roles,
    )
    try:
        return request.app.state.catalog.create(entry)
    except CatalogConflictError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/{entry_id}", response_model=CatalogEntry)
async def update_catalog_entry(
    request: Request,
    entry_id: str,
    body: CatalogPatch,
    guild_id: int,
):
    _require_guild(request, guild_id)
    try:
        previous = request.app.state.catalog.get(guild_id, entry_id)
        updated = request.app.state.catalog.patch(guild_id, entry_id, body)
    except CatalogNotFoundError as exc:
        raise HTTPException(404, "Catalog entry not found") from exc
    except CatalogConflictError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _sync_approved_term(request.app.state.bot, previous, updated)
    return updated
