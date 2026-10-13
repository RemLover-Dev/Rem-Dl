"""POST /api/tags/nekosbest serves the /endpoints catalogue with a daily disk cache."""
import json
import os
import time

import Rems_Dl

HEADERS = {"User-Agent": "RemsDlDesktopApp/1.0"}
EPS = {"neko": {"format": "png"}, "wink": {"format": "gif"}}


def _pin(tmp_path, monkeypatch, eps=EPS, age_s=0):
    monkeypatch.setattr(Rems_Dl, "DATABASE_DIR", str(tmp_path))
    monkeypatch.setattr(Rems_Dl, "_nekosbest_eps_cache", {"eps": {}, "at": 0.0})
    at = time.time() - age_s
    with open(os.path.join(str(tmp_path), "nekosbest_endpoints.json"), "w", encoding="utf-8") as f:
        json.dump({"eps": eps, "at": at}, f)

    def boom(*a, **k):
        raise AssertionError("fresh disk cache must not hit the network")

    monkeypatch.setattr(Rems_Dl, "get_session", boom)


def test_fresh_disk_cache_served(tmp_path, monkeypatch):
    # shape is flattened {category: format} for the frontend dropdown
    _pin(tmp_path, monkeypatch, eps={"neko": "png", "wink": "gif"})
    resp = Rems_Dl.app.test_client().post("/api/tags/nekosbest", headers=HEADERS, json={})
    assert resp.status_code == 200
    assert resp.get_json() == {"neko": "png", "wink": "gif"}


def test_stale_disk_cache_triggers_live_fetch(tmp_path, monkeypatch):
    _pin(tmp_path, monkeypatch, age_s=90000)

    class _Resp:
        status_code = 200

        def json(self):
            return EPS

    class _Sess:
        headers = {}

        def get(self, url, timeout=None):
            assert url == "https://nekos.best/api/v2/endpoints"
            return _Resp()

    monkeypatch.setattr(Rems_Dl, "get_session", lambda site, cfg: _Sess())
    resp = Rems_Dl.app.test_client().post("/api/tags/nekosbest", headers=HEADERS, json={})
    assert resp.get_json() == {"neko": "png", "wink": "gif"}
    # the live result lands on disk for the next startup
    with open(os.path.join(str(tmp_path), "nekosbest_endpoints.json"), "r", encoding="utf-8") as f:
        assert json.load(f)["eps"] == {"neko": "png", "wink": "gif"}


def test_failed_fetch_falls_back_to_stale_disk(tmp_path, monkeypatch):
    _pin(tmp_path, monkeypatch, eps={"neko": "png"}, age_s=90000)

    def boom(*a, **k):
        raise ConnectionError

    monkeypatch.setattr(Rems_Dl, "get_session", boom)
    resp = Rems_Dl.app.test_client().post("/api/tags/nekosbest", headers=HEADERS, json={})
    assert resp.get_json() == {"neko": "png"}
