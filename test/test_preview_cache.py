import json
import os
import subprocess

import pytest

import Rems_Dl

H = {"User-Agent": "RemsDlDesktopApp/1.0"}


def _make_mp4(path, audio=True):
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


def test_preview_no_audio_returns_original(tmp_path):
    from core.preview_cache import preview_path
    src = tmp_path / "silent.mp4"
    _make_mp4(src, audio=False)
    cache = tmp_path / "cache"
    out = preview_path(str(src), str(cache))
    assert out == str(src)
    assert not os.path.exists(cache) or not os.listdir(cache)


def test_preview_route_range_206(client, tmp_path, monkeypatch):
    src = tmp_path / "v.mp4"
    _make_mp4(src, audio=True)
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr(Rems_Dl, "PREVIEW_CACHE_DIR", str(tmp_path / "pcache"))
    r = client.get("/api/gallery/preview/v.mp4",
                   headers={**H, "Range": "bytes=0-99"})
    assert r.status_code == 206
    assert r.headers.get("Content-Range", "").startswith("bytes 0-99/")
    assert len(r.data) == 100
    r2 = client.get("/api/gallery/preview/v.mp4", headers=H)
    assert r2.status_code == 200
    assert r2.content_type == "video/mp4"


def test_preview_route_traversal_blocked(client, tmp_path, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path / "lib"))
    monkeypatch.setattr(Rems_Dl, "PREVIEW_CACHE_DIR", str(tmp_path / "pcache"))
    r = client.get("/api/gallery/preview/..%2F..%2Fetc%2Fpasswd", headers=H)
    assert r.status_code in (403, 404)
