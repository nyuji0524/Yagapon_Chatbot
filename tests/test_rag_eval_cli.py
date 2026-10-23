import io
import json
import socket
import urllib.error

import pytest
import yaml

from rag_eval.__main__ import (
    DEFAULT_REQUEST_TIMEOUT_SECONDS,
    RequestFailure,
    main,
    post_json,
)


def _cases_file(tmp_path, cases):
    path = tmp_path / "cases.yaml"
    path.write_text(yaml.safe_dump(cases, allow_unicode=True), encoding="utf-8")
    return path


def _arguments(cases, output, *extra):
    return [
        "--base-url",
        "https://example.invalid",
        "--guild-id",
        "123",
        "--cases",
        str(cases),
        "--output",
        str(output),
        "--min-pass-rate",
        "0",
        *extra,
    ]


def test_post_json_uses_safe_default_timeout(monkeypatch):
    observed = []

    def open_request(_request, timeout):
        observed.append(timeout)
        return io.BytesIO(b'{"response":"ok"}')

    monkeypatch.setattr("urllib.request.urlopen", open_request)

    assert post_json("https://example.invalid/ask", "token", {"question": "test"}) == {"response": "ok"}
    assert observed == [DEFAULT_REQUEST_TIMEOUT_SECONDS]
    assert DEFAULT_REQUEST_TIMEOUT_SECONDS == 180.0


def test_post_json_classifies_http_error_without_echoing_body(monkeypatch):
    error = urllib.error.HTTPError(
        url="https://example.invalid/rag/evaluate",
        code=422,
        msg="Unprocessable Entity",
        hdrs=None,
        fp=io.BytesIO(b'{"detail":"private prompt and bearer token"}'),
    )
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(RequestFailure) as raised:
        post_json("https://example.invalid/rag/evaluate", "token", {"question": "test"})

    assert raised.value.kind == "http_error"
    assert "private prompt" not in str(raised.value)
    assert "bearer token" not in str(raised.value)


@pytest.mark.parametrize(
    ("error", "kind"),
    [
        (TimeoutError("private timeout detail"), "timeout"),
        (urllib.error.URLError(socket.timeout("private timeout detail")), "timeout"),
        (urllib.error.URLError("private DNS detail"), "connection_error"),
    ],
)
def test_post_json_classifies_transport_failure_without_detail(monkeypatch, error, kind):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda *args, **kwargs: (_ for _ in ()).throw(error),
    )

    with pytest.raises(RequestFailure) as raised:
        post_json("https://example.invalid/ask", "token", {"question": "test"})

    assert raised.value.kind == kind
    assert "private" not in str(raised.value)


def test_ask_timeout_writes_secret_free_partial_report_and_stops(tmp_path, monkeypatch):
    cases = _cases_file(
        tmp_path,
        [
            {
                "id": "case-one",
                "question": "PRIVATE QUESTION ONE",
                "must_cite": False,
                "semantic_judge": False,
            },
            {
                "id": "case-two",
                "question": "PRIVATE QUESTION TWO",
                "must_cite": False,
                "semantic_judge": False,
            },
        ],
    )
    output = tmp_path / "report.json"
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise RequestFailure("timeout")

    monkeypatch.setenv("YAGAPON_API_TOKEN", "SUPER-SECRET-TOKEN")
    monkeypatch.setattr("rag_eval.__main__.post_json", fail)

    with pytest.raises(SystemExit, match=r"ask request failure \(timeout\)"):
        main(_arguments(cases, output, "--request-timeout-seconds", "7.5"))

    assert len(calls) == 1
    assert calls[0][1]["timeout"] == 7.5
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["completed"] == 0
    assert report["total"] == 2
    assert report["results"] == []
    assert report["failure"] == {
        "case_id": "case-one",
        "stage": "ask",
        "kind": "timeout",
    }
    rendered = output.read_text(encoding="utf-8")
    assert "PRIVATE QUESTION" not in rendered
    assert "SUPER-SECRET-TOKEN" not in rendered


def test_judge_connection_failure_does_not_publish_answer_or_continue(
    tmp_path,
    monkeypatch,
):
    cases = _cases_file(
        tmp_path,
        [
            {"id": "judge-case", "question": "PRIVATE QUESTION", "must_cite": False},
            {"id": "later-case", "question": "MUST NOT RUN", "must_cite": False},
        ],
    )
    output = tmp_path / "report.json"
    calls = []

    def request(url, _token, _payload, *, timeout):
        calls.append((url, timeout))
        if url.endswith("/ask"):
            return {
                "response": "PRIVATE ANSWER",
                "sources": ["PRIVATE SOURCE"],
                "query_id": "PRIVATE QUERY ID",
                "no_answer": False,
            }
        raise RequestFailure("connection_error")

    monkeypatch.setenv("YAGAPON_API_TOKEN", "SUPER-SECRET-TOKEN")
    monkeypatch.setattr("rag_eval.__main__.post_json", request)

    with pytest.raises(SystemExit, match=r"judge request failure \(connection_error\)"):
        main(_arguments(cases, output))

    assert [url.rsplit("/", 1)[-1] for url, _timeout in calls] == ["ask", "evaluate"]
    assert all(timeout == 180.0 for _url, timeout in calls)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["completed"] == 0
    assert report["failure"] == {
        "case_id": "judge-case",
        "stage": "judge",
        "kind": "connection_error",
    }
    rendered = output.read_text(encoding="utf-8")
    for secret in (
        "PRIVATE QUESTION",
        "PRIVATE ANSWER",
        "PRIVATE SOURCE",
        "PRIVATE QUERY ID",
        "SUPER-SECRET-TOKEN",
        "MUST NOT RUN",
    ):
        assert secret not in rendered


def test_completed_results_survive_later_timeout_without_more_requests(
    tmp_path,
    monkeypatch,
):
    cases = _cases_file(
        tmp_path,
        [
            {
                "id": "completed-case",
                "question": "first",
                "must_cite": False,
                "semantic_judge": False,
            },
            {
                "id": "failed-case",
                "question": "second",
                "must_cite": False,
                "semantic_judge": False,
            },
            {
                "id": "unreached-case",
                "question": "third",
                "must_cite": False,
                "semantic_judge": False,
            },
        ],
    )
    output = tmp_path / "report.json"
    calls = 0

    def request(_url, _token, _payload, *, timeout):
        nonlocal calls
        assert timeout == 180.0
        calls += 1
        if calls == 1:
            return {"response": "answer", "sources": [], "no_answer": False}
        raise RequestFailure("timeout")

    monkeypatch.setenv("YAGAPON_API_TOKEN", "token")
    monkeypatch.setattr("rag_eval.__main__.post_json", request)

    with pytest.raises(SystemExit, match=r"ask request failure \(timeout\)"):
        main(_arguments(cases, output))

    assert calls == 2
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["status"] == "failed"
    assert report["passed"] == 1
    assert report["completed"] == 1
    assert report["total"] == 3
    assert report["pass_rate"] == pytest.approx(0.333)
    assert [result["id"] for result in report["results"]] == ["completed-case"]
    assert report["failure"] == {
        "case_id": "failed-case",
        "stage": "ask",
        "kind": "timeout",
    }
