"""Wallpaper picker: native-path upload branch + /api/file/browse wiring."""
import io

import pytest

from Rems_Dl import app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("Rems_Dl.BASE_DIR", str(tmp_path))
    c = app.test_client()
    c.environ_base["HTTP_USER_AGENT"] = "RemsDlDesktopApp/1.0"
    return c


def _img(tmp_path, ext=".png"):
    p = tmp_path / f"picked{ext}"
    p.write_bytes(b"\x89PNG fake image bytes")
    return p


def test_path_upload_copies_into_user_wallpapers(client, tmp_path):
    src = _img(tmp_path)
    r = client.post("/api/upload_wallpaper", data={"path": str(src)})
    assert r.status_code == 200 and r.get_json()["success"] is True
    stored = tmp_path / "user_wallpapers" / src.name
    assert stored.read_bytes() == src.read_bytes()
    assert src.exists()  # original untouched


def test_path_upload_rejects_non_image_extension(client, tmp_path):
    src = _img(tmp_path, ext=".txt")
    r = client.post("/api/upload_wallpaper", data={"path": str(src)})
    assert r.status_code == 400


def test_path_upload_rejects_missing_file_and_directory(client, tmp_path):
    assert client.post("/api/upload_wallpaper",
                       data={"path": str(tmp_path / "nope.png")}).status_code == 400
    assert client.post("/api/upload_wallpaper",
                       data={"path": str(tmp_path)}).status_code == 400


def test_regular_file_upload_still_works(client, tmp_path):
    r = client.post("/api/upload_wallpaper",
                    data={"file": (io.BytesIO(b"\x89PNG x"), "up.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 200 and r.get_json()["success"] is True
    assert (tmp_path / "user_wallpapers" / "up.png").exists()


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
