from bot.knowledge_authority import (
    QuestionKind,
    authority_instruction,
    authority_score,
    can_answer_directly,
    classify_question,
)


def test_question_classification_covers_source_authority_modes():
    assert classify_question("この操作をできる権限は誰にありますか") == QuestionKind.AUTHORIZATION
    assert classify_question("文字起こしはどこに保存されますか") == QuestionKind.DATA_CONTRACT
    assert classify_question("今公開中の整理券はありますか") == QuestionKind.RUNTIME
    assert classify_question("整理券の運用方針を教えて") == QuestionKind.POLICY
    assert classify_question("やがサポの使い方は？") == QuestionKind.USAGE
    assert classify_question("この決定の経緯は？") == QuestionKind.HISTORY
    assert classify_question("10/10の役員反省会では何が出た？") == QuestionKind.HISTORY


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
