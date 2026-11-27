from core import shared
from core.dedup_store import DedupStore
from core.gallery_store import GalleryStore


def test_relocate_gallery_file_keeps_id_and_tags(tmp_path, monkeypatch):
    root = tmp_path / "Rems Dl"
    root.mkdir()
    old = root / "old.jxl"
    new = root / "new.jxl"
    old.write_bytes(b"image")

    gallery = GalleryStore(db_path=str(tmp_path / "gallery.sqlite3"))
    dedup = DedupStore(db_path=str(tmp_path / "dedup.sqlite3"))
    tags = {"tag": ["barrier_mao", "the_last_supper"], "artist": ["barrier_mao"]}
    gallery.insert_entry({
        "id": "stable-id", "filename": "old.jxl", "filepath": "old.jxl",
        "site": "gelbooru", "downloaded_at": "2026-11-27T11:59:11", "tags": tags,
    })
    monkeypatch.setattr(shared, "MASTER_FOLDER", str(root))
    monkeypatch.setattr(shared, "_store", lambda: gallery)
    monkeypatch.setattr("core.dedup_store.get_store", lambda: dedup)
    try:
        shared.relocate_gallery_file(str(old), str(new))
        assert new.exists() and not old.exists()
        entry = gallery.get_entry("stable-id")
        assert entry is not None
        assert entry["id"] == "stable-id"
        assert entry["filename"] == "new.jxl"
        assert entry["filepath"] == "new.jxl"
        assert entry["tags"] == tags
    finally:
        gallery.close()
        dedup.close()
