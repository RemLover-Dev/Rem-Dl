"""Wallpaper picker: native-path upload branch + /api/file/browse wiring."""
import io
import os

import pytest

import Rems_Dl
from Rems_Dl import _remember_wp_pick, _wp_recent_picks, app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("Rems_Dl.BASE_DIR", str(tmp_path))
    _wp_recent_picks.clear()
    c = app.test_client()
    c.environ_base["HTTP_USER_AGENT"] = "RemsDlDesktopApp/1.0"
    return c


def _png_bytes():
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def test_path_upload_copies_into_user_wallpapers(client, tmp_path):
    src = tmp_path / "picked.png"
    src.write_bytes(_png_bytes())
    _remember_wp_pick(str(src))
    r = client.post("/api/upload_wallpaper", data={"path": str(src)})
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True
    assert body["filename"].endswith(".png")
    stored = tmp_path / "user_wallpapers" / body["filename"]
    assert stored.read_bytes() == src.read_bytes()
    assert src.exists()  # original untouched


def test_path_upload_rejects_never_picked(client, tmp_path):
    src = tmp_path / "picked.png"
    src.write_bytes(_png_bytes())
    r = client.post("/api/upload_wallpaper", data={"path": str(src)})
    assert r.status_code == 403


def test_path_upload_rejects_non_image_extension(client, tmp_path):
    src = tmp_path / "picked.txt"
    src.write_bytes(b"plain text is not an image")
    _remember_wp_pick(str(src))
    r = client.post("/api/upload_wallpaper", data={"path": str(src)})
    assert r.status_code == 400


def test_path_upload_rejects_missing_file_and_directory(client, tmp_path):
    _remember_wp_pick(str(tmp_path / "nope.png"))
    assert client.post("/api/upload_wallpaper",
                       data={"path": str(tmp_path / "nope.png")}).status_code == 400
    _remember_wp_pick(str(tmp_path))
    assert client.post("/api/upload_wallpaper",
                       data={"path": str(tmp_path)}).status_code == 400


def test_regular_file_upload_still_works(client):
    data = {"file": (io.BytesIO(_png_bytes()), "up.png")}
    r = client.post("/api/upload_wallpaper", data=data,
                    content_type="multipart/form-data")
    assert r.status_code == 200
    body = r.get_json()
    assert body["success"] is True and body["filename"].endswith(".png")
    custom_dir = os.path.join(Rems_Dl.BASE_DIR, "user_wallpapers")
    assert os.path.exists(os.path.join(custom_dir, body["filename"]))


def test_browse_non_kde_gets_error(client, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    r = client.post("/api/file/browse")
    assert r.status_code == 501 and "error" in r.get_json()


def test_browse_kde_cancelled(client, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr("Rems_Dl._pick_image_file_desktop", lambda: None)
    r = client.post("/api/file/browse")
    assert r.status_code == 200 and r.get_json() == {"cancelled": True}


def test_browse_kde_no_picker(client, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr("Rems_Dl._pick_image_file_desktop", lambda: False)
    r = client.post("/api/file/browse")
    assert r.status_code == 500


def test_browse_kde_returns_path(client, monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr("Rems_Dl._pick_image_file_desktop", lambda: "/tmp/x.png")
    r = client.post("/api/file/browse")
    assert r.status_code == 200 and r.get_json() == {"path": "/tmp/x.png"}


def test_jxl_picker_prefers_kdialog(monkeypatch):
    import subprocess

    calls = []

    class R:
        returncode = 0
        stdout = "/tmp/x.png"
        stderr = ""

    def fake_run(cmd, **kw):
        calls.append(cmd[0])
        return R()

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert Rems_Dl._pick_jxl_image() == "/tmp/x.png"
    assert calls[0] == "kdialog"


def test_jxl_picker_cancelled_kdialog(monkeypatch):
    import subprocess

    class R:
        returncode = 1
        stdout = ""
        stderr = ""

    monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: R())
    assert Rems_Dl._pick_jxl_image() is None
