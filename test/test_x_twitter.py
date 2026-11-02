import sys

import pytest

from workers.x_twitter import XWorker

NET_CONFIG = {"anti_ban_pause": 0}


def _raise_value_error(*_a, **_k):
    raise AssertionError("add_account_cookies must not be called without a username")


@pytest.fixture
def make_worker(tmp_path, monkeypatch):
    def _make(tag="@rem", amount=10):
        monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
        return XWorker(tag, amount, dict(NET_CONFIG))
    return _make


class TestNormalizeHandle:
    @pytest.mark.parametrize("raw,expected", [
        ("@rem", "rem"),
        ("rem", "rem"),
        ("https://x.com/rem", "rem"),
        ("https://twitter.com/rem", "rem"),
        ("https://x.com/@rem", "rem"),
        ("https://x.com/rem/media", "rem"),
        ("  @rem  ", "rem"),
        ("rem/", "rem"),
        ("", ""),
    ])
    def test_variants(self, raw, expected):
        assert XWorker.normalize_handle(raw) == expected


class TestOrigPhotoUrl:
    def test_modern_url_with_path_extension(self):
        ext, url = XWorker.orig_photo_url(
            "https://pbs.twimg.com/media/AbCdEf.jpg?name=900x900")
        assert ext == "jpg"
        assert url == "https://pbs.twimg.com/media/AbCdEf.jpg?name=orig"

    def test_legacy_url_with_format_param(self):
        ext, url = XWorker.orig_photo_url(
            "https://pbs.twimg.com/media/AbCdEf?format=png&name=small")
        assert ext == "png"
        assert url == "https://pbs.twimg.com/media/AbCdEf.png?name=orig"

    def test_no_query_no_extension(self):
        ext, url = XWorker.orig_photo_url("https://pbs.twimg.com/media/AbCdEf")
        assert ext == "jpg"
        assert url == "https://pbs.twimg.com/media/AbCdEf.jpg?name=orig"


class TestBuildFilename:
    def test_basic(self):
        assert XWorker.build_filename(12345, 1, "jpg") == "12345_1.jpg"

    def test_ext_normalized(self):
        assert XWorker.build_filename(1, 2, ".PNG") == "1_2.png"


class TestBuildTags:
    class Tweet:
        def __init__(self, hashtags):
            self.hashtags = hashtags

    def test_hashtags_lowercased_deduped(self):
        tweet = self.Tweet(["Rem", "#Anime", "rem"])
        assert XWorker.build_tags(tweet) == ["rem", "anime"]

    def test_empty_hashtags_give_empty_tags(self):
        assert XWorker.build_tags(self.Tweet([])) == []


class TestAuthMode:
    def test_cookies_beat_password(self):
        assert XWorker.auth_mode({"x_cookies": "auth_token=x; ct0=y",
                                  "x_username": "rem"}) == "cookies"

    def test_password(self):
        assert XWorker.auth_mode({"x_username": "rem"}) == "password"

    def test_none(self):
        assert XWorker.auth_mode({}) == "none"


class TestCookieString:
    def test_raw_header_passes_through(self, tmp_path):
        raw = "auth_token=abc; ct0=def"
        assert XWorker.load_cookie_string(raw) == raw

    def test_file_path(self, tmp_path):
        f = tmp_path / "cookies.json"
        f.write_text('[{"name":"auth_token","value":"abc"},{"name":"ct0","value":"def"}]')
        assert XWorker.load_cookie_string(str(f)) == f.read_text().strip()

    def test_netscape_file(self, tmp_path):
        f = tmp_path / "cookies.txt"
        f.write_text(
            "# Netscape HTTP Cookie File\n"
            ".x.com\tTRUE\t/\tTRUE\t1900000000\tauth_token\tabc\n"
            ".x.com\tTRUE\t/\tTRUE\t1900000000\tct0\tdef\n"
            "other.example\tTRUE\t/\tFALSE\t1900000000\tjunk\tzzz\n")
        out = XWorker.load_cookie_string(str(f))
        assert out == "auth_token=abc; ct0=def; junk=zzz"

    def test_httponly_rows(self, tmp_path):
        f = tmp_path / "cookies.txt"
        f.write_text(
            "#HttpOnly_.x.com\tTRUE\t/\tTRUE\t1900000000\tct0\tdef\n")
        assert XWorker.load_cookie_string(str(f)) == "ct0=def"


class TestWorkerSetup:
    def test_profile_dir(self, make_worker, tmp_path):
        w = make_worker("@rem")
        assert w.profile_dir == str(tmp_path / "X" / "Rem")

    def test_empty_tag(self, make_worker):
        w = make_worker("")
        assert w.handle == ""

    def test_session_db_in_database_dir(self):
        import os

        from core.database import DATABASE_DIR
        from workers.x_twitter import TWSCRAPE_DB
        assert TWSCRAPE_DB == os.path.join(DATABASE_DIR, "x_accounts.db")


def test_cookies_without_username_are_rejected(make_worker):
    w = make_worker()
    w.net_config["x_cookies"] = "auth_token=abc; ct0=def"
    w.net_config.pop("x_username", None)
    pool_stub = type("P", (), {"add_account_cookies": _raise_value_error})()
    with pytest.raises(ValueError, match="X Username"):
        import asyncio
        asyncio.run(w._ensure_account(pool_stub, "cookies"))


class TestSettingsRoundTrip:
    def test_x_credentials_persist(self, tmp_path):
        import os

        from core.database import SettingsManager
        m = SettingsManager(str(tmp_path))
        m.save_api_settings({"x_username": "rem", "x_password": "pw",
                             "x_email": "rem@mail.com", "x_email_password": "epw"})
        assert os.environ.get("X_USERNAME") == "rem"
        loaded = m.load_api_settings()
        assert loaded["x_username"] == "rem"
        assert loaded["x_password"] == "pw"
        assert loaded["x_email"] == "rem@mail.com"
        assert loaded["x_email_password"] == "epw"


NETSCAPE_COOKIES = (
    "# Netscape HTTP Cookie File\n"
    ".x.com\tTRUE\t/\tTRUE\t1900000000\tauth_token\tabc\n"
    ".x.com\tTRUE\t/\tTRUE\t1900000000\tct0\tdef\n"
)


class TestCookieImport:
    def _client(self):
        import Rems_Dl
        Rems_Dl.app.config["TESTING"] = True
        return Rems_Dl.app.test_client()

    def test_upload_netscape_file(self, tmp_path, monkeypatch):
        import io

        import Rems_Dl
        monkeypatch.setattr(Rems_Dl, "DATABASE_DIR", str(tmp_path))
        with self._client() as client:
            r = client.post("/api/import-x-cookies", data={
                "file": (io.BytesIO(NETSCAPE_COOKIES.encode()), "cookies.txt")})
            assert r.status_code == 200
            data = r.get_json()
            assert data["success"] is True
            assert data["path"] == str(tmp_path / "x_cookies_import.txt")
            with open(data["path"], encoding="utf-8") as f:
                assert "auth_token\tabc" in f.read()

    def test_upload_rejects_missing_cookies(self):
        with self._client() as client:
            import io
            r = client.post("/api/import-x-cookies", data={
                "file": (io.BytesIO(b"nothing here"), "junk.txt")})
            assert r.status_code == 400
            assert "auth_token" in r.get_json()["error"]

    def test_path_upload_gated_by_recent_pick(self, tmp_path, monkeypatch):
        import time

        import Rems_Dl
        monkeypatch.setattr(Rems_Dl, "DATABASE_DIR", str(tmp_path))
        staged_src = tmp_path / "picked.txt"
        staged_src.write_text("auth_token=abc; ct0=def", encoding="utf-8")
        with self._client() as client:
            r = client.post("/api/import-x-cookies", data={"path": str(staged_src)})
            assert r.status_code == 403
            Rems_Dl._wp_recent_picks[str(staged_src)] = time.time()
            r = client.post("/api/import-x-cookies", data={"path": str(staged_src)})
            assert r.status_code == 200
            staged = r.get_json()["path"]
            assert staged == str(tmp_path / "x_cookies_import.txt")
            with open(staged, encoding="utf-8") as f:
                assert "auth_token=abc" in f.read()

    def test_browse_rejects_unknown_kind(self):
        with self._client() as client:
            r = client.post("/api/file/browse", json={"kind": "evil"})
            assert r.status_code == 400

    @pytest.mark.skipif(not sys.platform.startswith("linux"),
                        reason="kdialog/zenity picker is linux-only")
    def test_browse_kind_reaches_picker(self, monkeypatch):
        import Rems_Dl
        seen = {}
        monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
        monkeypatch.setattr(Rems_Dl, "_pick_image_file_desktop",
                            lambda kind="images": seen.setdefault("kind", kind) or None)
        with Rems_Dl.app.test_client() as client:
            client.post("/api/file/browse", json={"kind": "cookies"})
        assert seen.get("kind") == "cookies"
