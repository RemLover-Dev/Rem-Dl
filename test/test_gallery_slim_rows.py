"""Slim resident rows: None-valued columns are omitted at construction.

The resident gallery list holds every row for the life of the process, and
rating/sources/meta_mtime/md5_misses are None for ~95% of rows — skipping
them cuts ~25% of that RAM. Pinned: reads behave the same (img.get()),
saves round-trip real values, and NULLs stay NULLs after a full-replace
save (save_entries writes e.get(col) -> None for the omitted keys).
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest

from core.gallery_store import get_store, use_store_path


@pytest.fixture
def store(tmp_path):
    use_store_path(str(tmp_path / "gallery.db"))
    yield get_store()
    use_store_path(None)


def test_optional_none_columns_omitted(store):
    store.insert_entry({"id": "a", "filename": "a.png", "filepath": "x/a.png",
                        "site": "konachan", "favourite": False,
                        "downloaded_at": "2026-01-01T00:00:00", "tags": ["rem"]})
    e = store.lightweight_entries()[0]
    assert e.get("rating") is None
    assert e.get("sources") is None
    assert e.get("meta_mtime") is None
    assert e.get("md5_misses") is None
    assert "rating" not in e and "sources" not in e
    assert e["favourite"] is False
    assert e["filename"] == "a.png"


def test_present_values_round_trip_through_save(store):
    store.insert_entry({"id": "b", "filename": "b.png", "filepath": "x/b.png",
                        "site": "konachan", "favourite": True,
                        "downloaded_at": "2026-01-01T00:00:00",
                        "rating": "explicit", "meta_mtime": 123.5,
                        "md5_misses": 2,
                        "sources": ["https://konachan.com/post/show/1"],
                        "tags": ["rem"]})
    e = store.lightweight_entries()[0]
    assert e["rating"] == "explicit"
    assert e["sources"] == ["https://konachan.com/post/show/1"]
    assert e["meta_mtime"] == 123.5
    assert e["md5_misses"] == 2
    assert e["favourite"] is True
    # full-replace save of the resident copies must not null stored values
    store.save_entries(store.lightweight_entries())
    e2 = store.lightweight_entries()[0]
    assert e2["rating"] == "explicit"
    assert e2["sources"] == ["https://konachan.com/post/show/1"]
    assert e2["meta_mtime"] == 123.5
    assert e2["md5_misses"] == 2
    assert e2["favourite"] is True


def test_corrupt_sources_json_reads_as_none(store):
    import sqlite3
    store.insert_entry({"id": "c", "filename": "c.png", "filepath": "x/c.png",
                        "site": "konachan", "favourite": False,
                        "downloaded_at": "2026-01-01T00:00:00", "tags": ["x"]})
    conn = sqlite3.connect(store.db_path)
    conn.execute("UPDATE gallery SET sources = 'not-json' WHERE id = 'c'")
    conn.commit()
    conn.close()
    e = store.lightweight_entries()[0]
    assert e.get("sources") is None
