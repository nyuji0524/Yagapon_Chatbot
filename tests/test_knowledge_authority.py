import pytest

from bot.knowledge_authority import (
    QuestionKind,
    allows_unreviewed_evidence,
    authority_instruction,
    authority_score,
    can_answer_directly,
    classify_question,
    classify_question_facets,
    direct_authorities,
    is_history_narrative_source,
)


def test_question_classification_covers_source_authority_modes():
    assert classify_question("この操作をできる権限は誰にありますか") == QuestionKind.AUTHORIZATION
    assert classify_question("文字起こしはどこに保存されますか") == QuestionKind.DATA_CONTRACT
    assert classify_question("今公開中の整理券はありますか") == QuestionKind.RUNTIME
    assert classify_question("整理券の運用方針を教えて") == QuestionKind.POLICY
    assert classify_question("やがサポの使い方は？") == QuestionKind.USAGE
    assert classify_question("この決定の経緯は？") == QuestionKind.HISTORY
    assert classify_question("10/10の役員反省会では何が出た？") == QuestionKind.HISTORY
    assert classify_question("27thのrankingの構造はどうなっていた？") == QuestionKind.DATA_CONTRACT
    assert classify_question("27thのmapのfirestore構造ってどうなってた？") == QuestionKind.DATA_CONTRACT
    assert classify_question("27thの大賞企画の結果と票数を教えて") == QuestionKind.RUNTIME
    assert classify_question("去年の今頃って何してた？") == QuestionKind.HISTORY
    assert classify_question("新歓Webで他局から要求があった内容をまとめて") == QuestionKind.HISTORY
    assert classify_question("正常に処理が終わった録音PCMは残りますか") == QuestionKind.DATA_CONTRACT
    assert classify_question("27thで整理券機能全体を管理できたロールは何ですか") == QuestionKind.AUTHORIZATION


def test_authority_priority_changes_with_question_kind():
    assert authority_score(QuestionKind.POLICY, "canonical", "approved") > authority_score(
        QuestionKind.POLICY, "deployed_implementation", "deployed"
    )
    assert authority_score(QuestionKind.RUNTIME, "live_data", "current") > authority_score(
        QuestionKind.RUNTIME, "curated", "approved"
    )
    assert authority_score(
        QuestionKind.AUTHORIZATION, "deployed_implementation", "deployed"
    ) > authority_score(QuestionKind.AUTHORIZATION, "conversation", "raw")


def test_conflict_instruction_requires_both_policy_and_implementation():
    instruction = authority_instruction(QuestionKind.AUTHORIZATION)

    assert "矛盾" in instruction
    assert "Rules・Functions・サーバー側" in instruction
    assert "クライアントの表示制御" in instruction


def test_history_does_not_short_circuit_on_current_source_code():
    assert not can_answer_directly(QuestionKind.HISTORY, "repository_context", "current")
    assert can_answer_directly(QuestionKind.HISTORY, "reviewed_record", "approved")
    assert can_answer_directly(QuestionKind.HISTORY, "repository_history", "historical")
    assert not can_answer_directly(QuestionKind.RUNTIME, "repository_history", "historical")


@pytest.mark.parametrize(
    "kind",
    (
        QuestionKind.RUNTIME,
        QuestionKind.AUTHORIZATION,
        QuestionKind.USAGE,
        QuestionKind.DATA_CONTRACT,
        QuestionKind.GENERAL,
    ),
)
@pytest.mark.parametrize(
    ("authority", "current_status"),
    (
        ("repository_context", "current"),
        ("deployed_implementation", "deployed"),
    ),
)
@pytest.mark.parametrize("status", ("historical", "unknown", ""))
def test_current_questions_reject_stale_or_unknown_implementation(
    kind,
    authority,
    current_status,
    status,
):
    assert not can_answer_directly(kind, authority, status)
    assert can_answer_directly(kind, authority, current_status)


def test_compound_question_keeps_authority_facets_separate():
    assert classify_question_facets(
        "新歓Webの現在の実装と、これまでの変更経緯を詳しく教えて"
    ) == (QuestionKind.RUNTIME, QuestionKind.HISTORY)
    assert classify_question_facets("誰が操作できる権限を持つか教えて") == (
        QuestionKind.AUTHORIZATION,
    )
    assert classify_question_facets("権限と過去の障害を教えて") == (
        QuestionKind.AUTHORIZATION,
        QuestionKind.HISTORY,
    )


@pytest.mark.parametrize(
    "question",
    (
        "Google Driveでは議事録はどこに保存されますか",
        "生文字起こしは議事録と同じファイルに入りますか。保存先も教えてください",
        "議事録の保存先を教えて",
        "議事録はどこに保存されるか教えて",
        "Google Driveでは会議記録はどこに保存されますか",
    ),
)
def test_minutes_storage_questions_are_data_contract_only(question):
    assert classify_question_facets(question) == (QuestionKind.DATA_CONTRACT,)


def test_minutes_creation_question_is_usage_only():
    for question in ("議事録の作成手順を教えて", "会議記録の作成手順を教えて"):
        assert classify_question_facets(question) == (QuestionKind.USAGE,)


def test_minutes_content_question_is_history():
    for question in (
        "10/9の議事録の内容を教えて",
        "10/9の会議内容を教えて",
        "10/9の会議内容は？",
        "10/9の会議記録を見せて",
        "10/9の議事録の要点を教えて",
        "会議で話したことをまとめて",
    ):
        assert classify_question_facets(question) == (QuestionKind.HISTORY,)


@pytest.mark.parametrize(
    "question",
    (
        "IT局へのフィードバックは？",
        "どんな指摘がありましたか？",
        "今年の反省を教えて",
        "発生した問題を知りたい",
        "障害の内容を教えて",
        "何が出た？",
        "当日の意見をまとめて",
    ),
)
def test_history_feedback_terms_do_not_require_meeting_word(question):
    assert classify_question_facets(question) == (QuestionKind.HISTORY,)


def test_current_problem_keeps_runtime_and_history_facets():
    assert classify_question_facets("現在の問題を教えて") == (
        QuestionKind.RUNTIME,
        QuestionKind.HISTORY,
    )


def test_raw_evidence_is_not_sufficient_for_people_or_authorization():
    assert allows_unreviewed_evidence(QuestionKind.HISTORY)
    assert allows_unreviewed_evidence(QuestionKind.GENERAL)
    assert not allows_unreviewed_evidence(QuestionKind.PEOPLE)
    assert not allows_unreviewed_evidence(QuestionKind.AUTHORIZATION)


def test_direct_authorities_are_stable_and_exclude_conversation():
    authorities = direct_authorities(QuestionKind.AUTHORIZATION)

    assert authorities == tuple(sorted(authorities))
    assert "deployed_implementation" in authorities
    assert "conversation" not in authorities


@pytest.mark.parametrize(
    "source_type",
    ("discord_conversation", "meeting_transcript", "meeting_minutes", "meeting_source"),
)
def test_history_narrative_sources_cover_conversations_and_meeting_artifacts(source_type):
    assert is_history_narrative_source(source_type)


@pytest.mark.parametrize(
    "source_type",
    ("source_code_history", "source_code", "knowledge_repository", ""),
)
def test_repository_sources_are_not_mistaken_for_history_narratives(source_type):
    assert not is_history_narrative_source(source_type)
