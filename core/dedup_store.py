"""core/dedup_store.py — Persistent perceptual-hash dedup store for Rems-Dl.

Every image that gets downloaded is hashed once and its signature is saved
to a small SQLite database (database/dedup.sqlite3, next to the other files
DatabaseManager already manages). Every NEW image is checked against
everything stored so far before it's kept, so duplicates (including
re-uploads/re-encodes across different boorus) get caught even across
separate download sessions.

Detection logic:
  - phash / dhash / ahash / whash (structural perceptual hashing)
  - global color histogram + 4x4 grid color histograms
A pair only counts as a duplicate if structural hashes AND color profiles
are both sufficiently similar.

Normalization (so the same artwork with different padding/canvas matches):
  - transparency is flattened onto white (never onto whatever RGB happens to
    sit under transparent pixels, which differs between encoders)
  - the picture is then trimmed to its content box (flat uniform border /
    letterbox) before hashing, so padding and canvas size don't matter
Keep-larger: when a new image is a duplicate of a stored one but has more
content pixels (ties: bigger file), the new file is kept, the stored record is
repointed to it (all source hashes stay attached) and the smaller old file is
removed; the result comes back with is_duplicate=False and replaced_path set.

Performance & Stability:
  - Pure Python + Pillow: zero heavy scientific dependencies required (no scipy/pywt/numpy),
    meaning instant startup and lightweight RAM footprint (< 2MB).
  - Optional imagehash interoperability: gracefully uses external imagehash if available.
  - Thread-safe SQLite handling with WAL mode and in-memory phash index.
  - Auto-prunes ghost records if an original file was deleted from disk so redownload works.
"""

import hashlib
import json
import math
import multiprocessing
import os
import re
import sqlite3
import sys
import threading
import time
from collections import deque
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageOps

# --- paths, following the same convention as core/database.py -------------

from core.runtime_paths import app_base_dir as _app_base_dir


DATABASE_DIR = os.path.join(_app_base_dir(), "database")
DEDUP_DB_FILE = os.path.join(DATABASE_DIR, "dedup.sqlite3")


# every hash format the supported sites can be matched with; the API fields
# all answer md5 today, sha1/sha256 are kept so a site that switches or a
# future site with another format still finds its images
HASH_ALGOS = ("md5", "sha1", "sha256")
_HASH_HEX_LEN = {"md5": 32, "sha1": 40, "sha256": 64}


def normalize_source_hash(algo, value) -> str | None:
    """Valid lowercase hex digest of the named algorithm, else None."""
    n = _HASH_HEX_LEN.get(str(algo).lower())
    if not n or not isinstance(value, str):
        return None
    v = value.strip().lower()
    if len(v) != n:
        return None
    try:
        int(v, 16)
    except ValueError:
        return None
    return v


def normalize_source_md5(value) -> str | None:
    """Valid 32-hex md5 -> lowercase; anything else (incl. 40-hex sha1) -> None."""
    return normalize_source_hash("md5", value)


def normalize_any_source_hash(value) -> tuple | None:
    """(algo, lowercase hex) for a bare digest of any supported length, else None.

    Length alone picks the algo: md5/sha1/sha256 never share a hex length,
    so whatever hash type a site hands us classifies itself.
    """
    if not isinstance(value, str):
        return None
    v = value.strip().lower()
    algo = next((a for a, n in _HASH_HEX_LEN.items() if len(v) == n), None)
    digest = normalize_source_hash(algo, v) if algo else None
    return (algo, digest) if digest else None


def normalize_generated_hashes(hashes) -> dict:
    """{algo: hex} -> validated lowercase subset (unknown/ill-formed dropped)."""
    out = {}
    if isinstance(hashes, dict):
        for algo, value in hashes.items():
            v = normalize_source_hash(algo, value)
            if v:
                out[str(algo).lower()] = v
    return out


def hash_bytes(data: bytes) -> dict:
    """{algo: hexdigest} of these exact bytes — the formats to store for a
    site whose file we just received."""
    return {algo: hashlib.new(algo, data).hexdigest() for algo in HASH_ALGOS}


def hash_file(filepath: str) -> dict:
    """{algo: hexdigest} of the file's bytes — one read, all algorithms."""
    hashers = {algo: hashlib.new(algo) for algo in HASH_ALGOS}
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            for h in hashers.values():
                h.update(chunk)
    return {algo: h.hexdigest() for algo, h in hashers.items()}


# --- hashing config (tuned so palette-swaps/recolors are NOT duplicates,
#     while resizes/recompressions/minor crops ARE) -------------------------

MAX_DIM = 512
HASH_SIZE = 16
GRID = 4
COLOR_BINS = 24

THRESHOLDS = {
    "phash": 12,
    "dhash": 10,
    "ahash": 12,
    "whash": 12,
}

# bump when the hashing pipeline changes: rows stored with an older version are
# recomputed in place by DedupStore.rehash_outdated()
SIG_VERSION = 2
AUTO_UPGRADE_SIGNATURES = True      # get_store() rehashes old rows in a background thread

# content-box trimming
BORDER_TOL = 18                     # per-channel tolerance for the flat border color
MIN_CONTENT_FRAC = 0.04             # ignore a trim that would leave less than this

# keep-larger behaviour
KEEP_LARGER = True                  # a bigger duplicate replaces the stored smaller one
DELETE_REPLACED = True              # remove the smaller file from disk when replaced
LARGER_PIXEL_MARGIN = 1.02          # needs >2% more content pixels; closer -> file size decides

# parallel batch processing (check_and_add_many / add_missing / rehash_outdated)
DEFAULT_WORKERS = max(2, min(8, os.cpu_count() or 2))
PARALLEL_EXECUTOR = "thread"        # "thread" | "process" (see _make_pool)
PARALLEL_WINDOW = 4                 # signatures kept in flight per worker (bounds memory)
COLOR_SIM_THRESHOLD = 0.80
LOCAL_COLOR_SIM_THRESHOLD = 0.75
PREFILTER_MARGIN = THRESHOLDS["phash"]


# --- Pure-Python Perceptual Hash Implementation --------------------------

class PureHash:
    """Lightweight 64/256-bit perceptual image hash with zero external dependencies."""
    __slots__ = ("bits", "value")

    def __init__(self, value: int, bits: int = 256):
        self.value = int(value)
        self.bits = bits

    def __sub__(self, other) -> int:
        if isinstance(other, PureHash):
            diff = self.value ^ other.value
        elif isinstance(other, int):
            diff = self.value ^ other
        elif isinstance(other, str):
            diff = self.value ^ int(other, 16)
        else:
            diff = self.value ^ int(str(other), 16)
        return diff.bit_count()

    def __str__(self) -> str:
        hex_len = (self.bits + 3) // 4
        return f"{self.value:0{hex_len}x}"

    def __repr__(self) -> str:
        return f"PureHash({self!s})"

    def __int__(self) -> int:
        return self.value

    def __eq__(self, other) -> bool:
        if isinstance(other, PureHash):
            return self.value == other.value
        return str(self) == str(other)

    def __hash__(self) -> int:
        return hash(self.value)

    def __len__(self) -> int:
        return self.bits


# Precomputed 16x32 DCT-II basis matrix for 256-bit pHash
_DCT_M16 = [
    [math.cos(math.pi * (2 * n + 1) * k / 64.0) for n in range(32)]
    for k in range(16)
]


def _compute_phash_pure(img: Image.Image) -> PureHash:
    gray = img.convert('L').resize((32, 32), Image.Resampling.BILINEAR)
    pixels = list(gray.tobytes())
    T = [[0.0] * 32 for _ in range(16)]
    for k in range(16):
        m_row = _DCT_M16[k]
        for col in range(32):
            s = 0.0
            for n in range(32):
                s += m_row[n] * pixels[n * 32 + col]
            T[k][col] = s
    coeffs = []
    for k in range(16):
        t_row = T[k]
        for l in range(16):
            m_col = _DCT_M16[l]
            s = 0.0
            for col in range(32):
                s += t_row[col] * m_col[col]
            coeffs.append(s)
    # Exclude DC term (0,0) for median threshold
    ac = coeffs[1:]
    sorted_ac = sorted(ac)
    med = sorted_ac[len(sorted_ac) // 2]
    h = 0
    for i, c in enumerate(coeffs):
        if c > med:
            h |= (1 << i)
    return PureHash(h, bits=256)


def _compute_dhash_pure(img: Image.Image) -> PureHash:
    gray = img.convert('L').resize((17, 16), Image.Resampling.BILINEAR)
    pixels = list(gray.tobytes())
    h = 0
    for r in range(16):
        row = r * 17
        for c in range(16):
            if pixels[row + c] > pixels[row + c + 1]:
                h |= (1 << (r * 16 + c))
    return PureHash(h, bits=256)


def _compute_ahash_pure(img: Image.Image) -> PureHash:
    gray = img.convert('L').resize((16, 16), Image.Resampling.BILINEAR)
    pixels = list(gray.tobytes())
    avg = sum(pixels) / 256.0
    h = 0
    for i, p in enumerate(pixels):
        if p > avg:
            h |= (1 << i)
    return PureHash(h, bits=256)


def _compute_whash_pure(img: Image.Image) -> PureHash:
    gray = img.convert('L').resize((16, 16), Image.Resampling.BILINEAR)
    pixels = [float(p) for p in gray.tobytes()]
    med = sorted(pixels)[128]
    val = 0
    for i, p in enumerate(pixels):
        if p > med:
            val |= (1 << i)
    return PureHash(val, bits=256)


# Try importing external imagehash if available, else fallback cleanly
_USE_EXTERNAL_IMAGEHASH = False
try:
    import imagehash
    # Test if it actually functions without missing scipy/pywt
    _test_img = Image.new('L', (16, 16))
    imagehash.dhash(_test_img, hash_size=16)
    _USE_EXTERNAL_IMAGEHASH = True
except Exception:
    _USE_EXTERNAL_IMAGEHASH = False


@dataclass
class Signature:
    phash: PureHash
    dhash: PureHash
    ahash: PureHash
    whash: PureHash
    color_hist: list[float]
    grid_hists: list[list[float]]
    content_w: int = 0   # size of the picture itself (transparent/flat margins excluded)
    content_h: int = 0


@dataclass
class DedupResult:
    is_duplicate: bool
    filepath: str
    matched_path: str | None = None
    matched_site: str | None = None
    matched_post_id: str | None = None
    matched_by: str = "phash"  # "phash" | "source_md5"
    # set when the new file was a duplicate but larger: it is kept
    # (is_duplicate=False) and this smaller stored file was replaced by it
    replaced_path: str | None = None


# --- color histogram / correlation helpers --------------------------------

def _color_histogram(img: Image.Image, bins: int = COLOR_BINS) -> list[float]:
    thumb = img.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
    hist: list[float] = []
    for channel in thumb.split():
        h = channel.histogram()
        step = 256 // bins
        rebinned = [sum(h[i:i + step]) for i in range(0, 256, step)]
        # 3-point kernel smoothing to avoid quantization noise at bin boundaries
        smoothed = [0.0] * bins
        for i in range(bins):
            left = rebinned[i - 1] if i > 0 else rebinned[i]
            center = rebinned[i]
            right = rebinned[i + 1] if i < bins - 1 else rebinned[i]
            smoothed[i] = 0.2 * left + 0.6 * center + 0.2 * right
        total = sum(smoothed) or 1.0
        hist.extend([v / total for v in smoothed])
    return hist


def _grid_histograms(img: Image.Image, grid: int = GRID) -> list[list[float]]:
    thumb = img.convert("RGB").resize((64, 64), Image.Resampling.BILINEAR)
    w, h = thumb.size
    cw, ch = w // grid, h // grid
    hists = []
    for gy in range(grid):
        for gx in range(grid):
            box = (gx * cw, gy * ch, (gx + 1) * cw, (gy + 1) * ch)
            hists.append(_color_histogram(thumb.crop(box)))
    return hists


def _hist_corr(h1: list[float], h2: list[float]) -> float:
    n = len(h1)
    if n == 0:
        return 1.0
    m1 = sum(h1) / n
    m2 = sum(h2) / n
    num = sum((a - m1) * (b - m2) for a, b in zip(h1, h2))
    d1 = sum((a - m1) ** 2 for a in h1) ** 0.5
    d2 = sum((b - m2) ** 2 for b in h2) ** 0.5
    if d1 == 0 or d2 == 0:
        return 1.0 if d1 == d2 else 0.0
    return num / (d1 * d2)


def _content_bbox(rgb: Image.Image):
    """Box of the actual picture inside a flat-colored border, else None.

    Works on the image AFTER transparency is flattened onto white, so a
    transparent PNG and its white-background re-upload trim to the same box
    (an alpha-based box would include faint near-invisible fringe pixels that
    a flattened copy no longer has). Only trims when all four corners agree on
    the border color, i.e. real padding / letterboxing.
    """
    w, h = rgb.size
    corners = [rgb.getpixel(p) for p in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1))]
    bg = corners[0]
    for c in corners[1:]:
        if max(abs(c[i] - bg[i]) for i in range(3)) > BORDER_TOL:
            return None
    diff = ImageChops.difference(rgb, Image.new("RGB", rgb.size, bg))
    r, g, b = diff.split()
    diff = ImageChops.lighter(ImageChops.lighter(r, g), b)
    return diff.point([0] * (BORDER_TOL + 1) + [255] * (255 - BORDER_TOL)).getbbox()


def _load_content(path: str) -> Image.Image:
    """RGB image of just the picture: EXIF-rotated, flattened on white, margins trimmed."""
    with Image.open(path) as img:
        img.load()
        try:
            if img.getexif().get(0x0112, 1) != 1:
                img = ImageOps.exif_transpose(img)
        except Exception:
            pass
        try:
            rgba = img.convert("RGBA")
        except Exception:
            rgba = img.convert("RGB").convert("RGBA")
    if rgba.getchannel("A").getextrema()[0] < 255:
        rgba = Image.alpha_composite(Image.new("RGBA", rgba.size, (255, 255, 255, 255)), rgba)
    rgb = rgba.convert("RGB")
    # two passes: e.g. black letterbox bars first, then the picture's own white margin
    for _ in range(2):
        full_area = rgb.width * rgb.height
        box = _content_bbox(rgb)
        if box is None or box == (0, 0, rgb.width, rgb.height):
            break
        bw, bh = box[2] - box[0], box[3] - box[1]
        if bw * bh < MIN_CONTENT_FRAC * full_area:
            break  # degenerate (almost blank) -> keep what we have
        rgb = rgb.crop(box)
    return rgb


def _prepare(path: str):
    """(RGB thumbnail for hashing, content_w, content_h)."""
    rgb = _load_content(path)
    cw, ch = rgb.size
    if max(rgb.size) > MAX_DIM:
        scale = MAX_DIM / max(rgb.size)
        new_size = (max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale)))
        rgb = rgb.resize(new_size, Image.Resampling.BILINEAR)
    return rgb, cw, ch


def image_quality(path: str) -> tuple:
    """(content_pixels, file_size): what 'bigger' means for keep-larger."""
    try:
        size = os.path.getsize(path)
    except OSError:
        size = 0
    try:
        cw, ch = _load_content(path).size
        return (cw * ch, size)
    except Exception:
        return (0, size)


def is_larger(new_q: tuple, old_q: tuple) -> bool:
    """True if `new_q` (pixels, bytes) beats `old_q`. Pixels first, bytes break ties."""
    new_px, new_sz = new_q
    old_px, old_sz = old_q
    if new_px > old_px * LARGER_PIXEL_MARGIN:
        return True
    if new_px * LARGER_PIXEL_MARGIN < old_px:
        return False
    return new_sz > old_sz


def compute_signature(path: str) -> Signature:
    rgb, cw, ch = _prepare(path)

    if _USE_EXTERNAL_IMAGEHASH:
        try:
            p = imagehash.phash(rgb, hash_size=HASH_SIZE)
            d = imagehash.dhash(rgb, hash_size=HASH_SIZE)
            a = imagehash.average_hash(rgb, hash_size=HASH_SIZE)
            w = imagehash.whash(rgb, hash_size=HASH_SIZE if (HASH_SIZE & (HASH_SIZE - 1)) == 0 else 8)
            return Signature(
                phash=PureHash(int(str(p), 16), bits=HASH_SIZE*HASH_SIZE),
                dhash=PureHash(int(str(d), 16), bits=HASH_SIZE*HASH_SIZE),
                ahash=PureHash(int(str(a), 16), bits=HASH_SIZE*HASH_SIZE),
                whash=PureHash(int(str(w), 16), bits=HASH_SIZE*HASH_SIZE),
                color_hist=_color_histogram(rgb),
                grid_hists=_grid_histograms(rgb),
                content_w=cw, content_h=ch,
            )
        except Exception:
            pass

    return Signature(
        phash=_compute_phash_pure(rgb),
        dhash=_compute_dhash_pure(rgb),
        ahash=_compute_ahash_pure(rgb),
        whash=_compute_whash_pure(rgb),
        color_hist=_color_histogram(rgb),
        grid_hists=_grid_histograms(rgb),
        content_w=cw, content_h=ch,
    )


# --- parallel hashing -------------------------------------------------------
#
# Hashing (decode + trim + pure-Python DCT) is the expensive, order-independent
# part of a duplicate check; the check/add itself must stay serial and ordered
# so images inside one batch dedupe against each other. So: signatures are
# computed in parallel and handed back in input order through a bounded window.

_UNREADABLE = object()   # sentinel: signature job failed (corrupt/unreadable image)


def _signature_job(path: str):
    """Worker: (Signature, None) or (None, error text). Module-level so a
    process pool can pickle it."""
    try:
        return compute_signature(path), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _hash_job(path: str):
    try:
        return hash_file(path), None
    except Exception as e:
        return None, f"{type(e).__name__}: {e}"


def _make_pool(kind: str, workers: int):
    """Thread pool by default. 'process' gives true multi-core hashing for the
    pure-Python parts but uses the spawn start method, which re-imports the
    app's main module in each child — it must be guarded by
    `if __name__ == "__main__":`. Frozen builds and any pool failure fall back
    to threads (Pillow releases the GIL while decoding, so threads still help)."""
    if kind == "process" and not getattr(sys, "frozen", False):
        try:
            return ProcessPoolExecutor(max_workers=workers,
                                       mp_context=multiprocessing.get_context("spawn"))
        except Exception as e:
            print(f"[DEDUP] process pool unavailable ({e}); using threads")
    return ThreadPoolExecutor(max_workers=workers, thread_name_prefix="dedup-hash")


def _ordered_parallel(func, items, workers=None, kind=None):
    """Yield (item, result, error) for each item, IN INPUT ORDER, running
    `func(item) -> (result, error)` on a pool with at most workers*PARALLEL_WINDOW
    jobs in flight (so huge batches don't pile up results in memory)."""
    items = list(items)
    workers = DEFAULT_WORKERS if workers is None else max(1, int(workers))
    if workers <= 1 or len(items) <= 1:
        for item in items:
            res, err = func(item)
            yield item, res, err
        return
    pool = _make_pool(kind or PARALLEL_EXECUTOR, min(workers, len(items)))
    window = workers * PARALLEL_WINDOW
    source = iter(items)
    pending = deque()

    def fill():
        while len(pending) < window:
            try:
                item = next(source)
            except StopIteration:
                return
            pending.append((item, pool.submit(func, item)))

    try:
        fill()
        while pending:
            item, fut = pending.popleft()
            try:
                res, err = fut.result()
            except Exception:
                res, err = func(item)  # broken pool etc.: redo this one inline
            fill()  # keep workers busy while the consumer handles this result
            yield item, res, err
    finally:
        pool.shutdown(wait=True, cancel_futures=True)


def iter_signatures(paths, workers=None, executor=None):
    """Public: yield (path, Signature | None, error | None) in input order,
    hashing in parallel."""
    return _ordered_parallel(_signature_job, paths, workers, executor)


def _is_full_duplicate(sig_a: Signature, sig_b: Signature) -> bool:
    if (sig_a.phash - sig_b.phash) > THRESHOLDS["phash"]:
        return False
    if (sig_a.dhash - sig_b.dhash) > THRESHOLDS["dhash"]:
        return False
    if (sig_a.ahash - sig_b.ahash) > THRESHOLDS["ahash"]:
        return False
    if (sig_a.whash - sig_b.whash) > THRESHOLDS["whash"]:
        return False
    if _hist_corr(sig_a.color_hist, sig_b.color_hist) < COLOR_SIM_THRESHOLD:
        return False
    local_sims = [_hist_corr(a, b) for a, b in zip(sig_a.grid_hists, sig_b.grid_hists)]
    if local_sims and min(local_sims) < LOCAL_COLOR_SIM_THRESHOLD:
        return False
    return True


# --- (de)serialization helpers for storing a Signature in SQLite ----------

def _hash_to_str(h) -> str:
    return str(h)


def _hash_from_str(s: str, bits: int = 256) -> PureHash:
    s = str(s).strip()
    return PureHash(int(s, 16) if s else 0, bits=bits or len(s) * 4)


def _sig_to_row(sig: Signature) -> dict:
    return {
        "phash": _hash_to_str(sig.phash),
        "dhash": _hash_to_str(sig.dhash),
        "ahash": _hash_to_str(sig.ahash),
        "whash": _hash_to_str(sig.whash),
        "color_hist": json.dumps(sig.color_hist),
        "grid_hists": json.dumps(sig.grid_hists),
    }


def _row_to_sig(row: sqlite3.Row) -> Signature:
    return Signature(
        phash=_hash_from_str(row["phash"]),
        dhash=_hash_from_str(row["dhash"]),
        ahash=_hash_from_str(row["ahash"]),
        whash=_hash_from_str(row["whash"]),
        color_hist=json.loads(row["color_hist"]),
        grid_hists=json.loads(row["grid_hists"]),
    )


# --- store -------------------------------------------------------------

class DedupStore:
    """
    Thread-safe SQLite-backed store of image signatures.

    Maintains a fast in-memory phash index for microsecond candidate prefiltering,
    and safely purges ghost records if an original file was removed by the user.
    """

    def __init__(self, db_path: str = None):
        # resolved here, not in the signature: def-arg defaults are bound at
        # import time, so a later redirect of DEDUP_DB_FILE would be ignored
        # and a test would write into the app's real dedup db
        if db_path is None:
            db_path = DEDUP_DB_FILE
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
        self._phash_index = self._load_phash_index()  # {id: int}
        # in-flight source-md5 claims: {md5: filepath} + reverse, guarded by _lock
        self._claims: dict = {}
        self._claims_by_fp: dict = {}

    def _init_schema(self):
        with self._lock:
            try:
                self._conn.execute("PRAGMA journal_mode = WAL;")
                self._conn.execute("PRAGMA synchronous = NORMAL;")
            except Exception:
                pass
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS image_hashes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filepath TEXT NOT NULL,
                    site TEXT,
                    post_id TEXT,
                    phash TEXT NOT NULL,
                    dhash TEXT NOT NULL,
                    ahash TEXT NOT NULL,
                    whash TEXT NOT NULL,
                    color_hist TEXT NOT NULL,
                    grid_hists TEXT NOT NULL,
                    added_at REAL NOT NULL
                )
            """)
            # one image -> N (site, algo, hash) pairs, like the gallery's
            # sources badge: a dup caught from a second site keeps BOTH sites'
            # hashes, each stored in the format that site can be matched with
            self._conn.execute("""
                CREATE TABLE IF NOT EXISTS source_hashes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    image_id INTEGER NOT NULL,
                    site TEXT,
                    algo TEXT NOT NULL DEFAULT 'md5',
                    hash TEXT NOT NULL,
                    added_at REAL NOT NULL
                )
            """)
            # interim shape from the first cut of this feature (a single md5
            # column): extend in place, carrying the values into `hash`.
            # guarded step by step so an interrupted init can simply rerun
            sh_cols = {r[1] for r in self._conn.execute("PRAGMA table_info(source_hashes)")}
            if "md5" in sh_cols:
                self._conn.execute("DROP INDEX IF EXISTS uq_source_hashes")
                self._conn.execute("DROP INDEX IF EXISTS idx_source_hashes_md5")
                if "algo" not in sh_cols:
                    self._conn.execute(
                        "ALTER TABLE source_hashes ADD COLUMN algo TEXT NOT NULL DEFAULT 'md5'")
                if "hash" not in sh_cols:
                    self._conn.execute(
                        "ALTER TABLE source_hashes ADD COLUMN hash TEXT NOT NULL DEFAULT ''")
                self._conn.execute("UPDATE source_hashes SET hash = md5 WHERE hash = ''")
                try:
                    self._conn.execute("ALTER TABLE source_hashes DROP COLUMN md5")
                except Exception:
                    pass  # ancient sqlite: column stays, simply unused
            # interim single-column image_hashes.source_md5: carry its values
            # into the per-site table, then drop the column (index first — an
            # indexed column cannot be dropped)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(image_hashes)")}
            if "source_md5" in cols:
                self._conn.execute(
                    "INSERT OR IGNORE INTO source_hashes (image_id, site, algo, hash, added_at) "
                    "SELECT id, site, 'md5', source_md5, added_at FROM image_hashes "
                    "WHERE source_md5 IS NOT NULL"
                )
                self._conn.execute("DROP INDEX IF EXISTS idx_source_md5")
                try:
                    self._conn.execute("ALTER TABLE image_hashes DROP COLUMN source_md5")
                except Exception:
                    pass  # ancient sqlite: column stays, simply unused
            # which hashing pipeline produced the row's signature (see SIG_VERSION)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(image_hashes)")}
            if "sig_ver" not in cols:
                self._conn.execute(
                    "ALTER TABLE image_hashes ADD COLUMN sig_ver INTEGER NOT NULL DEFAULT 1")
            # size of the picture itself, so keep-larger needn't re-decode stored files
            for col in ("content_w", "content_h"):
                if col not in cols:
                    self._conn.execute(
                        f"ALTER TABLE image_hashes ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_site ON image_hashes(site)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_image_hashes_filepath ON image_hashes(filepath)")
            self._conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_source_hashes_hash ON source_hashes(hash)"
            )
            self._conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_source_hashes "
                "ON source_hashes(image_id, site, algo, hash)"
            )
            self._conn.commit()

    def _load_phash_index(self):
        with self._lock:
            cur = self._conn.execute("SELECT id, phash FROM image_hashes")
            index = {}
            for row in cur.fetchall():
                try:
                    index[row["id"]] = int(str(row["phash"]), 16)
                except Exception:
                    pass
            return index

    def _candidate_ids(self, phash) -> list[int]:
        target = int(phash) if isinstance(phash, PureHash) else int(str(phash), 16)
        with self._lock:
            items = list(self._phash_index.items())
        out = []
        for row_id, ph in items:
            if (target ^ ph).bit_count() <= PREFILTER_MARGIN:
                out.append(row_id)
        return out

    def _prune_ids(self, ids: list[int]):
        if not ids:
            return
        placeholders = ",".join("?" * len(ids))
        with self._lock:
            try:
                self._conn.execute(f"DELETE FROM image_hashes WHERE id IN ({placeholders})", ids)
                self._conn.execute(
                    f"DELETE FROM source_hashes WHERE image_id IN ({placeholders})", ids)
                self._conn.commit()
                for i in ids:
                    self._phash_index.pop(i, None)
            except Exception as e:
                print(f"[DEDUP] Error pruning stale IDs: {e}")

    def find_duplicate(self, sig: Signature) -> sqlite3.Row | None:
        candidate_ids = self._candidate_ids(sig.phash)
        if not candidate_ids:
            return None
        placeholders = ",".join("?" * len(candidate_ids))
        with self._lock:
            cur = self._conn.execute(
                f"SELECT * FROM image_hashes WHERE id IN ({placeholders})",
                candidate_ids,
            )
            rows = cur.fetchall()

        stale_ids = []
        matched_row = None
        for row in rows:
            candidate_sig = _row_to_sig(row)
            if _is_full_duplicate(sig, candidate_sig):
                matched_fp = row["filepath"]
                if os.path.exists(matched_fp):
                    matched_row = row
                    break
                else:
                    # Previous download was deleted from disk: remove ghost record
                    stale_ids.append(row["id"])

        if stale_ids:
            self._prune_ids(stale_ids)

        return matched_row

    def add(self, filepath: str, sig: Signature, site: str = None, post_id: str = None,
            source_md5: str = None, generated_hashes: dict = None) -> int:
        src = normalize_any_source_hash(source_md5)
        extras = normalize_generated_hashes(generated_hashes)
        data = _sig_to_row(sig)
        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO image_hashes
                   (filepath, site, post_id, phash, dhash, ahash, whash,
                    color_hist, grid_hists, added_at, sig_ver, content_w, content_h)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (filepath, site, post_id, data["phash"], data["dhash"],
                 data["ahash"], data["whash"], data["color_hist"],
                 data["grid_hists"], time.time(), SIG_VERSION,
                 sig.content_w, sig.content_h),
            )
            new_id = cur.lastrowid
            self._phash_index[new_id] = int(data["phash"], 16)
            if src:
                self._add_source_hash_locked(new_id, site, src[1], src[0])
            for algo, value in extras.items():
                self._add_source_hash_locked(new_id, site, value, algo)
            self._conn.commit()
        return new_id

    def _add_source_hash_locked(self, image_id: int, site: str, digest: str,
                                algo: str = "md5") -> bool:
        """Attach one (site, algo, digest) pair to an image row. Caller holds the lock."""
        try:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO source_hashes "
                "(image_id, site, algo, hash, added_at) VALUES (?, ?, ?, ?, ?)",
                (image_id, site, algo, digest, time.time()),
            )
            return cur.rowcount > 0
        except Exception as e:
            print(f"[DEDUP] source hash insert failed: {e}")
            return False

    def add_source_hash(self, image_id: int, site: str, value: str, algo: str = "md5") -> bool:
        """Public: record another site's hash for an already-stored image."""
        digest = normalize_source_hash(algo, value)
        if digest is None or image_id is None:
            return False
        with self._lock:
            return self._add_source_hash_locked(int(image_id), site, digest, algo)

    def _find_source_locked(self, digest: str) -> sqlite3.Row | None:
        """Image row for a hash whose file still exists; prunes rows whose file is gone.

        Caller must hold the lock (RLock — find_duplicate may nest). Algos never
        share a hex length, so a bare value can only match its own format.
        """
        rows = self._conn.execute(
            "SELECT ih.id, ih.filepath, ih.site, ih.post_id FROM source_hashes sh "
            "JOIN image_hashes ih ON ih.id = sh.image_id WHERE sh.hash = ?",
            (digest,),
        ).fetchall()
        stale_ids = []
        matched = None
        for row in rows:
            if os.path.exists(row["filepath"]):
                matched = row
                break
            stale_ids.append(row["id"])
        if stale_ids:
            self._prune_ids(stale_ids)
        return matched

    def claim_source_md5(self, md5: str, filepath: str):
        """Persisted-dup check + in-flight reservation, atomic under the store lock.

        Returns (ok, match): ok True -> caller may proceed with `filepath`;
        ok False -> the hash is already represented by a stored file (`match`
        is the surviving row) or reserved by another in-flight download
        (`match` is None).
        """
        with self._lock:
            match = self._find_source_locked(md5)
            if match is not None:
                return False, match
            owner = self._claims.get(md5)
            if owner is not None and owner != filepath:
                return False, None
            self._claims[md5] = filepath
            self._claims_by_fp[filepath] = md5
            return True, None

    def release_source_md5(self, filepath: str):
        """Release the in-flight claim held for `filepath` (idempotent)."""
        with self._lock:
            md5 = self._claims_by_fp.get(filepath)
            if md5 is None:
                return
            if self._claims.get(md5) == filepath:
                del self._claims[md5]
            del self._claims_by_fp[filepath]

    def claimed_source_md5(self, filepath: str) -> str | None:
        with self._lock:
            return self._claims_by_fp.get(filepath)

    @staticmethod
    def _row_quality(row) -> tuple:
        """(content_pixels, file_size) of a stored row; decodes only for rows
        stored before content sizes were recorded."""
        path = row["filepath"]
        try:
            w, h = row["content_w"], row["content_h"]
        except (IndexError, KeyError):
            w = h = 0
        if w > 0 and h > 0:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            return (w * h, size)
        return image_quality(path)

    def check_and_add(self, filepath: str, site: str = None, post_id: str = None,
                      source_md5: str = None, generated_hashes: dict = None,
                      signature=None) -> DedupResult:
        """
        Check the candidate against source hashes (when provided) and perceptual
        signatures, and add it if not a duplicate.
        The source-hash check runs first, so a known cross-site duplicate is
        recognized without computing a perceptual signature.

        `signature` lets a caller that already hashed the file (see
        check_and_add_many) skip the hashing here.
        """
        src = normalize_any_source_hash(source_md5)
        extras = normalize_generated_hashes(generated_hashes)

        def _attach(image_id):
            # gallery-sources behavior: the surviving image keeps this site's
            # hashes — the API-claimed source digest (any supported type) and
            # every format computed from the bytes this site served — so a
            # later listing from any of its sites dedupes pre-download
            if src:
                self._add_source_hash_locked(image_id, site, src[1], src[0])
            for algo, value in extras.items():
                self._add_source_hash_locked(image_id, site, value, algo)

        if src:
            with self._lock:
                match = self._find_source_locked(src[1])
                if match is not None:
                    _attach(match["id"])
                    return DedupResult(
                        is_duplicate=True,
                        filepath=filepath,
                        matched_path=match["filepath"],
                        matched_site=match["site"],
                        matched_post_id=match["post_id"],
                        matched_by="source_md5",
                    )
        if signature is _UNREADABLE:
            return DedupResult(is_duplicate=False, filepath=filepath)
        try:
            sig = signature if signature is not None else compute_signature(filepath)
        except Exception:
            # Unreadable or corrupted image: don't block
            return DedupResult(is_duplicate=False, filepath=filepath)

        # hold the lock across find+add — released in between, two threads
        # downloading the same image both pass the check and both insert
        with self._lock:
            # another download may have persisted this hash while we hashed
            if src:
                match = self._find_source_locked(src[1])
                if match is not None:
                    _attach(match["id"])
                    return DedupResult(
                        is_duplicate=True,
                        filepath=filepath,
                        matched_path=match["filepath"],
                        matched_site=match["site"],
                        matched_post_id=match["post_id"],
                        matched_by="source_md5",
                    )
            match = self.find_duplicate(sig)
            if match is not None:
                # the killed candidate never gets its own row (no dangling
                # record); its (site, hashes) pairs are added to the survivor
                _attach(match["id"])
                if KEEP_LARGER and match["filepath"] != filepath:
                    try:
                        new_size = os.path.getsize(filepath)
                    except OSError:
                        new_size = 0
                    new_q = (sig.content_w * sig.content_h, new_size)
                    if is_larger(new_q, self._row_quality(match)):
                        return self._replace_with_larger_locked(
                            match, filepath, sig, site, post_id)
                return DedupResult(
                    is_duplicate=True,
                    filepath=filepath,
                    matched_path=match["filepath"],
                    matched_site=match["site"],
                    matched_post_id=match["post_id"],
                )
            self.add(filepath, sig, site=site, post_id=post_id,
                     source_md5=source_md5, generated_hashes=extras)
        return DedupResult(is_duplicate=False, filepath=filepath)

    def _replace_with_larger_locked(self, match, new_path: str, sig: Signature,
                                    site, post_id) -> DedupResult:
        """The new file is a bigger copy of a stored image: keep it, drop the old.

        The stored row (and every source hash attached to it) is repointed at
        the new file with its fresh signature, then the smaller file is removed.
        Returns is_duplicate=False so callers keep the new file as usual.
        Caller holds the lock.
        """
        old_path = match["filepath"]
        data = _sig_to_row(sig)
        new_site = site if site is not None else match["site"]
        new_pid = post_id if site is not None else match["post_id"]
        try:
            self._conn.execute(
                "UPDATE image_hashes SET filepath = ?, site = ?, post_id = ?, "
                "phash = ?, dhash = ?, ahash = ?, whash = ?, "
                "color_hist = ?, grid_hists = ?, sig_ver = ?, added_at = ?, "
                "content_w = ?, content_h = ? WHERE id = ?",
                (new_path, new_site, new_pid, data["phash"], data["dhash"],
                 data["ahash"], data["whash"], data["color_hist"], data["grid_hists"],
                 SIG_VERSION, time.time(), sig.content_w, sig.content_h, match["id"]),
            )
            self._conn.commit()
        except Exception as e:
            self._conn.rollback()
            print(f"[DEDUP] keep-larger swap failed, keeping stored file: {e}")
            return DedupResult(
                is_duplicate=True, filepath=new_path, matched_path=old_path,
                matched_site=match["site"], matched_post_id=match["post_id"])
        self._phash_index[match["id"]] = int(data["phash"], 16)
        if DELETE_REPLACED:
            try:
                os.remove(old_path)
            except OSError as e:
                print(f"[DEDUP] could not remove replaced file {old_path}: {e}")
        return DedupResult(
            is_duplicate=False, filepath=new_path, matched_path=old_path,
            matched_site=match["site"], matched_post_id=match["post_id"],
            replaced_path=old_path)

    def check_and_add_many(self, entries, workers=None, executor=None, progress=None) -> list:
        """check_and_add for many images, hashing them in parallel.

        `entries`: iterable of file paths, (path, site) tuples, or dicts with
        filepath / site / post_id / source_md5 / generated_hashes keys.
        Returns one DedupResult per entry, in input order.

        Only the hashing runs in parallel. The check/add runs one image at a
        time in input order, so images inside the same batch dedupe against
        each other exactly as if they had been submitted one by one (including
        keep-larger). Entries whose source hash already identifies a stored
        image skip hashing entirely. `progress(done, total, result)` is called
        from the calling thread after each entry.
        """
        items = [self._coerce_entry(e) for e in entries]
        total = len(items)
        if not total:
            return []

        # entries a known source hash already resolves never need a signature
        need = []
        for i, it in enumerate(items):
            src = normalize_any_source_hash(it["source_md5"])
            hit = False
            if src:
                with self._lock:
                    hit = self._find_source_locked(src[1]) is not None
            if not hit:
                need.append(i)
        need_set = set(need)

        gen = iter_signatures([items[i]["filepath"] for i in need], workers, executor)
        results = []
        try:
            for i, it in enumerate(items):
                signature = None
                if i in need_set:
                    _path, sig, err = next(gen)
                    if sig is None:
                        print(f"[DEDUP] could not hash {it['filepath']}: {err}")
                        signature = _UNREADABLE
                    else:
                        signature = sig
                try:
                    res = self.check_and_add(signature=signature, **it)
                except Exception as e:
                    print(f"[DEDUP] check failed for {it['filepath']}: {e}")
                    res = DedupResult(is_duplicate=False, filepath=it["filepath"])
                results.append(res)
                if progress:
                    try:
                        progress(i + 1, total, res)
                    except Exception:
                        pass
        finally:
            gen.close()
        return results

    @staticmethod
    def _coerce_entry(e) -> dict:
        if isinstance(e, dict):
            d = {"filepath": str(e["filepath"]), "site": e.get("site"),
                 "post_id": e.get("post_id"), "source_md5": e.get("source_md5"),
                 "generated_hashes": e.get("generated_hashes")}
        elif isinstance(e, (tuple, list)):
            d = {"filepath": str(e[0]), "site": e[1] if len(e) > 1 else None,
                 "post_id": e[2] if len(e) > 2 else None,
                 "source_md5": e[3] if len(e) > 3 else None,
                 "generated_hashes": e[4] if len(e) > 4 else None}
        else:
            d = {"filepath": str(e), "site": None, "post_id": None,
                 "source_md5": None, "generated_hashes": None}
        return d

    def rehash_outdated(self, workers=None) -> int:
        """Recompute signatures stored by an older hashing pipeline.

        Rows written before the current SIG_VERSION were hashed without
        transparency flattening / margin trimming, so they would not line up
        with new signatures. Rows whose file is gone are skipped (the stale
        prune removes them on lookup). Hashing runs in parallel. Safe to
        interrupt: finished rows keep their new version, the rest resume on
        the next run.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, filepath FROM image_hashes WHERE sig_ver < ?", (SIG_VERSION,)
            ).fetchall()
        live = [r for r in rows if os.path.isfile(r["filepath"])]
        done = 0
        gen = iter_signatures([r["filepath"] for r in live], workers)
        try:
            for row, (fp, sig, err) in zip(live, gen):
                if sig is None:
                    print(f"[DEDUP] rehash failed for {fp}: {err}")
                    continue
                data = _sig_to_row(sig)
                try:
                    with self._lock:
                        cur = self._conn.execute(
                            "UPDATE image_hashes SET phash = ?, dhash = ?, ahash = ?, whash = ?, "
                            "color_hist = ?, grid_hists = ?, sig_ver = ?, "
                            "content_w = ?, content_h = ? WHERE id = ? AND filepath = ?",
                            (data["phash"], data["dhash"], data["ahash"], data["whash"],
                             data["color_hist"], data["grid_hists"], SIG_VERSION,
                             sig.content_w, sig.content_h, row["id"], fp),
                        )
                        if cur.rowcount:
                            self._phash_index[row["id"]] = int(data["phash"], 16)
                            done += 1
                            if done % 50 == 0:
                                self._conn.commit()
                except Exception as e:  # store closed mid-run, etc.
                    print(f"[DEDUP] rehash stopped: {e}")
                    break
        finally:
            gen.close()
        try:
            with self._lock:
                self._conn.commit()
        except Exception:
            pass
        if done:
            print(f"[DEDUP] re-hashed {done} stored images with the current pipeline")
        return done

    def add_missing(self, entries, workers=None) -> int:
        """Hash image files that have no record yet (e.g. added manually).

        `entries` is an iterable of (absolute filepath, site). Files already
        recorded by filepath are skipped; one bad file never stops the run.
        Hashing runs in parallel. Returns the number of records added.
        """
        with self._lock:
            existing = {r["filepath"] for r in self._conn.execute("SELECT filepath FROM image_hashes")}
        todo = []
        for filepath, site in entries:
            fp = str(filepath)
            if fp in existing:
                continue
            existing.add(fp)
            todo.append((fp, site))
        added = 0
        gen = iter_signatures([fp for fp, _ in todo], workers)
        try:
            for (fp, site), (_p, sig, err) in zip(todo, gen):
                if sig is None:
                    print(f"[DEDUP] backfill failed for {fp}: {err}")
                    continue
                try:
                    self.add(fp, sig, site=site)
                except Exception as e:
                    print(f"[DEDUP] backfill failed for {fp}: {e}")
                    continue
                added += 1
        finally:
            gen.close()
        return added

    def backfill_source_hashes(self, workers=None) -> int:
        """Generate the sites' hash formats for stored images that lack them.

        Old installs stored only perceptual signatures; files dropped in and
        rows written outside the download path lack source hashes too. One
        pass over the rows with no `source_hashes` at all — a row that has
        any pair is done, so an interrupted run resumes on the next start and
        the steady state is a single cheap query. The site's own hash is
        recovered from the filename when the booru naming embeds it
        (-<32hex>.ext for most, bare <32hex> for moebooru/danbooru); the rest
        come from hashing the bytes we hold (read in parallel — hashlib
        releases the GIL, so threads scale). Files rewritten by
        write_image_metadata hash differently than their source, so their
        recovered filename md5 is the listing-matchable one.
        Returns the number of images that gained pairs.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT ih.id, ih.filepath, ih.site FROM image_hashes ih "
                "LEFT JOIN source_hashes sh ON sh.image_id = ih.id "
                "WHERE sh.id IS NULL"
            ).fetchall()
        # a gone file is skipped; the stale-prune removes the row on lookup
        live = [r for r in rows if os.path.isfile(r["filepath"])]
        done = 0
        gen = _ordered_parallel(_hash_job, [r["filepath"] for r in live], workers, "thread")
        try:
            for row, (filepath, hashes, err) in zip(live, gen):
                if hashes is None:
                    print(f"[DEDUP] source-hash generation failed for {filepath}: {err}")
                    continue
                pairs = []
                m = re.search(r'(?<![0-9a-f])([0-9a-f]{32})(?![0-9a-f])',
                              os.path.basename(filepath).lower())
                if m:
                    pairs.append(("md5", m.group(1)))
                pairs.extend(hashes.items())
                with self._lock:
                    added = False
                    for algo, value in pairs:
                        added = self._add_source_hash_locked(
                            row["id"], row["site"], value, algo) or added
                    if added:
                        self._conn.commit()
                        done += 1
        finally:
            gen.close()
        if done:
            print(f"[DEDUP] generated source hashes for {done} stored images")
        return done

    def relocate_filepath(self, old, new):
        """Keep source-hash/perceptual identities attached to a replaced image."""
        with self._lock:
            try:
                self._conn.execute('UPDATE image_hashes SET filepath = ? WHERE filepath = ?', (new, old))
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

    def remove_by_filepath(self, filepath: str):
        with self._lock:
            try:
                cur = self._conn.execute("SELECT id FROM image_hashes WHERE filepath = ?", (filepath,))
                ids = [r["id"] for r in cur.fetchall()]
                if ids:
                    self._conn.execute("DELETE FROM image_hashes WHERE filepath = ?", (filepath,))
                    placeholders = ",".join("?" * len(ids))
                    self._conn.execute(
                        f"DELETE FROM source_hashes WHERE image_id IN ({placeholders})", ids)
                    self._conn.commit()
                    for i in ids:
                        self._phash_index.pop(i, None)
            except Exception as e:
                print(f"[DEDUP] Error removing filepath: {e}")

    def remove_missing_files(self) -> int:
        """Delete records whose file no longer exists on disk. Returns count removed."""
        with self._lock:
            rows = self._conn.execute("SELECT id, filepath FROM image_hashes").fetchall()
        # stat outside the lock
        missing = [r["id"] for r in rows if not os.path.isfile(r["filepath"])]
        if not missing:
            return 0
        with self._lock:
            for start in range(0, len(missing), 500):
                batch = missing[start:start + 500]
                placeholders = ",".join("?" * len(batch))
                self._conn.execute(f"DELETE FROM image_hashes WHERE id IN ({placeholders})", batch)
                self._conn.execute(
                    f"DELETE FROM source_hashes WHERE image_id IN ({placeholders})", batch)
            self._conn.commit()
            for i in missing:
                self._phash_index.pop(i, None)
        return len(missing)

    def count(self) -> int:
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) AS c FROM image_hashes").fetchone()["c"]

    def clear(self):
        with self._lock:
            self._conn.execute("DELETE FROM image_hashes")
            self._conn.execute("DELETE FROM source_hashes")
            self._conn.commit()
            self._phash_index = {}

    def close(self):
        with self._lock:
            self._claims.clear()
            self._claims_by_fp.clear()
            try:
                self._conn.close()
            except Exception:
                pass


# --- module-level singleton ------------------------------------------------

_store_lock = threading.Lock()
_store_instance: DedupStore | None = None


def get_store() -> DedupStore:
    global _store_instance
    with _store_lock:
        if _store_instance is None:
            _store_instance = DedupStore()
            if AUTO_UPGRADE_SIGNATURES:
                # half the workers: leave CPU for the app while it upgrades
                threading.Thread(
                    target=_store_instance.rehash_outdated,
                    kwargs={"workers": max(1, DEFAULT_WORKERS // 2)},
                    name="dedup-rehash", daemon=True).start()
        return _store_instance
