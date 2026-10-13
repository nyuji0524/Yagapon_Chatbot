"""Seed a local Firestore Emulator with a minimal Yaga Support profile."""

from __future__ import annotations

import argparse
import asyncio
import os
from urllib.parse import quote

import aiohttp


def _fields(values: dict[str, object]) -> dict:
    encoded = {}
    for key, value in values.items():
        if isinstance(value, list):
            encoded[key] = {
                "arrayValue": {
                    "values": [{"stringValue": str(item)} for item in value]
                }
            }
        else:
            encoded[key] = {"stringValue": str(value)}
    return encoded


async def _write_document(session: aiohttp.ClientSession, base_url: str, path: str, values: dict) -> None:
    async with session.patch(
        f"{base_url}/{path}",
        json={"fields": _fields(values)},
    ) as response:
        if response.status >= 400:
            detail = await response.text()
            raise RuntimeError(f"Could not seed {path}: {response.status} {detail[:300]}")


async def seed(*, host: str, project: str, email: str, uid: str, bureau: str, grade: str) -> None:
    base_url = (
        f"http://{host}/v1/projects/{quote(project, safe='')}"
        "/databases/(default)/documents/support/functions"
    )
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        await _write_document(
            session,
            base_url,
            f"users/{quote(uid, safe='')}",
            {
                "uid": uid,
                "name": "テスト委員",
                "email": email,
                "departments": [bureau],
                "roles": [],
            },
        )
        await _write_document(
            session,
            base_url,
            f"staffInfos/{quote(email, safe='')}",
            {"email": email, "grade": grade},
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("FIRESTORE_EMULATOR_HOST", "support-firestore:8080"))
    parser.add_argument("--project", default=os.environ.get("YAGAPON_SUPPORT_FIREBASE_PROJECT", "demo-yagapon-support"))
    parser.add_argument("--email", default="test-member@keio.jp")
    parser.add_argument("--uid", default="test-support-uid")
    parser.add_argument("--bureau", default="IT局")
    parser.add_argument("--grade", default="2")
    args = parser.parse_args()
    asyncio.run(
        seed(
            host=args.host,
            project=args.project,
            email=args.email,
            uid=args.uid,
            bureau=args.bureau,
            grade=args.grade,
        )
    )
    print(f"Seeded {args.email} ({args.uid}) into {args.project} at {args.host}")


if __name__ == "__main__":
    main()
