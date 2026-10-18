"""Cross-site source-hash dedup: storage, backfill, pre-download gating, claims, adapters."""
import asyncio
import os
import random
import sqlite3
import threading

import pytest
from PIL import Image

from core.dedup_store import (
    DedupStore, hash_bytes, normalize_any_source_hash, normalize_generated_hashes,
    normalize_source_hash, normalize_source_md5,
)
from core.shared import BaseDownloader, check_duplicate
from core.database import get_settings


# --- helpers ---------------------------------------------------------------

def _mk_img(path, seed):
    Image.frombytes("RGB", (100, 100), random.Random(seed).randbytes(30000)).save(path)


def _img_bytes(seed):
    import io
    buf = io.BytesIO()
    Image.frombytes("RGB", (100, 100), random.Random(seed).randbytes(30000)).save(buf, format="PNG")
    return buf.getvalue()


def _mk_store(tmp_path, name="dedup.sqlite3"):
    return DedupStore(os.path.join(tmp_path, name))


def _mk_dl(tmp_path, monkeypatch, name="test_worker", site="Test:Site"):
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr("core.shared.write_image_metadata", lambda *a, **k: None)
    monkeypatch.setattr("core.shared.add_to_gallery", lambda *a, **k: None)
    monkeypatch.setattr("core.shared.send_tags", lambda *a, **k: None)
    dl = BaseDownloader(name, site, 10, {"anti_ban_pause": 0, "download_retries": 1})
    dl.log = lambda m: None
    dl.download_queue = asyncio.Queue()
    return dl


def _pairs(store, filepath):
    """[(site, algo, hash)] recorded for the image row of this filepath."""
    row = store._conn.execute(
        "SELECT id FROM image_hashes WHERE filepath=?", (filepath,)).fetchone()
    if row is None:
        return None
    return [(r["site"], r["algo"], r["hash"]) for r in store._conn.execute(
        "SELECT site, algo, hash FROM source_hashes WHERE image_id=?", (row["id"],))]


def _sites_for(store, digest):
    return {r["site"] for r in store._conn.execute(
        "SELECT site FROM source_hashes WHERE hash=?", (digest,))}


# --- storage & migration ---------------------------------------------------

class TestStorage:
    def test_new_db_has_source_hashes_table(self, tmp_path):
        store = _mk_store(tmp_path)
        cols = {r[1] for r in store._conn.execute("PRAGMA table_info(source_hashes)")}
        assert {"image_id", "site", "algo", "hash"} <= cols
        assert "md5" not in cols
        ih_cols = {r[1] for r in store._conn.execute("PRAGMA table_info(image_hashes)")}
        assert "source_md5" not in ih_cols
        idx = {r["name"] for r in store._conn.execute("PRAGMA index_list(source_hashes)")}
        assert "uq_source_hashes" in idx and "idx_source_hashes_hash" in idx

    def test_legacy_db_migrates_and_keeps_rows(self, tmp_path):
        # hand-build a pre-source-hash database with one perceptual row
        db = os.path.join(tmp_path, "legacy.sqlite3")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE image_hashes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filepath TEXT NOT NULL, site TEXT, post_id TEXT,
                phash TEXT NOT NULL, dhash TEXT NOT NULL, ahash TEXT NOT NULL,
                whash TEXT NOT NULL, color_hist TEXT NOT NULL,
                grid_hists TEXT NOT NULL, added_at REAL NOT NULL
            )""")
        conn.execute("""INSERT INTO image_hashes
            (filepath, site, post_id, phash, dhash, ahash, whash,
             color_hist, grid_hists, added_at)
            VALUES ('/x/old.png', 'oldsite', '42', 'ab', 'cd', 'ef', 'gh',
                    '[0.1]', '[[0.1]]', 1.0)""")
        conn.commit()
        conn.close()

        store = DedupStore(db)
        row = store._conn.execute(
            "SELECT * FROM image_hashes WHERE filepath='/x/old.png'").fetchone()
        assert row is not None
        assert row["phash"] == "ab"
        # no phantom source hash appeared for a row that never had one
        assert _pairs(store, "/x/old.png") == []
        # idempotent: a second init doesn't fail or duplicate anything
        store.close()
        store2 = DedupStore(db)
        assert store2._conn.execute("SELECT COUNT(*) c FROM image_hashes").fetchone()["c"] == 1

    def test_interim_columns_migrate_into_source_hashes(self, tmp_path):
        # hand-build both interim shapes: image_hashes.source_md5 (single column)
        # and a source_hashes table without algo/hash
        db = os.path.join(tmp_path, "interim.sqlite3")
        conn = sqlite3.connect(db)
        conn.execute("""
            CREATE TABLE image_hashes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                filepath TEXT NOT NULL, site TEXT, post_id TEXT,
                phash TEXT NOT NULL, dhash TEXT NOT NULL, ahash TEXT NOT NULL,
                whash TEXT NOT NULL, color_hist TEXT NOT NULL,
                grid_hists TEXT NOT NULL, added_at REAL NOT NULL,
                source_md5 TEXT
            )""")
        conn.execute("CREATE INDEX idx_source_md5 ON image_hashes(source_md5)")
        conn.execute("""
            CREATE TABLE source_hashes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL, site TEXT,
                md5 TEXT NOT NULL, added_at REAL NOT NULL
            )""")
        conn.execute("CREATE UNIQUE INDEX uq_source_hashes "
                     "ON source_hashes(image_id, site, md5)")
        conn.execute("CREATE INDEX idx_source_hashes_md5 ON source_hashes(md5)")
        conn.execute("""INSERT INTO image_hashes
            (filepath, site, post_id, phash, dhash, ahash, whash,
             color_hist, grid_hists, added_at, source_md5)
            VALUES ('/x/one.png', 's1', '1', 'aa', 'bb', 'cc', 'dd',
                    '[0.1]', '[[0.1]]', 1.0, ?)""", ("a" * 32,))
        conn.execute("""INSERT INTO image_hashes
            (filepath, site, post_id, phash, dhash, ahash, whash,
             color_hist, grid_hists, added_at, source_md5)
            VALUES ('/x/two.png', 's2', '2', 'ee', 'ff', '11', '22',
                    '[0.2]', '[[0.2]]', 2.0, NULL)""")
        conn.execute("INSERT INTO source_hashes (image_id, site, md5, added_at) "
                     "VALUES (2, 's2', ?, 3.0)", ("b" * 32,))
        conn.commit()
        conn.close()

        store = DedupStore(db)
        sh_cols = {r[1] for r in store._conn.execute("PRAGMA table_info(source_hashes)")}
        assert {"algo", "hash"} <= sh_cols and "md5" not in sh_cols
        ih_cols = {r[1] for r in store._conn.execute("PRAGMA table_info(image_hashes)")}
        assert "source_md5" not in ih_cols
        rows = store._conn.execute(
            "SELECT image_id, site, algo, hash FROM source_hashes ORDER BY image_id").fetchall()
        assert [(r["image_id"], r["site"], r["algo"], r["hash"]) for r in rows] == [
            (1, "s1", "md5", "a" * 32), (2, "s2", "md5", "b" * 32)]
        # idempotent second init
        store.close()
        store2 = DedupStore(db)
        assert store2._conn.execute("SELECT COUNT(*) c FROM source_hashes").fetchone()["c"] == 2

    def test_add_persists_normalized_source_md5(self, tmp_path):
        store = _mk_store(tmp_path)
        p1 = os.path.join(tmp_path, "a.png")
        _mk_img(p1, 1)
        md5 = "A" * 32
        store.check_and_add(p1, site="s1", post_id="1", source_md5=md5)
        assert _pairs(store, p1) == [("s1", "md5", md5.lower())]
        # lookup sees it
        ok, match = store.claim_source_md5(md5.lower(), "/other/x.png")
        assert ok is False and match is not None

    def test_add_stores_generated_hashes_verbatim(self, tmp_path):
        store = _mk_store(tmp_path)
        p1 = os.path.join(tmp_path, "a.png")
        _mk_img(p1, 2)
        with open(p1, "rb") as f:
            served = hash_bytes(f.read())
        store.check_and_add(p1, site="s1", generated_hashes=served)
        pairs = _pairs(store, p1)
        for algo in ("md5", "sha1", "sha256"):
            assert ("s1", algo, served[algo]) in pairs

    def test_check_and_add_source_hit_skips_signature(self, tmp_path, monkeypatch):
        store = _mk_store(tmp_path)
        p1 = os.path.join(tmp_path, "a.png")
        _mk_img(p1, 1)
        store.check_and_add(p1, site="s1", source_md5="b" * 32)

        # a different file with the same source hash is a duplicate even though
        # it would never match perceptually — and no signature is computed
        def boom(*a, **k):
            raise AssertionError("signature must not be computed on a source hit")
        monkeypatch.setattr("core.dedup_store.compute_signature", boom)
        p2 = os.path.join(tmp_path, "b.png")
        _mk_img(p2, 2)
        with open(p2, "rb") as f:
            g2 = hash_bytes(f.read())
        res = store.check_and_add(p2, site="s2", source_md5="b" * 32, generated_hashes=g2)
        assert res.is_duplicate
        assert res.matched_by == "source_md5"
        assert res.matched_path == p1
        # candidate never got its own row
        assert store._conn.execute(
            "SELECT COUNT(*) c FROM image_hashes WHERE filepath=?", (p2,)).fetchone()["c"] == 0
        # the second site's hashes are kept on the survivor, like the sources badge
        pairs = _pairs(store, p1)
        assert ("s2", "md5", "b" * 32) in pairs
        for algo in ("md5", "sha1", "sha256"):
            assert ("s2", algo, g2[algo]) in pairs

    def test_normalize(self):
        assert normalize_source_md5("ABCDEF0123456789ABCDEF0123456789") == "abcdef0123456789abcdef0123456789"
        assert normalize_source_md5("abcdef0123456789abcdef0123456789") == "abcdef0123456789abcdef0123456789"
        assert normalize_source_md5("  deadbeef" + "0" * 24 + "  ") == "deadbeef" + "0" * 24
        sha1 = "da39a3ee5e6b4b0d3255bfef95601890afd80709"  # 40 hex — not an md5
        assert normalize_source_md5(sha1) is None
        assert normalize_source_md5("0" * 31) is None
        assert normalize_source_md5("0" * 33) is None
        assert normalize_source_md5("z" * 32) is None
        assert normalize_source_md5("") is None
        assert normalize_source_md5(None) is None
        assert normalize_source_md5(12345) is None
        # the same value IS a valid sha1, and sha256 has its own length
        assert normalize_source_hash("sha1", sha1) == sha1
        assert normalize_source_hash("SHA1", sha1.upper()) == sha1
        assert normalize_source_hash("sha256", "B" * 64) == "b" * 64
        assert normalize_source_hash("sha1", "a" * 32) is None
        assert normalize_source_hash("sha256", "a" * 40) is None
        assert normalize_source_hash("sha512", "a" * 128) is None
        assert normalize_source_hash("md5", None) is None

    def test_normalize_any_source_hash_classifies_by_length(self):
        # whatever hash type a site hands us: length alone picks the algo
        assert normalize_any_source_hash("A" * 32) == ("md5", "a" * 32)
        assert normalize_any_source_hash("da39a3ee5e6b4b0d3255bfef95601890afd80709") == (
            "sha1", "da39a3ee5e6b4b0d3255bfef95601890afd80709")
        assert normalize_any_source_hash("  " + "B" * 64) == ("sha256", "b" * 64)
        assert normalize_any_source_hash("a" * 33) is None
        assert normalize_any_source_hash("z" * 40) is None
        assert normalize_any_source_hash("") is None
        assert normalize_any_source_hash(None) is None
        assert normalize_any_source_hash(42) is None

    def test_normalize_generated_hashes_drops_junk(self):
        assert normalize_generated_hashes({
            "md5": "A" * 32, "sha1": "y" * 40, "sha512": "a" * 128, "md5x": "a" * 32,
        }) == {"md5": "a" * 32}
        assert normalize_generated_hashes(None) == {}
        assert normalize_generated_hashes("not a dict") == {}

    def test_source_hash_entry_accepts_sha1(self, tmp_path, monkeypatch):
        # a site handing us a sha1 instead of an md5 must be stored under its
        # true algo and gate duplicates before any signature is computed
        store = _mk_store(tmp_path)
        p1 = os.path.join(tmp_path, "a.png")
        _mk_img(p1, 1)
        sha1 = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
        store.check_and_add(p1, site="s1", source_md5=sha1)
        assert _pairs(store, p1) == [("s1", "sha1", sha1)]

        def boom(*a, **k):
            raise AssertionError("signature must not be computed on a source hit")
        monkeypatch.setattr("core.dedup_store.compute_signature", boom)
        p2 = os.path.join(tmp_path, "b.png")
        _mk_img(p2, 2)
        res = store.check_and_add(p2, site="s2", source_md5=sha1)
        assert res.is_duplicate
        assert res.matched_by == "source_md5"
        assert res.matched_path == p1
        assert ("s2", "sha1", sha1) in _pairs(store, p1)

    def test_stale_source_record_does_not_block(self, tmp_path):
        store = _mk_store(tmp_path)
        p1 = os.path.join(tmp_path, "gone.png")
        _mk_img(p1, 3)
        store.check_and_add(p1, source_md5="c" * 32)
        os.remove(p1)
        # stale row no longer blocks and is pruned
        ok, _ = store.claim_source_md5("c" * 32, "/new/x.png")
        assert ok is True
        assert store._conn.execute(
            "SELECT COUNT(*) c FROM source_hashes WHERE hash=?", ("c" * 32,)).fetchone()["c"] == 0

    def test_claim_lifecycle(self, tmp_path):
        store = _mk_store(tmp_path)
        md5 = "d" * 32
        assert store.claim_source_md5(md5, "/fp1.png")[0] is True
        # second owner rejected while first holds it
        assert store.claim_source_md5(md5, "/fp2.png") == (False, None)
        # releasing an unrelated filepath cannot free someone else's claim
        store.release_source_md5("/fp2.png")
        assert store.claim_source_md5(md5, "/fp2.png")[0] is False
        # owner release frees it; re-claim by owner is idempotent
        store.release_source_md5("/fp1.png")
        assert store.claimed_source_md5("/fp1.png") is None
        store.release_source_md5("/fp1.png")  # no-op, must not raise
        assert store.claim_source_md5(md5, "/fp2.png")[0] is True

    def test_concurrent_claims_one_winner(self, tmp_path):
        store = _mk_store(tmp_path)
        md5 = "e" * 32
        results = []
        barrier = threading.Barrier(2)

        def worker(fp):
            barrier.wait()
            results.append(store.claim_source_md5(md5, fp)[0])

        threads = [threading.Thread(target=worker, args=(f"/fp{i}.png",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(results) == [False, True]

    def test_phash_fallback_attaches_hashes_without_dangling_row(self, tmp_path):
        store = _mk_store(tmp_path)
        p1 = os.path.join(tmp_path, "orig.png")
        _mk_img(p1, 5)
        store.check_and_add(p1, site="s1")  # survivor, no hashes of its own
        # same image from another site (re-compressed copy) with its own md5
        p2 = os.path.join(tmp_path, "copy.png")
        _mk_img(p2, 5)
        with open(p2, "rb") as f:
            g2 = hash_bytes(f.read())
        res = store.check_and_add(p2, site="s2", source_md5="f" * 32, generated_hashes=g2)
        assert res.is_duplicate
        assert res.matched_by == "phash"
        # no dangling row for the killed copy
        assert store._conn.execute(
            "SELECT COUNT(*) c FROM image_hashes WHERE filepath=?", (p2,)).fetchone()["c"] == 0
        # surviving file now carries the duplicate's site hashes, all formats
        pairs = _pairs(store, p1)
        assert ("s2", "md5", "f" * 32) in pairs
        for algo in ("md5", "sha1", "sha256"):
            assert ("s2", algo, g2[algo]) in pairs
        # each of them gates future listings from that site pre-download
        for digest in ("f" * 32, g2["md5"], g2["sha1"]):
            ok, match = store.claim_source_md5(digest, "/elsewhere/z.png")
            assert ok is False and match["filepath"] == p1

    def test_phash_fallback_keeps_both_sites_hashes(self, tmp_path):
        # an image from 2 sites with 2 different served files: both sites'
        # hashes must survive on the one record, not just the first site's
        store = _mk_store(tmp_path)
        p1 = os.path.join(tmp_path, "orig.png")
        _mk_img(p1, 6)
        store.check_and_add(p1, site="s1", source_md5="1" * 32)
        p2 = os.path.join(tmp_path, "copy.png")
        _mk_img(p2, 6)
        res = store.check_and_add(p2, site="s2", source_md5="2" * 32)
        assert res.is_duplicate
        pairs = _pairs(store, p1)
        assert ("s1", "md5", "1" * 32) in pairs
        assert ("s2", "md5", "2" * 32) in pairs
        assert _sites_for(store, "1" * 32) == {"s1"}
        assert _sites_for(store, "2" * 32) == {"s2"}
        # both md5s resolve to the same surviving file for future listings
        for digest in ("1" * 32, "2" * 32):
            ok, match = store.claim_source_md5(digest, "/incoming/q.png")
            assert ok is False and match["filepath"] == p1


# --- backfill for existing images ------------------------------------------

class TestBackfill:
    def test_generates_hashes_for_old_rows(self, tmp_path):
        store = _mk_store(tmp_path)
        # an old download: booru filename embeds the site's md5
        embedded = "ab" * 16
        p1 = os.path.join(tmp_path, f"123-{embedded}.png")
        _mk_img(p1, 50)
        assert store.add_missing([(p1, "site1")]) == 1
        # old install: the row exists, no source hashes yet
        assert _pairs(store, p1) == []

        assert store.backfill_source_hashes() == 1
        with open(p1, "rb") as f:
            disk = hash_bytes(f.read())
        pairs = _pairs(store, p1)
        # the site's own md5 recovered from the filename...
        assert ("site1", "md5", embedded) in pairs
        # ...plus every format of the bytes we hold, one read
        for algo in ("md5", "sha1", "sha256"):
            assert ("site1", algo, disk[algo]) in pairs
        # the recovered site hash gates a re-download pre-transfer
        ok, match = store.claim_source_md5(embedded, "/incoming/z.png")
        assert ok is False and match is not None and match["filepath"] == p1
        # idempotent: nothing left to generate
        assert store.backfill_source_hashes() == 0

    def test_backfill_skips_gone_files_and_hashed_rows(self, tmp_path):
        store = _mk_store(tmp_path)
        p1 = os.path.join(tmp_path, "a.png")
        _mk_img(p1, 51)
        p2 = os.path.join(tmp_path, "b.png")
        _mk_img(p2, 52)
        store.add_missing([(p1, "s1"), (p2, "s2")])
        row2 = store._conn.execute(
            "SELECT id FROM image_hashes WHERE filepath=?", (p2,)).fetchone()
        assert store.add_source_hash(row2["id"], "s2", "9" * 32) is True
        os.remove(p1)
        # p1 is gone (nothing to read), p2 already carries a hash (skip)
        assert store.backfill_source_hashes() == 0
        assert _pairs(store, p1) == []

    def test_backfill_plain_filename_still_gets_byte_hashes(self, tmp_path):
        store = _mk_store(tmp_path)
        p1 = os.path.join(tmp_path, "no-md5-name.png")
        _mk_img(p1, 53)
        store.add_missing([(p1, "s1")])
        assert store.backfill_source_hashes() == 1
        with open(p1, "rb") as f:
            disk = hash_bytes(f.read())
        assert set(_pairs(store, p1)) == {
            ("s1", "md5", disk["md5"]), ("s1", "sha1", disk["sha1"]),
            ("s1", "sha256", disk["sha256"])}


# --- pre-download behavior --------------------------------------------------

class TestPreDownload:
    @pytest.fixture(autouse=True)
    def _store(self, tmp_path, monkeypatch):
        store = _mk_store(tmp_path)
        monkeypatch.setattr("core.dedup_store.get_store", lambda: store)
        settings = get_settings()
        settings.update({"dedup_enabled": True})
        yield store
        settings.update({"dedup_enabled": True})

    async def _enqueue(self, dl, md5=None, filename="shot.png"):
        filepath = os.path.join(dl.site_root, "Tag", filename)
        return await dl.enqueue_download(
            "http://example.com/img.png", filepath, filename, [], source_md5=md5)

    def test_stored_md5_blocks_enqueue(self, tmp_path, monkeypatch, _store):
        dl = _mk_dl(tmp_path, monkeypatch)
        known = os.path.join(tmp_path, "known.png")
        _mk_img(known, 10)
        _store.check_and_add(known, site="othersite", post_id="9", source_md5="9" * 32)

        assert asyncio.run(self._enqueue(dl, md5="9" * 32)) is False
        # never scheduled: no queue item, no counter bump
        assert dl.download_queue.empty()
        assert dl.enqueued_count == 0

    def test_stored_sha1_blocks_enqueue(self, tmp_path, monkeypatch, _store):
        # pre-download gating must accept any stored hash type, not just md5
        dl = _mk_dl(tmp_path, monkeypatch)
        known = os.path.join(tmp_path, "known.png")
        _mk_img(known, 10)
        sha1 = "da39a3ee5e6b4b0d3255bfef95601890afd80709"
        _store.check_and_add(known, site="othersite", post_id="9", source_md5=sha1)

        assert asyncio.run(self._enqueue(dl, md5=sha1)) is False
        assert dl.download_queue.empty()
        assert dl.enqueued_count == 0

    def test_pre_download_skip_merges_tags_via_absorb(self, tmp_path, monkeypatch, _store):
        dl = _mk_dl(tmp_path, monkeypatch)
        known = os.path.join(tmp_path, "known.png")
        _mk_img(known, 10)
        _store.check_and_add(known, site="othersite", post_id="9", source_md5="9" * 32)

        absorbed = []
        monkeypatch.setattr(
            "core.shared.absorb_duplicate",
            lambda dup, *a, **k: absorbed.append((dup, a)) or True)
        filepath = os.path.join(dl.site_root, "Tag", "shot.png")
        assert asyncio.run(dl.enqueue_download(
            "http://example.com/img.png", filepath, "shot.png",
            ["rem", "blue_hair"], ["artist1"], source_md5="9" * 32)) is False
        # the surviving record gets the duplicate's tags/site — no download, no new record
        assert len(absorbed) == 1
        dup, args = absorbed[0]
        assert dup.matched_by == "source_md5"
        assert dup.matched_path == known
        assert args[1] == ["rem", "blue_hair"]  # tags_list forwarded
        assert args[2] == ["artist1"]           # artists forwarded

    def test_pre_download_skip_keeps_both_sites_hashes(self, tmp_path, monkeypatch, _store):
        # same bytes served by two sites: the skipping site's attribution must
        # land on the survivor too, so listings from either site dedupe later
        dl = _mk_dl(tmp_path, monkeypatch, name="site2")
        known = os.path.join(tmp_path, "known.png")
        _mk_img(known, 11)
        _store.check_and_add(known, site="site1", post_id="9", source_md5="9" * 32)
        monkeypatch.setattr("core.shared.absorb_duplicate", lambda *a, **k: True)

        assert asyncio.run(self._enqueue(dl, md5="9" * 32)) is False
        assert _sites_for(_store, "9" * 32) == {"site1", "site2"}

    def test_inflight_claim_does_not_absorb(self, tmp_path, monkeypatch, _store):
        # an in-flight reservation has no surviving record yet — must not call absorb
        dl1 = _mk_dl(tmp_path, monkeypatch, name="worker1", site="Site1")
        dl2 = _mk_dl(tmp_path, monkeypatch, name="worker2", site="Site2")
        assert asyncio.run(self._enqueue(dl1, md5="7" * 32, filename="a.png")) is True
        absorbed = []
        monkeypatch.setattr(
            "core.shared.absorb_duplicate",
            lambda dup, *a, **k: absorbed.append(dup) or True)
        assert asyncio.run(self._enqueue(dl2, md5="7" * 32, filename="b.png")) is False
        assert absorbed == []

    def test_different_md5_accepted(self, tmp_path, monkeypatch, _store):
        dl = _mk_dl(tmp_path, monkeypatch)
        known = os.path.join(tmp_path, "known.png")
        _mk_img(known, 10)
        _store.check_and_add(known, source_md5="9" * 32)

        assert asyncio.run(self._enqueue(dl, md5="8" * 32)) is True
        assert dl.download_queue.qsize() == 1

    def test_invalid_hash_uses_existing_behavior(self, tmp_path, monkeypatch, _store):
        dl = _mk_dl(tmp_path, monkeypatch)
        assert asyncio.run(self._enqueue(dl, md5="not-a-hash")) is True
        assert asyncio.run(self._enqueue(dl, md5=None, filename="other.png")) is True
        assert dl.download_queue.qsize() == 2

    def test_inflight_claim_blocks_second_enqueue(self, tmp_path, monkeypatch, _store):
        dl1 = _mk_dl(tmp_path, monkeypatch, name="worker1", site="Site1")
        dl2 = _mk_dl(tmp_path, monkeypatch, name="worker2", site="Site2")
        assert asyncio.run(self._enqueue(dl1, md5="7" * 32, filename="a.png")) is True
        # same source from a different site while dl1's download is in flight
        assert asyncio.run(self._enqueue(dl2, md5="7" * 32, filename="b.png")) is False
        assert dl2.download_queue.empty()

    def test_dedup_disabled_no_blocking(self, tmp_path, monkeypatch, _store):
        dl = _mk_dl(tmp_path, monkeypatch)
        known = os.path.join(tmp_path, "known.png")
        _mk_img(known, 10)
        _store.check_and_add(known, source_md5="9" * 32)
        get_settings().update({"dedup_enabled": False})
        assert asyncio.run(self._enqueue(dl, md5="9" * 32)) is True
        # and no reservation was taken either
        assert _store.claimed_source_md5(os.path.join(dl.site_root, "Tag", "shot.png")) is None

    def test_filename_dedup_still_works(self, tmp_path, monkeypatch, _store):
        dl = _mk_dl(tmp_path, monkeypatch)
        assert asyncio.run(self._enqueue(dl, md5=None, filename="same.png")) is True
        assert asyncio.run(self._enqueue(dl, md5=None, filename="same.png")) is False

    def _drain_worker(self, dl):
        async def _run():
            dl.is_scanning = False
            await dl._download_worker()
        asyncio.run(_run())

    def _mock_session(self, dl, status=200, payload=None):
        class C:
            def __init__(s, chunks): s.chunks = chunks
            async def iter_chunked(s, n):
                for c in s.chunks: yield c
        class R:
            def __init__(s):
                s.status = status
                s.headers = {"Content-Length": str(len(payload or b""))}
                s.content = C([payload] if payload else [])
            def raise_for_status(s):
                if s.status >= 400: raise RuntimeError("http error")
            async def __aenter__(s): return s
            async def __aexit__(s, *a): pass
        class S:
            headers = {}
            def get(s, url, headers=None, timeout=None): return R()
        dl.session = S()

    def test_success_persists_claimed_and_streamed_hashes(self, tmp_path, monkeypatch, _store):
        dl = _mk_dl(tmp_path, monkeypatch)
        payload = _img_bytes(20)
        self._mock_session(dl, 200, payload)
        assert asyncio.run(self._enqueue(dl, md5="5" * 32)) is True
        self._drain_worker(dl)
        assert dl.downloaded_count == 1
        fp = os.path.join(dl.site_root, "Tag", "shot.png")
        served = hash_bytes(payload)
        pairs = _pairs(_store, fp)
        # the API-claimed md5 AND every format of the served bytes are kept
        assert ("test_worker", "md5", "5" * 32) in pairs
        for algo in ("md5", "sha1", "sha256"):
            assert ("test_worker", algo, served[algo]) in pairs
        # both md5s resolve to the stored file for future listings
        for digest in ("5" * 32, served["md5"]):
            ok, match = _store.claim_source_md5(digest, "/elsewhere/q.png")
            assert ok is False and match["filepath"] == fp
        # claim released after the run
        assert _store.claimed_source_md5(fp) is None

    def test_failed_download_releases_claim(self, tmp_path, monkeypatch, _store):
        dl = _mk_dl(tmp_path, monkeypatch)
        self._mock_session(dl, 404, None)
        assert asyncio.run(self._enqueue(dl, md5="4" * 32)) is True
        self._drain_worker(dl)
        assert dl.failed_count == 1
        assert _store._conn.execute("SELECT COUNT(*) c FROM image_hashes").fetchone()["c"] == 0
        # not poisoned: a fresh enqueue with the same md5 goes through
        assert asyncio.run(self._enqueue(dl, md5="4" * 32, filename="retry.png")) is True

    def test_exception_releases_claim(self, tmp_path, monkeypatch, _store):
        dl = _mk_dl(tmp_path, monkeypatch)
        assert asyncio.run(self._enqueue(dl, md5="3" * 32)) is True
        fp = os.path.join(dl.site_root, "Tag", "shot.png")

        async def boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(dl, "_async_download_file", boom)

        def _run():
            dl.is_scanning = False
            asyncio.run(dl._download_worker())
        with pytest.raises(RuntimeError):
            _run()
        assert _store.claimed_source_md5(fp) is None

    def test_run_end_releases_orphaned_claims(self, tmp_path, monkeypatch, _store):
        # items a STOP orphaned before a worker dequeued them: run_async_loop's
        # finally calls _release_source_claims() with no argument
        dl = _mk_dl(tmp_path, monkeypatch)
        assert asyncio.run(self._enqueue(dl, md5="2" * 32)) is True
        fp = os.path.join(dl.site_root, "Tag", "shot.png")
        assert _store.claimed_source_md5(fp) == "2" * 32
        dl._release_source_claims()
        assert _store.claimed_source_md5(fp) is None
        assert asyncio.run(self._enqueue(dl, md5="2" * 32, filename="again.png")) is True


# --- adapters ---------------------------------------------------------------

class TestAdapters:
    def test_rule34_passes_result_hash(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
        import workers.rule34 as r34mod
        monkeypatch.setattr(r34mod, "pace_requests", lambda: asyncio.sleep(0))

        w = r34mod.Rule34Worker("rem", 1, "and", "id", "desc", [], {}, exclude_ai=False)
        w.log = lambda m: None

        class FakePost:
            image = "http://example.com/img.jpg"
            id = 12836964
            tags = []
            hash = "1bab3104189ed96ed8665843ffa38c5b"

        w.client = type("C", (), {"search": lambda self, *a, **k: [FakePost()]})()

        captured = []
        async def fake_enqueue(url, filepath, filename, tags_list, artists=None,
                               characters=None, copyrights=None, metadata_tags=None,
                               **kw):
            captured.append(kw)
            return True
        monkeypatch.setattr(w, "enqueue_download", fake_enqueue)

        asyncio.run(w.scraper_task())
        assert len(captured) == 1
        # the verified API field — NOT pHash (that name is a local var in the lib)
        assert captured[0]["source_md5"] == "1bab3104189ed96ed8665843ffa38c5b"

    def test_adapters_pass_expected_source_field(self):
        # cheap static net: every adapter that has a verified md5 field passes it
        expect = {
            "gelbooru.py": 'source_md5=post.get("md5")',
            "safebooru.py": 'source_md5=post.get("hash")',
            "yande.py": 'source_md5=post.get("md5")',
            "konachan.py": 'source_md5=post.get("md5")',
            "danbooru.py": 'source_md5=post.get("md5")',
            "rule34.py": 'source_md5=getattr(result, "hash", None)',
            "anime_dl.py": 'source_md5=detail.get("md5")',
            "gsbooru.py": 'source_md5=post.get("md5")',
            "sankaku.py": 'source_md5=post.get("md5")',
        }
        base = os.path.join(os.path.dirname(__file__), "..", "workers")
        for fname, needle in expect.items():
            with open(os.path.join(base, fname), encoding="utf-8") as f:
                src = f.read()
            assert needle in src, f"{fname} must pass {needle}"

    def test_backfill_is_wired_into_the_scan(self):
        # the rescan/startup pass is the only thing that runs the generation
        # for old installs — a dropped call would silently strand them
        with open(os.path.join(os.path.dirname(__file__), "..", "Rems_Dl.py"),
                  encoding="utf-8") as f:
            src = f.read()
        body = src.split("def _hash_missing_dedup(")[1].split("\ndef ")[0]
        assert "backfill_source_hashes()" in body

    def test_sankaku_custom_enqueue_releases_claim(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
        store = _mk_store(tmp_path, "sankaku.sqlite3")
        monkeypatch.setattr("core.dedup_store.get_store", lambda: store)
        from workers.sankaku import SankakuWorker
        w = SankakuWorker("rem", 10, "", [], {})
        w.log = lambda m: None
        w.download_queue = asyncio.Queue()

        async def fake_download(url, filepath, filename, tags_list, artists, file_size=0):
            # claim must still be held while the (immediate) download runs
            assert store.claimed_source_md5(filepath) is not None
            return True
        monkeypatch.setattr(w, "_async_download_file", fake_download)

        fp1 = os.path.join(w.site_root, "a.png")
        fp2 = os.path.join(w.site_root, "b.png")
        md5 = "6" * 32
        assert asyncio.run(w.enqueue_download("u", fp1, "a.png", [], source_md5=md5)) is True
        # while fp1's download ran the claim was held; afterwards it's released,
        # so a later enqueue of the same source (new filename) succeeds
        assert store.claimed_source_md5(fp1) is None
        assert asyncio.run(w.enqueue_download("u", fp2, "b.png", [], source_md5=md5)) is True

    def test_anime_dl_custom_download_persists_hashes(self, tmp_path, monkeypatch):
        monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
        store = _mk_store(tmp_path, "anime.sqlite3")
        monkeypatch.setattr("core.dedup_store.get_store", lambda: store)
        import workers.anime_dl as adm
        monkeypatch.setattr(adm, "write_image_metadata", lambda *a, **k: None)
        monkeypatch.setattr(adm, "add_to_gallery", lambda *a, **k: None)
        monkeypatch.setattr(adm, "send_tags", lambda *a, **k: None)
        monkeypatch.setattr(adm, "save_history", lambda *a, **k: None)
        from workers.anime_dl import AnimeDlWorker
        w = AnimeDlWorker("rem", 10, {})
        w.log = lambda m: None
        w.download_queue = asyncio.Queue()

        payload = _img_bytes(30)
        mid = len(payload) // 2
        class Resp:
            status_code = 200
            headers = {"Content-Length": str(len(payload))}
            def iter_content(self):
                yield payload[:mid]
                yield payload[mid:]
        w.curl_session = type("CS", (), {"get": lambda self, *a, **k: Resp()})()

        md5 = "a1b2c3d4" + "0" * 24
        fp = os.path.join(w.site_root, "img.png")
        assert asyncio.run(w.enqueue_download(
            "http://example.com/img.png", fp, "img.png", [], source_md5=md5)) is True

        seen = []
        w._emit_progress = lambda: seen.append(dict(w._inflight))
        async def _run():
            w.is_scanning = False
            await w._download_worker()
        asyncio.run(_run())
        assert w.downloaded_count == 1
        # live progress: a mid-stream emit reported a partial fraction for this file
        assert any(k == fp and 0 < v <= 1 for snap in seen for k, v in snap.items())
        assert not os.path.exists(fp + ".part")
        with open(fp, "rb") as f:
            assert f.read() == payload
        served = hash_bytes(payload)
        pairs = _pairs(store, fp)
        assert (w.name, "md5", md5) in pairs
        for algo in ("md5", "sha1", "sha256"):
            assert (w.name, algo, served[algo]) in pairs

    def test_zerochan_custom_download_persists_hashes(self, tmp_path, monkeypatch):
        # zerochan's API has no hash field: the served bytes' formats ARE its
        # site hashes and must be recorded from the custom download path
        monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
        store = _mk_store(tmp_path, "zerochan.sqlite3")
        monkeypatch.setattr("core.dedup_store.get_store", lambda: store)
        import workers.zerochan as zmod
        monkeypatch.setattr(zmod, "write_image_metadata", lambda *a, **k: None)
        monkeypatch.setattr(zmod, "add_to_gallery", lambda *a, **k: None)
        monkeypatch.setattr(zmod, "send_tags", lambda *a, **k: None)
        monkeypatch.setattr(zmod, "save_history", lambda *a, **k: None)

        w = zmod.ZerochanWorker("rem", 10, {})
        w.log = lambda m: None

        payload = _img_bytes(31)
        class Resp:
            status_code = 200
            content = payload
            headers = {"content-type": "image/png"}

        class CS:
            def get(self, *a, **k): return Resp()
        monkeypatch.setattr(w, "_ensure_curl_session", lambda: CS())
        monkeypatch.setattr(w, "_verify_flag", lambda: True)

        fp = os.path.join(w.site_root, "Tag", "shot.png")
        assert asyncio.run(w._async_download_file(
            "http://example.com/x.png", fp, "shot.png", [], [])) is True
        served = hash_bytes(payload)
        pairs = _pairs(store, fp)
        for algo in ("md5", "sha1", "sha256"):
            assert (w.name, algo, served[algo]) in pairs

    def test_check_duplicate_resolves_claimed_md5(self, tmp_path, monkeypatch):
        # shared pipeline: the post-download check picks up the md5 claimed at
        # enqueue without the queue tuple having to carry it
        store = _mk_store(tmp_path, "resolve.sqlite3")
        monkeypatch.setattr("core.dedup_store.get_store", lambda: store)
        p1 = os.path.join(tmp_path, "x.png")
        _mk_img(p1, 40)
        md5 = "ab" * 16
        assert store.claim_source_md5(md5, p1)[0] is True
        res = check_duplicate(p1, "site")
        assert res is not None and not res.is_duplicate
        assert ("site", "md5", md5) in _pairs(store, p1)

    def test_check_duplicate_passes_generated_hashes(self, tmp_path, monkeypatch):
        store = _mk_store(tmp_path, "generated.sqlite3")
        monkeypatch.setattr("core.dedup_store.get_store", lambda: store)
        p1 = os.path.join(tmp_path, "y.png")
        _mk_img(p1, 41)
        with open(p1, "rb") as f:
            served = hash_bytes(f.read())
        res = check_duplicate(p1, "site", generated_hashes=served)
        assert res is not None and not res.is_duplicate
        pairs = _pairs(store, p1)
        for algo in ("md5", "sha1", "sha256"):
            assert ("site", algo, served[algo]) in pairs
