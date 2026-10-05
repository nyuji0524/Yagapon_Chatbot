"""CLI for generating review-required glossary and people candidates."""

import argparse
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from bot.ai_models import fast_model
from knowledge_catalog.extract import consolidate, discover_documents, extract_batches, people_from_name_map
from knowledge_catalog.store import CatalogStore


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="Extract glossary and people candidates from Knowledge documents")
    parser.add_argument("--root", type=Path, required=True, help="YagamiFes-IT-Knowledge repository root")
    parser.add_argument("--guild-id", type=int, required=True)
    parser.add_argument("--output", type=Path, help="Catalog JSON path")
    parser.add_argument("--csv", type=Path, help="Optional review CSV path")
    parser.add_argument("--model", default=os.environ.get("YAGAPON_CATALOG_MODEL") or fast_model())
    parser.add_argument(
        "--passes",
        type=int,
        default=int(os.environ.get("YAGAPON_CATALOG_PASSES", "2")),
        help="Independent extraction passes to improve recall (1-5)",
    )
    parser.add_argument("--max-documents", type=int, default=0, help="Limit documents for a canary run")
    parser.add_argument(
        "--include-monthly",
        action="store_true",
        help="Also scan monthly timeline digests (slower and likely to duplicate curated sources)",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.passes <= 5:
        parser.error("--passes must be between 1 and 5")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    root = args.root.resolve()
    paths = discover_documents(root, include_monthly=args.include_monthly)
    if args.max_documents > 0:
        paths = paths[:args.max_documents]
    if not paths:
        raise SystemExit(f"No source documents found under {root}")

    extracted = []
    for pass_number in range(1, args.passes + 1):
        logging.getLogger("yagapon.catalog").info(
            "Starting extraction pass %s/%s", pass_number, args.passes
        )
        extracted.extend(extract_batches(root, paths, args.model))
    entries = consolidate(args.guild_id, extracted, people_from_name_map(root, args.guild_id))
    term_count = sum(entry.kind == "term" for entry in entries)
    people_count = sum(entry.kind == "person" for entry in entries)
    print(
        f"documents={len(paths)} passes={args.passes} "
        f"terms={term_count} people={people_count} model={args.model}"
    )
    if args.dry_run:
        return

    store = CatalogStore(args.output)
    created, refreshed = store.upsert_candidates(entries)
    print(f"created={created} refreshed={refreshed} catalog={store.path}")
    if args.csv:
        count = store.export_csv(args.guild_id, args.csv)
        print(f"csv_entries={count} csv={args.csv}")


if __name__ == "__main__":
    main()
