from datetime import date, datetime, timezone

from bot.festival import festival_from_query, festival_number, festival_period


def test_festival_changes_on_october_first():
    assert festival_number(date(2025, 9, 30)) == 26
    assert festival_number(date(2025, 10, 1)) == 27
    assert festival_number(date(2026, 9, 30)) == 27
    assert festival_number(date(2026, 10, 1)) == 28


def test_twenty_seventh_period_matches_operating_year():
    period = festival_period(27)

    assert period.starts_on == date(2025, 10, 1)
    assert period.ends_on == date(2026, 9, 30)


def test_period_formula_covers_before_and_after_twenty_seventh():
    expected = {
        24: (date(2022, 10, 1), date(2023, 9, 30)),
        25: (date(2023, 10, 1), date(2024, 9, 30)),
        26: (date(2024, 10, 1), date(2025, 9, 30)),
        28: (date(2026, 10, 1), date(2027, 9, 30)),
        29: (date(2027, 10, 1), date(2028, 9, 30)),
        30: (date(2028, 10, 1), date(2029, 9, 30)),
    }

    for number, (starts_on, ends_on) in expected.items():
        period = festival_period(number)
        assert (period.starts_on, period.ends_on) == (starts_on, ends_on)


def test_query_resolves_explicit_and_relative_festival():
    now = datetime(2026, 10, 7, tzinfo=timezone.utc)

    assert festival_from_query("27thの整理券", now) == 27
    assert festival_from_query("2026年9月の反省", now) == 27
    assert festival_from_query("今年度の担当", now) == 28
    assert festival_from_query("昨年度の担当", now) == 27
    assert festival_from_query("MovingTasksとは", now) is None
