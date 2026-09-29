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
