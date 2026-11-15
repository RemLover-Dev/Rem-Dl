"""h:/w: dimension search — parser, store SQL, and integration."""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pytest
from PIL import Image

import Rems_Dl
from core import shared
from core.gallery_store import get_store, use_store_path

# (id, filename, tags, width, height) — one video-like row without dims
DIMS = [
    ("a", "a.png", ["rem"], 800, 600),
    ("b", "b.png", ["rem"], 1200, 900),
    ("c", "c.png", ["ram"], 800, 900),
    ("d", "d.png", ["rem"], None, None),
]


@pytest.fixture
def seeded(tmp_path):
    use_store_path(str(tmp_path / "gallery.db"))
    store = get_store()
    for eid, fn, tags, w, h in DIMS:
        e = {"id": eid, "filename": fn, "filepath": "konachan/" + fn,
             "site": "konachan", "favourite": False,
             "downloaded_at": "2026-01-01T00:00:00", "tags": tags}
        if w is not None:
            e["width"], e["height"] = w, h
        store.insert_entry(e)
    yield
    use_store_path(None)


# --- parser ---

def test_parse_bare_is_exact():
    conds, rest = Rems_Dl._parse_dim_terms("h:800")
    assert conds == [("height", "=", 800, False)]
    assert "h:" not in rest


def test_parse_operators_both_positions():
    conds, _ = Rems_Dl._parse_dim_terms("w:>600 h:600<")
    assert ("width", ">", 600, False) in conds
    assert ("height", "<", 600, False) in conds


def test_parse_negation_and_quotes():
    conds, _ = Rems_Dl._parse_dim_terms('-"h:800"')
    assert conds == [("height", "=", 800, True)]
    conds, _ = Rems_Dl._parse_dim_terms('h:"800"')
    assert conds == [("height", "=", 800, False)]


def test_parse_leaves_tags_and_emoticons():
    conds, rest = Rems_Dl._parse_dim_terms("rem, h:800")
    assert conds == [("height", "=", 800, False)]
    assert "rem" in rest
    conds, rest = Rems_Dl._parse_dim_terms("flash:>900 >_<")
    assert conds == []
    assert rest == "flash:>900 >_<"


# --- store SQL ---

def test_store_exact_and_operators(seeded):
    # a=800x600, b=1200x900, c=800x900 — width 800 hits a and c
    assert shared.search_gallery_dims([("width", "=", 800)]) == {"a", "c"}
    assert shared.search_gallery_dims([("height", "=", 900)]) == {"b", "c"}
    assert shared.search_gallery_dims([("width", ">", 800)]) == {"b"}
    assert shared.search_gallery_dims([("width", ">", 600)]) == {"a", "b", "c"}
    assert shared.search_gallery_dims([("height", "<", 700)]) == {"a"}
    assert shared.search_gallery_dims(
        [("height", "=", 900), ("width", ">", 800)]) == {"b"}
    assert shared.search_gallery_dims([("height", "=", 800)]) == set()


def test_store_missing_dims_never_match(seeded):
    # d has no dimensions: matches nothing positive, survives negation
    assert shared.search_gallery_dims([("height", "<", 100000)]) == {"a", "b", "c"}
    assert "d" not in shared.search_gallery_dims([("width", "=", 800)])


def test_store_missing_dims_selector(seeded):
    missing = get_store().entries_missing_dims(10)
    assert sorted(m for m, _ in missing) == ["d"]
    get_store().update_fields("d", {"width": -1, "height": -1})
    assert get_store().entries_missing_dims(10) == []


# --- integration with the gallery search pipeline ---

def _search(q, images, seeded):
    keep = Rems_Dl._search_id_set(q.lower().strip())
    out = Rems_Dl._apply_gallery_filters(list(images), keep, [], False, [], [])
    return sorted(i["filename"] for i in out)


def _images():
    return [{"id": eid, "filename": fn} for eid, fn, _, _, _ in DIMS]


def test_search_dim_exact(seeded):
    assert _search("h:900", _images(), seeded) == ["b.png", "c.png"]
    assert _search("w:800", _images(), seeded) == ["a.png", "c.png"]


def test_search_dim_with_tag_and_order(seeded):
    assert _search("h:900, rem", _images(), seeded) == ["b.png"]
    assert _search("rem, h:900", _images(), seeded) == ["b.png"]


def test_search_dim_negation(seeded):
    assert _search("-h:900", _images(), seeded) == ["a.png", "d.png"]


def test_search_all_negation_incl_dims(seeded):
    assert _search("-h:600, -ram", _images(), seeded) == ["b.png", "d.png"]


# --- backfill thread ---

def test_backfill_thread(tmp_path):
    imgs = []
    for i, size in enumerate([(640, 480), (800, 600), (1024, 768)]):
        p = tmp_path / f"{i}.png"
        Image.new("RGB", size).save(p)
        imgs.append((p, size))
    vids = tmp_path / "v.bin"
    vids.write_bytes(b"not an image")
    use_store_path(str(tmp_path / "gallery.db"))
    store = get_store()
    store.insert_entry({"id": "b0", "filename": "0.png", "filepath": str(imgs[0][0]),
                        "site": "konachan", "favourite": False, "tags": ["x"]})
    store.insert_entry({"id": "b1", "filename": "1.png", "filepath": str(imgs[1][0]),
                        "site": "konachan", "favourite": False, "tags": ["x"]})
    store.insert_entry({"id": "b2", "filename": "2.png", "filepath": str(imgs[2][0]),
                        "site": "konachan", "favourite": False, "tags": ["x"]})
    store.insert_entry({"id": "b3", "filename": "v.bin", "filepath": str(vids),
                        "site": "konachan", "favourite": False, "tags": ["x"]})
    assert len(store.entries_missing_dims(10)) == 4
    shared._dims_backfill_started = False
    shared.start_dims_backfill()
    for _ in range(100):
        if not store.entries_missing_dims(10):
            break
        time.sleep(0.05)
    assert store.entries_missing_dims(10) == []
    assert shared.search_gallery_dims([("height", "=", 600)]) == {"b1"}
    assert shared.search_gallery_dims([("width", "=", 1024)]) == {"b2"}
    # the video probed to the -1 sentinel: it no longer blocks the selector
    with store._reader() as conn:
        assert conn.execute(
            "SELECT width FROM gallery WHERE id = 'b3'").fetchone()[0] == -1
    use_store_path(None)
    shared._dims_backfill_started = False


def test_backfill_resolves_relative_paths(tmp_path, monkeypatch):
    # stored paths are master-relative; the backfill must join MASTER_FOLDER
    # (an earlier version probed the raw relative path and poisoned every row)
    (tmp_path / "sub").mkdir()
    Image.new("RGB", (320, 240)).save(tmp_path / "sub" / "r.png")
    monkeypatch.setattr(shared, "MASTER_FOLDER", str(tmp_path))
    use_store_path(str(tmp_path / "gallery.db"))
    store = get_store()
    store.insert_entry({"id": "rel", "filename": "r.png",
                        "filepath": "sub/r.png",
                        "site": "konachan", "favourite": False, "tags": ["x"]})
    shared._dims_backfill_started = False
    shared.start_dims_backfill()
    for _ in range(100):
        if not store.entries_missing_dims(10):
            break
        time.sleep(0.05)
    assert shared.search_gallery_dims([("height", "=", 240)]) == {"rel"}
    use_store_path(None)
    shared._dims_backfill_started = False


def test_reset_dim_sentinels_spares_videos(tmp_path):
    use_store_path(str(tmp_path / "gallery.db"))
    store = get_store()
    for eid, fn in [("j1", "a.jxl"), ("p1", "b.png"), ("v1", "c.mp4")]:
        store.insert_entry({"id": eid, "filename": fn, "filepath": "x/" + fn,
                            "site": "konachan", "favourite": False,
                            "width": -1, "height": -1, "tags": ["x"]})
    store.reset_dim_sentinels()
    missing = {gid for gid, _ in store.entries_missing_dims(10)}
    assert missing == {"j1", "p1"}   # images re-probed, video keeps -1
    use_store_path(None)


# --- probe ---

def test_probe_image_dims(tmp_path):
    p = str(tmp_path / "x.png")
    Image.new("RGB", (321, 123)).save(p)
    assert shared.probe_image_dims(p) == (321, 123)


def test_probe_undimensionable(tmp_path):
    p = str(tmp_path / "x.bin")
    with open(p, "wb") as f:
        f.write(b"not an image")
    assert shared.probe_image_dims(p) is None


def test_history_enrichment_carries_dims(monkeypatch):
    hist = [{"filepath": "konachan/a.png", "filename": "a.png"},
            {"filepath": "konachan/d.png", "filename": "d.png"}]
    monkeypatch.setattr(shared, "load_gallery", lambda: {"images": [
        {"id": "a", "filepath": "konachan/a.png", "width": 800, "height": 600},
        {"id": "d", "filepath": "konachan/d.png", "width": -1, "height": -1},
    ]})
    Rems_Dl._enrich_image_history(hist)
    assert (hist[0]["width"], hist[0]["height"]) == (800, 600)
    # the -1 sentinel must not leak into the payload the pill reads
    assert "width" not in hist[1]
