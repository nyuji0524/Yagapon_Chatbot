from bot.meeting_time import display_meeting_date, document_name, meeting_name


def test_meeting_document_name_uses_japan_date_and_sanitized_meeting_name():
    value = "2026-10-11T16:30:00+00:00"

    assert document_name("全体/会議", value) == "261012全体 会議"
    assert display_meeting_date(value) == "26/10/12"


def test_meeting_name_falls_back_for_blank_values():
    assert meeting_name("\n") == "会議"
