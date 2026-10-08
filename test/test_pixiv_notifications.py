import os
import time

import pytest

# import Rems_Dl with the same env snapshot/restore dance as
# test_download_queue, so load_dotenv side effects don't leak into test_settings
_ENV_KEYS = [
    "USE_PROXY", "PROXY_URL", "VERIFY_TLS", "PIXIV_REFRESH_TOKEN",
]
_env_before = {k: os.environ.get(k) for k in _ENV_KEYS}

import Rems_Dl  # noqa: E402
import core.notifications as notifications  # noqa: E402
import core.pixiv_notify as pn  # noqa: E402

for _k, _v in _env_before.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


def _work(wid, user=7, title=None, name="Artist"):
    return {
        "id": wid,
        "title": title or f"work {wid}",
        "user": {"id": user, "name": name},
        "image_urls": {"square_medium": f"https://i.pximg.net/thumb/{wid}.jpg"},
        "create_date": "2026-09-30T12:00:00+09:00",
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(notifications, "NOTIF_FILE", str(tmp_path / "notifications.json"))
    monkeypatch.setattr(notifications, "LEGACY_PIXIV_FILE", str(tmp_path / "legacy_missing.json"))
    monkeypatch.setattr(notifications, "_migration_done", True)
    pn.STATE.update({"error": "", "checking": False, "last_new": 0, "pending_toast": 0})
    return tmp_path


def test_ingest_dedup(store):
    fresh = pn.ingest([_work(1), _work(2), _work(3)])
    assert [e["work_id"] for e in fresh] == ["1", "2", "3"]
    again = pn.ingest([_work(1), _work(2), _work(3)])
    assert again == []
    assert len(pn.load_items()) == 3


def test_persistence_across_restart(store):
    pn.ingest([_work(1), _work(2)])
    pn.mark_read(["1"])
    # fresh read from disk = simulated app restart
    items = pn.load_items()
    assert [i["work_id"] for i in items] == ["1", "2"]
    assert {i["work_id"]: i["read"] for i in items} == {"1": True, "2": False}
    assert pn._load_raw()["last_check"] == 0  # no check stamped yet


def test_read_vs_unread(store):
    pn.ingest([_work(1), _work(2), _work(3)])
    assert pn.unread_count() == 3
    assert pn.mark_read() == 0
    pn.ingest([_work(4)])
    assert pn.unread_count() == 1
    assert pn.mark_read(["4"]) == 0
    pn.ingest([_work(5), _work(6)])
    assert pn.mark_read(["5"]) == 1  # subset marking leaves 6 unread


def test_startup_discovery_and_toast(store, monkeypatch):
    monkeypatch.setattr(pn, "_get_config", lambda: {"refresh_token": "tok"})
    monkeypatch.setattr(pn, "_build_api", lambda cfg: object())
    batch = [_work(1), _work(2), _work(3)]
    monkeypatch.setattr(pn, "_fetch_works",
                        lambda api, known: [w for w in batch if str(w["id"]) not in known])
    res = pn.run_check()
    assert res["error"] == "" and res["new"] == 3
    assert pn.take_pending_toast() == 3
    assert pn.take_pending_toast() == 0
    # next launch finds nothing new -> no toast
    assert pn.run_check()["new"] == 0
    assert pn.take_pending_toast() == 0


def test_multi_upload_batch(store, monkeypatch):
    monkeypatch.setattr(pn, "_get_config", lambda: {"refresh_token": "tok"})
    monkeypatch.setattr(pn, "_build_api", lambda cfg: object())
    seen = []
    monkeypatch.setattr(pn, "_fetch_works",
                        lambda api, known: [w for w in seen if str(w["id"]) not in known])
    seen.extend([_work(1, user=9), _work(2, user=9), _work(3, user=9)])
    assert pn.run_check()["new"] == 3
    seen.extend([_work(4, user=9), _work(5, user=9)])
    assert pn.run_check()["new"] == 2  # same artist, later batch: 3 + 2 = 5
    assert len(pn.load_items()) == 5


def test_auth_failure_isolated(store, monkeypatch):
    monkeypatch.setattr(pn, "_get_config", lambda: {"refresh_token": "bad"})
    monkeypatch.setattr(pn, "_build_api",
                        lambda cfg: (_ for _ in ()).throw(ValueError("Invalid refresh token")))
    res = pn.run_check()
    assert res["new"] == 0
    assert "refresh token" in res["error"].lower()
    assert pn.load_items() == []
    assert pn.STATE["pending_toast"] == 0


def test_invalid_refresh_token_logs_steps(store, monkeypatch):
    # the hint must fire on a dead token, not just a missing one
    from workers.pixiv import PixivAppAPI
    lines = []
    monkeypatch.setattr(pn, "_log_fn", lines.append)
    monkeypatch.setattr(pn, "_hint_logged", False)

    class _Resp:
        status_code = 400
        text = '{"has_error":true,"errors":{"system":{"code":1508}}}'

    class _Session:
        headers = {}

        def post(self, *a, **k):
            return _Resp()

    api = PixivAppAPI(_Session(), lambda *a, **k: None, "dead-token")
    with pytest.raises(ValueError, match="Invalid refresh token"):
        api.login()
    text = "\n".join(lines)
    assert "Get login URL" in text and "callback?state=" in text
    # once per app run — a second failed login must not repeat the steps
    lines.clear()
    with pytest.raises(ValueError):
        api.login()
    assert lines == []


def test_missing_token_reported_not_raised(store, monkeypatch):
    monkeypatch.setattr(pn, "_get_config", lambda: {})
    res = pn.run_check()
    assert res["new"] == 0 and "refresh token" in res["error"].lower()
    assert pn.load_items() == []
    # a failing check still stamps the clock so it cannot hot-loop
    assert pn.last_check_ts() > 0


def test_missing_token_logs_steps_once(store, monkeypatch):
    lines = []
    monkeypatch.setattr(pn, "_get_config", lambda: {})
    monkeypatch.setattr(pn, "_log_fn", lines.append)
    monkeypatch.setattr(pn, "_hint_logged", False)
    res = pn.run_check()
    assert res["new"] == 0
    text = "\n".join(lines)
    assert "Settings" in text and "Get login URL" in text
    assert "app-api.pixiv.net/web/v1/login" in text
    assert "callback?state=" in text
    # once per app run — later scans must not spam the console
    lines.clear()
    pn.run_check()
    assert lines == []


def test_interval_clamps_to_1_2_hours(store, monkeypatch):
    for value, expected in [(30, 60), (60, 60), (90, 90), (120, 120), (500, 120)]:
        monkeypatch.setattr(pn.DatabaseManager, "load_ui_config",
                            lambda v=value: {"pixiv_watch_minutes": v})
        assert pn.interval_minutes() == expected
    monkeypatch.setattr(pn.DatabaseManager, "load_ui_config",
                        lambda: {"pixiv_watch_minutes": "junk"})
    assert pn.interval_minutes() == 90


def test_timer_counts_while_closed(store, monkeypatch):
    monkeypatch.setattr(pn.DatabaseManager, "load_ui_config",
                        lambda: {"pixiv_watch_minutes": 90})
    # never checked -> due immediately (scan once the app is open)
    assert pn.seconds_until_due() == 0
    # checked 20 min ago -> 70 min of wall-clock left
    pn._stamp_check(time.time() - 20 * 60)
    due = pn.seconds_until_due()
    assert 69 * 60 <= due <= 70 * 60
    # checked 3 h ago (PC was off) -> overdue, scan at once
    pn._stamp_check(time.time() - 180 * 60)
    assert pn.seconds_until_due() == 0


def test_navigation_payload_artist_id(store):
    (e,) = pn.ingest([_work(12345678, user=12345678, name="Rem")])
    # this is the value the ⚡ button fills into the Pixiv downloader
    assert e["artist_id"] == "12345678"
    assert e["thumb"].startswith("https://i.pximg.net/")
    assert e["read"] is False


def test_clear_does_not_renotify(store):
    pn.ingest([_work(1), _work(2)])
    pn.clear_items()
    assert pn.load_items() == []
    # cleared ids are remembered: next scan must not re-notify them
    assert pn.ingest([_work(1), _work(2)]) == []
    fresh = pn.ingest([_work(3)])
    assert [e["work_id"] for e in fresh] == ["3"]


def test_api_endpoints(store, monkeypatch):
    monkeypatch.setattr(pn.DatabaseManager, "load_ui_config",
                        lambda: {"pixiv_watch_minutes": 90})
    client = Rems_Dl.app.test_client()
    client.environ_base["HTTP_USER_AGENT"] = "RemsDlDesktopApp test"
    data = client.get("/api/pixiv/notifications").get_json()
    assert data["items"] == [] and data["unread"] == 0
    assert data["interval_minutes"] == 90

    pn.ingest([_work(1), _work(2)])
    assert client.get("/api/pixiv/notifications").get_json()["unread"] == 2
    assert client.post("/api/pixiv/notifications/read", json={}).get_json()["unread"] == 0
    assert client.get("/api/pixiv/notifications").get_json()["unread"] == 0

    pn.ingest([_work(3)])
    r = client.post("/api/pixiv/notifications/clear")
    assert r.status_code == 200 and r.get_json()["unread"] == 0
    assert client.get("/api/pixiv/notifications").get_json()["items"] == []


def test_thumb_rejects_foreign_hosts(store):
    client = Rems_Dl.app.test_client()
    client.environ_base["HTTP_USER_AGENT"] = "RemsDlDesktopApp test"
    assert client.get("/api/pixiv/notif_thumb?url=https://evil.example/x.jpg").status_code == 400
    assert client.get("/api/pixiv/notif_thumb?url=notaurl").status_code == 400


def test_oauth_code_exchange(store, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "_PIXIV_OAUTH", {"verifier": ""})
    client = Rems_Dl.app.test_client()
    client.environ_base["HTTP_USER_AGENT"] = "RemsDlDesktopApp test"

    # code pasted before "Get login URL" -> clear guidance, no network
    r = client.post("/api/pixiv/exchange-cookie", json={"cookie": "AbCdEf123"})
    assert r.status_code == 400 and "Get login URL" in r.get_json()["error"]

    # start mirrors gallery-dl's oauth:pixiv URL and keeps the verifier
    r = client.post("/api/pixiv/oauth/start").get_json()
    assert r["success"]
    assert r["url"].startswith("https://app-api.pixiv.net/web/v1/login?client=pixiv-android")
    assert "code_challenge=" in r["url"] and "code_challenge_method=S256" in r["url"]
    assert Rems_Dl._PIXIV_OAUTH["verifier"]

    # gallery-dl's step-by-step text pasted instead of a code -> explicit error
    r = client.post("/api/pixiv/exchange-cookie",
                    json={"cookie": "1) Open your browser's Developer Tools (F12)"})
    assert r.status_code == 400 and "code" in r.get_json()["error"].lower()

    # full callback URL -> code extracted and exchanged with the stored verifier
    class _Resp:
        def json(self):
            return {"refresh_token": "NEW_TOK"}

    class _Session:
        def post(self, url, **kw):
            assert url == "https://oauth.secure.pixiv.net/auth/token"
            assert kw["data"]["code"] == "THECODE"
            assert kw["data"]["code_verifier"] == Rems_Dl._PIXIV_OAUTH["verifier"]
            return _Resp()

    monkeypatch.setattr(Rems_Dl, "_pixiv_session", lambda cookie=None: _Session())
    saved = {}
    monkeypatch.setattr(Rems_Dl.settings, "save_api_settings", lambda d: saved.update(d))
    monkeypatch.setattr(Rems_Dl.settings, "load_api_settings", lambda: {"pixiv_cookie": "old"})
    r = client.post("/api/pixiv/exchange-cookie", json={
        "cookie": "https://app-api.pixiv.net/web/v1/users/auth/pixiv/callback?state=x&code=THECODE"})
    body = r.get_json()
    assert body["success"] and body["refresh_token"] == "NEW_TOK"
    assert saved["pixiv_refresh_token"] == "NEW_TOK"
    assert saved["pixiv_cookie"] == "old"  # code path must not clobber the cookie


def test_pixiv_session_proxy_respects_use_proxy(monkeypatch):
    # proxy off (default) -> direct connection; on -> configured proxy
    monkeypatch.setattr(Rems_Dl.settings, "get",
                        lambda k, d=None: {"use_proxy": False, "proxy_url": "http://127.0.0.1:10808"}.get(k, d))
    assert Rems_Dl._pixiv_session().proxies == {}
    monkeypatch.setattr(Rems_Dl.settings, "get",
                        lambda k, d=None: {"use_proxy": True, "proxy_url": "http://127.0.0.1:10808"}.get(k, d))
    s = Rems_Dl._pixiv_session()
    assert s.proxies["https"] == "http://127.0.0.1:10808"
