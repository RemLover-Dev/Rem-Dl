from core import tag_excludes


def test_apply_appends_negatives():
    tag_excludes.save("gelbooru", ["guro", "1boy"])
    assert tag_excludes.apply("gelbooru", "rem") == "rem -guro -1boy"


def test_apply_skips_tags_already_negated():
    tag_excludes.save("dan", ["guro"])
    assert tag_excludes.apply("dan", "rem -guro") == "rem -guro"


def test_dan_rejects_more_than_two():
    tags, err = tag_excludes.save("dan", ["a", "b", "c"])
    assert tags is None
    assert "at most 2" in err
    tag_excludes.save("dan", ["a", "b"])
    assert tag_excludes.apply("dan", "rem") == "rem -a -b"


def test_unsupported_site_is_rejected_and_ignored():
    tags, err = tag_excludes.save("x", ["a"])
    assert tags is None and err
    assert tag_excludes.apply("x", "rem") == "rem"


def test_zero_keeps_case_and_uses_commas():
    tag_excludes.save("zero", ["Ines (Arknights)", "solo"])
    assert tag_excludes.get("zero") == ["Ines (Arknights)", "solo"]
    assert (tag_excludes.apply("zero", "Kafka (Honkai: Star Rail)")
            == "Kafka (Honkai: Star Rail),-Ines (Arknights),-solo")


def test_space_tag_sites_keep_one_tag():
    tag_excludes.save("anime_dl", ["kafka (honkai: star rail)"])
    assert tag_excludes.get("anime_dl") == ["kafka (honkai: star rail)"]
    assert (tag_excludes.apply("anime_dl", "hutao")
            == "hutao -kafka (honkai: star rail)")


def test_nekosapi_uses_commas():
    tag_excludes.save("nekosapi", ["solo"])
    assert tag_excludes.apply("nekosapi", "kemonomimi") == "kemonomimi,-solo"


def test_all_returns_cleaned_tags_for_every_site():
    tag_excludes.save("gelbooru", ["Guro"])
    tag_excludes.save("zero", ["Ines (Arknights)"])
    every = tag_excludes.all()
    assert every["gelbooru"] == ["guro"]
    assert every["zero"] == ["Ines (Arknights)"]
    assert set(every) == set(tag_excludes.SITES)


def test_strip_removes_positive_excludes_and_keeps_negatives():
    tag_excludes.save("gelbooru", ["solo", "1boy"])
    assert tag_excludes.strip("gelbooru", "rem solo") == "rem"
    assert tag_excludes.strip("gelbooru", "solo") == ""
    assert tag_excludes.strip("gelbooru", "rem -solo") == "rem -solo"


def test_strip_handles_comma_sites():
    tag_excludes.save("zero", ["Ines (Arknights)"])
    assert (tag_excludes.strip("zero", "Kafka (Honkai: Star Rail),Ines (Arknights)")
            == "Kafka (Honkai: Star Rail)")


def test_dispatch_worker_applies_excludes(monkeypatch):
    import Rems_Dl

    captured = {}
    monkeypatch.setattr(Rems_Dl, "worker_danbooru",
                        lambda tag, *a, **k: captured.update(tag=tag))
    monkeypatch.setattr(Rems_Dl.DatabaseManager, "add_tag_history", lambda *a, **k: None)
    monkeypatch.setattr(Rems_Dl.DatabaseManager, "add_learned_tag", lambda *a, **k: None)
    tag_excludes.save("dan", ["guro"])

    Rems_Dl._dispatch_worker({"worker": "dan", "tag": "rem", "limit": "1"})

    assert captured["tag"] == "rem -guro"


def test_dispatch_worker_passes_space_tag_excludes_separately(monkeypatch):
    import Rems_Dl

    captured = {}
    monkeypatch.setattr(Rems_Dl, "worker_anime_dl",
                        lambda tag, amount, net, ex: captured.update(tag=tag, ex=ex))
    monkeypatch.setattr(Rems_Dl.DatabaseManager, "add_tag_history", lambda *a, **k: None)
    monkeypatch.setattr(Rems_Dl.DatabaseManager, "add_learned_tag", lambda *a, **k: None)
    tag_excludes.save("anime_dl", ["kafka (honkai: star rail)"])

    Rems_Dl._dispatch_worker({"worker": "anime_dl", "tag": "hutao", "limit": "1"})

    assert captured["tag"] == "hutao"
    assert captured["ex"] == ["kafka (honkai: star rail)"]
