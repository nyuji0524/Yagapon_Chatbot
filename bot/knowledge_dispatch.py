"""Trigger the Knowledge repository intake workflow after durable queueing.

The bot never receives a GitHub contents or pull-request write token.  It may
optionally mint a short-lived, repository-restricted GitHub App token whose
only non-metadata permission is ``actions:write``.  The Knowledge repository's
workflow remains the sole component that can turn a validated GCS bundle into
a Draft PR.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

from bot.secret_value import read_secret
from source_index.github_auth import GitHubAppTokenProvider

log = logging.getLogger("yagapon.knowledge_dispatch")

DEFAULT_WORKFLOW = "yagapon-intake.yml"
DEFAULT_REF = "main"
GITHUB_API_VERSION = "2022-11-28"


@dataclass(frozen=True)
class KnowledgeDispatchResult:
    status: str
    reason: str
    error_type: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)


class GitHubWorkflowDispatcher:
    """Minimal client for one fixed workflow-dispatch endpoint."""

    def __init__(
        self,
        token: str,
        repository: str,
        *,
        opener=None,
    ) -> None:
        if not re.fullmatch(
            r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}",
            repository,
        ):
            raise RuntimeError("Knowledge repository name is invalid")
        self.token = token
        self.repository = repository
        self._opener = opener or urllib.request.urlopen

    def dispatch(self, *, workflow: str, ref: str) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}\.ya?ml", workflow) or "/" in workflow:
            raise RuntimeError("Knowledge intake workflow name is invalid")
        if not re.fullmatch(r"[A-Za-z0-9._/-]{1,200}", ref) or ".." in ref:
            raise RuntimeError("Knowledge intake workflow ref is invalid")
        workflow_path = urllib.parse.quote(workflow, safe="")
        request = urllib.request.Request(
            (f"https://api.github.com/repos/{self.repository}/actions/workflows/{workflow_path}/dispatches"),
            data=json.dumps({"ref": ref}, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": "yagapon-knowledge-dispatch",
                "X-GitHub-Api-Version": GITHUB_API_VERSION,
            },
        )
        with self._opener(request, timeout=30) as response:
            status = int(getattr(response, "status", 204))
            if status != 204:
                raise RuntimeError(f"Knowledge intake workflow dispatch returned HTTP {status}")


def _configuration() -> tuple[dict[str, str] | None, KnowledgeDispatchResult | None]:
    if (
        os.environ.get(
            "YAGAPON_KNOWLEDGE_INTAKE_DISPATCH_ENABLED",
            "true",
        )
        .strip()
        .casefold()
        != "true"
    ):
        return None, KnowledgeDispatchResult("waiting", "dispatch-disabled")

    values = {
        "app_id": read_secret("YAGAPON_KNOWLEDGE_GITHUB_APP_ID"),
        "installation_id": read_secret("YAGAPON_KNOWLEDGE_GITHUB_APP_INSTALLATION_ID"),
        "private_key": read_secret("YAGAPON_KNOWLEDGE_GITHUB_APP_PRIVATE_KEY"),
    }
    configured = {name for name, value in values.items() if value}
    if not configured:
        return None, KnowledgeDispatchResult(
            "waiting",
            "github-app-not-configured",
        )
    if len(configured) != len(values):
        return None, KnowledgeDispatchResult(
            "failed",
            "github-app-configuration-incomplete",
            "ConfigurationError",
        )
    return values, None


def request_knowledge_intake_dispatch() -> KnowledgeDispatchResult:
    """Request the intake Action once, returning privacy-safe diagnostics."""
    try:
        values, terminal = _configuration()
    except (OSError, UnicodeError) as exc:
        log.warning(
            "Knowledge intake GitHub App credential could not be read error=%s",
            type(exc).__name__,
        )
        return KnowledgeDispatchResult(
            "failed",
            "github-app-credential-unreadable",
            type(exc).__name__,
        )
    if terminal is not None:
        return terminal
    assert values is not None
    repository = os.environ.get(
        "YAGAPON_KNOWLEDGE_REPOSITORY",
        "YagamiFes-IT/YagamiFes-IT-Knowledge",
    ).strip()
    workflow = os.environ.get(
        "YAGAPON_KNOWLEDGE_INTAKE_WORKFLOW",
        DEFAULT_WORKFLOW,
    ).strip()
    ref = os.environ.get(
        "YAGAPON_KNOWLEDGE_BASE_BRANCH",
        DEFAULT_REF,
    ).strip()
    try:
        repository_name = repository.split("/", 1)[1]
        provider = GitHubAppTokenProvider(
            app_id=values["app_id"],
            installation_id=values["installation_id"],
            private_key=values["private_key"],
            repositories=(repository_name,),
            permissions={"actions": "write"},
        )
        GitHubWorkflowDispatcher(provider(), repository).dispatch(
            workflow=workflow,
            ref=ref,
        )
    except (IndexError, RuntimeError, urllib.error.HTTPError, urllib.error.URLError) as exc:
        log.warning(
            "Knowledge intake workflow dispatch failed error=%s",
            type(exc).__name__,
        )
        return KnowledgeDispatchResult(
            "failed",
            "workflow-dispatch-failed",
            type(exc).__name__,
        )
    # GitHub returns 204 when the dispatch request is accepted.  That response
    # does not identify a workflow run or prove that a Draft PR was created.
    return KnowledgeDispatchResult("pr-requested", "workflow-dispatch-accepted")


async def dispatch_queued_knowledge_bundles() -> dict[str, str | None]:
    """Run the blocking App-token and GitHub calls outside the event loop."""
    result = await asyncio.to_thread(request_knowledge_intake_dispatch)
    return result.as_dict()
