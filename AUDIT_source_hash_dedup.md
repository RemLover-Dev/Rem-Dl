# Audit Report: Cross-Site Source-Hash Pre-Download Deduplication

Investigation only — no source code, schema, config, tests, or docs were modified.
Evidence: code reads (file:line cited), two read-only explorer passes, and runtime API probes run **2026-10-14 through `socks5h://127.0.0.1:10808`** with credentials referenced **by `.env` key name only** (values never printed).
Repo state at audit time: branch `tests`, commit `90249bb`, working tree clean.

---

## A. Executive summary

Rems-Dl has **two separate dedup layers**, both running *after* the image bytes are already on disk, plus one module that already does source-hash API lookups — but **nothing stores a source-provided hash, and no adapter ever reads one**.

1. **Filename/name history** — per-site `download_history.json` sets (`core/shared.py:1070-1087`) checked at enqueue (`core/shared.py:1307`). Pre-download, per-site only, filename-keyed.
2. **Perceptual dedup** — `core/dedup_store.py`, a SQLite store (`database/dedup.sqlite3`) of phash/dhash/ahash/whash + color histograms, checked *after* the file is fully downloaded (`core/shared.py:1411` → `dedup_store.check_and_add` `core/dedup_store.py:476`). Cross-site and cross-session, but requires fetching the file first.
3. **`core/md5_refetch.py`** — already proves the *reverse* direction works: it hashes a local file (or reads md5 from the filename), queries source APIs by `md5:`/`id:`, and recovers tags. It does **not** persist the hash, and only supports 5 sites (`_query`, `core/md5_refetch.py:48-77`).

The key enabler: **9 of 18 adapters' listing responses already contain the source hash for free** (runtime-verified — gelbooru `md5`, rule34 `hash`, safebooru `hash`, yande `md5`, konachan `md5`, danbooru `md5`, sankaku `md5`, anime-pictures `md5`, gsbooru `md5`). The adapters simply never read it (grep of `workers/` for md5/hash keys matches only pixiv's auth header `workers/pixiv.py:85`). So the hash can be captured with a one-line pass-through per adapter at the existing enqueue call — **zero extra API requests** for those sites — and checked at `enqueue_download`, before any image transfer.

A critical byte-level fact shapes the design: **downloaded files are mutated after hashing**. `write_image_metadata` injects a COM/tEXt segment at byte level (`core/shared.py:803-861`), so hashing the local file later does *not* reproduce the source md5 (this is also why `md5_of` prefers the 32-hex filename stem over file bytes, `core/md5_refetch.py:263-266`). **The source hash must be captured from the API response/filename at discovery time, never recomputed from disk.**

---

## B. Codebase architecture

### Download engine (all paths funnel here unless noted)

| Symbol | Location | Role |
|---|---|---|
| `_dispatch_worker(data)` | `Rems_Dl.py:2742-2800` | One site job per thread; if/elif chain on raw site id; injects credentials from env into `net_config` (zerochan `:2766`, pinterest `:2782`, pixiv `:2787`) |
| `BaseDownloader.__init__` | `core/shared.py:1192-1238` | Per-site: `dl_history = load_history(site_root)` (`:1210`), `queued_items = set()` (`:1222`), `_inflight = {}` (`:1225`), `STOP_EVENTS[name]` replaced not appended (`:1202`), retries from `download_retries` (`:1205`) |
| `enqueue_download` | `core/shared.py:1296-1322` | Sanitizes filename; **skip if `filename ∈ dl_history ∨ ∈ queued_items ∨ os.path.exists(filepath)`** (`:1307-1311`); claims name in `queued_items` (`:1319`); `queue.put_nowait(tuple)` (`:1320`) — 13-field tuple: `(url, filepath, filename, tags_list, artists, file_size, characters, copyrights, metadata_tags, outfits, groups, hair, eyes, rating)` |
| `run_async_loop` | `core/shared.py:1500-1563` | Creates queue, spawns **4** `_download_worker` tasks (`:1507-1508`), scraper phase, then joins queue with stop-aware polling (`:1525-1530`); finally flushes gallery/history (`:1542-1558`) |
| `_download_worker` | `core/shared.py:1480-1490` | Claims tuple → `await _async_download_file(*item)`; `finally` pops `_inflight[filepath]` and `task_done()` |
| `_async_download_file` | `core/shared.py:1324-1478` | The actual transfer — see trace §E |
| `check_duplicate(filepath, site, post_id=None)` | `core/shared.py:1095-1116` | Reads `dedup_enabled` setting (`:1101-1103`), skips container extensions (`:1093`, `:1107-1109`), delegates to `DedupStore.check_and_add` (`:1113`) |
| `add_to_gallery` | `core/shared.py:543-589` | Under `_GALLERY_LOCK`; upsert-by-filename or insert record `id = md5(f"{site}:{filename}")[:12]` (`:575`), site canonicalized (`:578`) |
| `absorb_duplicate` | `core/shared.py:592-648` | On a phash kill: unions the killed copy's tags into the surviving record and appends its site to `sources` (`:631-644`) |
| `_remember_filename` | `core/shared.py:1285-1294` | Adds phash-killed names to `dl_history` too (so they're never re-fetched) |
| History I/O | `core/shared.py:1070-1087`, `HISTORY_LOCK`; flushed every 10 writes + worker end (`:1431-1434`, `:1549-1551`) |

**Custom paths that bypass the base flow** (each calls `check_duplicate` itself):
- `workers/zerochan.py:684` overrides `enqueue_download` (10-field tuple) and `:713` overrides `_async_download_file` — **no `super()` calls**; duplicated logic.
- `workers/sankaku.py:78` overrides `enqueue_download` only (base `_async_download_file`).
- `workers/anime_dl.py:73` overrides `_async_download_file` with the full base signature.
- `workers/pinterest_worker.py` downloads directly in `scraper_task` (no enqueue; `check_duplicate` at `:263`).
- Post-download `check_duplicate` calls with `post_id`: `workers/pixiv.py:425` (`work_id`), `workers/pinterest_worker.py:263` (`media.id`), `workers/anime_dl.py:108`, `workers/zerochan.py:769` (no post_id). The base path passes **no** `post_id` (`core/shared.py:1411`).

### Perceptual dedup store — `core/dedup_store.py`

- `compute_signature(path)` `:260` — pure-Pillow phash/dhash/ahash/whash + global/grid histograms (`:117-258`); `Signature`/`DedupResult` dataclasses `:73-106`.
- `_is_full_duplicate(a, b)` `:296` — structural hashes **AND** color profiles both similar (never a single-hash compare).
- `check_and_add` `:476-500` — computes signature, then holds `self._lock` **across find+add** (comment `:487-489`: releasing it would let two threads both pass).
- `find_duplicate` `:429-457` — in-memory phash XOR prefilter (`_candidate_ids` `:406`, index loaded at `:395`), then row compare; **ghost-prunes rows whose file is gone** (`:447-455`, `_prune_ids` `:416`).
- `add` `:459-474` — plain INSERT (no uniqueness constraint on filepath or anything else).
- `remove_by_filepath` `:526` — called by the delete route `Rems_Dl.py:2236-2237`.

### Source-hash module — `core/md5_refetch.py`

- `_query(site, md5, post_id)` `:48-77` — builds per-site lookup: gelbooru dapi (+`api_key`/`user_id` from env `:54-56`), safebooru dapi, yande/konachan `post.json`, danbooru `posts.json` with `id:` from the filename (`:65-72`, `+api_key`); returns `None` for everything else (gsbooru comment `:73-76`).
- `supports(site)` `:213`, `worth_trying` `:226`, `is_bare` `:252`, `miss_count` `:219`, `MISS_LIMIT = 2` `:104`, `clear_stale_md5_misses` `:235`.
- `_get` `:115-157` — curl_cffi, browser-impersonation (except danbooru honest API UA `:130-134`), proxy-first/direct-second (`:124-128`), global `pace_wait()` (`:137`), host marked dead after both routes fail (`_dead_hosts` `:80`, `HostDead` `:111`), empty-200-as-`[]` for safebooru (`:147-152`).
- `md5_of(img)` `:263-274` — **32-hex filename stem first, local bytes second**.
- `_is_ours` `:170-182` — verifies `post["md5"] or post["hash"]` (safebooru) equals the expected hash; danbooru re-encode caveat.
- `_upgrade` `:288-303` — replaces re-encoded copies, md5-verified before `os.replace`.
- Entry points: app startup self-heal `Rems_Dl.py:2924-2957`; CLI `main()` `:424`.
- **What it does NOT include:** sankaku, rule34, anime-pictures — all three *can* do id→hash lookups (runtime-verified, §D), so `_query` has room to grow; but for this feature the listing already carries the hash, so no lookup is needed.

### Gallery persistence — `core/gallery_store.py` (see §C)

`insert_entry` `:569`, `update_fields` `:575`, `save_entries` (full-replace, prunes absent ids) `:454-483`, `replace_all` (explicit `BEGIN`/`COMMIT` `:638-644`), `delete`/`delete_by_filename` `:611-629`, search index `:490-551`.

### Rescan / healing — `Rems_Dl.py`

`_scan_and_merge_gallery` `:2263-2310` (folder walk; adds placeholders, fixes `filepath`, reads embedded metadata once per mtime), `/api/gallery/rescan` `:2380`, `startup_rescan` `:2969`, warm-up thread `:3017-3056`. Delete route removes gallery row + image_history + dedup row (`:2204-2241`).

---

## C. Current database schema

### `database/gallery.db` — SQLite, WAL, `synchronous=NORMAL` (`core/gallery_store.py:151-154`), one shared connection `check_same_thread=False, timeout=30`, guarded by store-internal `threading.RLock` **and** a process-level `_GALLERY_LOCK` (`core/shared.py:405`) around the resident cache.

```sql
gallery (            id TEXT PRIMARY KEY,          -- md5("{site}:{filename}")[:12]  (shared.py:575)
                     filename TEXT NOT NULL,       -- indexed idx_gallery_filename (169)
                     filepath TEXT,                -- relative to MASTER_FOLDER
                     site TEXT,                    -- indexed (171), canonicalized at read (shared.py:357-366)
                     favourite INTEGER DEFAULT 0,
                     downloaded_at TEXT,           -- indexed (170)
                     rating TEXT, sources TEXT,    -- sources = JSON list of canonical sites (absorb_duplicate)
                     meta_mtime REAL, md5_misses INTEGER )
gallery_tags (       gallery_id, bucket, tag, PRIMARY KEY(gallery_id,bucket,tag) WITHOUT ROWID, idx_gt_tag )
image_history (      seq INTEGER PK AUTOINCREMENT, site, filename, filepath, tags TEXT, downloaded_at REAL,
                     idx_hist_filename )           -- one row per successful download (DatabaseManager.add_image_history, core/database.py:172-184)
```

Schema DDL: `core/gallery_store.py:155-194`. Non-tag columns enumerated by `ENTRY_FIELDS` (`:34-37`); `update_fields` builds `UPDATE gallery SET <cols>` from that tuple (`:575-590`); `_upsert_fields_locked` hardcodes the 10-column `INSERT OR REPLACE` (`:433-452`). **No UNIQUE constraints beyond the PKs. No foreign keys.** Tags are deliberately stored in a separate table (module docstring).

### `database/dedup.sqlite3` — SQLite, WAL (`core/dedup_store.py:373`)

```sql
image_hashes ( id INTEGER PK AUTOINCREMENT, filepath TEXT NOT NULL,   -- ABSOLUTE path
               site TEXT, post_id TEXT,
               phash,dhash,ahash,whash TEXT NOT NULL,                 -- hex strings
               color_hist,grid_hists TEXT NOT NULL, added_at REAL NOT NULL )
               -- + idx_site (392).  NO UNIQUE anywhere.
```

DDL: `core/dedup_store.py:377-392`. Rows are pruned when their file disappears (`:447-455`, `:526-537`, `:539-552`).

### Transactions & races

- Gallery writes: one `commit()` per operation under `self._lock` (`insert_entry:573`, `update_fields:590`); `save_entries` relies on Python sqlite3's implicit transaction + `commit` (`:462-483`); `replace_all` uses explicit `BEGIN/COMMIT` (`:638-644`); the JSON import is one implicit transaction with rollback-on-mismatch (`:240-256`).
- Dedup writes: `RLock` held across find+add (`:489-499`) — the existing anti-race pattern to reuse.
- **The two databases are never written in one transaction.** A crash between `check_and_add` and `add_to_gallery` leaves a file + dedup row without a gallery row; the rescan (`Rems_Dl.py:2263`) heals the gallery from disk.
- **There is no `ALTER TABLE` anywhere in the repo** (verified). The only schema evolution path is: `_migrate_if_needed` (`:214-264`) imports `gallery.json` **once** into a freshly-created table (only runs when the table is empty, `:217-219`) and renames the JSON to `.migrated`. `git log -S` shows `sources`, `meta_mtime`, `md5_misses` and the store itself all landed in **one commit, `fb05e41`** — i.e. the SQLite schema has never been extended in place on this branch. **Any new column therefore needs a first-of-its-kind upgrade guard** (`PRAGMA table_info` → `ALTER TABLE … ADD COLUMN`), because DBs created between `fb05e41` and HEAD exist in the wild (the dev DB in `database/` is one).
- Settings are **not** in SQLite: `SettingsManager` (`core/database.py:333-475`) holds config in memory seeded from env (`:348`), persists via `.env` (`save_config` `:397-412`, `save_api_settings` `:414`), `.env` is gitignored and loaded with `load_dotenv`.

### Can the schema accommodate source hashes?

Yes, in **either** store; the deciding factors:

| Option | Pros | Cons |
|---|---|---|
| **`image_hashes.source_md5` + index** (recommended) | Written at the exact "successfully registered" moment (`add`, `core/dedup_store.py:459`); lookup lives under the same `RLock` as the existing find+add → race-safe by construction; auto-loses the hash when the file is deleted (ghost prune) — *correct*, a deleted file must stop deduping; `site`/`post_id`/`filepath` columns already exist for the "associated record" | Not visible to the viewer/`absorb` logic (optional later) |
| `gallery.source_md5` column | Viewer + `sources`-style absorption; `ENTRY_FIELDS` plumbing exists | Write point (`add_to_gallery`) is *after* the dedup decision; rescan-created placeholders never have a hash; manual deletes must keep two stores consistent (they already do, `Rems_Dl.py:2236`) |

**A single UNIQUE constraint on the hash is explicitly not sufficient** (and shouldn't be added): legacy rows are `NULL` (SQLite UNIQUE allows multiple NULLs, but existing *non-null* dupes from past races must not break an upgrade); failed/queued downloads must not occupy the hash (they're simply never written — good); two legitimately distinct files can share an md5 only if one was re-encoded (different md5 — no conflict) — yet an IntegrityError path would still need handling. Use a **non-unique index** + lock-guarded check-then-insert instead; a partial `UNIQUE … WHERE source_md5 IS NOT NULL` is optional hardening only.

---

## D. Adapter capability matrix

Canonical ids: `SITE_CANONICAL`, `core/shared.py:82-97` (no separate display-name map exists).

Confidence labels: **Verified in code** = implementation inspected; **Verified at runtime** = live probe this session; **Unverified** = insufficient evidence; **Unsupported** = no viable mechanism found.

| Site (canonical id) | Adapter | Listing endpoint | Listing has hash (API) | Adapter reads it | id → hash | Hash field / format | Auth | Extra req for hash | Confidence |
|---|---|---|---|---|---|---|---|---|---|
| gelbooru | `workers/gelbooru.py` | dapi `index.php?page=dapi…json=1` (`:119`) | **yes** | no | `tags=id:{id}` ✅ | `md5`, 32-hex | `GELBOORU_API_KEY`+`GELBOORU_USER_ID` (`:57,107`) — **401 without** | none | runtime (all); verified in code (auth, not-read) |
| rule34 | `workers/rule34.py` (via `rule34Py` pkg) | `api.rule34.xxx` dapi (pkg `api_urls.py:25,33`) | **yes** | no (pkg maps `json["hash"]` → `result.hash`, `rule34Py/post.py:59,113-120`) | `tags=id:{id}` ✅ | `hash`, **md5**, 32-lowercase-hex | `RULE34_API_KEY`+`RULE34_USER_ID` (`:92-93`) | none | runtime incl. format proof (2026-10-14, `md5:` round-trip); verified in code (pass-through available) |
| safebooru | `workers/safebooru.py` | dapi (`:144` region) | **yes** | no | `tags=id:{id}` ✅ | `hash`, 32-hex | none | none | runtime; verified in code |
| yande | `workers/yande.py` | `post.json` | **yes** | no | `tags=id:{id}` ✅ | `md5` | none | none | runtime; verified in code |
| konachan | `workers/konachan.py` | `post.json` (`:191` region) | **yes** | no | `tags=id:{id}` ✅ | `md5` | `KONACHAN_USERNAME/PASSWORD` (`:82`) optional | none | runtime; verified in code |
| danbooru | `workers/danbooru.py` | `posts.json` | **yes** | no | `tags=id:{id}` ✅ | `md5` | `DANBOORU_LOGIN`+`DANBOORU_API_KEY` (`:27`) for write/limits; reads work anonymously | none | runtime; verified in code |
| sankaku | `workers/sankaku.py` | `sankakuapi.com/posts` (`:6`, `:120`) | **yes** | no | `tags=id:{id}` ✅ | `md5` | `SANKA_ACCESS_TOKEN` Bearer (`:46-72`, login fallback `:50-57`) | none | runtime; verified in code |
| anime-pictures (`anime_dl`) | `workers/anime_dl.py` | `api.anime-pictures.net/api/v3/posts` (`:13`, `:209-211`) + per-post `/posts/{id}` | **yes** | no | detail-by-id ✅ | `md5` | none (Cloudflare needs browser impersonation — worker uses `curl_cffi`, `:5`) | none | runtime; verified in code |
| gsbooru | `workers/gsbooru.py` | `/api/posts` (`:365` region) | **yes** | no | ❌ `tags=id:` unsupported (empty) | `md5` | `GSBOORU_API_KEY` Bearer (`:36`) | none (listing has it) | runtime; verified in code |
| zerochan | `workers/zerochan.py` | HTML/API scrape; URL derived from id (`{id}.full.jpg`) | **no** | — | id→URL only, no hash anywhere | — | `ZEROCHAN_LOGIN/PASSWORD` (`:273-276`) | n/a | unsupported — verified in code |
| pixiv | `workers/pixiv.py` | AppAPI | **no** | — | none | — | `PIXIV_REFRESH_TOKEN` (`:184`) | n/a | unsupported — verified in code |
| eshuushuu | `workers/eshuushuu.py` | HTML search scrape (`:261` enqueue) | **no** | — | none | — | optional `user_id` (`Rems_Dl.py:2791`) | n/a | unsupported — verified in code |
| nekos.best / nekos.life / nekosapi / nekosia / waifu.im / pinterest | `workers/nekos_best.py`, `nekos_life.py`, `nekosapi.py`, `nekosia.py`, `waifu_im.py`, `pinterest_worker.py` | JSON random-image/listing APIs | **no** | — | none | — | pinterest cookies/creds (`Rems_Dl.py:2782-2784`) | n/a | unsupported — verified in code |

Shared behavior (verified in code): no adapter performs a per-post detail request for metadata (anime-pictures' `/posts/{id}` exists but is used for tag/size data); no adapter reads any hash field; post dicts are **not** retained between listing and download — only the derived 13-field queue tuple (plus tag-type caches, e.g. `workers/gelbooru.py:56`); rediscovery across pagination/pages is handled purely by the filename/`dl_history` skip inside `enqueue_download` (`core/shared.py:1307`); global pacing `REQ_RATE_LIMIT` (`core/shared.py:1132-1157`, aiohttp trace `:1271-1272`, sync `pace_wait` `:1153`), retries `download_retries` ×2s backoff (`:1340,1468-1469`), `anti_ban_pause` (`:1204`), timeouts 30s/10s (`:1256`), shared tag-fetch retry helper `workers/__init__.py:_run_tag_fetch` (3 attempts, backoff, pacing).

**Key point:** every "hash field" cell above is already populated in the response the adapter receives — **zero additional API requests** are needed for hash capture on all 9 booru-family sites.

---

## E. Current download & deduplication flow (actual order)

1. `POST /start` → `_dispatch_worker` in its own thread (`Rems_Dl.py:2742-2743`).
2. `worker.run()` → `run_async_loop` (`core/shared.py:1498-1508`): session, queue, **4** download tasks.
3. `scraper_task` fetches listing pages; parses post → `file_url`, filename, tags (e.g. `workers/gelbooru.py:169-191`).
4. `enqueue_download` (`core/shared.py:1296`): **skip if filename in `dl_history`/`queued_items` or file exists** (`:1307`) → claim (`:1319`) → queue (`:1320`). *(no network of the image yet)*
5. `_download_worker` claims item (`:1486`).
6. `_async_download_file` (`:1324`): stop check; `.part` resume via `Range` (`:1346-1348`); up to `dl_retries` attempts with **incremental md5 verification when the filename embeds `-<32hex>.ext`** (`:1335-1401`) → md5 mismatch = "degraded file", retry (`:1466-1467`).
7. `os.replace(part → filepath)` (`:1405`) — file fully on disk.
8. `check_duplicate` on a thread (`:1411`) → `compute_signature` (decode!) → locked find+add (`core/dedup_store.py:489-499`).
9. If duplicate: delete file (`:1414`), `_remember_filename` (`:1419`), `absorb_duplicate` (tags+sources union, `:1420`) → return.
10. Success: `dl_history.add` (`:1427`, flushed every 10 / worker end).
11. `send_tags` → `image_history` row (`:1451`; `core/database.py:172-184`).
12. `write_image_metadata` — **byte injection mutates the file** (`:1454`; `core/shared.py:803-861`).
13. `add_to_gallery` (`:1455` → `core/shared.py:543`).
14. `[SUCCESS]` log (`:1456`).
15. Worker end: queue join (`:1525-1530`), flush gallery/history (`:1542-1558`).

### Earliest safe interception points (before image bytes transfer)

- **P1 — inside `enqueue_download`, at/just after the existing skip block (`core/shared.py:1307-1311`).** Before queueing, zero image transfer, already the per-site dedup chokepoint, one indexed SELECT per candidate. Adapters reach it with the full post dict in scope (hash available for free).
- **P2 — top of `_async_download_file` (after `:1327`)** as a cheap in-memory re-check (no DB) to close enqueue↔start races.
- **P3 — adapter parse loop** (full post available, but per-adapter code ×9 and would slow scanning; unnecessary since the hash flows through P1).
- **Not usable:** anything at/after steps 7-8 (bytes already fetched; phash runs post-download by design).
- **Custom paths:** zerochan/pinterest have no source hash (nothing to do); anime_dl uses base `enqueue_download` (P1 applies) but its `_async_download_file` override (`workers/anime_dl.py:73`) must accept any tuple extension; sankaku's `enqueue_download` override (`workers/sankaku.py:78`) must mirror base.

**Safest location: P1**, because the process is single-instance (one Flask+SocketIO process; per-site jobs are threads each running `asyncio` with 4 tasks), so one `threading.RLock`-guarded check-claim in the dedup store serializes every worker — exactly the pattern `check_and_add` already uses (`core/dedup_store.py:487-489`).

---

## F. Proposed minimal implementation

No new dependencies, no new settings, no DB replacement. Touch list:

### 1. `core/dedup_store.py` — storage + lookup (primary change)
- `_init_schema` (`:370`): after CREATE, add a first-of-its-kind upgrade guard — `PRAGMA table_info(image_hashes)` → if `source_md5` missing: `ALTER TABLE image_hashes ADD COLUMN source_md5 TEXT`; then `CREATE INDEX IF NOT EXISTS idx_source_hash ON image_hashes(source_md5)` (non-unique; must tolerate legacy NULLs and possible legacy dupes).
- New `find_by_source_hash(h)` — `SELECT … WHERE source_md5 = ?`, return row only if `os.path.exists(row["filepath"])` (mirror `find_duplicate:447` ghost handling; delete stale row if gone).
- New `has_source_hash(h)` — lightweight claim check for P1 (returns matched filepath or None).
- Extend `check_and_add(filepath, site=None, post_id=None, source_md5=None)`:
  1. under the **existing `self._lock`** (`:489`): first `find_by_source_hash` → hit ⇒ return `DedupResult(is_duplicate=True, matched_path=…, kind="source_hash")` **before** `compute_signature` (skips decoding the image — also a perf win);
  2. miss ⇒ existing phash logic unchanged;
  3. on insert, pass `source_md5` into `add` (only when non-None; `add` gains one column).
- `DedupResult` gains a discriminator field (default `"phash"`) so callers can log/report which rule fired.

### 2. `core/shared.py` — carry the hash, check at P1, persist at success
- `enqueue_download(…, source_md5=None)` (`:1296`): normalize (`lower()`, accept only `[0-9a-f]{32}` → **rejects safebooru's 40-hex sha1 filenames**); if dedup enabled: under a module-level claim lock — (a) `has_source_hash` hit ⇒ `return False` (silent skip, matching the existing `dl_history` skip semantics at `:1307`); (b) else record the claim `{hash: filepath}` in a module dict and continue queueing. Missing/invalid hash ⇒ **fall through to existing behavior verbatim**.
- `check_duplicate(..., source_md5=None)` (`:1095`): forward to `check_and_add`.
- `_async_download_file` (`:1324`): resolve `source_md5` for this filepath from the claim dict; pass it into `check_duplicate` at `:1411` (persist on success).
- `_download_worker.finally` (`:1489`, already keyed by filepath) **and** the failure branch (`:1470-1477`): release the claim — a failed/abandoned download must never suppress a retry.
- Optional (no adapter edits, phase 1): derive `source_md5` from a 32-hex filename **stem** using the exact `md5_of` rule (`core/md5_refetch.py:264-266`) — covers gelbooru/gsbooru (URL basename *is* the md5; runtime-verified) with zero pass-through code.

### 3. Adapters — one line each at the existing enqueue call (phase 2, full coverage)
Pass `source_md5=post.get("md5") or post.get("hash")` (or `getattr(result, "hash", None)` for rule34):
`workers/gelbooru.py:192`, `workers/safebooru.py:144`, `workers/yande.py:166`, `workers/konachan.py:191`, `workers/danbooru.py:167`, `workers/rule34.py:200`, `workers/anime_dl.py:296`, `workers/gsbooru.py:365`, plus `workers/sankaku.py:209` **and** its enqueue override at `:78` (signature must accept/forward the kwarg). No changes for zerochan/pixiv/eshuushuu/nekos*/waifu/pinterest (no hash exists). If the base queue tuple is extended instead of a side-channel, `workers/anime_dl.py:73` must gain the parameter (zerochan's self-consistent override stays untouched).

**Flow of the hash:** adapter post dict → `enqueue_download(source_md5=…)` → claim dict `{hash: filepath}` → on success `check_and_add(source_md5=…)` writes `image_hashes.source_md5` → later pre-download checks read it. Gallery schema untouched (optional later: expose in viewer / feed `absorb_duplicate`-style `sources` merging).

**Keep untouched:** phash logic, `absorb_duplicate`, `dl_history`, `queued_items`, `md5_refetch`, GUI/settings (reuse `dedup_enabled`, `core/shared.py:1101-1103` / `core/database.py:348,406` — no new setting).

---

## G. Concurrency and correctness

- **Two workers, same hash, simultaneously:** both site jobs are threads in one process; the P1 check-claim runs under a store/module `RLock` → first claimer queues, second is skipped before any transfer. This mirrors the existing find+add lock discipline (`core/dedup_store.py:487-489`).
- **Same site, same file:** already handled by `queued_items`/`dl_history`/`os.path.exists` (`core/shared.py:1307`); unchanged.
- **Claim lifecycle (queued → downloading → done/failed):**
  - claim = in-memory `{hash: filepath}` — set at enqueue, released in `_download_worker.finally`/failure branch. Memory-only ⇒ a crash mid-download loses claims, but the persisted hash was never written ⇒ next run re-downloads. **No skip can ever rest on an unverified record.**
  - persisted `source_md5` is written **only inside the post-`os.replace` success path** (at `check_duplicate`, step 8), i.e. only for files fully on disk — the requirement "save on successful registration". Failed downloads (`:1470-1477`) leave no hash and no history entry (verified: `dl_history.add` only at `:1427` and phash-kill `:1419`).
- **Lookup checks file existence** (mirror `find_duplicate:447-455`), so records whose file was deleted stop deduping instead of silently skipping forever; the delete route already removes the dedup row (`Rems_Dl.py:2236-2237`).
- **No cross-DB transaction** is introduced: hash write happens inside `check_and_add`'s existing commit; gallery registration stays as-is. A crash between them degrades to today's behavior (rescan heals the gallery).
- **Source md5 never meets perceptual hashes:** source pre-check runs *before* `compute_signature` and is a distinct result kind; `find_duplicate`/`_is_full_duplicate` are untouched.
- **Hash retrieval failure / missing field:** `source_md5=None` → entire feature bypassed → exact existing behavior (filename/history/phash). Never "duplicate by absence".
- **Two app instances (unsupported scenario):** optional hardening — partial `UNIQUE INDEX … WHERE source_md5 IS NOT NULL` + `IntegrityError`→treated-as-duplicate in `add`. Not required for the single-instance app.
- **Race already present today (unchanged):** gallery vs dedup can disagree briefly (crash window); acknowledged.

---

## H. Testing plan

Conventions (verified): pytest, `pytest.ini` (`pythonpath=.`, `testpaths=test`), flat `test/test_*.py` (~47 files), fixtures+`monkeypatch`; **`test/conftest.py:20-60` session-isolates every `*_FILE` constant containing `/database/` into a temp dir and drops `gallery_store._store` / `dedup_store._store_instance` singletons — any new module file constant must follow that naming** (`*_FILE` + `/database/` path) or tests will write into the live DB. Last full suite: **381 passed at `90249bb`** (re-run before finalizing).

1. **Unit — dedup store** (extend `test/test_dedup.py`, reuse `dedup_env` fixture pattern `:13-20`):
   - `find_by_source_hash` hit/miss; hit skips `compute_signature` (monkeypatch it to explode ⇒ proves no decode);
   - hash write only via `check_and_add(..., source_md5=…)`; NULL legacy rows unaffected;
   - lookup with deleted file ⇒ miss + stale-row prune;
   - schema upgrade: build a DB with the old column set → `_init_schema` adds column+index idempotently.
2. **Unit — hash normalization** (`test_dedup_source_hash.py`): accepts 32-hex (case-insensitive); rejects 40-hex (safebooru sha1 filename), title-based, empty, non-hex; `source_md5=None` ⇒ behavior byte-identical to today.
3. **Integration — enqueue (P1)**: known hash + existing file ⇒ `enqueue_download` returns `False`, nothing queued, `dl_history` untouched; unknown hash ⇒ queued and claimed; claim released on simulated download failure ⇒ re-enqueue succeeds.
4. **Adapter pass-through**: fixture payload with `md5`/`hash` → enqueue receives `source_md5` (mock style of `test/test_md5_refetch.py:50,95,381` and rating tests' monkeypatched sessions); missing hash field ⇒ no kwarg; rule34 `result.pHash` case.
5. **Concurrency**: two threads enqueue the same hash under the lock ⇒ exactly one claim (`threading` barrier test); parallel `check_and_add` same hash ⇒ one row.
6. **Regression (must stay green, existing files)**: `test_dedup.py`, `test_dedup_absorb.py`, `test_dedup_remember.py` (phash kill still remembered), `test_download_resume.py` / `test_metadata_latency.py` (their `monkeypatch.setattr("core.shared.check_duplicate", …)` with `*a, **k` keeps working), `test_md5_refetch.py`, `test_gallery_store_migration.py`, `test_download_queue.py`, `test_queue_finish_report.py`, `test_stop_join.py`; `dedup_enabled=False` ⇒ source-hash check also disabled (`core/shared.py:1101-1103`).
7. **Failure cases** (§7 requirements): 401/403/429/malformed-JSON/missing-hash are all structurally "no hash passed" ⇒ fallback (test by omission); no new network calls exist to fail.

---

## I. Outstanding questions

1. ~~**`rule34Py` `hash` format**~~ — **RESOLVED 2026-10-14 (runtime):** post 12836964's `hash` = `1bab3104189ed96ed8665843ffa38c5b` (32 lowercase hex); `tags=md5:<hash>` returns that same post while the calibration `tags=md5:000…0` returns empty — the field **is the post's md5** and `md5:` search works on `api.rule34.xxx`. The lib maps `json["hash"]` to the `Post.hash` property (`rule34Py/post.py:59,91,113-120`; `pHash` is only a local var); `workers/rule34.py` never reads it.
2. **Legacy DBs missing `source_md5`** — `gallery.db`/`dedup.sqlite3` files created between `fb05e41` and HEAD predate the column; repo has *no* ALTER precedent, so the `PRAGMA table_info` guard is mandatory, and whether real user DBs have drifted from `CREATE TABLE` (e.g. an intermediate dev build) can't be proven from the repo. *Resolve:* the upgrade-guard test above + running the app once against a copy of the real `database/` DBs.
3. **Whether `sources`-style absorption of the skipping post's tags is desired** (skip silently vs. absorb tags/site into the surviving record like `absorb_duplicate`). Not determinable from code.
4. **Two-instance concurrency** — assumed unsupported (single process; `STOP_EVENTS`/`ACTIVE_JOBS` are process-local). Confirm no multi-instance usage before relying on the in-memory claim dict alone.
5. **eshuushuu/zerochan/pixiv/nekos*/waifu/pinterest** — permanently hash-less; nothing to resolve (Unsupported, verified in code).

---

## J. Implementation readiness

**Yes — sufficient evidence to implement.** The modules to change are precisely identified (`core/dedup_store.py`, `core/shared.py`, 9 one-line adapter pass-throughs + `workers/sankaku.py:78` override, optionally `workers/anime_dl.py:73` if the tuple is extended), the schema change and its upgrade guard are clear, the earliest safe interception point (P1, `core/shared.py:1307-1311`) is verified against every call path including the custom zerochan/pinterest/anime_dl routes, and the test scaffolding (conftest isolation, fixture patterns) is known. Outstanding question 1 (rule34 `hash` format) was resolved by runtime verification on 2026-10-14 (§I.1); the remaining §I items are product decisions or deployment checks, not evidence gaps.

---

*Skipped:* didn't enumerate every field of every adapter's payload (hash-relevant keys only), and didn't re-run the test suite during this audit (read-only; last green run 381 passed at `90249bb`).
*Risks:* source APIs changed auth recently (gelbooru's anonymous 401) — same class of change could break hash capture silently, which is exactly why "no hash ⇒ full fallback" must stay the rule; and the `ALTER` upgrade path has no repo precedent, so the guard is the one piece to get right.
