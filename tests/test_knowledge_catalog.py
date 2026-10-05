import json

import pytest

from knowledge_catalog.extract import consolidate, discover_documents, people_from_name_map
from knowledge_catalog.models import CatalogEntry, CatalogEvidence, CatalogPatch, ExtractedTerm, ExtractionBatch
from knowledge_catalog.store import CatalogConflictError, CatalogStore, catalog_entry_id


def candidate(guild_id: int = 1, description: str = "初期説明") -> CatalogEntry:
    return CatalogEntry(
        id=catalog_entry_id(guild_id, "term", "やがサポ"),
        guild_id=guild_id,
        kind="term",
        label="やがサポ",
        description=description,
        evidence=[CatalogEvidence(source="source.md", excerpt="やがサポを使う")],
        source_count=1,
    )


def test_reviewed_catalog_entry_is_not_overwritten_by_reextraction(tmp_path):
    store = CatalogStore(tmp_path / "catalog.json")
    store.upsert_candidates([candidate()])
    reviewed = store.patch(
        1,
        candidate().id,
        CatalogPatch(expected_revision=1, description="管理者が確認した説明", status="approved"),
    )

    created, refreshed = store.upsert_candidates([candidate(description="AIが再生成した説明")])

    assert (created, refreshed) == (0, 0)
    assert store.get(1, reviewed.id).description == "管理者が確認した説明"


def test_catalog_patch_uses_optimistic_revision(tmp_path):
    store = CatalogStore(tmp_path / "catalog.json")
    store.upsert_candidates([candidate()])

    with pytest.raises(CatalogConflictError):
        store.patch(1, candidate().id, CatalogPatch(expected_revision=2, description="競合"))


def test_name_map_people_are_candidates_with_source(tmp_path):
    root = tmp_path
    people_dir = root / "sources/discord/archive/people"
    people_dir.mkdir(parents=True)
    (people_dir / "name-map.json").write_text(json.dumps({
        "people": [{
            "discordUserId": "123",
            "fullName": "矢上太郎",
            "nameConfirmation": "本人申告",
            "bot": False,
            "claimedAliases": ["やがたろ"],
            "observedDisplayNames": [],
            "observedUsernames": [],
            "confirmedRoles": [{"role": "IT局長"}],
            "role": "27th: IT局長",
        }]
    }, ensure_ascii=False), encoding="utf-8")

    entries = people_from_name_map(root, 1)

    assert len(entries) == 1
    assert entries[0].discord_user_id == "123"
    assert entries[0].roles == ["IT局長"]
    assert entries[0].status == "candidate"


def test_discover_documents_skips_monthly_digests_unless_requested(tmp_path):
    canonical = tmp_path / "years/27th/overview.md"
    monthly = tmp_path / "years/27th/timeline/monthly/2026-09.md"
    canonical.parent.mkdir(parents=True)
    monthly.parent.mkdir(parents=True)
    canonical.write_text("overview", encoding="utf-8")
    monthly.write_text("monthly", encoding="utf-8")

    assert discover_documents(tmp_path) == [canonical]
    assert discover_documents(tmp_path, include_monthly=True) == [canonical, monthly]


def test_consolidate_merges_duplicate_term_evidence():
    batches = [ExtractionBatch(terms=[
        ExtractedTerm(
            term="やがサポ",
            proposed_definition="運営アプリ",
            reason="固有名詞",
            confidence=0.8,
            source="a.md",
            locator="第1節",
            evidence_excerpt="やがサポ",
        ),
        ExtractedTerm(
            term="やがサポ",
            aliases=["Yaga Support"],
            proposed_definition="矢上祭の運営用アプリ",
            reason="固有名詞",
            confidence=0.9,
            source="b.md",
            locator="第2節",
            evidence_excerpt="Yaga Support",
        ),
    ])]

    entries = consolidate(1, batches, [])

    assert len(entries) == 1
    assert entries[0].description == "矢上祭の運営用アプリ"
    assert entries[0].aliases == ["Yaga Support"]
    assert entries[0].source_count == 2


def test_consolidate_merges_label_into_single_alias_owner():
    canonical = CatalogEntry(
        id=catalog_entry_id(1, "term", "やがサポート"),
        guild_id=1,
        kind="term",
        label="やがサポート",
        aliases=["やがサポ"],
        description="運営用アプリ",
        evidence=[CatalogEvidence(source="a.md")],
    )
    alias = CatalogEntry(
        id=catalog_entry_id(1, "term", "やがサポ"),
        guild_id=1,
        kind="term",
        label="やがサポ",
        description="意味未確認",
        evidence=[CatalogEvidence(source="b.md")],
    )

    entries = consolidate(1, [], [canonical, alias])

    assert len(entries) == 1
    assert entries[0].label == "やがサポート"
    assert entries[0].source_count == 2


def test_consolidate_does_not_merge_ambiguous_alias():
    people = [
        CatalogEntry(
            id=catalog_entry_id(1, "person", label),
            guild_id=1,
            kind="person",
            label=label,
            aliases=["佐藤"],
        )
        for label in ("佐藤太郎", "佐藤花子")
    ]
    people.append(CatalogEntry(
        id=catalog_entry_id(1, "person", "佐藤"),
        guild_id=1,
        kind="person",
        label="佐藤",
    ))

    entries = consolidate(1, [], people)

    assert len(entries) == 3


def test_consolidate_marks_inferred_term_definition_as_unconfirmed():
    batches = [ExtractionBatch(terms=[ExtractedTerm(
        term="広報局ドライブ",
        proposed_definition="Google Driveと思われるが明記なし",
        source="a.md",
    )])]

    entries = consolidate(1, batches, [])

    assert entries[0].description == "文書だけでは意味未確認"
