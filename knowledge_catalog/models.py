"""Models shared by extraction, storage, and the future admin UI."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field

CatalogKind = Literal["term", "person"]
CatalogStatus = Literal["candidate", "approved", "rejected"]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class CatalogEvidence(BaseModel):
    source: str = Field(min_length=1, max_length=1000)
    locator: str = Field(default="", max_length=500)
    excerpt: str = Field(default="", max_length=500)


class CatalogEntry(BaseModel):
    id: str = Field(min_length=8, max_length=80)
    guild_id: int
    kind: CatalogKind
    label: str = Field(min_length=1, max_length=200)
    reading: str = Field(default="", max_length=200)
    aliases: list[str] = Field(default_factory=list)
    description: str = Field(default="", max_length=2000)
    roles: list[str] = Field(default_factory=list)
    reason: str = Field(default="", max_length=1000)
    confidence: float = Field(default=0.5, ge=0, le=1)
    status: CatalogStatus = "candidate"
    discord_user_id: str = Field(default="", max_length=30)
    evidence: list[CatalogEvidence] = Field(default_factory=list)
    source_count: int = Field(default=0, ge=0)
    manually_edited: bool = False
    revision: int = Field(default=1, ge=1)
    created_at: str = Field(default_factory=utc_now)
    updated_at: str = Field(default_factory=utc_now)


class CatalogPatch(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=200)
    reading: str | None = Field(default=None, max_length=200)
    aliases: list[str] | None = None
    description: str | None = Field(default=None, max_length=2000)
    roles: list[str] | None = None
    reason: str | None = Field(default=None, max_length=1000)
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: CatalogStatus | None = None
    expected_revision: int = Field(ge=1)


class ExtractedTerm(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    reading: str = Field(default="", max_length=200)
    aliases: list[str] = Field(default_factory=list)
    proposed_definition: str = Field(default="", max_length=1000)
    reason: str = Field(default="", max_length=500)
    confidence: float = Field(default=0.5, ge=0, le=1)
    source: str = Field(min_length=1, max_length=1000)
    locator: str = Field(default="", max_length=500)
    evidence_excerpt: str = Field(default="", max_length=500)


class ExtractedPerson(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    aliases: list[str] = Field(default_factory=list)
    explicit_roles: list[str] = Field(default_factory=list)
    proposed_description: str = Field(default="", max_length=1000)
    confidence: float = Field(default=0.5, ge=0, le=1)
    source: str = Field(min_length=1, max_length=1000)
    locator: str = Field(default="", max_length=500)
    evidence_excerpt: str = Field(default="", max_length=500)


class ExtractionBatch(BaseModel):
    terms: list[ExtractedTerm] = Field(default_factory=list)
    people: list[ExtractedPerson] = Field(default_factory=list)
