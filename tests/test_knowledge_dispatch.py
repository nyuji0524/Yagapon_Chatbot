import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from bot import knowledge_dispatch
from bot.commands.knowledge import _knowledge_refresh_embed, _knowledge_refresh_status
from knowledge_curation import __main__ as curation
from source_index.github_auth import GitHubAppTokenProvider


def _clear_dispatch_environment(monkeypatch):
    for name in (
        "YAGAPON_KNOWLEDGE_GITHUB_APP_ID",
        "YAGAPON_KNOWLEDGE_GITHUB_APP_ID_FILE",
        "YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID",
        "YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID_FILE",
        "YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY",
        "YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY_FILE",
        "YAGAPON_KNOWLEDGE_INTAKE_DISPATCH_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)


def test_dispatch_waits_safely_when_dedicated_app_is_not_configured(monkeypatch):
    _clear_dispatch_environment(monkeypatch)
    # Source-index credentials are intentionally not an implicit fallback.
    monkeypatch.setenv("YAGAPON_SOURCE_GITHUB_APP_ID", "source-reader")
    monkeypatch.setenv("YAGAPON_SOURCE_GITHUB_APP_INSTALLATION_ID", "123")
    monkeypatch.setenv(
        "YAGAPON_SOURCE_GITHUB_APP_PRIVATE_KEY",
        "-----BEGIN PRIVATE KEY-----\nsource-only\n-----END PRIVATE KEY-----",
    )

    result = knowledge_dispatch.request_knowledge_intake_dispatch()

    assert result.status == "waiting"
    assert result.reason == "github-app-not-configured"


def test_dispatch_fails_closed_for_partial_app_configuration(monkeypatch):
    _clear_dispatch_environment(monkeypatch)
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GITHUB_APP_ID", "123")

    result = knowledge_dispatch.request_knowledge_intake_dispatch()

    assert result.status == "failed"
    assert result.reason == "github-app-configuration-incomplete"
    assert result.error_type == "ConfigurationError"


def test_dispatch_requests_actions_write_for_only_the_knowledge_repo(monkeypatch):
    _clear_dispatch_environment(monkeypatch)
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GITHUB_APP_ID", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv(
        "YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY",
        "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----",
    )
    monkeypatch.setenv(
        "YAGAPON_KNOWLEDGE_REPOSITORY",
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
    )
    provider_args = {}
    dispatch_args = {}

    class Provider:
        def __init__(self, **kwargs):
            provider_args.update(kwargs)

        def __call__(self):
            return "short-lived-installation-token"

    class Dispatcher:
        def __init__(self, token, repository):
            dispatch_args.update(token=token, repository=repository)

        def dispatch(self, **kwargs):
            dispatch_args.update(kwargs)

    monkeypatch.setattr(knowledge_dispatch, "GitHubAppTokenProvider", Provider)
    monkeypatch.setattr(knowledge_dispatch, "GitHubWorkflowDispatcher", Dispatcher)

    result = knowledge_dispatch.request_knowledge_intake_dispatch()

    assert result.status == "pr-requested"
    assert provider_args["repositories"] == ("YagamiFes-IT-Knowledge",)
    assert provider_args["permissions"] == {"actions": "write"}
    assert dispatch_args == {
        "token": "short-lived-installation-token",
        "repository": "YagamiFes-IT/YagamiFes-IT-Knowledge",
        "workflow": "yagapon-intake.yml",
        "ref": "main",
    }


def test_workflow_dispatcher_calls_only_fixed_dispatch_endpoint():
    calls = []

    class Response:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def opener(request, timeout):
        calls.append((request, timeout))
        return Response()

    dispatcher = knowledge_dispatch.GitHubWorkflowDispatcher(
        "installation-token",
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
        opener=opener,
    )
    dispatcher.dispatch(workflow="yagapon-intake.yml", ref="main")

    request, timeout = calls[0]
    assert timeout == 30
    assert request.full_url.endswith("/YagamiFes-IT-Knowledge/actions/workflows/yagapon-intake.yml/dispatches")
    assert request.method == "POST"
    assert json.loads(request.data) == {"ref": "main"}
    assert request.headers["Authorization"] == "Bearer installation-token"


def test_dispatch_failure_returns_safe_diagnostic(monkeypatch):
    _clear_dispatch_environment(monkeypatch)
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GITHUB_APP_ID", "123")
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID", "456")
    monkeypatch.setenv(
        "YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY",
        "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----",
    )

    class FailingProvider:
        def __init__(self, **_kwargs):
            pass

        def __call__(self):
            raise RuntimeError("sensitive upstream details")

    monkeypatch.setattr(
        knowledge_dispatch,
        "GitHubAppTokenProvider",
        FailingProvider,
    )

    result = knowledge_dispatch.request_knowledge_intake_dispatch()

    assert result.status == "failed"
    assert result.reason == "workflow-dispatch-failed"
    assert result.error_type == "RuntimeError"
    assert "sensitive" not in str(result.as_dict())


def test_github_app_token_can_request_an_exact_actions_only_subset(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return json.dumps(
                {
                    "token": "dispatch-token",
                    "expires_at": datetime.fromtimestamp(
                        1_700_003_600,
                        timezone.utc,
                    ).isoformat(),
                    "permissions": {"actions": "write", "metadata": "read"},
                    "repositories": [{"name": "YagamiFes-IT-Knowledge"}],
                }
            ).encode()

    def opener(request, timeout):
        assert timeout == 30
        assert json.loads(request.data)["permissions"] == {"actions": "write"}
        return Response()

    monkeypatch.setattr(
        "source_index.github_auth.jwt.encode",
        lambda *_args, **_kwargs: "app.jwt",
    )
    provider = GitHubAppTokenProvider(
        app_id="123",
        installation_id="456",
        private_key="-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----",
        repositories=("YagamiFes-IT-Knowledge",),
        permissions={"actions": "write"},
        clock=lambda: 1_700_000_000,
        opener=opener,
    )

    assert provider() == "dispatch-token"


@pytest.mark.parametrize(
    "permissions",
    (
        {},
        {"contents": "write"},
        {"actions": "read"},
        {"pull_requests": "write"},
        {"contents": "read", "actions": "write"},
    ),
)
def test_github_app_token_rejects_every_unapproved_permission_profile(permissions):
    with pytest.raises(RuntimeError, match="exactly one approved profile"):
        GitHubAppTokenProvider(
            app_id="123",
            installation_id="456",
            private_key="-----BEGIN PRIVATE KEY-----\nvalue\n-----END PRIVATE KEY-----",
            repositories=("YagamiFes-IT-Knowledge",),
            permissions=permissions,
        )


@pytest.mark.asyncio
async def test_curation_coalesces_queued_bundles_into_one_immediate_dispatch(
    monkeypatch,
):
    dispatch = AsyncMock(
        return_value={
            "status": "pr-requested",
            "reason": "workflow-dispatch-accepted",
            "error_type": None,
        }
    )
    monkeypatch.setattr(curation, "dispatch_queued_knowledge_bundles", dispatch)

    result = await curation._finalize_curation_delivery(
        {"processed": 3, "queued": 3},
        queued_for_intake=3,
        published_pull_request_urls=[],
    )

    dispatch.assert_awaited_once_with()
    assert result["delivery"]["queued_bundles"] == 3
    assert result["delivery"]["immediate_pr"]["status"] == "pr-requested"


@pytest.mark.asyncio
async def test_curation_does_not_dispatch_for_empty_output(monkeypatch):
    dispatch = AsyncMock()
    monkeypatch.setattr(curation, "dispatch_queued_knowledge_bundles", dispatch)

    result = await curation._finalize_curation_delivery(
        {"processed": 0, "queued": 0},
        queued_for_intake=0,
        published_pull_request_urls=[],
    )

    dispatch.assert_not_awaited()
    assert result["delivery"]["immediate_pr"]["status"] == "not-needed"


def test_discord_result_distinguishes_requested_waiting_and_failed_dispatch():
    result = {
        "mode": "curation",
        "curation": {
            "enabled": True,
            "checkpoint_ready": True,
            "processed": 2,
            "queued": 2,
            "failed": 0,
            "delivery": {
                "queued_bundles": 2,
                "published_bundles": 0,
                "pull_request_urls": [],
                "immediate_pr": {
                    "status": "pr-requested",
                    "reason": "workflow-dispatch-accepted",
                    "error_type": None,
                },
            },
        },
    }

    assert _knowledge_refresh_status(result) == "pr-requested"
    requested = _knowledge_refresh_embed(result)
    assert "整理完了・PR作成要求済み" in requested.title
    value = next(
        field.value
        for field in requested.fields
        if field.name == "Draft PR作成"
    )
    assert "queue保存: 2" in value
    assert "PR作成要求を受け付けました" in value
    assert "PR URLはまだ確認できていません" in value

    result["curation"]["delivery"]["immediate_pr"] = {
        "status": "waiting",
        "reason": "github-app-not-configured",
        "error_type": None,
    }
    assert _knowledge_refresh_status(result) == "pr-waiting"
    waiting = _knowledge_refresh_embed(result)
    assert "PR作成待ち" in waiting.title
    assert "定期Actionsで再試行" in next(
        field.value for field in waiting.fields if field.name == "Draft PR作成"
    )

    result["curation"]["delivery"]["immediate_pr"] = {
        "status": "failed",
        "reason": "workflow-dispatch-failed",
        "error_type": "HTTPError",
    }
    assert _knowledge_refresh_status(result) == "partial"
    failed = next(
        field.value
        for field in _knowledge_refresh_embed(result).fields
        if field.name == "Draft PR作成"
    )
    assert "bundleは保持" in failed
    assert "`HTTPError`" in failed
