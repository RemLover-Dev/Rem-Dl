import os
import re
import sys
import threading
import json
import time
import asyncio
import hashlib
import atexit
import aiohttp
import zlib

def _app_base_dir():
    # core/ lives one level below the repo root — go up one more.
    # Frozen (PyInstaller) builds: keep user data next to the exe.
    if getattr(sys, 'frozen', False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

BASE_DIR = _app_base_dir()
APP_NAME = "Rems Dl"
DOWNLOAD_DIR_NAME = "Rems Dl"
LEGACY_DOWNLOAD_DIR_NAME = "Rem God"
MASTER_FOLDER = os.path.join(BASE_DIR, DOWNLOAD_DIR_NAME)
# Auto-migrate legacy "Rem God" download folder to "Rems Dl" (one-time, safe).
if os.path.isdir(os.path.join(BASE_DIR, LEGACY_DOWNLOAD_DIR_NAME)) and not os.path.isdir(MASTER_FOLDER):
    try:
        os.rename(os.path.join(BASE_DIR, LEGACY_DOWNLOAD_DIR_NAME), MASTER_FOLDER)
    except Exception:
        pass
HISTORY_LOCK = threading.Lock()
STOP_EVENTS = {}

WAIFU_TAG_MAP = {}

# queue batch reporting — the app registers a callback telling us whether more
# jobs are waiting for a site, so a queued run reports its totals once
queue_has_more = None  # (site) -> bool, set by Rems_Dl (avoids circular import)
_BATCH_STATS = {}

def finish_report(site, downloaded, failed, duplicates, stopped, log):
    """Accumulate per-site batch stats, log the finish line (batch final is
    box-only), build the worker_finished payload."""
    b = _BATCH_STATS.setdefault(site, {"jobs": 0, "downloaded": 0, "failed": 0, "duplicates": 0})
    b["jobs"] += 1
    b["downloaded"] += downloaded
    b["failed"] += failed
    b["duplicates"] += duplicates
    dup_note = f" ({duplicates} duplicates removed)" if duplicates else ""
    more = (not stopped) and bool(queue_has_more and queue_has_more(site))
    if stopped:
        if downloaded > 0 and failed > 0:
            log(f"--- Task finished: {downloaded} downloaded successfully, {failed} failed to download{dup_note}! ---")
        elif downloaded > 0:
            log(f"--- All {downloaded} downloads completed successfully!{dup_note} ---")
    elif more:
        # keep the bars alive — the next queued job rebuilds them with Phase 1
        log(f"--- Tag finished: {downloaded} downloaded, {failed} failed — next queued job starting ---")
    elif b["jobs"] == 1:
        if downloaded > 0 and failed > 0:
            log(f"--- Task finished: {downloaded} downloaded successfully, {failed} failed to download{dup_note}! ---")
        elif downloaded > 0:
            log(f"--- All {downloaded} downloads completed successfully!{dup_note} ---")
        else:
            log(f"Task finished. No new images to download{dup_note}.")
    # batch final (jobs > 1): the report lives in the progress box via
    # worker_finished — deliberately not logged to the console
    payload = {"worker": site, "downloaded": b["downloaded"], "failed": b["failed"], "duplicates": b["duplicates"], "stopped": stopped, "jobs": b["jobs"], "more": more}
    if not more:
        _BATCH_STATS.pop(site, None)
    return payload

# --- LOGGING & TAG SYSTEM ---
def default_logger(worker_name, msg): print(f"[{worker_name.upper()}] {msg}")
log_callback = default_logger
def log_msg(worker_name, msg): log_callback(worker_name, msg)

def default_tag_handler(worker_name, filename, tags_list, artist_list, filepath=None, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None): pass
tag_callback = default_tag_handler
TAG_CATEGORIES = ["artist", "character", "copyright", "metadata", "outfit", "group", "hair", "eyes", "tag"]

SITE_CANONICAL = {
    "dan": "danbooru", "danbooru": "danbooru",
    "eshuushuu": "eshuushuu", "e-shuushuu": "eshuushuu",
    "gelbooru": "gelbooru", "gsbooru": "gsbooru",
    "konachan": "konachan", "kona": "konachan", "yande": "yande", "yande.re": "yande",
    "sankaku": "sankaku", "safebooru": "safebooru", "safe": "safebooru", "rule34": "rule34",
    "pinterest": "pinterest", "pixiv": "pixiv",
    "zero": "zerochan", "zerochan": "zerochan",
    "neko": "nekos.life", "nekos.life": "nekos.life",
    "nekos.best": "nekos.best", "nekosapi": "nekosapi", "nekosia": "nekosia",
    "waifu": "waifu.im", "waifu.im": "waifu.im",
    "anime_dl": "anime_dl", "animepictures": "anime_dl",
}

def normalize_site(site):
    return SITE_CANONICAL.get(site.lower().strip(), site.lower().strip())

def sort_tags_by_category(tags_dict):
    """Sort a tags dict by category order. Returns new ordered dict."""
    from collections import OrderedDict
    result = OrderedDict()
    for cat in TAG_CATEGORIES:
        if cat in tags_dict and tags_dict[cat]:
            result[cat] = sorted(tags_dict[cat])
    return result

def count_tags(tags):
    """How many tags an entry carries (0 for missing/legacy list shapes)."""
    if not isinstance(tags, dict):
        return 0
    return sum(len(v) for v in tags.values() if isinstance(v, list))

def tags_dict_from_lists(tags_list, artists=None, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None):
    """Build a categorized tags dict from flat lists."""
    result = {"artist": [], "character": [], "copyright": [], "metadata": [], "outfit": [], "group": [], "hair": [], "eyes": [], "tag": []}
    if artists:
        result["artist"] = [a.strip() for a in artists if a.strip()]
    if characters:
        result["character"] = [c.strip() for c in characters if c.strip()]
    if copyrights:
        result["copyright"] = [c.strip() for c in copyrights if c.strip()]
    if metadata_tags:
        result["metadata"] = [m.strip() for m in metadata_tags if m.strip()]
    if outfits:
        result["outfit"] = [o.strip() for o in outfits if o.strip()]
    if groups:
        result["group"] = [g.strip() for g in groups if g.strip()]
    if hair:
        result["hair"] = [h.strip() for h in hair if h.strip()]
    if eyes:
        result["eyes"] = [e.strip() for e in eyes if e.strip()]
    if tags_list:
        result["tag"] = [t.strip() for t in tags_list if t.strip()]
    return sort_tags_by_category(result)

def build_tagd(artists=None, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None, tags_list=None, limit=5):
    """Compact categorized tag segment for SUCCESS log lines: 'outfit:X, character:Y'."""
    seen = []
    # ponytail: artists render as badges outside the pill row — if they shared
    # its budget, an artist-bearing post would show one pill fewer
    for _t in artists or []:
        _t = _t.strip()
        if _t:
            seen.append(f"artist:{_t}")
    pills = 0
    for _cat, _items in (("character", characters), ("copyright", copyrights), ("metadata", metadata_tags), ("outfit", outfits), ("group", groups), ("hair", hair), ("eyes", eyes), ("tag", tags_list)):
        for _t in _items or []:
            _t = _t.strip()
            if _t and pills < limit:
                seen.append(f"{_cat}:{_t}")
                pills += 1
    return ", ".join(seen)

# --- PATH & FILENAME SANITIZATION (Filesystem-safe on Windows & POSIX) ---
_UNSAFE_PATH_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9"
}

def sanitize_path_component(name: str, fallback: str = "misc", max_length: int = 120) -> str:
    """Make a tag or directory name completely safe for Windows and POSIX filesystems.

    Removes prohibited characters (< > : " / \\ | ? * and control chars 0x00-0x1F),
    strips trailing/leading spaces and dots, avoids Windows reserved names (CON, AUX, etc.),
    and enforces a safe maximum length.
    """
    if not name:
        return fallback
    try:
        cleaned = _UNSAFE_PATH_CHARS_RE.sub("", str(name))
        cleaned = cleaned.strip().rstrip(". ").strip()
        if not cleaned:
            return fallback
        # Check against Windows reserved device names
        root_name = cleaned.split(".")[0].upper()
        if root_name in _WINDOWS_RESERVED_NAMES:
            cleaned = f"_{cleaned}"
        if len(cleaned) > max_length:
            cleaned = cleaned[:max_length].rstrip(". ")
        return cleaned or fallback
    except Exception:
        return fallback

def sanitize_filename(name: str, fallback: str = "image.jpg", max_length: int = 150) -> str:
    """Make a filename safe for the filesystem while preserving its extension."""
    if not name:
        return fallback
    try:
        base, ext = os.path.splitext(str(name).strip())
        safe_ext = _UNSAFE_PATH_CHARS_RE.sub("", ext).strip()
        safe_base = sanitize_path_component(base, fallback="image", max_length=max(10, max_length - len(safe_ext)))
        return f"{safe_base}{safe_ext}" if safe_ext else safe_base
    except Exception:
        return fallback

def safe_ensure_dir(path: str) -> str:
    """Ensure directory exists with safe, sanitized components across all platforms (Windows, Linux, macOS).
    Creates parent directories safely.
    """
    if not path:
        return ""
    try:
        drive, rest = os.path.splitdrive(path)
        is_abs = os.path.isabs(path)
        parts = [p for p in rest.replace("\\", "/").split("/") if p]
        safe_parts = [sanitize_path_component(p) for p in parts]
        if drive:
            prefix = drive + os.sep if drive.endswith(":") else drive
            safe_path = os.path.join(prefix, *safe_parts)
        elif is_abs:
            safe_path = os.path.join(os.sep, *safe_parts)
        else:
            safe_path = os.path.join(*safe_parts) if safe_parts else "."
        os.makedirs(safe_path, exist_ok=True)
        return safe_path
    except Exception:
        try:
            os.makedirs(path, exist_ok=True)
            return path
        except Exception:
            return path

def safe_filepath(folder: str, filename: str) -> tuple:
    """Sanitize filename and ensure parent folder exists, returning (safe_filepath, safe_filename)."""
    safe_name = sanitize_filename(filename)
    safe_dir = safe_ensure_dir(folder)
    return os.path.join(safe_dir, safe_name), safe_name

def send_tags(worker_name, filename, tags_list, artist_list=None, filepath=None, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None):
    if artist_list is None: artist_list = []
    tag_callback(worker_name, filename, tags_list, artist_list, filepath, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)

def default_emit(event, data): pass
emit_callback = default_emit
def socketio_emit(event, data): emit_callback(event, data)

GALLERY_FILE = os.path.join(BASE_DIR, "database", "gallery.json")
TAG_TYPE_MAP = {0: "tag", 1: "artist", 3: "copyright", 4: "character", 5: "metadata"}

def load_gallery():
    """Return the gallery, cached in memory after first disk read.

    All readers/mutators share one object (guarded by _GALLERY_LOCK), so
    concurrent workers can't clobber each other's entries. Call
    flush_gallery() or save_gallery() to persist.
    """
    with _GALLERY_LOCK:
        return _gallery_cached_locked()

def _load_gallery_from_disk():
    if os.path.exists(GALLERY_FILE):
        try:
            with open(GALLERY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            # quarantine the bad file — returning an empty gallery here would
            # get persisted over all real entries by the next save
            try:
                os.replace(GALLERY_FILE, GALLERY_FILE + ".corrupt")
            except OSError:
                pass
            return {"images": []}
        try:
            _migrate_gallery_tags(data)
        except Exception:
            pass
        return data
    return {"images": []}

def _migrate_gallery_tags(data):
    """Convert old flat-list tags to categorized dicts and normalize site names in-place."""
    changed = False
    for img in data.get("images", []):
        tags = img.get("tags")
        if isinstance(tags, list):
            img["tags"] = tags_dict_from_lists(tags)
            changed = True
        site = img.get("site", "")
        canon = normalize_site(site)
        if canon != site:
            img["site"] = canon
            changed = True
    if changed:
        save_gallery(data)

def save_gallery(data):
    with _GALLERY_LOCK:
        # write under the lock: a worker save racing a route save used to
        # interleave two truncate+dumps and corrupt gallery.json
        _write_gallery(data)
        if data is _gallery_cache["data"]:
            _gallery_cache["filenames"] = {i.get("filename") for i in data.get("images", [])}
            _gallery_cache["dirty"] = 0

def _write_gallery(data):
    # compact separators: gallery.json is gitignored data, indent only
    # cost parse time and disk on every write; tmp+replace so a crash
    # mid-write can't truncate the live file
    tmp = GALLERY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, GALLERY_FILE)

_GALLERY_LOCK = threading.RLock()
_gallery_cache = {"data": None, "filenames": set(), "dirty": 0}
_GALLERY_FLUSH_EVERY = 10

def _gallery_cached_locked():
    g = _gallery_cache["data"]
    if g is None:
        g = _load_gallery_from_disk()
        _gallery_cache["data"] = g
        _gallery_cache["filenames"] = {i.get("filename") for i in g.get("images", [])}
        _gallery_cache["dirty"] = 0
    return g

def flush_gallery():
    """Persist pending gallery appends, if any."""
    with _GALLERY_LOCK:
        if _gallery_cache["data"] is not None and _gallery_cache["dirty"]:
            _write_gallery(_gallery_cache["data"])
            _gallery_cache["dirty"] = 0

# A batched write plus an exit (Ctrl-C, an exception, a CLI run) used to drop
# the last N records — and the next scan rebuilt them from the folder name
# alone, losing their tags. Every program importing this module now flushes
# on the way out. os._exit / SIGTERM skip this: those callers flush by hand.
atexit.register(flush_gallery)

# --- tag-category repair: downloads freeze categories at fetch time, so a
# tag that failed its lookup (or hit the v1 [:150] fetch cap) stays in the
# general bucket forever even after later runs heal the worker cache. ---

def _norm_cat(c):
    """Cache values are category strings; old entries store raw type ints."""
    if isinstance(c, str) and c in _TAG_CAT_SET:
        return c
    return TAG_TYPE_MAP.get(c, "tag")

_TAG_CAT_SET = frozenset(TAG_CATEGORIES)

def load_tag_caches():
    """{site: {tag: category}} from database/*_tag_types.json."""
    caches = {}
    db = os.path.join(BASE_DIR, "database")
    if not os.path.isdir(db):
        return caches
    for name in os.listdir(db):
        if not name.endswith("_tag_types.json"):
            continue
        try:
            with open(os.path.join(db, name), encoding="utf-8") as f:
                raw = json.load(f)
        except (OSError, ValueError):
            continue
        if isinstance(raw, dict):
            caches[name[:-len("_tag_types.json")]] = {t: _norm_cat(c) for t, c in raw.items()}
    return caches

def recategorize_tags(tags_dict, site, caches):
    """Move tags between buckets to the category the site's cache knows;
    True if anything moved. Only the tag-type buckets — outfit/group/hair/
    eyes come from other sources (zerochan titles) and cache never yields them.
    Caches from load_tag_caches() are pre-normalized — the per-tag _norm_cat
    only guards direct callers with raw entries."""
    cache = caches.get(site)
    if not cache or not isinstance(tags_dict, dict):
        return False
    changed = False
    for bucket in ("tag", "artist", "character", "copyright", "metadata"):
        lst = tags_dict.get(bucket)
        if not isinstance(lst, list):
            continue
        keep = []
        for t in lst:
            raw = cache.get(t)
            cat = _norm_cat(raw) if raw is not None else None
            if cat and cat != bucket:
                target = tags_dict.setdefault(cat, [])
                if t not in target:
                    target.append(t)
                changed = True
            else:
                keep.append(t)
        if len(keep) != len(lst):
            tags_dict[bucket] = keep
    return changed

_recat_fp = None  # cache fingerprint from the last heal — skip full scans

def _tag_caches_fingerprint():
    """Cheap (name,size,mtime) signature of database/*_tag_types.json."""
    db = os.path.join(BASE_DIR, "database")
    fp = []
    try:
        for name in sorted(os.listdir(db)):
            if name.endswith("_tag_types.json"):
                st = os.stat(os.path.join(db, name))
                fp.append((name, st.st_size, st.st_mtime_ns))
    except OSError:
        pass
    return tuple(fp)

def recategorize_all():
    """Heal gallery + image_history entries from the worker tag caches.
    No-op unless a cache changed since the last pass (stat fingerprint),
    so a job that healed nothing costs two os.stat calls, not a gallery
    scan. Returns (gallery_entries_changed, history_entries_changed)."""
    global _recat_fp
    fp = _tag_caches_fingerprint()
    if fp == _recat_fp:
        return 0, 0
    caches = load_tag_caches()
    changed_g = 0
    with _GALLERY_LOCK:
        gal = _gallery_cached_locked()
        for img in gal.get("images", []):
            if recategorize_tags(img.get("tags"), img.get("site"), caches):
                changed_g += 1
        if changed_g:
            save_gallery(gal)
    from core.database import DatabaseManager
    changed_h = DatabaseManager.recat_image_history(caches)
    _recat_fp = fp  # cache state fully healed — later passes skip until it changes
    return changed_g, changed_h

def add_to_gallery(site, filename, filepath, tags_list, artists, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None, rating=None):
    with _GALLERY_LOCK:
        gallery = _gallery_cached_locked()
        if filename in _gallery_cache["filenames"]:
            # a scan that saw the file land first must not keep its
            # folder-name placeholder over the tags we just downloaded
            fresh = tags_dict_from_lists(tags_list, artists, characters,
                                         copyrights, metadata_tags, outfits,
                                         groups, hair, eyes)
            existing = next((i for i in gallery["images"]
                             if i.get("filename") == filename), None)
            if existing is None or count_tags(fresh) <= count_tags(existing.get("tags")):
                return
            existing["tags"] = fresh
            if filepath:
                existing["filepath"] = filepath
            if rating:
                existing["rating"] = rating
            _gallery_cache["dirty"] += 1
            if _gallery_cache["dirty"] >= _GALLERY_FLUSH_EVERY:
                _write_gallery(gallery)
                _gallery_cache["dirty"] = 0
            return
        tags_dict = tags_dict_from_lists(tags_list, artists, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
        record = {
            "id": hashlib.md5(f"{site}:{filename}".encode()).hexdigest()[:12],
            "filename": filename,
            "filepath": filepath,
            "site": normalize_site(site),
            "tags": dict(tags_dict),
            "favourite": False,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S")
        }
        # per-image rating only when the worker knows it — records without the
        # field keep falling back to path/tag sniffing
        if rating:
            record["rating"] = rating
        gallery["images"].insert(0, record)
        _gallery_cache["filenames"].add(filename)
        _gallery_cache["dirty"] += 1
        if _gallery_cache["dirty"] >= _GALLERY_FLUSH_EVERY:
            _write_gallery(gallery)
            _gallery_cache["dirty"] = 0


def absorb_duplicate(dup, site, tags_list, artists, characters=None, copyrights=None,
                     metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None):
    """A phash kill deletes the duplicate's file — carry what it carried:
    union its tags into the original's gallery record and append its site
    to the record's `sources` list (viewer shows "N sources", click to expand)."""
    if dup is None or not getattr(dup, "is_duplicate", False) or not getattr(dup, "matched_path", None):
        return False
    try:
        with _GALLERY_LOCK:
            gallery = _gallery_cached_locked()
            target = os.path.normpath(str(dup.matched_path))
            rec = None
            for i in gallery["images"]:
                fp = i.get("filepath") or ""
                if fp and os.path.normpath(os.path.join(MASTER_FOLDER, fp)) == target:
                    rec = i
                    break
            if rec is None:
                base = os.path.basename(target)
                rec = next((i for i in gallery["images"] if i.get("filename") == base), None)
            if rec is None:
                return False
            changed = False
            fresh = tags_dict_from_lists(tags_list, artists, characters,
                                         copyrights, metadata_tags, outfits,
                                         groups, hair, eyes)
            tags = rec.setdefault("tags", {})
            for bucket, vals in fresh.items():
                have = tags.setdefault(bucket, [])
                for v in vals:
                    if v and v not in have:
                        have.append(v)
                        changed = True
            sources = rec.get("sources")
            if not sources:
                first = normalize_site(rec.get("site") or "")
                rec["sources"] = [first] if first else []
                changed = True
                sources = rec["sources"]
            ns = normalize_site(site or "")
            if ns and ns not in sources:
                sources.append(ns)
                changed = True
            if changed:
                _gallery_cache["dirty"] += 1
                if _gallery_cache["dirty"] >= _GALLERY_FLUSH_EVERY:
                    _write_gallery(gallery)
                    _gallery_cache["dirty"] = 0
            return True
    except Exception as e:
        print(f"[DEDUP] absorb failed for {getattr(dup, 'matched_path', '?')}: {e}")
        return False

# --- container-level metadata helpers (byte injection, never re-encode) ---
def _riff_chunks(data):
    """[(fourcc, start, end)] over a RIFF file; None when truncated."""
    out, i = [], 12
    while i + 8 <= len(data):
        ln = int.from_bytes(data[i + 4:i + 8], "little")
        end = i + 8 + ln + (ln & 1)
        if end > len(data):
            if ln & 1 and end - 1 == len(data):
                end = len(data)  # last chunk's pad byte omitted
            else:
                return None
        out.append((data[i:i + 4], i, end))
        i = end
    if i != len(data):
        return None
    return out


def _gif_subblocks(raw):
    out, k = bytearray(), 0
    while k < len(raw):
        sz = raw[k]
        k += 1
        if sz == 0:
            break
        out += raw[k:k + sz]
        k += sz
    return bytes(out)


def _gif_blocks(data):
    """(gct_end, [(start, end, label)]) or None when the GIF is malformed.
    label is the extension's second byte (0x21 blocks), else 0x2C/0x3B."""
    if not data.startswith(b"GIF8") or len(data) < 14:
        return None
    packed = data[10]
    gct_end = 13 + (3 * (1 << ((packed & 7) + 1)) if packed & 0x80 else 0)
    if gct_end > len(data):
        return None
    blocks, i = [], gct_end
    while i < len(data):
        b = data[i]
        if b == 0x00:  # stray padding some encoders leave between blocks
            i += 1
            continue
        if b == 0x21:
            if i + 2 > len(data):
                return None
            j = i + 2
            while j < len(data):
                sz = data[j]
                j += 1
                if sz == 0:
                    break
                j += sz
            if j > len(data) or data[j - 1] != 0:
                return None
            blocks.append((i, j, data[i + 1]))
            i = j
        elif b == 0x2C:
            if i + 10 > len(data):
                return None
            lp = data[i + 9]  # separator + left/top/w/h = 9 bytes, packed follows
            j = i + 10 + (3 * (1 << ((lp & 7) + 1)) if lp & 0x80 else 0)
            if j + 1 > len(data):
                return None
            j += 1  # LZW minimum code size
            while j < len(data):
                sz = data[j]
                j += 1
                if sz == 0:
                    break
                j += sz
            if j > len(data) or data[j - 1] != 0:
                return None
            blocks.append((i, j, 0x2C))
            i = j
        elif b == 0x3B:
            blocks.append((i, len(data), 0x3B))
            i = len(data)
        else:
            return None
    return gct_end, blocks


def _gif_comment_block(payload):
    out = bytearray(b"\x21\xfe")
    for k in range(0, len(payload), 255):
        chunk = payload[k:k + 255]
        out.append(len(chunk))
        out += chunk
    out.append(0)
    return bytes(out)


def _ebml_elem(eid, payload):
    n = len(payload)
    w = 1
    while w < 8 and n >= (1 << (7 * w)) - 1:
        w += 1
    return eid + (n | (1 << (7 * w))).to_bytes(w, "big") + payload


def _ebml_size(data, j):
    """(payload_start, size|None) for an EBML size vint at j; None when malformed."""
    if j >= len(data):
        return None
    w, mask = 1, 0x80
    while w <= 8 and not (data[j] & mask):
        mask >>= 1
        w += 1
    if w > 8 or j + w > len(data):
        return None
    val = int.from_bytes(data[j:j + w], "big") & ((1 << (7 * w)) - 1)
    if val == (1 << (7 * w)) - 1:
        return j + w, None
    return j + w, val


def _ebml_find_rems(data):
    """Read our Tags element. Searched backwards from EOF: we always append it
    last, which sidesteps walking past unknown-size Clusters entirely."""
    k = data.rfind(b"\x12\x54\xc3\x27")
    while k != -1:
        p = _ebml_size(data, k + 4)
        if p and p[1] is not None and p[0] + p[1] <= len(data):
            start, end = p[0], p[0] + p[1]
            i = start
            while i + 2 <= end:
                if data[i:i + 2] == b"\x45\xa3":
                    q = _ebml_size(data, i + 2)
                    if q and q[1] is not None and data[q[0]:q[0] + q[1]] == b"Rems_Dl":
                        j = q[0] + q[1]
                        while j + 2 <= end:
                            if data[j:j + 2] == b"\x44\x87":
                                r = _ebml_size(data, j + 2)
                                if r and r[1] is not None:
                                    text = data[r[0]:r[0] + r[1]]
                                    if text.startswith(b"Rems_Dl\n"):
                                        text = text[8:]
                                    return text.decode("utf-8", "ignore")
                                break
                            j += 1
                i += 1
        k = data.rfind(b"\x12\x54\xc3\x27", 0, k)
    return None


# extensions write_image_metadata can inject into without re-encoding
EMBEDDABLE = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".mp4", ".webm", ".mov", ".avi", ".mkv")


def write_image_metadata(filepath, tags_list, artists, site, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None):
    ext = filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ''
    tags_dict = tags_dict_from_lists(tags_list, artists, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
    meta_lines = [f"site:{site}"]
    for cat in TAG_CATEGORIES:
        for t in tags_dict.get(cat, []):
            meta_lines.append(f"{cat}:{t}")
    meta_text = "\n".join(meta_lines)

    try:
        with open(filepath, "rb") as f:
            data = f.read()
        if ext in ('jpg', 'jpeg'):
            # inject a COM segment at byte level — saving through PIL here would
            # recompress the image (3MB originals were shrinking to ~1.4MB)
            payload = b"Rems_Dl\n" + meta_text.encode("utf-8")
            payload = payload[:65531]
            if not data.startswith(b"\xff\xd8"):
                return
            # segment length includes the 2 length bytes themselves
            seg = b"\xff\xfe" + (len(payload) + 2).to_bytes(2, "big") + payload
            tmp = filepath + ".meta"
            with open(tmp, "wb") as f:
                f.write(data[:2] + seg + data[2:])
            os.replace(tmp, filepath)
        elif ext == 'png':
            # byte-level chunk insert after IHDR. The old PIL img.save() path
            # both re-encoded the whole image (slow) AND failed outright: the
            # ".meta" temp name made PIL infer the format from the extension,
            # raising "unknown file extension" — so PNGs never got metadata.
            raw = meta_text.encode("utf-8")
            try:
                ctype = b"tEXt"
                payload = b"Rems_Dl\x00" + meta_text.encode("latin-1")
            except UnicodeEncodeError:
                # PIL PngInfo.add_text semantics: non-latin1 falls back to iTXt
                ctype = b"iTXt"
                payload = b"Rems_Dl\x00\x00\x00\x00\x00" + raw
            if data[:8] != b"\x89PNG\r\n\x1a\n":
                return
            chunk = (len(payload).to_bytes(4, "big") + ctype + payload +
                     (zlib.crc32(ctype + payload) & 0xFFFFFFFF).to_bytes(4, "big"))
            out = bytearray(data[:8])
            i, inserted = 8, False
            while i + 12 <= len(data):
                ln = int.from_bytes(data[i:i + 4], "big")
                kind = data[i + 4:i + 8]
                if kind in (b"tEXt", b"iTXt") and data[i + 8:i + 16] == b"Rems_Dl\x00":
                    i += 12 + ln  # drop a stale entry from a previous write
                    continue
                out += data[i:i + 12 + ln]
                if kind == b"IHDR" and not inserted:
                    out += chunk
                    inserted = True
                i += 12 + ln
            tmp = filepath + ".meta"
            with open(tmp, "wb") as f:
                f.write(out)
            os.replace(tmp, filepath)
        elif ext in ('webp', 'avi'):
            # RIFF: append an unknown chunk (spec says readers skip them) and
            # fix the RIFF size field — bitstream untouched
            payload = b"Rems_Dl\n" + meta_text.encode("utf-8")
            if not data.startswith(b"RIFF"):
                return
            form = data[8:12]
            if (ext == 'webp' and form != b"WEBP") or (ext == 'avi' and form != b"AVI "):
                return
            chunks = _riff_chunks(data)
            if chunks is None:
                return
            out = bytearray(data[:12])
            for kind, s, e in chunks:
                if kind != b"Rems":
                    out += data[s:e]
            out += b"Rems" + len(payload).to_bytes(4, "little") + payload
            if len(payload) & 1:
                out.append(0)
            out[4:8] = (len(out) - 8).to_bytes(4, "little")
            tmp = filepath + ".meta"
            with open(tmp, "wb") as f:
                f.write(out)
            os.replace(tmp, filepath)
        elif ext == 'gif':
            # Comment Extension after the color table — decoders skip comments
            payload = b"Rems_Dl\n" + meta_text.encode("utf-8")
            parsed = _gif_blocks(data)
            if not parsed:
                return
            gct_end, blocks = parsed
            rest = bytearray()
            for s, e, label in blocks:
                if label == 0xFE and _gif_subblocks(data[s + 2:e - 1]).startswith(b"Rems_Dl\n"):
                    continue  # drop a stale comment from a previous write
                rest += data[s:e]
            out = data[:gct_end] + _gif_comment_block(payload) + bytes(rest)
            tmp = filepath + ".meta"
            with open(tmp, "wb") as f:
                f.write(out)
            os.replace(tmp, filepath)
        elif ext in ('mp4', 'mov'):
            # top-level custom box after ftyp — ISO BMFF parsers skip unknown
            # boxes by size, exactly like the ubiquitous `free` box
            payload = b"Rems_Dl\n" + meta_text.encode("utf-8")
            if data[4:8] != b"ftyp":
                return
            boxes, i, n = [], 0, len(data)
            while i + 8 <= n:
                sz = int.from_bytes(data[i:i + 4], "big")
                if sz == 1:
                    if i + 16 > n:
                        boxes.append(data[i:])
                        break
                    sz = int.from_bytes(data[i + 8:i + 16], "big")
                if sz == 0 or i + sz > n:
                    boxes.append(data[i:])
                    break
                if data[i + 4:i + 8] != b"rems":
                    boxes.append(data[i:i + sz])
                i += sz
            if not boxes:
                return
            rems = (len(payload) + 8).to_bytes(4, "big") + b"rems" + payload
            out = boxes[0] + rems + b"".join(boxes[1:])
            tmp = filepath + ".meta"
            with open(tmp, "wb") as f:
                f.write(out)
            os.replace(tmp, filepath)
        elif ext in ('mkv', 'webm'):
            # standard Matroska <Tags> appended at the end of the Segment and
            # the Segment size patched — nothing before it shifts
            payload = b"Rems_Dl\n" + meta_text.encode("utf-8")
            if data[:4] != b"\x1a\x45\xdf\xa3":
                return
            hdr = _ebml_size(data, 4)
            if not hdr or hdr[1] is None:
                return
            seg = hdr[0] + hdr[1]
            if data[seg:seg + 4] != b"\x18\x53\x80\x67":
                return
            sp = _ebml_size(data, seg + 4)
            if not sp:
                return
            pstart, psize = sp
            simple = _ebml_elem(b"\x67\xc8",
                                _ebml_elem(b"\x45\xa3", b"Rems_Dl") +
                                _ebml_elem(b"\x44\x87", payload))
            tags = _ebml_elem(b"\x12\x54\xc3\x27", _ebml_elem(b"\x73\x73", simple))
            if psize is None:
                out = bytearray(data + tags)  # unknown-size Segment = runs to EOF
            else:
                seg_end = pstart + psize
                if seg_end > len(data):
                    return
                new_size = psize + len(tags)
                vw = pstart - (seg + 4)
                w = vw
                while w < 8 and new_size >= (1 << (7 * w)) - 1:
                    w += 1
                if w == vw:
                    out = bytearray(data[:seg_end] + tags + data[seg_end:])
                    out[seg + 4:pstart] = (new_size | (1 << (7 * vw))).to_bytes(vw, "big")
                else:
                    # ponytail: widening the Segment vint shifts every
                    # SeekHead/Cues offset — only reachable within bytes of a
                    # vint boundary, i.e. never for real gallery files
                    out = bytearray(data[:seg + 4] +
                                    (new_size | (1 << (7 * w))).to_bytes(w, "big") +
                                    data[pstart:seg_end] + tags + data[seg_end:])
            tmp = filepath + ".meta"
            with open(tmp, "wb") as f:
                f.write(out)
            os.replace(tmp, filepath)
        # bmp/tiff: no container mechanism to hang metadata on — stay skipped
    except Exception as e:
        print(f"Metadata write error on {filepath}: {e}")

def read_image_metadata(filepath):
    """Read back what write_image_metadata embedded: JPEG COM, PNG tEXt|iTXt,
    WebP/AVI RIFF `Rems` chunk, GIF comment, MP4/MOV `rems` box, MKV/WebM Tags.

    Returns the raw "site:...\ncat:tag" text, or None when the file carries
    no Rems_Dl block (or is an unsupported format)."""
    try:
        with open(filepath, "rb") as f:
            data = f.read()
    except OSError:
        return None
    try:
        if data[:2] == b"\xff\xd8":
            i = 2
            while i + 4 <= len(data):
                if data[i] != 0xFF:
                    break
                marker = data[i + 1]
                if marker in (0xD8, 0xD9):
                    i += 2
                    continue
                seglen = int.from_bytes(data[i + 2:i + 4], "big")
                if seglen < 2:
                    break
                if marker == 0xFE:
                    payload = data[i + 4:i + 2 + seglen]
                    if payload.startswith(b"Rems_Dl\n"):
                        return payload[8:].decode("utf-8", "ignore")
                if marker == 0xDA:  # SOS — entropy data, no segments after
                    break
                i += 2 + seglen
            return None
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            i = 8
            while i + 12 <= len(data):
                ln = int.from_bytes(data[i:i + 4], "big")
                kind = data[i + 4:i + 8]
                body = data[i + 8:i + 8 + ln]
                if kind == b"tEXt" and body.startswith(b"Rems_Dl\x00"):
                    return body[8:].decode("latin-1", "ignore")
                if kind == b"iTXt" and body.startswith(b"Rems_Dl\x00"):
                    # keyword\0 flag method lang tkey \0 text — exactly the
                    # shape write_image_metadata emits (flag/method/lang/tkey empty)
                    parts = body[8:].split(b"\x00", 4)
                    if len(parts) == 5:
                        return parts[4].decode("utf-8", "ignore")
                if kind == b"IEND":
                    break
                i += 12 + ln
            return None
        if data[:4] == b"RIFF" and data[8:12] in (b"WEBP", b"AVI "):
            for kind, s, _e in _riff_chunks(data) or []:
                if kind == b"Rems":
                    ln = int.from_bytes(data[s + 4:s + 8], "little")
                    body = data[s + 8:s + 8 + ln]
                    if body.startswith(b"Rems_Dl\n"):
                        return body[8:].decode("utf-8", "ignore")
            return None
        if data[:4] == b"GIF8":
            parsed = _gif_blocks(data)
            if parsed:
                for s, e, label in parsed[1]:
                    if label == 0xFE:
                        body = _gif_subblocks(data[s + 2:e - 1])
                        if body.startswith(b"Rems_Dl\n"):
                            return body[8:].decode("utf-8", "ignore")
            return None
        if data[4:8] == b"ftyp":
            i, n = 0, len(data)
            while i + 8 <= n:
                sz = int.from_bytes(data[i:i + 4], "big")
                typ = data[i + 4:i + 8]
                if sz == 1:
                    if i + 16 > n:
                        break
                    sz = int.from_bytes(data[i + 8:i + 16], "big")
                if sz == 0 or i + sz > n:
                    break
                if typ == b"rems" and data[i + 8:i + 16] == b"Rems_Dl\n":
                    return data[i + 16:i + sz].decode("utf-8", "ignore")
                i += sz
            return None
        if data[:4] == b"\x1a\x45\xdf\xa3":
            return _ebml_find_rems(data)
    except Exception as e:
        print(f"Metadata read error on {filepath}: {e}")
    return None

# --- HISTORY SYSTEM ---
def load_history(site_root):
    hist_path = os.path.join(site_root, "download_history.json")
    with HISTORY_LOCK:
        if os.path.exists(hist_path):
            try:
                with open(hist_path, "r", encoding="utf-8") as f: return set(json.load(f))
            except Exception: return set()
        return set()

def save_history(site_root, history_set):
    safe_ensure_dir(site_root)
    hist_path = os.path.join(site_root, "download_history.json")
    with HISTORY_LOCK:
        try:
            tmp = hist_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f: json.dump(list(history_set), f)
            os.replace(tmp, hist_path)
        except Exception as e: print(f"Error saving history: {e}")


# --- PERSISTENT IMAGE DEDUP (SQLite-backed, cross-session) ---
# ponytail: one helper here, not a copy in every worker — all download paths
# route through _async_download_file (plus 5 custom paths that import this).
_DEDUP_SKIP_EXTS = {'.mp4', '.webm', '.mov', '.avi', '.mkv', '.zip'}

def check_duplicate(filepath, site, post_id=None):
    """Hash `filepath` and check it against everything downloaded so far.

    Returns a DedupResult, or None when dedup doesn't apply (disabled in settings,
    non-image file, unreadable file, or store unavailable) — the caller keeps the file. """
    try:
        from core.database import get_settings
        if not get_settings().get("dedup_enabled", True):
            return None
    except Exception:
        pass

    ext = os.path.splitext(str(filepath))[1].lower()
    if ext in _DEDUP_SKIP_EXTS:
        return None
    try:
        from core.dedup_store import get_store
        pid = str(post_id) if post_id is not None else None
        return get_store().check_and_add(str(filepath), site=site, post_id=pid)
    except Exception as e:
        print(f"[DEDUP] check skipped for {filepath}: {e}")
        return None


# ==========================================
# === OOP ASYNCIO ENGINE ===
# ==========================================

# --- ISP egress rate limit (Settings → req_rate_limit, default 4 req/s) ---
# One budget for the whole process: every aiohttp request (page scans, tag
# fan-out, downloads) passes through the trace hook in _create_session, and
# sync curl_cffi paths call pace_wait(). ponytail: r/s cap only — streaming
# connections aren't counted, just request starts.
_pace_lock = threading.Lock()
_pace_next = 0.0


def _rate_interval():
    try:
        rps = float(os.environ.get("REQ_RATE_LIMIT", "4"))
    except ValueError:
        return 0.25
    return 1.0 / rps if rps > 0 else 0.0


def _pace_slot():
    """Reserve the next request-start slot; returns seconds to wait for it."""
    global _pace_next
    interval = _rate_interval()
    if interval <= 0:
        return 0.0
    with _pace_lock:
        now = time.monotonic()
        slot = max(now, _pace_next)
        _pace_next = slot + interval
        return slot - now


def pace_wait():
    """Sync pacing (curl_cffi/requests threads) — same budget as pace_requests."""
    delay = _pace_slot()
    if delay > 0:
        time.sleep(delay)


async def pace_requests():
    delay = _pace_slot()
    if delay > 0:
        await asyncio.sleep(delay)


async def _pace_on_request_start(session, ctx, params):
    await pace_requests()


def pace_session(session):
    """Wrap session.request once so sync callers (requests / curl_cffi) join
    the global ISP pace — every verb funnels through request()."""
    if getattr(session, "_isp_paced", False):
        return session
    raw_request = session.request

    def request(*args, **kwargs):
        pace_wait()
        return raw_request(*args, **kwargs)

    session.request = request
    session._isp_paced = True
    return session


def _seed_md5(path, digest):
    with open(path, 'rb') as pf:
        for blk in iter(lambda: pf.read(65536), b""):
            digest.update(blk)


class BaseDownloader:
    def __init__(self, name, site_folder, amount, net_config):
        self.name = name
        self.site_folder = site_folder
        self.amount = max(0, int(amount))
        self.net_config = net_config

        self.stop_event = threading.Event()
        # ponytail: replace, not append — stale events from dead runs must never let
        # one STOP press kill a freshly started worker. One live worker per name.
        STOP_EVENTS[name] = [self.stop_event]

        self.anti_ban_pause = float(net_config.get("anti_ban_pause", 3.0))
        self.dl_retries = int(net_config.get("download_retries", 3))

        safe_folder = sanitize_path_component(site_folder, fallback=self.name)
        self.site_root = os.path.join(MASTER_FOLDER, safe_folder)
        os.makedirs(self.site_root, exist_ok=True)
        self.dl_history = load_history(self.site_root)

        self.session = None  # created lazily in _create_session
        self.downloaded_count = 0
        self.failed_count = 0
        self.duplicate_count = 0
        self.downloaded_bytes = 0
        self.total_bytes = 0
        self.total_to_download = 0
        self.download_queue = None
        self.is_scanning = False
        self.enqueued_count = 0
        self.queued_items = set()
        self._history_dirty = 0
        # live progress: filepath -> fraction of the current file downloaded
        self._inflight = {}
        self._last_progress_emit = 0.0

    def _progress_pct(self):
        # same target math as the [SUCCESS] log line, plus fractional units
        # for files still streaming — the bar moves while bytes arrive
        if self.is_scanning and self.amount > 0:
            target = max(self.amount, self.enqueued_count)
        else:
            target = max(self.enqueued_count, self.downloaded_count)
        if target <= 0:
            return 0.0
        inflight = sum(self._inflight.values())
        return min(100.0, (self.downloaded_count + inflight) / target * 100)

    def _emit_progress(self):
        pct = self._progress_pct()
        if pct > 0:
            socketio_emit("dl_progress", {"worker": self.name, "pct": round(pct, 1)})

    def check_amount_warning(self, total_found):
        """Helper to warn the user if they requested more images than were retrieved."""
        if total_found > 0:
            self.log(f"Total valid items found: {total_found}")
            if self.amount > 0 and total_found < self.amount:
                extra = f" ({self.failed_count} failed)" if self.failed_count else ""
                self.log(f"⚠️ Notice: You requested {self.amount} images, but only {total_found} were downloaded{extra}. The site may have blocked further pages, or downloads failed.")

    # --- aiohttp session factory (override in subclasses for curl_cffi etc.) ---
    async def _create_session(self):
        """Create and return an aiohttp.ClientSession with proxy and headers."""
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        headers = {
            "User-Agent": "Rems_Dl/5.0 (by RemLover on GitHub)",
            "Accept": "application/json",
        }
        proxy = None
        if self.net_config.get("use_proxy"):
            proxy = self.net_config.get("proxy_url")
        if not proxy:
            proxy = os.environ.get("https_proxy") or os.environ.get("http_proxy") or os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")

        connector = aiohttp.TCPConnector(
            ssl=False if not self.net_config.get("verify_tls", False) else None,
            limit=10,
        )
        trace = aiohttp.TraceConfig()
        trace.on_request_start.append(_pace_on_request_start)
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers,
            proxy=proxy,
            trace_configs=[trace],
        )
        self.proxy = proxy
        return session

    def log(self, msg): log_msg(self.name, msg)

    def _remember_filename(self, filename):
        # park the name in dl_history so the next run's enqueue/download skips
        # it — phash kills used to leave no trace, so the same file was fetched
        # again just to be hash-killed again. batched like successes (rewriting
        # the whole history file per hit is O(history) each time)
        self.dl_history.add(filename)
        self._history_dirty += 1
        if self._history_dirty >= 10:
            save_history(self.site_root, self.dl_history)
            self._history_dirty = 0

    async def enqueue_download(self, url, filepath, filename, tags_list, artists=None, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None, rating=None):
        if artists is None: artists = []
        
        # Defensive sanitization against prohibited filesystem characters in folders and filenames
        clean_filename = sanitize_filename(filename, fallback="image.jpg")
        file_dir = os.path.dirname(filepath)
        safe_dir = safe_ensure_dir(file_dir) if file_dir else self.site_root
        filepath = os.path.join(safe_dir, clean_filename)
        filename = clean_filename

        try:
            if filename in self.dl_history or filename in self.queued_items or os.path.exists(filepath):
                return False
        except Exception:
            if filename in self.dl_history or filename in self.queued_items:
                return False

        # ponytail: no HEAD request here — it cost a full round-trip per file
        # just to feed total_bytes, which nothing reads. The GET reconciles
        # sizes itself (see content_length handling below).
        file_size = 0
            
        self.total_bytes += file_size
        self.queued_items.add(filename)
        self.download_queue.put_nowait((url, filepath, filename, tags_list, artists, file_size, characters, copyrights, metadata_tags, outfits, groups, hair, eyes, rating))
        self.enqueued_count += 1
        return True

    async def _async_download_file(self, url, filepath, filename, tags_list, artists, file_size=0, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None, rating=None):
        if self.stop_event.is_set():
            self.enqueued_count -= 1
            return False

        # Ensure parent directory exists before writing .part file
        parent_dir = os.path.dirname(filepath)
        if parent_dir:
            safe_ensure_dir(parent_dir)

        part_path = filepath + ".part"
        # booru filenames embed their md5 (-<32hex>.ext); hash incrementally
        # during download instead of re-reading the whole file afterwards
        m = re.search(r'-([0-9a-f]{32})\.[^.]+$', filename, re.I)
        want_md5 = m.group(1).lower() if m else None
        h = hashlib.md5() if want_md5 else None
        for attempt in range(self.dl_retries):
            try:
                referer = self.session.headers.get("Referer") or url
                # a truncated attempt keeps its .part — the retry resumes it
                # with a Range request instead of restarting a big file at 0
                headers = {"Referer": referer}
                base = os.path.getsize(part_path) if os.path.exists(part_path) else 0
                if base:
                    headers["Range"] = f"bytes={base}-"
                # ponytail: downloads need way more than the API's 30s total timeout
                # sock_read kills stalled/trickling connections fast; a bare
                # total timeout lets a dead stream hang for minutes looking
                # like the worker "stopped"
                async with self.session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=300, connect=10, sock_read=30)) as resp:
                    resumed = bool(base) and getattr(resp, "status", None) == 206
                    resp.raise_for_status()
                    if resumed:
                        downloaded = base
                        total_expected = (base + int(resp.headers.get('Content-Length', 0))) or file_size
                    else:
                        downloaded = 0
                        total_expected = int(resp.headers.get('Content-Length', 0)) or file_size
                        if total_expected and (total_expected != file_size):
                            self.total_bytes += (total_expected - file_size)

                    if h is not None:
                        h = hashlib.md5()
                        if resumed:
                            # seed the incremental hash with the bytes already on disk
                            await asyncio.to_thread(_seed_md5, part_path, h)
                    with open(part_path, 'ab' if resumed else 'wb') as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            if self.stop_event.is_set(): break
                            f.write(chunk)
                            if h is not None:
                                h.update(chunk)
                            downloaded += len(chunk)
                            # live bar updates ~5/s while bytes stream in
                            now = time.monotonic()
                            if total_expected and now - self._last_progress_emit >= 0.2:
                                self._last_progress_emit = now
                                self._inflight[filepath] = downloaded / total_expected
                                self._emit_progress()

                    # ponytail: proxies can drop the tail silently; verify against
                    # Content-Length, or against the enqueue-time HEAD size when the
                    # response is content-encoded (CL then describes compressed bytes)
                    if not resp.headers.get('Content-Encoding') or file_size:
                        if total_expected and downloaded != total_expected:
                            raise Exception(f"Incomplete download: got {downloaded} of {total_expected} bytes")

                if self.stop_event.is_set():
                    if os.path.exists(part_path): os.remove(part_path)
                    self.enqueued_count -= 1
                    return False

                # booru filenames embed their md5 (-<32hex>.ext); a CDN serving a
                # stale recompressed variant passes size checks, so verify the
                # incrementally computed hash — no second read of the file
                if want_md5 and h.hexdigest() != want_md5:
                    os.remove(part_path)
                    raise Exception("md5 mismatch: server sent a different/degraded file")

                # publish under the real name only after full verification — a
                # killed app must never leave a truncated file posing as complete
                os.replace(part_path, filepath)

                # persistent perceptual-hash dedup: skip re-uploads/re-encodes
                # already seen (any site, any session) before counting success.
                # to_thread: hashing decodes the image — running it on the event
                # loop would stall this worker's other 3 download tasks
                dup = await asyncio.to_thread(check_duplicate, filepath, self.name)
                if dup is not None and dup.is_duplicate:
                    try:
                        os.remove(filepath)
                    except OSError:
                        pass
                    self.enqueued_count -= 1
                    self.duplicate_count += 1
                    await asyncio.to_thread(self._remember_filename, filename)
                    await asyncio.to_thread(absorb_duplicate, dup, self.name, tags_list,
                                            artists, characters, copyrights, metadata_tags,
                                            outfits, groups, hair, eyes)
                    return False

                self.downloaded_count += 1
                self.downloaded_bytes += downloaded
                self.dl_history.add(filename)
                # ponytail: rewriting the whole history file per download is
                # O(history) each time — flush every 10, plus a final flush
                # when the loop ends
                self._history_dirty += 1
                if self._history_dirty >= 10:
                    await asyncio.to_thread(save_history, self.site_root, self.dl_history)
                    self._history_dirty = 0

                # درصدگیری بی‌نقص بر اساس Limit
                if self.is_scanning and self.amount > 0:
                    target_total = max(self.amount, self.enqueued_count)
                else:
                    target_total = max(self.enqueued_count, self.downloaded_count)

                pct = int((self.downloaded_count / target_total) * 100) if target_total > 0 else 0

                rel_path = os.path.relpath(filepath, MASTER_FOLDER)
                top_tags = ", ".join(tags_list[:5]) if tags_list else "No tags"
                tagd = build_tagd(artists, characters, copyrights, metadata_tags, outfits, groups, hair, eyes, tags_list)

                # send_tags first: the viewer's tag box reads imageHistory via
                # the update_history socket, so the history row must exist
                # before the image is clickable
                await asyncio.to_thread(send_tags, self.name, filename, tags_list, artists, rel_path, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
                # ponytail: metadata + gallery publish BEFORE the SUCCESS log — the log card
                # requests its thumb instantly and would otherwise read a half-written file
                await asyncio.to_thread(write_image_metadata, filepath, tags_list, artists, self.name, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
                await asyncio.to_thread(add_to_gallery, self.name, filename, rel_path, tags_list, artists, characters, copyrights, metadata_tags, outfits, groups, hair, eyes, rating)
                self.log(f"[SUCCESS] Downloaded {filename} ({self.downloaded_count}/{target_total}) [{pct}%] |PATH| {rel_path} |TAGS| {top_tags} |TAGD| {tagd}")
                return True

            except Exception as e:
                if self.stop_event.is_set():
                    if os.path.exists(part_path): os.remove(part_path)
                    self.enqueued_count -= 1
                    break
                # truncated streams keep the .part so the next attempt resumes;
                # HTTP-status rejections and md5 mismatches start over
                if isinstance(e, aiohttp.ClientResponseError) or "md5 mismatch" in str(e):
                    if os.path.exists(part_path): os.remove(part_path)
                if attempt < self.dl_retries - 1: 
                    await asyncio.sleep(2)
                else: 
                    if os.path.exists(part_path): os.remove(part_path)
                    self.enqueued_count -= 1
                    err_msg = str(e).strip()
                    if not err_msg: err_msg = "HTTP 404 / File Deleted from Server"
                    self.log(f"[FAILED] {filename}: {err_msg}")
                    self.failed_count += 1
                    if os.path.exists(filepath): os.remove(filepath)
        return False

    async def _download_worker(self):
        while not self.stop_event.is_set():
            if self.download_queue.empty():
                if not self.is_scanning: break
                await asyncio.sleep(0.5)
                continue
            item = await self.download_queue.get()
            try: await self._async_download_file(*item)
            finally:
                self._inflight.pop(item[1], None)
                self.download_queue.task_done()

    def run(self):
        """Synchronous entry point shared by every worker.

        Override only when the worker needs setup/teardown around the loop
        (e.g. pinterest's proxy env save/restore).
        """
        asyncio.run(self.run_async_loop(self.scraper_task))

    async def run_async_loop(self, scraper_coroutine):
        try:
            self.session = await self._create_session()
            self.log(f"Proxy: {self.proxy or 'Direct'}")
            self.download_queue = asyncio.Queue()
            self.is_scanning = True

            num_workers = 4
            download_tasks = [asyncio.create_task(self._download_worker()) for _ in range(num_workers)]

            self.log("Phase 1: Gathering links from API... Please wait.")
            try: await scraper_coroutine()
            except Exception as e: self.log(f"Scraper Error: {e}")

            self.is_scanning = False
            # ponytail: always join the queue so in-flight downloads finish
            # before we check counts or cancel workers.  Previously we
            # skipped join() when total_to_download looked like 0 (qsize
            # doesn't count in-flight items), which cancelled workers mid-
            # download via BaseException — file landed on disk but gallery,
            # metadata, history, and SUCCESS log were never written.
            # a STOP during phase 2 orphans pending items (workers exit at
            # the loop top without task_done) — a bare join() then waits
            # forever and pins this site's ACTIVE_JOBS slot, so every later
            # START just enqueues behind the corpse
            while not self.stop_event.is_set():
                try:
                    await asyncio.wait_for(self.download_queue.join(), 0.5)
                    break
                except asyncio.TimeoutError:
                    continue
            for t in download_tasks: t.cancel()

            # ponytail: dedicated finish signal — log parsing alone is too fragile to drive UI state
            try:
                socketio_emit("worker_finished", finish_report(
                    self.name, self.downloaded_count, self.failed_count,
                    self.duplicate_count, bool(self.stop_event.is_set()), self.log))
            except Exception:
                pass
        except Exception as critical_e:
            self.log(f"CRITICAL ERROR: {critical_e}")
        finally:
            # flush anything batched during the run (gallery appends, history)
            try:
                await asyncio.to_thread(flush_gallery)
            except Exception:
                pass
            try:
                if getattr(self, "_history_dirty", 0):
                    await asyncio.to_thread(save_history, self.site_root, self.dl_history)
                    self._history_dirty = 0
            except Exception:
                pass
            try:
                from core.database import DatabaseManager
                await asyncio.to_thread(DatabaseManager.flush_image_history)
            except Exception:
                pass
            if self.session and hasattr(self.session, 'closed') and not self.session.closed:
                await self.session.close()

        if self.name in STOP_EVENTS and self.stop_event in STOP_EVENTS[self.name]:
            STOP_EVENTS[self.name].remove(self.stop_event)
