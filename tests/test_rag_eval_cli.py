import io
import urllib.error

import pytest

from rag_eval.__main__ import post_json


def test_post_json_surfaces_api_error_body(monkeypatch):
    error = urllib.error.HTTPError(
        url="https://example.invalid/rag/evaluate",
        code=422,
        msg="Unprocessable Entity",
        hdrs=None,
        fp=io.BytesIO(b'{"detail":"reference_facts invalid"}'),
    )
    monkeypatch.setattr("urllib.request.urlopen", lambda *args, **kwargs: (_ for _ in ()).throw(error))

    with pytest.raises(RuntimeError, match="reference_facts invalid"):
        post_json("https://example.invalid/rag/evaluate", "token", {"question": "test"})
