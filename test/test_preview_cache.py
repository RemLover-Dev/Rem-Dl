import json
import os
import shutil
import subprocess
import time

import pytest

pytestmark = pytest.mark.usefixtures("bypass_access_guard")

import Rems_Dl

H = {"User-Agent": "RemsDlDesktopApp/1.0"}
HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
requires_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="ffmpeg and ffprobe are required")


def _make_mp4(path, audio=True):
    if not HAVE_FFMPEG:
        pytest.skip("ffmpeg and ffprobe are required")
    cmd = ["ffmpeg", "-nostdin", "-y", "-v", "error",
           "-f", "lavfi", "-i", "testsrc2=duration=0.3:size=64x64:rate=10"]
    if audio:
        cmd += ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.3"]
    cmd += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        cmd += ["-c:a", "aac", "-shortest"]
    cmd += [str(path)]
    subprocess.run(cmd, check=True, timeout=60)


def _streams(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=30).stdout
    return json.loads(out)["streams"]


@pytest.fixture
def client():
    Rems_Dl.app.config["TESTING"] = True
    with Rems_Dl.app.test_client() as c:
        yield c


@requires_ffmpeg
def test_preview_strips_audio(tmp_path):
    from core.preview_cache import preview_path
    src = tmp_path / "with_audio.mp4"
    _make_mp4(src, audio=True)
    cache = tmp_path / "cache"
    out = preview_path(str(src), str(cache))
    assert out != str(src)
    streams = _streams(out)
    assert not any(s["codec_type"] == "audio" for s in streams)
    video = next(s for s in streams if s["codec_type"] == "video")
    assert video["codec_name"] == "h264"
    assert len(os.listdir(cache)) == 1
    # second call hits the cache, no new file
    assert preview_path(str(src), str(cache)) == out
    assert len(os.listdir(cache)) == 1


def test_prune_removes_cold_previews_only(tmp_path):
    from core.preview_cache import prune_older_than
    cache = tmp_path / "cache"
    cache.mkdir()
    cold = cache / "cold.mp4"
    warm = cache / "warm.mp4"
    cold.write_bytes(b"x")
    warm.write_bytes(b"x")
    old = time.time() - 40 * 86400
    os.utime(str(cold), (old, old))
    assert prune_older_than(str(cache), days=30) == 1
    assert not cold.exists()
    assert warm.exists()


def test_serving_a_preview_touches_its_cache(tmp_path):
    from core.preview_cache import _cache_path, ready_for
    src = tmp_path / "video.mp4"
    src.write_bytes(b"v")
    cache = tmp_path / "cache"
    cache.mkdir()
    cached = _cache_path(str(src), str(cache))
    with open(cached, "wb") as f:
        f.write(b"x")
    old = time.time() - 40 * 86400
    os.utime(cached, (old, old))
    assert ready_for(str(src), str(cache)) == cached
    assert time.time() - os.path.getmtime(cached) < 60


def test_prune_keeps_previews_read_recently(tmp_path):
    from core.preview_cache import prune_older_than
    cache = tmp_path / "cache"
    cache.mkdir()
    seen = cache / "seen.mp4"
    seen.write_bytes(b"x")
    old = time.time() - 40 * 86400
    os.utime(str(seen), (time.time(), old))  # read now, mtime cold
    assert prune_older_than(str(cache), days=30) == 0
    assert seen.exists()


def test_viewing_the_original_touches_the_remux(client, tmp_path, monkeypatch):
    from core.preview_cache import _cache_path
    src = tmp_path / "v.mp4"
    src.write_bytes(b"v")
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr(Rems_Dl, "PREVIEW_CACHE_DIR", str(tmp_path / "pcache"))
    cached = _cache_path(str(src), str(tmp_path / "pcache"))
    os.makedirs(os.path.dirname(cached), exist_ok=True)
    with open(cached, "wb") as f:
        f.write(b"x")
    old = time.time() - 40 * 86400
    os.utime(cached, (old, old))
    r = client.get("/api/gallery/file/v.mp4", headers=H)
    assert r.status_code == 200
    assert time.time() - os.path.getmtime(cached) < 60


@requires_ffmpeg
def test_evict_removes_cached_preview(tmp_path):
    from core.preview_cache import evict, preview_path
    src = tmp_path / "with_audio.mp4"
    _make_mp4(src, audio=True)
    cache = tmp_path / "cache"
    out = preview_path(str(src), str(cache))
    assert out != str(src)
    assert evict(str(src), str(cache)) is True
    assert not os.listdir(cache)
    # gone already: no crash, nothing removed
    assert evict(str(src), str(cache)) is False


@requires_ffmpeg
def test_evict_caches_drops_thumb_and_preview(tmp_path, monkeypatch):
    import Rems_Dl
    from core.preview_cache import preview_path
    src = tmp_path / "with_audio.mp4"
    _make_mp4(src, audio=True)
    thumbs = tmp_path / "thumbs"
    pcache = tmp_path / "pcache"
    thumbs.mkdir()
    monkeypatch.setattr(Rems_Dl, "THUMB_CACHE", str(thumbs))
    monkeypatch.setattr(Rems_Dl, "PREVIEW_CACHE_DIR", str(pcache))
    thumb = Rems_Dl._thumb_cache_path(str(src))
    with open(thumb, "wb") as f:
        f.write(b"x")
    assert preview_path(str(src), str(pcache)) != str(src)
    Rems_Dl._evict_caches(str(src))
    assert not os.path.exists(thumb)
    assert not os.listdir(pcache)


@requires_ffmpeg
def test_preview_no_audio_returns_original(tmp_path):
    from core.preview_cache import preview_path
    src = tmp_path / "silent.mp4"
    _make_mp4(src, audio=False)
    cache = tmp_path / "cache"
    out = preview_path(str(src), str(cache))
    assert out == str(src)
    assert not os.path.exists(cache) or not os.listdir(cache)


@requires_ffmpeg
def test_preview_route_range_206(client, tmp_path, monkeypatch):
    import time

    from Rems_Dl import preview_ready_for

    src = tmp_path / "v.mp4"
    _make_mp4(src, audio=True)
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr(Rems_Dl, "PREVIEW_CACHE_DIR", str(tmp_path / "pcache"))
    # cold cache: the remux goes to the background pool, the request answers
    # immediately (inline ffmpeg would hold the request for up to 120s)
    r0 = client.get("/api/gallery/preview/v.mp4",
                    headers={**H, "Range": "bytes=0-99"})
    assert r0.status_code == 204
    assert r0.data == b""
    # wait until the warm pool finished the remux
    dst = None
    for _ in range(120):
        dst = preview_ready_for(str(src), str(tmp_path / "pcache"))
        if dst is not None:
            break
        time.sleep(0.05)
    assert dst is not None
    r = client.get("/api/gallery/preview/v.mp4",
                   headers={**H, "Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.headers.get("Content-Range", "").startswith("bytes 0-99/")
    assert len(r.data) == 100
    r2 = client.get("/api/gallery/preview/v.mp4", headers=H)
    assert r2.status_code == 200
    assert r2.content_type == "video/mp4"
    # the range replies serve the cached no-audio copy, not the original
    assert dst != str(src)


def test_preview_route_traversal_blocked(client, tmp_path, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path / "lib"))
    monkeypatch.setattr(Rems_Dl, "PREVIEW_CACHE_DIR", str(tmp_path / "pcache"))
    r = client.get("/api/gallery/preview/..%2F..%2Fetc%2Fpasswd", headers=H)
    assert r.status_code in (403, 404)


def _make_image(path, pix_fmt):
    subprocess.run(
        ["ffmpeg", "-nostdin", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc=size=96x64", "-frames:v", "1", "-pix_fmt", pix_fmt,
         str(path)], check=True, timeout=60)


@requires_ffmpeg
def test_thumbnails_use_ffmpeg_not_pil(tmp_path, monkeypatch):
    """Alpha probe and thumbnail generation are ffmpeg/ffprobe only — no
    in-process decode (the old path decoded every source with PIL)."""
    png = tmp_path / "alpha.png"
    _make_image(png, "rgba")

    def no_pil(*_a, **_k):
        raise AssertionError("PIL must not decode thumbnails")

    monkeypatch.setattr(Rems_Dl.Image, "open", no_pil)
    assert Rems_Dl._thumb_alpha_hint(str(png)) is True
    data, ext, mime = Rems_Dl._generate_thumb_bytes(str(png))
    assert (ext, mime) == (".png", "image/png")
    assert data[:8] == b"\x89PNG\r\n\x1a\n"


@requires_ffmpeg
def test_opaque_thumbnail_is_jpeg(tmp_path):
    jpg = tmp_path / "plain.jpg"
    _make_image(jpg, "yuvj420p")
    data, ext, mime = Rems_Dl._generate_thumb_bytes(str(jpg))
    assert (ext, mime) == (".jpg", "image/jpeg")
    assert data[:2] == b"\xff\xd8"
