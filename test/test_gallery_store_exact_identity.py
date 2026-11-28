"""Exact gallery/history deletes must not fan out by basename."""
from core.gallery_store import GalleryStore


def test_delete_gallery_id_preserves_same_named_sibling(tmp_path):
    store = GalleryStore(db_path=str(tmp_path / "gallery.sqlite3"))
    try:
        for gid, path, tag in (
            ("g-a", "one/image.jpg", "first"),
            ("g-b", "two/image.jpg", "second"),
        ):
            store.insert_entry({
                "id": gid, "filename": "image.jpg", "filepath": path,
                "site": "test", "favourite": False,
            })
            store.set_tags(gid, {"tag": [tag]})

        store.delete("g-a")
        rows = store.lightweight_entries()
        assert [row["id"] for row in rows] == ["g-b"]
        assert store.get_tags("g-b")["tag"] == ["second"]
    finally:
        store.close()


def test_history_delete_by_path_preserves_same_named_other_site(tmp_path):
    store = GalleryStore(db_path=str(tmp_path / "history.sqlite3"))
    try:
        first = store.history_insert({
            "site": "danbooru", "filename": "image.jpg",
            "filepath": "Danbooru/tag/image.jpg", "tags": {"tag": ["a"]},
        })
        second = store.history_insert({
            "site": "pixiv", "filename": "image.jpg",
            "filepath": "Pixiv/tag/image.jpg", "tags": {"tag": ["b"]},
        })
        assert store.history_delete_by_filepath("Danbooru/tag/image.jpg") == 1
        rows = store.history_all()
        assert [row["history_id"] for row in rows] == [second]
        assert first not in {row["history_id"] for row in rows}

        third = store.history_insert({
            "site": "danbooru", "filename": "same.jpg",
            "filepath": r"Danbooru\tag\same.jpg", "tags": {},
        })
        assert store.history_delete_by_filepath("Danbooru/tag/same.jpg") == 1
        assert third not in {row["history_id"] for row in store.history_all()}
    finally:
        store.close()
