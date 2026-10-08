import Rems_Dl


def _merge(monkeypatch, learned, online, query="zani"):
    monkeypatch.setattr(
        Rems_Dl.DatabaseManager,
        "get_learned_suggestions",
        lambda site, q, limit=50: list(learned),
    )
    return Rems_Dl._merge_learned_and_online("yande", query, online)


def test_online_popularity_order_survives_learned(monkeypatch):
    # yande/konachan hand back order=count (258, 3, 1) — a learned tag that
    # also exists online must not jump above the more popular one
    out = _merge(monkeypatch, ["zani_journey", "zani"], ["zani", "zani_journey", "zania"])
    assert out == ["zani", "zani_journey", "zania"]


def test_learned_only_tags_pin_ahead(monkeypatch):
    out = _merge(monkeypatch, ["zani_custom", "zani"], ["zani", "zania"])
    assert out == ["zani_custom", "zani", "zania"]


def test_online_is_deduped_case_insensitively(monkeypatch):
    out = _merge(monkeypatch, [], ["Zani", "zani", "Zania"])
    assert out == ["Zani", "Zania"]


def test_learned_is_deduped_case_insensitively(monkeypatch):
    out = _merge(monkeypatch, ["zani", "Zani"], [])
    assert out == ["zani"]


def test_dict_online_entries_keep_name(monkeypatch):
    out = _merge(monkeypatch, [], [{"name": "zani"}])
    assert out == [{"name": "zani"}]


def test_learned_suggestions_match_space_typed_query(monkeypatch):
    # "fire k" typed with a space must still hit learned "fire_keeper"
    monkeypatch.setattr(
        Rems_Dl.DatabaseManager,
        "load_learned_tags",
        lambda: {"kona": ["fire_keeper", "fire_girl", "cat"]},
    )
    assert Rems_Dl.DatabaseManager.get_learned_suggestions("kona", "fire k") == ["fire_keeper"]
    # ...and the reverse: underscored query hits a space-stored learned tag
    monkeypatch.setattr(
        Rems_Dl.DatabaseManager,
        "load_learned_tags",
        lambda: {"kona": ["fire keeper"]},
    )
    assert Rems_Dl.DatabaseManager.get_learned_suggestions("kona", "fire_k") == ["fire keeper"]


def test_kona_suggest_query_normalizes_spaces(monkeypatch):
    # typed "fire k" must reach konachan as name=fire_k* — spaces return nothing
    seen = {}
    monkeypatch.setattr(
        Rems_Dl, "_live_tag_suggest",
        lambda session, url, timeout=5: (seen.update(url=url) or ["fire_keeper"]),
    )
    monkeypatch.setattr(
        Rems_Dl.DatabaseManager, "get_learned_suggestions",
        lambda site, q, limit=50: [],
    )
    Rems_Dl.app.config["TESTING"] = True
    H = {"User-Agent": "RemsDlDesktopApp/1.0"}
    with Rems_Dl.app.test_client() as client:
        r = client.post("/api/tags/kona", json={"query": "fire k"}, headers=H)
    assert r.status_code == 200
    assert "fire_k" in seen.get("url", "")
    assert r.get_json() == ["fire_keeper"]
