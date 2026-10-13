"""SQLite store for the gallery index and the image history.

gallery.json / image_history.json held every entry parsed in RAM for the whole
process life, so memory scaled with the library (measured ~1.5MB per 10k
history entries; ~600MB resident at a ~90k-download install). Entries now live
in SQLite and only the lightweight columns stay resident.

TAG SAFETY (the whole point of the split):

Tags live in their own table, `gallery_tags`, written *only* by the explicit
tag-writing entry points below (`insert_entry`, `set_tags`, `merge_tags`,
`history_insert`, `set_history_tags`). A filepath / favourite / meta_mtime
update writes that table not at all, so a stale or tagless in-memory entry
structurally cannot wipe tags the way a whole-record rewrite used to.

The original JSON files are renamed to *.migrated, never deleted, and
migration verifies every entry's tags round-trip before it commits — on any
mismatch it raises and leaves the JSON in place to be retried.
"""
import json
import os
import sqlite3
import sys
import threading

TAG_BUCKETS = (
    "tag", "artist", "character", "copyright",
    "metadata", "outfit", "group", "hair", "eyes",
)

# Resident columns: everything except tags. `sources`, `meta_mtime` and
# `md5_misses` are sparse/small and read on the hot paths; tags are ~72% of
# the bytes and are the only thing we keep off the heap.
ENTRY_FIELDS = (
    "id", "filename", "filepath", "site", "favourite",
    "downloaded_at", "rating", "sources", "meta_mtime", "md5_misses",
)


def _app_base_dir():
    # core/ lives one level below the repo root — go up one more.
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


BASE_DIR = _app_base_dir()
DATABASE_DIR = os.path.join(BASE_DIR, "database")
GALLERY_DB_FILE = os.path.join(DATABASE_DIR, "gallery.db")
GALLERY_JSON_FILE = os.path.join(DATABASE_DIR, "gallery.json")
HISTORY_JSON_FILE = os.path.join(DATABASE_DIR, "image_history.json")


class MigrationError(RuntimeError):
    """gallery.json did not round-trip into SQLite — JSON left untouched."""


def _normalize_tags(tags):
    """Accept every historical tag shape: categorized dict, or a legacy flat list."""
    if isinstance(tags, dict):
        return tags
    if isinstance(tags, (list, tuple, set)):
        return {"tag": [str(t) for t in tags]}
    return {}


def _tags_fingerprint(tags):
    """Order- and duplicate-insensitive identity for an entry's tags.

    Buckets compare as sets: list order carries no meaning and one image
    carries each tag once (repeats are deduped on write), so a reordered or
    de-duplicated bucket is the same tag set — only a genuinely missing tag
    changes this.
    """
    tags = _normalize_tags(tags)
    return json.dumps(
        {b: sorted({str(t) for t in (tags.get(b) or [])}) for b in TAG_BUCKETS
         if tags.get(b)},
        sort_keys=True, separators=(",", ":"),
    )


def _tags_from_row(rows):
    """[(bucket, tag)] -> {bucket: [tag]}"""
    out = {}
    for bucket, tag in rows:
        out.setdefault(bucket, []).append(tag)
    return out


def _entry_from_row(row):
    """sqlite row -> the entry shape callers expect (sources is stored as JSON)."""
    d = dict(row)
    d["favourite"] = bool(d.get("favourite"))
    src = d.get("sources")
    if isinstance(src, str):
        try:
            d["sources"] = json.loads(src)
        except ValueError:
            d["sources"] = None
    return d


class GalleryStore:
    """Thread-safe SQLite-backed gallery + image history."""

    def __init__(self, db_path=None, gallery_json=None, history_json=None):
        # Resolved here, not in the signature: Python binds def-arg defaults
        # when the module is imported, so the test harness redirecting these
        # constants afterwards would never be seen — and a test would build
        # its store on top of the app's real data.
        self.db_path = db_path or GALLERY_DB_FILE
        self.gallery_json = gallery_json or GALLERY_JSON_FILE
        self.history_json = history_json or HISTORY_JSON_FILE
        self._lock = threading.RLock()
        self._conn = None
        self._migrated = False
        # search index, None until first use and invalidated on every tag
        # write: normalized-tag (lower + '_'->space) -> gallery ids.
        # See _norm_tag_ids.
        self._norm_idx = None

    # --- connection / schema -------------------------------------------------

    def _connect(self):
        with self._lock:
            if self._conn is not None:
                if self._norm_idx is not None and self._conn.in_transaction:
                    # an open write transaction means the tag table is being
                    # mutated; the index is derived state, so mark it dirty
                    # and let the next search pay the one-off rebuild. Doing
                    # this here rather than at each write site keeps the rule
                    # to one place, so a future write path cannot silently
                    # ship a stale index.
                    self._norm_idx = None
                return self._conn
            os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL;")
            conn.execute("PRAGMA synchronous = NORMAL;")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gallery (
                    id TEXT PRIMARY KEY,
                    filename TEXT NOT NULL,
                    filepath TEXT,
                    site TEXT,
                    favourite INTEGER NOT NULL DEFAULT 0,
                    downloaded_at TEXT,
                    rating TEXT,
                    sources TEXT,
                    meta_mtime REAL,
                    md5_misses INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_gallery_filename ON gallery(filename)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_gallery_ts ON gallery(downloaded_at)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_gallery_site ON gallery(site)")
            # Tags are isolated here on purpose — see module docstring.
            # Uniqueness dedups repeated tags inside a bucket ('brown_hair'
            # twice), which is desired: one image carries each tag once.
            conn.execute("""
                CREATE TABLE IF NOT EXISTS gallery_tags (
                    gallery_id TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    tag TEXT NOT NULL,
                    PRIMARY KEY (gallery_id, bucket, tag)
                ) WITHOUT ROWID
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_gt_tag ON gallery_tags(tag)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS image_history (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    site TEXT,
                    filename TEXT,
                    filepath TEXT,
                    tags TEXT,
                    downloaded_at REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_hist_filename ON image_history(filename)")
            conn.commit()
            try:
                self._migrate_if_needed(conn)
            except Exception:
                # do not cache a connection to an unverified store: a later
                # call would silently serve an empty gallery, which looks
                # exactly like data loss. Fail loudly every time instead —
                # the source JSON is still on disk, untouched.
                self._conn = None
                try:
                    conn.close()
                except Exception:
                    pass
                raise
            self._conn = conn
            return conn

    # --- one-time JSON -> SQLite migration -----------------------------------

    def _migrate_if_needed(self, conn):
        """Import the JSON stores once, verifying tags round-trip."""
        with self._lock:
            have = conn.execute("SELECT COUNT(*) FROM gallery").fetchone()[0]
            if have:
                return
            src = self.gallery_json
            if not os.path.exists(src):
                return
            try:
                with open(src, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                # leave the file alone — a corrupt/absent source must not
                # silently become an empty gallery
                return
            entries = data.get("images", []) if isinstance(data, dict) else []

            # 1) verify every entry's tags would round-trip before writing
            expected = {}
            for e in entries:
                key = e.get("id") or e.get("filename")
                if key is None:
                    continue
                expected[key] = _tags_fingerprint(e.get("tags"))

            # one implicit transaction: python's sqlite3 auto-begins on DML, so
            # rollback() below undoes the whole import if verification fails
            try:
                for e in entries:
                    self._insert_entry_locked(conn, e)
                # 2) read back and compare
                got = self._fingerprints_locked(conn)
                bad = [k for k, fp in expected.items() if got.get(k) != fp]
                if bad:
                    raise MigrationError(
                        "%d entries did not round-trip their tags (first: %s)"
                        % (len(bad), bad[:3]))
                self._import_history_locked(conn)
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            # 3) only now retire the JSON — kept, never deleted
            for path, suffix in ((self.gallery_json, ".migrated"),
                                 (self.history_json, ".migrated")):
                try:
                    if os.path.exists(path):
                        os.replace(path, path + suffix)
                except OSError:
                    pass

    def _fingerprints_locked(self, conn):
        out = {}
        cur = conn.execute("SELECT gallery_id, bucket, tag FROM gallery_tags")
        tmp = {}
        for row in cur:
            tmp.setdefault(row["gallery_id"], []).append((row["bucket"], row["tag"]))
        for gid, rows in tmp.items():
            out[gid] = _tags_fingerprint(_tags_from_row(rows))
        # entries with no tags at all still need a key
        for row in conn.execute("SELECT id FROM gallery"):
            out.setdefault(row["id"], _tags_fingerprint({}))
        return out

    def _insert_entry_locked(self, conn, e):
        fn = e.get("filename")
        if not fn:
            return
        gid = self._upsert_fields_locked(conn, e)
        self._set_tags_locked(conn, gid, e.get("tags"))

    def _set_tags_locked(self, conn, gid, tags):
        """Replace one entry's tags. Only ever called with real tag data."""
        conn.execute("DELETE FROM gallery_tags WHERE gallery_id = ?", (gid,))
        tags = _normalize_tags(tags)
        # dirty-marked here as well as in _connect: a batch may have built
        # the index mid-transaction, and that snapshot cannot describe the
        # rows this statement is about to change
        self._norm_idx = None
        if not tags:
            return
        rows = []
        for bucket, vals in tags.items():
            if not isinstance(vals, (list, tuple, set)):
                continue
            rows.extend((gid, str(bucket), str(v)) for v in vals if v)
        if rows:
            conn.executemany(
                "INSERT OR IGNORE INTO gallery_tags (gallery_id, bucket, tag) VALUES (?,?,?)",
                rows)

    def _import_history_locked(self, conn):
        if not os.path.exists(self.history_json):
            return
        try:
            with open(self.history_json, "r", encoding="utf-8") as f:
                hist = json.load(f)
        except Exception:
            return
        rows = [(h.get("site"), h.get("filename"), h.get("filepath"),
                 json.dumps(h.get("tags") or {}), h.get("downloaded_at"))
                for h in hist if isinstance(h, dict)]
        if rows:
            conn.executemany(
                """INSERT INTO image_history
                   (site, filename, filepath, tags, downloaded_at)
                   VALUES (?,?,?,?,?)""", rows)

    # --- gallery: reads ------------------------------------------------------

    def count(self):
        return self._connect().execute("SELECT COUNT(*) FROM gallery").fetchone()[0]

    def lightweight_entries(self):
        """Every entry WITHOUT tags — this is what stays resident."""
        conn = self._connect()
        cols = ", ".join(ENTRY_FIELDS)
        out = []
        for row in conn.execute("SELECT %s FROM gallery" % cols):
            out.append(_entry_from_row(row))
        return out

    def chunk_with_tags(self, offset, limit):
        """A slice of full entries (tags included) for chunked filtering.

        Filtering is elementwise, so callers can walk the whole gallery one
        chunk at a time and never hold more than `limit` entries' tags.
        """
        conn = self._connect()
        cols = ", ".join("g.%s" % c for c in ENTRY_FIELDS)
        rows = conn.execute(
            "SELECT %s FROM gallery g ORDER BY g.rowid LIMIT ? OFFSET ?"
            % cols, (limit, offset)).fetchall()
        if not rows:
            return []
        ids = [r["id"] for r in rows]
        tagmap = self._tags_for_ids(conn, ids)
        out = []
        for r in rows:
            d = _entry_from_row(r)
            d["tags"] = tagmap.get(d["id"], {})
            out.append(d)
        return out

    def _tags_for_ids(self, conn, ids):
        if not ids:
            return {}
        marks = ",".join("?" * len(ids))
        cur = conn.execute(
            "SELECT gallery_id, bucket, tag FROM gallery_tags WHERE gallery_id IN (%s)"
            % marks, ids)
        out = {}
        for row in cur:
            out.setdefault(row["gallery_id"], []).append((row["bucket"], row["tag"]))
        return {k: _tags_from_row(v) for k, v in out.items()}

    def get_entry(self, gid):
        """One full entry (tags included) by id."""
        conn = self._connect()
        cols = ", ".join(ENTRY_FIELDS)
        row = conn.execute("SELECT %s FROM gallery WHERE id = ?" % cols, (gid,)).fetchone()
        if row is None:
            return None
        d = _entry_from_row(row)
        d["tags"] = self._tags_for_ids(conn, [gid]).get(gid, {})
        return d

    def get_tags(self, gid):
        """One entry's tags, or {} — the recovery/compare path."""
        conn = self._connect()
        cur = conn.execute(
            "SELECT bucket, tag FROM gallery_tags WHERE gallery_id = ?", (gid,))
        return _tags_from_row([(r["bucket"], r["tag"]) for r in cur])

    def filenames(self):
        return {r["filename"] for r in
                self._connect().execute("SELECT filename FROM gallery")}

    def favourite_filenames(self):
        return {r["filename"] for r in self._connect().execute(
            "SELECT filename FROM gallery WHERE favourite = 1")}

    def all_entries_with_tags(self):
        """Full entries — offline scripts only; this is the resident-RAM path."""
        conn = self._connect()
        cols = ", ".join(ENTRY_FIELDS)
        rows = conn.execute("SELECT %s FROM gallery ORDER BY rowid" % cols).fetchall()
        tagmap = self._tags_for_ids(conn, [r["id"] for r in rows])
        out = []
        for r in rows:
            d = _entry_from_row(r)
            d["tags"] = tagmap.get(d["id"], {})
            out.append(d)
        return out

    # --- gallery: writes -----------------------------------------------------

    def _upsert_fields_locked(self, conn, e):
        """Write the non-tag columns. tags are not reachable from here."""
        fn = e.get("filename")
        if not fn:
            return None
        gid = e.get("id") or fn
        # INSERT OR REPLACE rewrites only this table — gallery_tags, keyed by
        # the same id, is untouched, which is what keeps a tagless entry from
        # ever clearing the tags that are stored for it.
        conn.execute(
            """INSERT OR REPLACE INTO gallery
               (id, filename, filepath, site, favourite, downloaded_at,
                rating, sources, meta_mtime, md5_misses)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (gid, fn, e.get("filepath"), e.get("site"),
             1 if e.get("favourite") else 0, e.get("downloaded_at"),
             e.get("rating"),
             json.dumps(e.get("sources")) if e.get("sources") is not None else None,
             e.get("meta_mtime"), e.get("md5_misses")))
        return gid

    def save_entries(self, entries):
        """Full-replace save (what save_gallery does).

        Tags are written ONLY for entries carrying a `tags` key; an entry
        without one updates just its own columns. Rows missing from
        `entries` are pruned along with their tags — that is how a rescan
        deletes entries whose file is gone.
        """
        with self._lock:
            conn = self._connect()
            present = set()
            for e in entries:
                gid = self._upsert_fields_locked(conn, e)
                if gid is None:
                    continue
                present.add(gid)
                if "tags" in e:
                    self._set_tags_locked(conn, gid, e["tags"])
            stale = [r["id"] for r in conn.execute("SELECT id FROM gallery")
                     if r["id"] not in present]
            for gid in stale:
                conn.execute("DELETE FROM gallery_tags WHERE gallery_id = ?", (gid,))
                conn.execute("DELETE FROM gallery WHERE id = ?", (gid,))
            conn.commit()
            return len(stale)

    def get_tags_many(self, ids):
        """{id: tags} for a batch — the on-demand tag read for filtering."""
        conn = self._connect()
        return self._tags_for_ids(conn, list(ids))

    def _norm_tag_ids(self, conn, term, exact=False):
        """Gallery ids whose stored tag equals/contains `term`.

        Normalizing inside the WHERE — `TRIM(REPLACE(LOWER(tag), '_', ' '))`
        — hid the tag column from SQLite and forced a 547k-row scan per term
        (~130ms, measured on a 56MB library). Instead the whole table is
        normalized once into a keyed index, which costs ~770ms the first time
        a search runs after startup (or after a write invalidates it) and
        nothing after: every term after that is a 1-2ms dict scan. Measured
        2-term query: 237ms -> 1.6ms.

        547k rows collapse to ~20k distinct tags, so the index is small
        enough to stay resident; the fetch dominates the build (355ms of the
        770ms) because sqlite has to hand 547k rows over one at a time.

        The term is normalized identically, so 'blue hair' and 'blue_hair'
        are the same query — which is what _parse_search_terms relies on when
        it rewrites the term's underscores to spaces before we ever see it.
        """
        norm = self._norm_idx
        if norm is None:
            # built in python, not SQL: `SELECT DISTINCT LOWER(...)` groups
            # inside sqlite but still ships every row out one at a time
            # (measured 504ms here, against 552ms fetching and grouping in
            # python), so the DISTINCT buys nothing
            norm = {}
            for key, gid in conn.execute(
                    "SELECT DISTINCT LOWER(REPLACE(tag, '_', ' ')), "
                    "gallery_id FROM gallery_tags"):
                norm.setdefault(key, set()).add(gid)
            self._norm_idx = norm
        needle = term.lower().replace("_", " ")
        if exact:
            return norm.get(needle, set())
        if not norm:
            return set()
        return set().union(*(norm[key] for key in norm if needle in key))

    def ids_matching_tags(self, term, exact=False):
        """Ids whose stored tag equals/contains `term` — the search side of a
        tag filter.

        Tags are stored underscored and mixed-case while the UI filter
        normalizes both sides, so the same normalization runs here: the two
        must agree exactly or a search silently changes results.
        """
        return self._norm_tag_ids(self._connect(), term, exact)

    def all_ids(self):
        """Every gallery id — the starting set for an all-negation search."""
        return {r[0] for r in self._connect().execute("SELECT id FROM gallery")}

    def insert_entry(self, record):
        with self._lock:
            conn = self._connect()
            self._insert_entry_locked(conn, record)
            conn.commit()

    def update_fields(self, gid, fields):
        """Update non-tag columns only — tags are not reachable from here."""
        cols = [c for c in ENTRY_FIELDS if c in fields and c != "id"]
        if not cols:
            return
        with self._lock:
            conn = self._connect()
            # sources is a list in the entry dicts and JSON text in the row —
            # passing it raw is a "type 'list' is not supported" bind error
            vals = [json.dumps(fields[c]) if c == "sources" and fields[c] is not None
                    else fields[c] for c in cols]
            conn.execute(
                "UPDATE gallery SET %s WHERE id = ?"
                % ", ".join("%s = ?" % c for c in cols),
                vals + [gid])
            conn.commit()

    def set_tags(self, gid, tags):
        with self._lock:
            conn = self._connect()
            self._set_tags_locked(conn, gid, tags)
            conn.commit()

    def set_favourite(self, ids, value):
        ids = list(ids)
        if not ids:
            return 0
        with self._lock:
            conn = self._connect()
            marks = ",".join("?" * len(ids))
            cur = conn.execute(
                "UPDATE gallery SET favourite = ? WHERE id IN (%s)" % marks,
                [1 if value else 0] + ids)
            conn.commit()
            return cur.rowcount

    def delete(self, gid):
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM gallery_tags WHERE gallery_id = ?", (gid,))
            conn.execute("DELETE FROM gallery WHERE id = ?", (gid,))
            conn.commit()

    def delete_by_filename(self, filename):
        with self._lock:
            conn = self._connect()
            gids = [r["id"] for r in conn.execute(
                "SELECT id FROM gallery WHERE filename = ?", (filename,))]
            for gid in gids:
                conn.execute("DELETE FROM gallery_tags WHERE gallery_id = ?", (gid,))
                conn.execute("DELETE FROM gallery WHERE id = ?", (gid,))
            conn.commit()
            return bool(gids)

    def replace_all(self, entries):
        """Wholesale rewrite (offline scripts). Writes tags from real data."""
        with self._lock:
            conn = self._connect()
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM gallery_tags")
                conn.execute("DELETE FROM gallery")
                for e in entries:
                    self._insert_entry_locked(conn, e)
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    # --- image history -------------------------------------------------------

    def history_all(self):
        conn = self._connect()
        out = []
        for row in conn.execute(
                "SELECT site, filename, filepath, tags, downloaded_at "
                "FROM image_history ORDER BY seq DESC"):
            d = dict(row)
            try:
                d["tags"] = json.loads(d.get("tags") or "{}")
            except Exception:
                d["tags"] = {}
            out.append(d)
        return out

    def history_insert(self, entry):
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                """INSERT INTO image_history
                   (site, filename, filepath, tags, downloaded_at)
                   VALUES (?,?,?,?,?)""",
                (entry.get("site"), entry.get("filename"), entry.get("filepath"),
                 json.dumps(entry.get("tags") or {}), entry.get("downloaded_at")))
            conn.commit()
            return cur.lastrowid

    def history_delete(self, filename):
        with self._lock:
            conn = self._connect()
            cur = conn.execute("DELETE FROM image_history WHERE filename = ?", (filename,))
            conn.commit()
            return cur.rowcount

    def history_clear(self):
        with self._lock:
            conn = self._connect()
            conn.execute("DELETE FROM image_history")
            conn.commit()

    def history_replace(self, entries):
        """Rewrite every history row, preserving the caller's list order.

        seq is assigned backwards so `ORDER BY seq DESC` reads back exactly
        what was passed: load(save(x)) == x, and a category heal cannot
        reorder someone's history.
        """
        with self._lock:
            conn = self._connect()
            n = len(entries)
            conn.execute("BEGIN")
            try:
                conn.execute("DELETE FROM image_history")
                conn.executemany(
                    """INSERT INTO image_history
                       (seq, site, filename, filepath, tags, downloaded_at)
                       VALUES (?,?,?,?,?,?)""",
                    [(n - i, e.get("site"), e.get("filename"), e.get("filepath"),
                      json.dumps(e.get("tags") or {}), e.get("downloaded_at"))
                     for i, e in enumerate(entries)])
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def close(self):
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._norm_idx = None


_store = None
_store_lock = threading.Lock()


def get_store():
    """Process-wide store singleton (one connection, shared across threads)."""
    global _store
    with _store_lock:
        if _store is None:
            _store = GalleryStore()
        return _store


def use_store_path(db_path=None):
    """Repoint the singleton at `db_path` (None = back to the default).

    Only for tests: it closes the live connection, so the next get_store()
    migrates from `db_path`'s sibling gallery.json if one is there.
    """
    global _store
    with _store_lock:
        if _store is not None:
            _store.close()
        _store = None
        if db_path is None:
            return None
        _store = GalleryStore(db_path=db_path,
                              gallery_json=os.path.join(os.path.dirname(db_path),
                                                        "gallery.json"))
        return _store
