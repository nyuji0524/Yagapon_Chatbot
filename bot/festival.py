"""Festival-year rules shared by ingestion, retrieval, and evaluation."""

import re
from calendar import monthrange
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


@dataclass(frozen=True)
class CalendarWindow:
    """Inclusive calendar window inferred from a coarse time expression."""

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

    dated_matches = list(
        re.finditer(r"(20\d{2})[年/.-](1[0-2]|0?[1-9])(?:月)?", normalized)
    )
    if dated_matches:
        dated_festivals = {
            festival_number(date(int(match.group(1)), int(match.group(2)), 1))
            for match in dated_matches
        }
        # A comparison across the September/October boundary intentionally
        # spans two operating years and must not be narrowed to the first date.
        if len(dated_festivals) > 1:
            return None
        return dated_festivals.pop()

    current = current_festival(now)
    if re.search(r"今年度|今年|今期|現在|今の|今回", normalized):
        return current
    if re.search(r"昨年度|去年|昨年|前回", normalized):
        return current - 1
    target_date = calendar_date_from_query(normalized, now)
    if target_date:
        return festival_number(target_date)
    return None


def calendar_date_from_query(
    query: str,
    now: datetime | None = None,
    *,
    festival: int | None = None,
) -> date | None:
    """Resolve an explicit or yearless calendar date to the latest occurrence.

    Committee questions commonly abbreviate a recent meeting as ``10/9``.  A
    yearless date is interpreted as the latest occurrence on or before today,
    so a question on 2026-10-21 resolves to 2026-10-09 instead of searching all
    festival years.
    """
    normalized = query.translate(
        str.maketrans("０１２３４５６７８９／．－", "0123456789/.-")
    )
    explicit_matches = list(re.finditer(
        r"(?<!\d)(20\d{2})\s*(?:年|[/.-])\s*(1[0-2]|0?[1-9])\s*"
        r"(?:月|[/.-])\s*(3[01]|[12]\d|0?[1-9])\s*日?",
        normalized,
    ))
    # A single-date filter must not silently discard evidence for comparison
    # questions containing two distinct dates.
    if len(explicit_matches) > 1:
        return None
    if explicit_matches:
        explicit = explicit_matches[0]
        try:
            return date(int(explicit.group(1)), int(explicit.group(2)), int(explicit.group(3)))
        except ValueError:
            return None

    month_day = re.search(
        r"(?<![\d/])(1[0-2]|0?[1-9])\s*"
        r"(?:月\s*(3[01]|[12]\d|0?[1-9])\s*日|/\s*(3[01]|[12]\d|0?[1-9]))"
        r"(?=\s*(?:[（(][月火水木金土日](?:曜)?[)）]\s*)?"
        r"(?:$|[、。?？]|[のにはがをもで]|開催|会議|ミーティング|反省会))",
        normalized,
    )
    if not month_day:
        return None

    current = (now or datetime.now(JST)).astimezone(JST).date()
    month = int(month_day.group(1))
    day = int(month_day.group(2) or month_day.group(3))
    if festival is not None:
        period = festival_period(festival)
        year = period.starts_on.year if month >= 10 else period.ends_on.year
        try:
            return date(year, month, day)
        except ValueError:
            return None
    for year in range(current.year, current.year - 8, -1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate <= current:
            return candidate
    return None


def calendar_window_from_query(
    query: str,
    now: datetime | None = None,
    *,
    festival: int | None = None,
) -> CalendarWindow | None:
    """Resolve month-level expressions such as ``去年の今頃``.

    Exact dates remain the responsibility of :func:`calendar_date_from_query`.
    A month is intentionally used instead of one guessed day: committee work
    is usually discussed over several days and a one-day filter would make a
    natural question such as "去年の今頃" miss almost every relevant record.
    """
    if calendar_date_from_query(query, now, festival=festival) is not None:
        return None
    normalized = query.translate(
        str.maketrans("０１２３４５６７８９／．－", "0123456789/.-")
    )
    current = (now or datetime.now(JST)).astimezone(JST).date()

    explicit_matches = list(re.finditer(
        r"(?<!\d)(20\d{2})\s*(?:年|[/.-])\s*(1[0-2]|0?[1-9])\s*月?"
        r"(?!\s*(?:[/.-]|月)\s*(?:3[01]|[12]\d|0?[1-9]))",
        normalized,
    ))
    if len(explicit_matches) > 1:
        windows = [
            _month_window(int(match.group(1)), int(match.group(2)))
            for match in explicit_matches
        ]
        return CalendarWindow(
            starts_on=min(window.starts_on for window in windows),
            ends_on=max(window.ends_on for window in windows),
        )
    if explicit_matches:
        explicit = explicit_matches[0]
        # A partially-qualified comparison such as ``2024年5月と6月`` is
        # ambiguous without parsing the connective (and ``12月から1月`` may
        # cross a year).  A broad search is safer than silently restricting
        # the second month out of the grounding audit.
        named_months = list(
            re.finditer(r"(?<!\d)(1[0-2]|0?[1-9])\s*月(?!\s*\d)", normalized)
        )
        if len(named_months) > 1:
            return None
        year, month = int(explicit.group(1)), int(explicit.group(2))
        return _month_window(year, month)

    relative_month = None
    if re.search(r"先月", normalized):
        if current.month == 1:
            relative_month = (current.year - 1, 12)
        else:
            relative_month = (current.year, current.month - 1)
        if festival is not None:
            period = festival_period(festival)
            month = relative_month[1]
            relative_month = (
                period.starts_on.year if month >= 10 else period.ends_on.year,
                month,
            )
    elif re.search(r"(?:去年|昨年|昨年度).{0,4}(?:今頃|この時期|今月)", normalized):
        if festival is None:
            relative_month = (current.year - 1, current.month)
        else:
            # festival_from_query() has already resolved relative wording such
            # as 昨年度 to the target operating year.  Map the month into that
            # period instead of subtracting the Gregorian year a second time.
            period = festival_period(festival)
            relative_month = (
                period.starts_on.year if current.month >= 10 else period.ends_on.year,
                current.month,
            )
    elif re.search(r"今頃|この時期|今月", normalized):
        if festival is None:
            relative_month = (current.year, current.month)
        else:
            period = festival_period(festival)
            relative_month = (
                period.starts_on.year if current.month >= 10 else period.ends_on.year,
                current.month,
            )
    if relative_month:
        return _month_window(*relative_month)

    mentioned_months = list(
        re.finditer(r"(?<!\d)(1[0-2]|0?[1-9])\s*月(?!\s*\d)", normalized)
    )
    if mentioned_months and festival is not None:
        period = festival_period(festival)
        windows = []
        for match in mentioned_months:
            month = int(match.group(1))
            year = period.starts_on.year if month >= 10 else period.ends_on.year
            windows.append(_month_window(year, month))
        return CalendarWindow(
            starts_on=min(window.starts_on for window in windows),
            ends_on=max(window.ends_on for window in windows),
        )
    return None


def _month_window(year: int, month: int) -> CalendarWindow:
    return CalendarWindow(
        starts_on=date(year, month, 1),
        ends_on=date(year, month, monthrange(year, month)[1]),
    )


def festival_metadata_filter(number: int | None) -> str | None:
    return f"festival = {number}" if number is not None else None
