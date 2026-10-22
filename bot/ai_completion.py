"""Shared checks for model responses that must be complete before persistence.

Generated text is often still available when Gemini stops at its output-token
limit.  Treating that text as a successful transcript or minutes document is
unsafe: downstream delivery can make the partial artifact durable and delete
the only recoverable recording.  Callers in durable pipelines use this module
to fail closed before reading ``response.text``.
"""

from __future__ import annotations


class IncompleteModelOutput(RuntimeError):
    """Raised when a model explicitly reports a non-terminal completion."""

    def __init__(self, operation: str, finish_reason: str):
        self.operation = operation
        self.finish_reason = finish_reason
        super().__init__(f"{operation} returned incomplete output ({finish_reason})")


def _finish_reason_name(value) -> str:
    if value is None:
        return ""
    name = getattr(value, "name", None)
    if name:
        return str(name).upper()
    rendered = str(value).rsplit(".", 1)[-1]
    return rendered.upper()


def require_complete_model_output(response, *, operation: str) -> None:
    """Reject every explicit non-complete status or finish reason.

    The Interactions transcription endpoint reports an interaction ``status``
    rather than GenerateContent candidates.  Older SDK response adapters do not
    always expose either field, so absence of metadata is not treated as an
    error. Explicit ``incomplete``/``budget_exceeded`` statuses, ``MAX_TOKENS``,
    safety stops, malformed calls, and other non-terminal results are rejected.
    Empty output remains the caller's concern.
    """

    status = _finish_reason_name(getattr(response, "status", None))
    if status and status != "COMPLETED":
        raise IncompleteModelOutput(operation, f"STATUS_{status}")

    direct_reason = _finish_reason_name(getattr(response, "finish_reason", None))
    if direct_reason and direct_reason != "STOP":
        raise IncompleteModelOutput(operation, direct_reason)

    candidates = getattr(response, "candidates", None) or []
    for candidate in candidates:
        reason = _finish_reason_name(getattr(candidate, "finish_reason", None))
        if reason and reason != "STOP":
            raise IncompleteModelOutput(operation, reason)
