import os
import re
import sys
import threading
import json
import time
import asyncio
import hashlib
import aiohttp
from PIL import Image, PngImagePlugin

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
    "wallhaven": "wallhaven", "wallhaven.cc": "wallhaven",
    "anime_dl": "anime_dl", "animepictures": "anime_dl",
}

def normalize_site(site):
    return SITE_CANONICAL.get(site.lower().strip(), site.lower().strip())

def categorize_tag(tag):
    """Determine the category of a tag based on common booru prefix patterns."""
    tag_lower = tag.lower().strip()
    for cat in TAG_CATEGORIES:
        prefix = cat + ":"
        if tag_lower.startswith(prefix):
            return cat
    return "tag"

def sort_tags_by_category(tags_dict):
    """Sort a tags dict by category order. Returns new ordered dict."""
    from collections import OrderedDict
    result = OrderedDict()
    for cat in TAG_CATEGORIES:
        if cat in tags_dict and tags_dict[cat]:
            result[cat] = sorted(tags_dict[cat])
    return result

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

def add_to_gallery(site, filename, filepath, tags_list, artists, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None):
    with _GALLERY_LOCK:
        gallery = _gallery_cached_locked()
        if filename in _gallery_cache["filenames"]:
            return
        tags_dict = tags_dict_from_lists(tags_list, artists, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
        gallery["images"].insert(0, {
            "id": hashlib.md5(f"{site}:{filename}".encode()).hexdigest()[:12],
            "filename": filename,
            "filepath": filepath,
            "site": normalize_site(site),
            "tags": dict(tags_dict),
            "favourite": False,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S")
        })
        _gallery_cache["filenames"].add(filename)
        _gallery_cache["dirty"] += 1
        if _gallery_cache["dirty"] >= _GALLERY_FLUSH_EVERY:
            _write_gallery(gallery)
            _gallery_cache["dirty"] = 0

def write_image_metadata(filepath, tags_list, artists, site, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None):
    ext = filepath.rsplit('.', 1)[-1].lower() if '.' in filepath else ''
    tags_dict = tags_dict_from_lists(tags_list, artists, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
    meta_lines = [f"site:{site}"]
    for cat in TAG_CATEGORIES:
        for t in tags_dict.get(cat, []):
            meta_lines.append(f"{cat}:{t}")
    meta_text = "\n".join(meta_lines)

    try:
        if ext in ('jpg', 'jpeg'):
            # inject a COM segment at byte level — saving through PIL here would
            # recompress the image (3MB originals were shrinking to ~1.4MB)
            payload = b"Rems_Dl\n" + meta_text.encode("utf-8")
            payload = payload[:65531]
            with open(filepath, "rb") as f:
                data = f.read()
            if not data.startswith(b"\xff\xd8"):
                return
            # segment length includes the 2 length bytes themselves
            seg = b"\xff\xfe" + (len(payload) + 2).to_bytes(2, "big") + payload
            tmp = filepath + ".meta"
            with open(tmp, "wb") as f:
                f.write(data[:2] + seg + data[2:])
            os.replace(tmp, filepath)
        elif ext == 'png':
            pnginfo = PngImagePlugin.PngInfo()
            pnginfo.add_text("Rems_Dl", meta_text)
            img = Image.open(filepath)
            # tmp+replace like the jpg branch — saving over the live file
            # leaves a truncated image if we crash mid-write
            tmp = filepath + ".meta"
            img.save(tmp, pnginfo=pnginfo)  # lossless for PNG
            os.replace(tmp, filepath)
        # webp/gif skipped: embedding would re-encode and lose quality
    except Exception as e:
        print(f"Metadata write error on {filepath}: {e}")

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
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers=headers,
            proxy=proxy,
        )
        self.proxy = proxy
        return session

    def log(self, msg): log_msg(self.name, msg)

    async def enqueue_download(self, url, filepath, filename, tags_list, artists=None, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None):
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
        self.download_queue.put_nowait((url, filepath, filename, tags_list, artists, file_size, characters, copyrights, metadata_tags, outfits, groups, hair, eyes))
        self.enqueued_count += 1
        return True

    async def _async_download_file(self, url, filepath, filename, tags_list, artists, file_size=0, characters=None, copyrights=None, metadata_tags=None, outfits=None, groups=None, hair=None, eyes=None):
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
                            with open(part_path, 'rb') as pf:
                                for blk in iter(lambda: pf.read(65536), b""):
                                    h.update(blk)
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
                    return False

                self.downloaded_count += 1
                self.downloaded_bytes += downloaded
                self.dl_history.add(filename)
                # ponytail: rewriting the whole history file per download is
                # O(history) each time — flush every 10, plus a final flush
                # when the loop ends
                self._history_dirty += 1
                if self._history_dirty >= 10:
                    save_history(self.site_root, self.dl_history)
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

                # ponytail: metadata + gallery publish BEFORE the SUCCESS log — the log card
                # requests its thumb instantly and would otherwise read a half-written file
                # to_thread: PNG metadata re-encodes the whole image, too slow for the loop
                await asyncio.to_thread(write_image_metadata, filepath, tags_list, artists, self.name, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
                add_to_gallery(self.name, filename, rel_path, tags_list, artists, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
                self.log(f"[SUCCESS] Downloaded {filename} ({self.downloaded_count}/{target_total}) [{pct}%] |PATH| {rel_path} |TAGS| {top_tags} |TAGD| {tagd}")
                await asyncio.to_thread(send_tags, self.name, filename, tags_list, artists, rel_path, characters, copyrights, metadata_tags, outfits, groups, hair, eyes)
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
            if not self.stop_event.is_set():
                await self.download_queue.join()
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
                flush_gallery()
            except Exception:
                pass
            try:
                if getattr(self, "_history_dirty", 0):
                    save_history(self.site_root, self.dl_history)
                    self._history_dirty = 0
            except Exception:
                pass
            if self.session and hasattr(self.session, 'closed') and not self.session.closed:
                await self.session.close()

        if self.name in STOP_EVENTS and self.stop_event in STOP_EVENTS[self.name]:
            STOP_EVENTS[self.name].remove(self.stop_event)
