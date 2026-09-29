import os
from unittest.mock import patch, MagicMock

import pytest

# Rems_Dl's load_dotenv() pollutes os.environ, which SettingsManager reads for
# its defaults — snapshot and restore so test_settings stays isolated.
_ENV_KEYS = [
    "API_TIMEOUT", "RETRY_WAIT", "ANTI_BAN_PAUSE", "DOWNLOAD_RETRIES",
    "USE_PROXY", "PROXY_URL", "VERIFY_TLS",
]
_env_before = {k: os.environ.get(k) for k in _ENV_KEYS}

import Rems_Dl  # noqa: E402

for _k, _v in _env_before.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

from core import shared  # noqa: E402

H = {"User-Agent": "RemsDlDesktopApp/1.0"}


@pytest.fixture
def client():
    Rems_Dl.app.config["TESTING"] = True
    with Rems_Dl.app.test_client() as c:
        yield c


@pytest.fixture
def lib(tmp_path, monkeypatch):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "one.png").write_bytes(b"x")
    (tmp_path / "two.png").write_bytes(b"y")
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    return tmp_path


class _FakeStdin:
    def __init__(self):
        self.buf = b""

    def write(self, b):
        self.buf += b

    def close(self):
        pass


class _FakeProc:
    def __init__(self, *a, **k):
        self.stdin = _FakeStdin()
        self.returncode = 0

    def wait(self, timeout=None):
        return 0


def test_clipboard_multi_uri_linux(client, lib):
    captured = {}

    def fake_popen(*a, **k):
        p = _FakeProc()
        captured["stdin"] = p.stdin
        return p

    with patch("sys.platform", "linux"), \
         patch("shutil.which", return_value="/usr/bin/wl-copy"), \
         patch("subprocess.Popen", side_effect=fake_popen):
        r = client.post(
            "/api/clipboard?uri=1",
            data="a/one.png\ntwo.png",
            headers=H,
        )
    assert r.status_code == 200
    assert b"one.png\r\n" in captured["stdin"].buf
    assert b"two.png\r\n" in captured["stdin"].buf


def test_clipboard_single_uri_linux(client, lib):
    captured = {}

    def fake_popen(*a, **k):
        p = _FakeProc()
        captured["stdin"] = p.stdin
        return p

    with patch("sys.platform", "linux"), \
         patch("shutil.which", return_value="/usr/bin/wl-copy"), \
         patch("subprocess.Popen", side_effect=fake_popen):
        r = client.post("/api/clipboard?uri=1", data="two.png", headers=H)
    assert r.status_code == 200
    assert b"two.png\r\n" in captured["stdin"].buf


def test_clipboard_windows_powershell(client, lib):
    mock_run = MagicMock()
    mock_run.return_value.returncode = 0
    with patch("sys.platform", "win32"), \
         patch("subprocess.run", mock_run):
        r = client.post("/api/clipboard?uri=1", data="two.png", headers=H)
    assert r.status_code == 200
    mock_run.assert_called_once()
    assert "Set-Clipboard" in mock_run.call_args[0][0][4]


def test_clipboard_uri_traversal_forbidden(client, lib):
    with patch("subprocess.Popen") as popen:
        r = client.post("/api/clipboard?uri=1", data="../etc/passwd", headers=H)
    assert r.status_code == 403
    popen.assert_not_called()


def test_clipboard_uri_missing_404(client, lib):
    with patch("subprocess.Popen") as popen:
        r = client.post("/api/clipboard?uri=1", data="nope.png", headers=H)
    assert r.status_code == 404
    popen.assert_not_called()


def test_favourite_single_toggle(client, monkeypatch):
    gal = {"images": [{"id": "aaa", "filename": "1.png", "filepath": "1.png", "favourite": False}]}
    monkeypatch.setattr(shared, "load_gallery", lambda: gal)
    monkeypatch.setattr(shared, "save_gallery", lambda d: None)

    r = client.post("/api/gallery/favourite", json={"id": "aaa"}, headers=H)
    assert r.status_code == 200
    assert r.get_json() == {"success": True, "favourite": True}
    r = client.post("/api/gallery/favourite", json={"id": "aaa"}, headers=H)
    assert r.get_json()["favourite"] is False


def test_gallery_rating_and_site_filters():
    mock_images = [
        {"id": "1", "filename": "safe1.png", "site": "zerochan", "filepath": "Zerochan/safe1.png", "tags": {"tag": ["1girl", "smile"]}},
        {"id": "2", "filename": "safe2.png", "site": "nekos.best", "filepath": "Nekos.best/safe2.png", "tags": {"tag": ["neko"]}},
        {"id": "3", "filename": "safe3.png", "site": "anime_dl", "filepath": "AnimePictures/safe3.png", "tags": {"tag": ["blue hair"]}},
        {"id": "4", "filename": "nsfw1.png", "site": "rule34", "filepath": "Rule34/nsfw1.png", "tags": {"tag": ["nude"]}},
        {"id": "5", "filename": "nsfw2.png", "site": "yande", "filepath": "Yande/NSFW/nsfw2.png", "tags": {"tag": ["explicit", "nipples"]}},
    ]
    # Filter safe only
    safe_res = Rems_Dl._apply_gallery_filters(mock_images, "", [], False, [], ["safe"])
    safe_ids = {i["id"] for i in safe_res}
    assert "1" in safe_ids
    assert "2" in safe_ids
    assert "3" in safe_ids
    assert "4" not in safe_ids
    assert "5" not in safe_ids

    # Filter explicit only
    nsfw_res = Rems_Dl._apply_gallery_filters(mock_images, "", [], False, [], ["explicit"])
    nsfw_ids = {i["id"] for i in nsfw_res}
    assert "4" in nsfw_ids
    assert "5" in nsfw_ids
    assert "1" not in nsfw_ids

    # Filter by specific site
    site_res = Rems_Dl._apply_gallery_filters(mock_images, "", ["zerochan"], False, [], [])
    assert len(site_res) == 1
    assert site_res[0]["id"] == "1"



def test_gallery_endpoint_lists_images(client):
    # regression: get_gallery referenced `dirty` without initializing it —
    # crashed with UnboundLocalError whenever no filepath repair was needed
    r = client.get("/api/gallery?page=1&per_page=5", headers=H)
    assert r.status_code == 200
    j = r.get_json()
    assert isinstance(j["images"], list)
    assert j["total"] >= len(j["images"])
    for img in j["images"]:
        assert img.get("filepath")


def test_gallery_date_filter(client, monkeypatch):
    # self-seeded: other suite tests may leave the real gallery empty, and the
    # endpoint must never persist anything from this run
    newer = {"filename": "__tsfilter_newer__.png", "site": "zerochan", "filepath": "Zerochan/__tsfilter_newer__.png", "tags": {},
             "downloaded_at": "2026-01-02T03:04:05"}
    older = {"filename": "__tsfilter_older__.png", "site": "zerochan", "filepath": "Zerochan/__tsfilter_older__.png", "tags": {},
             "downloaded_at": "2026-01-02T01:00:00"}
    monkeypatch.setattr(Rems_Dl.shared, "load_gallery", lambda: {"images": [newer, older]})
    monkeypatch.setattr(Rems_Dl, "_build_filepath_cache", lambda: {
        newer["filename"]: newer["filepath"],
        older["filename"]: older["filepath"],
    })
    data = client.get("/api/gallery?page=1&per_page=400", headers=H).get_json()
    newest = data["images"][0]
    ts = Rems_Dl._image_timestamp(newest)
    win = client.get(f"/api/gallery?page=1&per_page=400&from_ts={ts}&to_ts={ts + 1}", headers=H).get_json()
    assert any(i["filename"] == newest["filename"] for i in win["images"])
    assert win["total"] < data["total"]
    assert all(ts <= Rems_Dl._image_timestamp(i) <= ts + 1 for i in win["images"])
    # malformed timestamps are ignored instead of 500ing
    bad = client.get("/api/gallery?page=1&per_page=400&from_ts=notanumber", headers=H).get_json()
    assert bad["total"] == data["total"]
