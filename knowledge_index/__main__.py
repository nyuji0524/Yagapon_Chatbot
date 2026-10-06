"""Synchronize reviewed Knowledge Markdown into the configured File Search store."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import yaml

from bot.config import ConfigManager
from bot.corpus import CorpusManager

log = logging.getLogger("yagapon.knowledge_index")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"schema_version": 1, "files": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def read_document(path: Path) -> tuple[dict, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if not text.startswith("---\n") or "\n---\n" not in text[4:]:
        return {}, text
    front_matter, body = text[4:].split("\n---\n", 1)
    return yaml.safe_load(front_matter) or {}, body.strip()


def discover(root: Path) -> list[Path]:
    paths = []
    for path in root.rglob("*.md"):
        relative = path.relative_to(root).as_posix()
        if path.name == "README.md" or "/templates/" in f"/{relative}":
            continue
        paths.append(path)
    return sorted(paths)


def metadata_for(root: Path, path: Path, front_matter: dict, guild_id: int, digest: str) -> list[dict]:
    relative = path.relative_to(root).as_posix()
    base_url = os.environ.get("YAGAPON_KNOWLEDGE_BASE_URL", "").rstrip("/")
    source_url = f"{base_url}/{relative}" if base_url else relative
    metadata = [
        {"key": "source", "string_value": "knowledge"},
        {"key": "source_type", "string_value": str(front_matter.get("type") or "knowledge")[:500]},
        {"key": "schema", "string_value": "knowledge-v1"},
        {"key": "guild_id", "string_value": str(guild_id)},
        {"key": "status", "string_value": str(front_matter.get("status") or "unknown")[:500]},
        {"key": "authority", "string_value": "curated"},
        {"key": "document_key", "string_value": hashlib.sha256(relative.encode()).hexdigest()[:24]},
        {"key": "source_url", "string_value": source_url[:500]},
        {"key": "content_sha256", "string_value": digest[:64]},
    ]
    festival = front_matter.get("festival")
    if isinstance(festival, int):
        metadata.append({"key": "festival", "numeric_value": float(festival)})
    return metadata


async def sync_once(args) -> dict:
    root = args.root.resolve()
    state = load_state(args.state)
    allowed = {item.strip() for item in args.statuses.split(",") if item.strip()}
    config = ConfigManager()
    store_name = os.environ.get("YAGAPON_CORPUS_STORE_NAME") or config.get_corpus(args.guild_id)
    if not store_name:
        raise RuntimeError("No File Search store configured for the guild")
    corpus = CorpusManager()
    current_paths = set()
    generated = unchanged = removed = skipped = 0

    for path in discover(root):
        relative = path.relative_to(root).as_posix()
        current_paths.add(relative)
        front_matter, body = read_document(path)
        status = str(front_matter.get("status") or "")
        previous = state["files"].get(relative)
        if status not in allowed:
            if previous and previous.get("remote_name") and not args.dry_run:
                await corpus._delete_documents([previous["remote_name"]])
                state["files"].pop(relative, None)
                removed += 1
            else:
                skipped += 1
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        digest = hashlib.sha256(source.encode()).hexdigest()
        if previous and previous.get("sha256") == digest:
            unchanged += 1
            continue
        if args.dry_run:
            generated += 1
            continue
        remote_name = await corpus._upload_document(
            store_name,
            f"Knowledge | {relative}"[:512],
            source,
            metadata_for(root, path, front_matter, args.guild_id, digest),
        )
        if not remote_name:
            raise RuntimeError(f"Failed to index {relative}")
        if previous and previous.get("remote_name"):
            await corpus._delete_documents([previous["remote_name"]])
        state["files"][relative] = {"sha256": digest, "remote_name": remote_name, "status": status}
        generated += 1

    for relative in set(state["files"]) - current_paths:
        previous = state["files"].pop(relative)
        if not args.dry_run and previous.get("remote_name"):
            await corpus._delete_documents([previous["remote_name"]])
        removed += 1
    if not args.dry_run:
        save_state(args.state, state)
    return {"indexed": generated, "unchanged": unchanged, "removed": removed, "skipped": skipped}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(os.environ.get("YAGAPON_KNOWLEDGE_ROOT", "/knowledge")))
    parser.add_argument("--guild-id", type=int, default=int(os.environ.get("YAGAPON_KNOWLEDGE_GUILD_ID", "0")))
    parser.add_argument("--state", type=Path, default=Path(os.environ.get("YAGAPON_KNOWLEDGE_INDEX_STATE", "/data/knowledge-index-state.json")))
    parser.add_argument("--statuses", default=os.environ.get("YAGAPON_KNOWLEDGE_INDEX_STATUSES", "approved,verified,current"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--watch-interval", type=int, default=0)
    args = parser.parse_args()
    if args.guild_id <= 0:
        parser.error("--guild-id or YAGAPON_KNOWLEDGE_GUILD_ID is required")
    if args.watch_interval and args.watch_interval < 60:
        parser.error("--watch-interval must be at least 60 seconds")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    while True:
        try:
            print(json.dumps(asyncio.run(sync_once(args)), ensure_ascii=False), flush=True)
        except Exception:
            if not args.watch_interval:
                raise
            log.exception("Knowledge indexing failed; retrying on the next interval")
        if not args.watch_interval:
            return
        time.sleep(args.watch_interval)


if __name__ == "__main__":
    main()
