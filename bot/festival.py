"""Festival-year rules shared by ingestion, retrieval, and evaluation."""

import re
from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

JST = ZoneInfo("Asia/Tokyo")
FESTIVAL_YEAR_OFFSET = 1998


def festival_number(value: date | datetime) -> int:
    """Return the festival number for an October-to-September operating year."""
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(JST)
        value = value.date()
    start_year = value.year if value.month >= 10 else value.year - 1
    return start_year - FESTIVAL_YEAR_OFFSET


def current_festival(now: datetime | None = None) -> int:
    return festival_number(now or datetime.now(JST))


@dataclass(frozen=True)
class FestivalPeriod:
    number: int
    starts_on: date
    ends_on: date


def festival_period(number: int) -> FestivalPeriod:
    if number < 1:
        raise ValueError("festival number must be positive")
    start_year = number + FESTIVAL_YEAR_OFFSET
    return FestivalPeriod(
        number=number,
        starts_on=date(start_year, 10, 1),
        ends_on=date(start_year + 1, 9, 30),
    )


def festival_from_query(query: str, now: datetime | None = None) -> int | None:
    """Resolve explicit/relative festival references; leave timeless queries unfiltered."""
    normalized = query.strip()
    match = re.search(r"(?:第\s*)?(\d{1,3})(?:th|回)", normalized, re.IGNORECASE)
    if match:
        return int(match.group(1))

    dated = re.search(r"(20\d{2})[年/.-](1[0-2]|0?[1-9])(?:月)?", normalized)
    if dated:
        return festival_number(date(int(dated.group(1)), int(dated.group(2)), 1))

    current = current_festival(now)
    if re.search(r"今年度|今年|今期|現在|今の|今回", normalized):
        return current
    if re.search(r"昨年度|去年|昨年|前回", normalized):
        return current - 1
    return None


def festival_metadata_filter(number: int | None) -> str | None:
    return f"festival = {number}" if number is not None else None
