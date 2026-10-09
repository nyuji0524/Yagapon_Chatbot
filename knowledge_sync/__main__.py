"""Generate local Knowledge draft files from a Google Shared Drive."""

import argparse
import hashlib
import json
import logging
import os
import time
from pathlib import Path

from knowledge_sync.draft import (
    redact_credentials,
    render_source_draft,
    stable_filename,
    structure_source,
)
from knowledge_sync.drive import DriveReader

log = logging.getLogger("yagapon.knowledge_sync")


def load_state(path: Path) -> dict:
    if not path.exists():
        return {"page_token": None, "files": {}}
    return json.loads(path.read_text(encoding="utf-8"))


def save_state(path: Path, state: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def sync_once(args) -> dict:
    state_path = Path(os.environ.get("YAGAPON_DRIVE_SYNC_STATE", "/data/drive-sync-state.json"))
    output_dir = Path(os.environ.get("YAGAPON_KNOWLEDGE_OUTPUT", "/data/knowledge-drafts"))
    reader = DriveReader(
        credentials_path=os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON") or None,
        drive_id=os.environ["GOOGLE_DRIVE_ID"],
        folder_id=os.environ.get("GOOGLE_DRIVE_FOLDER_ID") or None,
    )
    state = load_state(state_path)

    if args.check:
        return {"connection": reader.check_access()}

    if args.full:
        documents = reader.list_all()
        new_page_token = reader.get_start_page_token()
    elif state.get("page_token"):
        documents, new_page_token = reader.list_changes(state["page_token"])
    else:
        state["page_token"] = reader.get_start_page_token()
        save_state(state_path, state)
        return {"initialized": True, "generated": 0, "skipped": 0, "removed": 0}

    output_dir.mkdir(parents=True, exist_ok=True)
    generated = 0
    changed = 0
    skipped = 0
    removed = 0
    for document in documents:
        if document.removed:
            state["files"].setdefault(document.file_id, {})["removed"] = True
            removed += 1
            continue
        text = reader.read_text(document)
        if text is None:
            skipped += 1
            continue
        sanitized, redaction_count = redact_credentials(text)
        source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        previous = state["files"].get(document.file_id, {})
        if previous.get("sha256") == source_hash:
            continue
        changed += 1
        if not args.dry_run:
            structured = structure_source(sanitized, os.environ.get("YAGAPON_KNOWLEDGE_MODEL", "gemini-3.8-flash"))
            output = output_dir / stable_filename(document)
            temporary = output.with_suffix(output.suffix + ".tmp")
            temporary.write_text(
                render_source_draft(document, text, structured, redaction_count),
                encoding="utf-8",
            )
            os.replace(temporary, output)
            generated += 1
            state["files"][document.file_id] = {
                "sha256": source_hash,
                "modified_time": document.modified_time,
                "name": document.name,
                "removed": False,
            }

    if args.dry_run:
        return {"would_generate": changed, "skipped": skipped, "removed": removed}
    state["page_token"] = new_page_token
    save_state(state_path, state)
    return {"generated": generated, "skipped": skipped, "removed": removed}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="Import all direct children on the first run")
    parser.add_argument("--dry-run", action="store_true", help="Report changes without drafts or state updates")
    parser.add_argument("--watch-interval", type=int, default=0, help="Repeat differential sync at this interval")
    parser.add_argument("--check", action="store_true", help="Verify credentials and Drive access only")
    args = parser.parse_args()
    if args.watch_interval and args.watch_interval < 60:
        parser.error("--watch-interval must be at least 60 seconds")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    while True:
        try:
            print(json.dumps(sync_once(args), ensure_ascii=False), flush=True)
        except Exception:
            if not args.watch_interval:
                raise
            log.exception("Drive differential sync failed; retrying on the next interval")
        if not args.watch_interval:
            return
        args.full = False
        time.sleep(args.watch_interval)


if __name__ == "__main__":
    main()
