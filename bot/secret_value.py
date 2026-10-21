"""Read deployment secrets from environment variables or mounted files."""

from __future__ import annotations

import os
from pathlib import Path


def read_secret(name: str) -> str:
    """Return ``name`` or ``name_FILE`` without ever logging the value."""
    direct = os.environ.get(name, "").strip()
    if direct:
        return direct
    file_path = os.environ.get(f"{name}_FILE", "").strip()
    if not file_path:
        return ""
    return Path(file_path).read_text(encoding="utf-8").strip()
