from datetime import date, datetime, timezone

import pytest

from bot.festival import (
    CalendarWindow,
    calendar_date_from_query,
    calendar_window_from_query,
    festival_from_query,
    festival_number,
    festival_period,
    festival_subject_dates_from_query,
    has_invalid_explicit_calendar_reference,
)


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


def test_query_resolves_yearless_meeting_date_to_latest_occurrence():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert calendar_date_from_query("10/9の会議内容", now) == date(2026, 10, 9)
    assert calendar_date_from_query("12月1日の会議内容", now) == date(2025, 12, 1)
    assert calendar_date_from_query("2025-10-09の会議", now) == date(2025, 10, 9)
    assert festival_from_query("10/9の会議内容", now) == 28
    assert festival_from_query("12月1日の会議内容", now) == 27


def test_invalid_calendar_date_does_not_create_a_filter():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert calendar_date_from_query("2/30の会議", now) is None


@pytest.mark.parametrize(
    "query",
    ["2024年10月32日の会議", "2024年10月99日の会議", "2024-10-32の会議"],
)
def test_invalid_day_is_not_partially_matched(query):
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert festival_from_query(query, now) is None
    assert calendar_date_from_query(query, now) is None
    assert calendar_window_from_query(query, now) is None


def test_invalid_explicit_date_disables_even_an_explicit_festival_filter():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert festival_from_query("27th 2024年10月32日の会議", now) is None


@pytest.mark.parametrize("query", ["2024年13月の活動", "2024/13の活動"])
def test_invalid_month_is_not_partially_matched(query):
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert festival_from_query(query, now) is None
    assert calendar_date_from_query(query, now) is None
    assert calendar_window_from_query(query, now) is None


def test_multiple_explicit_dates_do_not_create_a_single_date_filter():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert calendar_date_from_query(
        "2026年9月30日と2026年10月1日はそれぞれ何thですか",
        now,
    ) is None
    assert festival_from_query(
        "2026年9月30日と2026年10月1日はそれぞれ何thですか",
        now,
    ) is None


@pytest.mark.parametrize(
    "query",
    [
        "2026年9月30日と2026年10月1日はそれぞれ何thですか",
        "2026/9/30と2026/10/1の年度区分を教えて",
        "２０２６年９月３０日と２０２６年１０月１日はどの回次？",
        "2026年9月30日の矢上祭の回次だけ教えてください",
        "2026年9月30日がどの年度かだけ判定してください",
    ],
)
def test_festival_mapping_dates_are_subjects_not_evidence_filters(query):
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    expected_dates = [date(2026, 9, 30)]
    if "10月1日" in query or "10/1" in query or "１０月１日" in query:
        expected_dates.append(date(2026, 10, 1))
    assert festival_subject_dates_from_query(query) == tuple(expected_dates)
    assert calendar_date_from_query(query, now) is None
    assert calendar_window_from_query(query, now) is None


def test_non_mapping_date_question_keeps_evidence_date_semantics():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert festival_subject_dates_from_query("2026年9月30日の会議内容") == ()
    assert festival_subject_dates_from_query("2026年9月30日に会議は何回ありましたか") == ()
    assert calendar_date_from_query("2026年9月30日の会議内容", now) == date(
        2026, 9, 30
    )


def test_compound_mapping_question_keeps_normal_evidence_date_semantics():
    query = "2026年9月30日は何th？また、その日の会議内容も教えて"
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert festival_subject_dates_from_query(query) == ()
    assert calendar_date_from_query(query, now) == date(2026, 9, 30)


@pytest.mark.parametrize(
    "query",
    [
        "2026年2月29日は何thですか",
        "2024年10月32日の会議",
        "2024年13月の活動",
        "2024/13の活動",
    ],
)
def test_invalid_explicit_calendar_reference_is_detected(query):
    assert has_invalid_explicit_calendar_reference(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "2024年150人の参加者がいました",
        "2024年13回目の定例会議です",
        "2024年度は15人で運用しました",
    ],
)
def test_quantities_after_year_are_not_treated_as_invalid_months(query):
    assert has_invalid_explicit_calendar_reference(query) is False


@pytest.mark.parametrize("query", ["1.5倍速", "2-3人で参加", "1-2時間", "gemini-3.1-flash", "2/3人"])
def test_non_date_numbers_do_not_create_a_filter(query):
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert calendar_date_from_query(query, now) is None


def test_yearless_leap_day_resolves_to_the_latest_valid_occurrence():
    now = datetime(2025, 2, 28, tzinfo=timezone.utc)

    assert calendar_date_from_query("2月29日の会議", now) == date(2024, 2, 29)


@pytest.mark.parametrize("query", ["10/9は何が決まった？", "10月9日は何があった？", "10/9が会議日です"])
def test_yearless_date_accepts_common_japanese_particles(query):
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert calendar_date_from_query(query, now) == date(2026, 10, 9)


def test_yearless_date_uses_explicit_or_relative_festival_period():
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert festival_from_query("昨年度の10/9の会議", now) == 27
    assert calendar_date_from_query("昨年度の10/9の会議", now, festival=27) == date(2025, 10, 9)
    assert festival_from_query("27thの3/1の会議", now) == 27
    assert calendar_date_from_query("27thの3/1の会議", now, festival=27) == date(2026, 3, 1)


@pytest.mark.parametrize("query", ["10/9(金)の会議", "10月9日（金）の会議"])
def test_yearless_date_accepts_weekday_suffix(query):
    now = datetime(2026, 10, 21, tzinfo=timezone.utc)

    assert calendar_date_from_query(query, now) == date(2026, 10, 9)


def test_relative_period_resolves_last_year_current_month():
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query(
        "去年の今頃って何してた？",
        now,
        festival=27,
    ) == CalendarWindow(date(2025, 10, 1), date(2025, 10, 31))


def test_relative_last_year_wording_uses_already_resolved_festival():
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query(
        "昨年度の今頃",
        now,
        festival=25,
    ) == CalendarWindow(date(2023, 10, 1), date(2023, 10, 31))


@pytest.mark.parametrize("query", ["27thの今頃", "27thのこの時期"])
def test_relative_period_uses_explicit_festival_operating_year(query):
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query(query, now, festival=27) == CalendarWindow(
        date(2025, 10, 1),
        date(2025, 10, 31),
    )


def test_relative_previous_month_uses_explicit_festival_operating_year():
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query("27thの先月", now, festival=27) == CalendarWindow(
        date(2026, 9, 1),
        date(2026, 9, 30),
    )


def test_relative_period_maps_january_into_explicit_festival():
    now = datetime(2026, 1, 15, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query("27thの今頃", now, festival=27) == CalendarWindow(
        date(2026, 1, 1),
        date(2026, 1, 31),
    )


def test_month_window_uses_festival_operating_year_and_leap_day():
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query("27thの2月頃", now, festival=27) == CalendarWindow(
        date(2026, 2, 1),
        date(2026, 2, 28),
    )
    assert calendar_window_from_query("2024年2月の活動", now) == CalendarWindow(
        date(2024, 2, 1),
        date(2024, 2, 29),
    )


def test_explicit_multi_month_range_covers_every_mentioned_month():
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query(
        "2024年10月から2024年12月までの活動",
        now,
    ) == CalendarWindow(date(2024, 10, 1), date(2024, 12, 31))

    assert calendar_date_from_query(
        "2024年10月9日から2024年12月までの活動",
        now,
    ) is None
    assert calendar_window_from_query(
        "2024年10月9日から2024年12月までの活動",
        now,
    ) == CalendarWindow(date(2024, 10, 1), date(2024, 12, 31))


@pytest.mark.parametrize(
    "query",
    [
        "2024年10月1日から2024年10月5日まで",
        "2024-10-01 - 2024-10-05",
        "2024-10-01..2024-10-05",
    ],
)
def test_explicit_date_range_preserves_exact_endpoints(query):
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query(query, now) == CalendarWindow(
        date(2024, 10, 1),
        date(2024, 10, 5),
    )


def test_partially_qualified_multi_month_comparison_is_not_narrowed():
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query("2024年5月と6月の比較", now, festival=25) is None
    assert calendar_date_from_query("2024年5月9日と6月の比較", now) is None
    assert calendar_window_from_query("2024年5月9日と6月の比較", now, festival=25) is None


def test_multi_month_window_maps_across_festival_operating_year():
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query(
        "27thの11月から2月まで",
        now,
        festival=27,
    ) == CalendarWindow(date(2025, 11, 1), date(2026, 2, 28))

    assert calendar_window_from_query(
        "27thの2月から11月まで",
        now,
        festival=27,
    ) is None


def test_reverse_explicit_calendar_range_is_not_silently_reordered():
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query(
        "2024年12月から2024年10月まで",
        now,
    ) is None

    assert calendar_window_from_query(
        "2024-12-09 - 2024-10-01",
        now,
    ) is None

    assert calendar_window_from_query(
        "2024-12-09..2024-10-01",
        now,
    ) is None


@pytest.mark.parametrize("query", ["27thの第1月曜の定例", "27thの第2月曜日の会議"])
def test_weekday_ordinal_is_not_mistaken_for_a_calendar_month(query):
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query(query, now, festival=27) is None


@pytest.mark.parametrize(
    "query",
    ["例年この時期は何をしてる？", "過去のこの時期のトラブルは？", "この時期は毎年何をする？"],
)
def test_multi_year_season_question_is_not_narrowed_to_one_year(query):
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query(query, now) is None


def test_exact_date_does_not_also_create_a_month_window():
    now = datetime(2026, 10, 22, 3, tzinfo=timezone.utc)

    assert calendar_window_from_query("2025年10月9日の会議", now, festival=27) is None
