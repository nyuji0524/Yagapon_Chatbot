from knowledge_index.__main__ import discover, metadata_for, read_document


def test_read_document_preserves_status_and_body(tmp_path):
    path = tmp_path / "decision.md"
    path.write_text("---\nstatus: approved\ntype: decision\nfestival: 27\n---\n# 決定\n本文", encoding="utf-8")

    metadata, body = read_document(path)

    assert metadata == {"status": "approved", "type": "decision", "festival": 27}
    assert body.startswith("# 決定")


def test_discover_skips_readmes_and_templates(tmp_path):
    included = tmp_path / "years/27th/decision.md"
    readme = tmp_path / "README.md"
    template = tmp_path / "templates/source.md"
    included.parent.mkdir(parents=True)
    template.parent.mkdir(parents=True)
    for path in (included, readme, template):
        path.write_text("text", encoding="utf-8")

    assert discover(tmp_path) == [included]


def test_metadata_contains_festival_status_and_stable_key(tmp_path, monkeypatch):
    monkeypatch.setenv("YAGAPON_KNOWLEDGE_BASE_URL", "https://example.com/knowledge")
    path = tmp_path / "years/27th/decision.md"
    path.parent.mkdir(parents=True)
    path.write_text("text", encoding="utf-8")

    metadata = metadata_for(
        tmp_path,
        path,
        {"status": "approved", "type": "decision", "festival": 27},
        1,
        "a" * 64,
    )
    values = {item["key"]: item.get("string_value", item.get("numeric_value")) for item in metadata}

    assert values["festival"] == 27.0
    assert values["status"] == "approved"
    assert values["source_type"] == "decision"
    assert values["source_url"] == "https://example.com/knowledge/years/27th/decision.md"
    assert len(values["document_key"]) == 24
