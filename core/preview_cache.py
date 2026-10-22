import hashlib
import json
import os
import subprocess
import tempfile
import threading
import weakref

_locks = weakref.WeakValueDictionary()
_no_audio: dict[str, bool] = {}
_guard = threading.Lock()
_original_fallback = {}


def _lock(key):
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def _has_audio(src: str) -> bool:
    try:
        key = _key(src)
    except OSError:
        return True
    cached = _no_audio.get(key)
    if cached is not None:
        return not cached
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "json", src],
            capture_output=True, text=True, timeout=15, check=True).stdout
        audio = bool(json.loads(out).get("streams"))
    except Exception:
        audio = True  # unknown: be safe and strip
    with _guard:
        # a no-audio video is never remuxed, so unmemoized it re-probed on
        # every hover forever; bound the dict the same way as _locks
        if len(_no_audio) > 4096:
            _no_audio.pop(next(iter(_no_audio)))
        _no_audio[key] = not audio
    return audio


def _key(src: str) -> str:
    st = os.stat(src)
    return hashlib.sha1(
        f"{os.path.abspath(src)}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()


def _cache_path(src: str, cache_dir: str) -> str:
    return os.path.join(cache_dir, f"{_key(src)}.mp4")


def evict(src: str, cache_dir: str) -> bool:
    """Drop the cached remux for src. Call while src still exists — the key
    derives from its mtime/size. Returns True if a cache file was removed."""
    try:
        os.remove(_cache_path(src, cache_dir))
        return True
    except OSError:
        return False


def ready_for(src: str, cache_dir: str):
    """Path to serve NOW: the cached no-audio copy, or src itself when it has
    no audio. None when the remux is not generated yet (side-effect-free —
    generation is the caller's business, on a thread that isn't a request)."""
    dst = _cache_path(src, cache_dir)
    if os.path.exists(dst):
        return dst
    key = _key(src)
    with _guard:
        # No probing subprocess on a request thread: an unknown source is
        # prepared by the preview pool. Failed remuxes serve the original.
        if _no_audio.get(key) or key in _original_fallback:
            return src
    return None


def preview_path(src: str, cache_dir: str) -> str:
    """Return a path to a video-only copy of src (or src itself if it has no audio)."""
    dst = _cache_path(src, cache_dir)
    if os.path.exists(dst):
        return dst
    with _lock(dst):
        if os.path.exists(dst):
            return dst
        key = _key(src)
        with _guard:
            if key in _original_fallback:
                return src
        if not _has_audio(src):
            return src
        os.makedirs(cache_dir, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=cache_dir, suffix=".mp4")
        os.close(fd)
        try:
            subprocess.run(
                ["ffmpeg", "-nostdin", "-y", "-v", "error", "-i", src,
                 "-map", "0:v:0", "-an", "-c:v", "copy",
                 "-movflags", "+faststart", tmp],
                check=True, timeout=120)
            os.replace(tmp, dst)
        except Exception:
            with _guard:
                if len(_original_fallback) >= 4096:
                    _original_fallback.pop(next(iter(_original_fallback)))
                _original_fallback[key] = True
            return src  # make the route's ready_for() fallback effective too
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return dst
