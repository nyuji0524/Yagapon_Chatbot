"""Shared meeting date and Google document naming helpers."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")


def meeting_datetime(value: datetime | str | None = None) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            session_time = re.search(r"(\d{8}T\d{6}Z)", value)
            value = (
                datetime.strptime(session_time.group(1), "%Y%m%dT%H%M%SZ").replace(
                    tzinfo=timezone.utc
                )
                if session_time
                else None
            )
    if value is None:
        value = datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(JST)


def meeting_name(value: str) -> str:
    """Keep a human-readable name while avoiding separators illegal in file names."""
    cleaned = re.sub(r"[\\/:*?\"<>|\r\n]+", " ", value or "会議")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or "会議"


def document_name(name: str, value: datetime | str | None = None) -> str:
    return f"{meeting_datetime(value):%y%m%d}{meeting_name(name)}"


def display_meeting_date(value: datetime | str | None = None) -> str:
    return meeting_datetime(value).strftime("%y/%m/%d")
