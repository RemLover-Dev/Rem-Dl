"""POST /api/tags/nekosapi serves the whole harvested vocabulary for the dropdown."""
import json
import os
import time

import Rems_Dl

HEADERS = {"User-Agent": "RemsDlDesktopApp/1.0"}
TAGS = ["catgirl", "blue_hair", "tail", "glasses"]


def _pin(tmp_path, monkeypatch, tags=TAGS, age_s=0):
    monkeypatch.setattr(Rems_Dl, "DATABASE_DIR", str(tmp_path))
    monkeypatch.setattr(Rems_Dl, "_nekosapi_live_cache", {"tags": [], "at": 0.0})
    at = time.time() - age_s
    with open(os.path.join(str(tmp_path), "nekosapi_live_tags.json"), "w", encoding="utf-8") as f:
        json.dump({"tags": list(tags), "at": at}, f)

    def boom(*a, **k):
        raise AssertionError("fresh disk cache must not trigger a live harvest")

    monkeypatch.setattr(Rems_Dl, "get_session", boom)


def test_no_query_returns_full_list(tmp_path, monkeypatch):
    _pin(tmp_path, monkeypatch)
    resp = Rems_Dl.app.test_client().post("/api/tags/nekosapi", headers=HEADERS, json={})
    assert resp.status_code == 200
    assert resp.get_json() == TAGS


def test_single_char_query_returns_full_list(tmp_path, monkeypatch):
    # the dropdown's own request has no query; a 1-char leftover still gets everything
    _pin(tmp_path, monkeypatch)
    resp = Rems_Dl.app.test_client().post("/api/tags/nekosapi", headers=HEADERS, json={"query": "c"})
    assert resp.get_json() == TAGS


def test_two_char_query_prefix_filters(tmp_path, monkeypatch):
    _pin(tmp_path, monkeypatch)
    resp = Rems_Dl.app.test_client().post("/api/tags/nekosapi", headers=HEADERS, json={"query": "ta"})
    assert resp.get_json() == ["tail"]


def test_query_caps_at_50(tmp_path, monkeypatch):
    tags = [f"tag{i:03d}" for i in range(80)]
    _pin(tmp_path, monkeypatch, tags=tags)
    resp = Rems_Dl.app.test_client().post("/api/tags/nekosapi", headers=HEADERS, json={"query": "tag"})
    assert len(resp.get_json()) == 50
