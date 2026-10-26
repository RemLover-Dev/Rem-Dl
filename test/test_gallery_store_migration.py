"""Tag preservation is the hard requirement — gallery tags must never be lost.

These pin the two guarantees the SQLite split is built on:
  1. gallery.json / image_history.json round-trip into SQLite losslessly,
  2. a non-tag write cannot reach the tags table at all.
"""
import json

import pytest

from core.gallery_store import GalleryStore, MigrationError, _tags_fingerprint


def _seed_gallery(path):
    entries = [
        {"id": "a", "filename": "one.png", "filepath": "gelbooru/x/one.png",
         "site": "gelbooru", "favourite": False, "downloaded_at": "2026-01-01T00:00:00",
         "tags": {"tag": ["1girl", "blue hair", "smile"], "artist": ["someone"],
                  "copyright": ["touhou"], "character": ["rem"]}},
        {"id": "b", "filename": "two.gif", "filepath": "nekos_best/two.gif",
         "site": "nekos.best", "favourite": True, "downloaded_at": "2026-01-02T00:00:00",
         "rating": "safe", "sources": ["nekos.life", "zerochan"],
         "tags": {"tag": ["hug"], "artist": ["azit"], "outfit": ["school uniform"]}},
        # no tags at all, sparse fields
        {"id": "c", "filename": "three.jpg", "filepath": "zerochan/three.jpg",
         "site": "zerochan", "favourite": False, "downloaded_at": "2026-01-03T00:00:00",
         "meta_mtime": 1767225600.0, "md5_misses": 2, "tags": {}},
        # list-shaped legacy tags
        {"id": "d", "filename": "four.jpg", "filepath": "gelbooru/four.jpg",
         "site": "gelbooru", "favourite": False, "downloaded_at": "2026-01-04T00:00:00",
         "tags": ["legacy_flat_tag"]},
    ]
    (path / "gallery.json").write_text(
        json.dumps({"images": entries}), encoding="utf-8")
    return entries


def _seed_history(path):
    hist = [
        {"site": "gelbooru", "filename": "one.png", "filepath": "gelbooru/x/one.png",
         "downloaded_at": 1767225600.0, "tags": {"tag": ["1girl"], "artist": ["someone"]}},
        {"site": "zerochan", "filename": "three.jpg", "filepath": "zerochan/three.jpg",
         "downloaded_at": 1767312000.0, "tags": {"tag": ["landscape"]}},
    ]
    (path / "image_history.json").write_text(json.dumps(hist), encoding="utf-8")
    return hist


@pytest.fixture
def store(tmp_path):
    entries = _seed_gallery(tmp_path)
    hist = _seed_history(tmp_path)
    s = GalleryStore(
        db_path=str(tmp_path / "gallery.db"),
        gallery_json=str(tmp_path / "gallery.json"),
        history_json=str(tmp_path / "image_history.json"),
    )
    s.count()  # forces connect -> migration
    return s, entries, hist


def test_migration_preserves_every_tag(store):
    s, entries, _ = store
    assert s.count() == len(entries)
    for src in entries:
        got = s.get_entry(src["id"])
        assert got is not None, "entry %s missing after migration" % src["filename"]
        assert _tags_fingerprint(got["tags"]) == _tags_fingerprint(src["tags"]), \
            "tags lost for %s: %r -> %r" % (src["filename"], src["tags"], got["tags"])


def test_migration_preserves_non_tag_fields(store):
    s, entries, _ = store
    by_fn = {e["filename"]: e for e in s.all_entries_with_tags()}
    for src in entries:
        got = by_fn[src["filename"]]
        for col in ("filepath", "site", "downloaded_at", "rating",
                    "meta_mtime", "md5_misses"):
            assert got.get(col) == src.get(col), \
                "%s.%s: %r -> %r" % (src["filename"], col, src.get(col), got.get(col))
        assert got["favourite"] is src["favourite"]
    assert by_fn["two.gif"]["sources"] == ["nekos.life", "zerochan"]


def test_history_tags_migrate(store):
    s, _, hist = store
    got = s.history_all()
    assert len(got) == len(hist)
    # newest first, same as the JSON's insert(0, ...) ordering
    assert got[0]["filename"] == "three.jpg"
    by_fn = {h["filename"]: h for h in got}
    assert by_fn["one.png"]["tags"] == {"tag": ["1girl"], "artist": ["someone"]}
    assert by_fn["three.jpg"]["tags"] == {"tag": ["landscape"]}


def test_json_is_retired_not_deleted(store):
    import os
    s, _, _ = store
    assert os.path.exists(s.gallery_json + ".migrated")
    assert not os.path.exists(s.gallery_json)


def test_non_tag_write_cannot_touch_tags(store):
    """The structural guarantee: filepath/favourite updates reach no tag row."""
    s, entries, _ = store
    before = s.get_tags("a")
    assert before == {"tag": ["1girl", "blue hair", "smile"], "artist": ["someone"],
                      "copyright": ["touhou"], "character": ["rem"]}

    s.update_fields("a", {"filepath": "gelbooru/renamed/one.png", "favourite": True})
    s.set_favourite(["b"], False)

    assert s.get_tags("a") == before, "a filepath write changed tags"
    assert s.get_tags("b") == {"tag": ["hug"], "artist": ["azit"],
                               "outfit": ["school uniform"]}
    got = s.get_entry("a")
    assert got["filepath"] == "gelbooru/renamed/one.png"
    assert got["favourite"] is True


def test_delete_removes_only_that_entry(store):
    s, _, _ = store
    assert s.delete_by_filename("one.png") is True
    assert s.get_entry("a") is None
    # untouched neighbour keeps its tags
    assert s.get_tags("b")["tag"] == ["hug"]


def test_broken_migration_leaves_json_alone(tmp_path, monkeypatch):
    """A tag that does not round-trip must abort before anything is retired."""
    _seed_gallery(tmp_path)
    s = GalleryStore(
        db_path=str(tmp_path / "gallery.db"),
        gallery_json=str(tmp_path / "gallery.json"),
        history_json=str(tmp_path / "image_history.json"),
    )
    # every entry would round-trip unless we make the store lie about them
    def _bad_set(self, conn, gid, tags):
        conn.execute("DELETE FROM gallery_tags WHERE gallery_id = ?", (gid,))
    monkeypatch.setattr(GalleryStore, "_set_tags_locked", _bad_set)

    with pytest.raises(MigrationError):
        s.count()
    import os
    assert os.path.exists(s.gallery_json), "json must survive a failed migration"


def test_save_with_tagless_entries_leaves_stored_tags_alone(store):
    """The cutover's central rule: a stripped entry must not clear tags.

    This is what load_gallery() hands back after the store split (entries
    carry no `tags`), and every caller rewrites the whole list through
    save_entries().
    """
    s, entries, _ = store
    stripped = [{k: v for k, v in e.items() if k != "tags"} for e in
                s.all_entries_with_tags()]
    # a rescan pruning + filepath fix-up, all on tagless copies
    for e in stripped:
        e["filepath"] = e["filepath"].upper()
    s.save_entries(stripped)

    for src in entries:
        got = s.get_entry(src["id"])
        assert _tags_fingerprint(got["tags"]) == _tags_fingerprint(src["tags"]), \
            "tags lost for %s" % src["filename"]
    assert s.get_entry("a")["filepath"] == "GELBOORU/X/ONE.PNG"


def test_save_persists_tags_when_an_entry_has_them(store):
    """The rescan / md5-recovery path must still be able to write tags."""
    s, _, _ = store
    all_entries = s.all_entries_with_tags()
    for e in all_entries:
        if e["id"] == "a":
            e["tags"] = {"tag": ["newtag"], "artist": ["someone"]}  # rewritten
        else:
            e.pop("tags", None)                                     # tagless
    s.save_entries(all_entries)
    assert s.get_tags("a")["tag"] == ["newtag"]
    assert s.get_tags("b")["tag"] == ["hug"]      # untouched sibling


def test_save_prunes_entries_that_are_gone(store):
    """startup_rescan deletes dead files by omitting them from the list."""
    s, entries, _ = store
    kept = [e for e in s.all_entries_with_tags() if e["filename"] != "one.png"]
    for e in kept:
        e.pop("tags", None)          # tagless, as load_gallery() returns them
    s.save_entries(kept)
    assert s.count() == len(entries) - 1
    assert s.get_entry("a") is None
    assert s.get_tags("b")["tag"] == ["hug"]     # survivor keeps its tags


def test_chunking_sees_the_same_tags_as_a_full_read(store):
    """Chunked reads (what filtering uses) must match the full read exactly."""
    s, entries, _ = store
    full = {e["filename"]: e for e in s.all_entries_with_tags()}
    seen = []
    # non-overlapping slices of 2 that tile exactly 4 entries
    for off in (0, 2):
        seen.extend(s.chunk_with_tags(off, 2))
    assert len(seen) == len(entries)
    for e in seen:
        src = full[e["filename"]]
        assert _tags_fingerprint(e["tags"]) == _tags_fingerprint(src["tags"])
