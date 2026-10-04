"""Category repair: entries frozen at download time with wrong/missing
categories are re-sorted from the worker tag caches (healed by later runs)."""
from core.shared import recategorize_tags, load_tag_caches


CACHES = {
    "gelbooru": {"old_hag": "character", "chan1": "artist",
                 "general_ok": "tag", "rawtype": 4},
}


def test_recategorize_moves_known_tags_and_leaves_unknowns():
    tags = {
        "tag": ["old_hag", "chan1", "general_ok", "mystery", "rawtype"],
        "character": ["rem"],
        "outfit": ["swimsuit"],
    }
    assert recategorize_tags(tags, "gelbooru", CACHES)
    assert tags["tag"] == ["general_ok", "mystery"]
    assert tags["artist"] == ["chan1"]
    # raw int cache values (yande-era entries) normalize to their category
    assert tags["character"] == ["rem", "old_hag", "rawtype"]
    # outfit/hair/... come from other sources — never touched
    assert tags["outfit"] == ["swimsuit"]


def test_recategorize_demotes_and_is_site_scoped():
    tags = {"character": ["general_ok"]}
    assert recategorize_tags(tags, "gelbooru", CACHES)
    assert tags["tag"] == ["general_ok"]
    # other sites' images are left alone (per-site caches)
    assert not recategorize_tags({"tag": ["old_hag"]}, "konachan", CACHES)
    # flat/legacy shapes are left alone
    assert not recategorize_tags(["old_hag"], "gelbooru", CACHES)
    assert not recategorize_tags(None, "gelbooru", CACHES)


def test_load_tag_caches_returns_normalized_strings():
    caches = load_tag_caches()
    assert "gelbooru" in caches
    assert all(isinstance(c, str) for c in caches["gelbooru"].values())


def test_tag_caches_fingerprint_tracks_changes(tmp_path, monkeypatch):
    import core.shared as shared

    db = tmp_path / "database"
    db.mkdir()
    f = db / "gelbooru_tag_types.json"
    f.write_text('{"a": "tag"}')
    monkeypatch.setattr(shared, "BASE_DIR", str(tmp_path))
    fp1 = shared._tag_caches_fingerprint()
    assert fp1 == shared._tag_caches_fingerprint()
    f.write_text('{"a": "character"}')  # size differs — deterministic
    assert shared._tag_caches_fingerprint() != fp1


def test_recategorize_all_skips_when_caches_unchanged(tmp_path, monkeypatch):
    """The post-job heal must cost nothing when no worker fetched anything new."""
    import core.shared as shared

    db = tmp_path / "database"
    db.mkdir()
    (db / "x_tag_types.json").write_text('{"a": "tag"}')
    monkeypatch.setattr(shared, "BASE_DIR", str(tmp_path))
    reads = []
    monkeypatch.setattr(shared, "load_tag_caches", lambda: reads.append(1) or {})
    # fingerprint primed to current state = cache untouched since last heal
    monkeypatch.setattr(shared, "_recat_fp", shared._tag_caches_fingerprint())
    assert shared.recategorize_all() == (0, 0)
    assert reads == []  # never loaded caches, never scanned gallery/history
