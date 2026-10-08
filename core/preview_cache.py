import hashlib
import json
import os
import subprocess
import tempfile
import threading

_locks: dict[str, threading.Lock] = {}
_guard = threading.Lock()


def _lock(key):
    with _guard:
        return _locks.setdefault(key, threading.Lock())


def _has_audio(src: str) -> bool:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "json", src],
            capture_output=True, text=True, timeout=15, check=True).stdout
        return bool(json.loads(out).get("streams"))
    except Exception:
        return True  # unknown: be safe and strip


def _cache_path(src: str, cache_dir: str) -> str:
    st = os.stat(src)
    key = hashlib.sha1(
        f"{os.path.abspath(src)}:{st.st_mtime_ns}:{st.st_size}".encode()).hexdigest()
    return os.path.join(cache_dir, f"{key}.mp4")


def evict(src: str, cache_dir: str) -> bool:
    """Drop the cached remux for src. Call while src still exists — the key
    derives from its mtime/size. Returns True if a cache file was removed."""
    try:
        os.remove(_cache_path(src, cache_dir))
        return True
    except OSError:
        return False


def preview_path(src: str, cache_dir: str) -> str:
    """Return a path to a video-only copy of src (or src itself if it has no audio)."""
    if not _has_audio(src):
        return src
    dst = _cache_path(src, cache_dir)
    if os.path.exists(dst):
        return dst
    with _lock(dst):
        if os.path.exists(dst):
            return dst
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
            return src  # fall back to the original rather than break the preview
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    return dst
