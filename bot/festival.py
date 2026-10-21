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

    target_date = calendar_date_from_query(normalized, now)
    if target_date:
        return festival_number(target_date)

    current = current_festival(now)
    if re.search(r"今年度|今年|今期|現在|今の|今回", normalized):
        return current
    if re.search(r"昨年度|去年|昨年|前回", normalized):
        return current - 1
    return None


def calendar_date_from_query(query: str, now: datetime | None = None) -> date | None:
    """Resolve an explicit or yearless calendar date to the latest occurrence.

    Committee questions commonly abbreviate a recent meeting as ``10/9``.  A
    yearless date is interpreted as the latest occurrence on or before today,
    so a question on 2026-10-21 resolves to 2026-10-09 instead of searching all
    festival years.
    """
    normalized = query.translate(
        str.maketrans("０１２３４５６７８９／．－", "0123456789/.-")
    )
    explicit = re.search(
        r"(?<!\d)(20\d{2})\s*(?:年|[/.-])\s*(1[0-2]|0?[1-9])\s*"
        r"(?:月|[/.-])\s*(3[01]|[12]\d|0?[1-9])\s*日?",
        normalized,
    )
    if explicit:
        try:
            return date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)))
        except ValueError:
            return None

    month_day = re.search(
        r"(?<![\d/])(1[0-2]|0?[1-9])\s*"
        r"(?:月\s*(3[01]|[12]\d|0?[1-9])\s*日|/\s*(3[01]|[12]\d|0?[1-9]))"
        r"(?=\s*(?:$|[、。?？]|の|に|で|開催|会議|ミーティング|反省会))",
        normalized,
    )
    if not month_day:
        return None

    current = (now or datetime.now(JST)).astimezone(JST).date()
    month = int(month_day.group(1))
    day = int(month_day.group(2) or month_day.group(3))
    for year in range(current.year, current.year - 8, -1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= current:
            return candidate
    return None


def festival_metadata_filter(number: int | None) -> str | None:
    return f"festival = {number}" if number is not None else None
