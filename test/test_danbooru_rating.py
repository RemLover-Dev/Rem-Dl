import pytest

from workers.danbooru import DanbooruWorker


NET_CONFIG = {
    "anti_ban_pause": 0.0,
    "download_retries": 1,
    "use_proxy": False,
    "api_timeout": 5,
}


@pytest.fixture
def master(tmp_path, monkeypatch):
    monkeypatch.setattr("core.shared.MASTER_FOLDER", str(tmp_path))
    return tmp_path


def test_no_rating_filter(master):
    w = DanbooruWorker("rem", 10, "", [], NET_CONFIG)
    assert w.api_tag == "rem"
    assert w.rating_display == ""
    assert w.rating_allowed == set()


def test_single_rating_uses_positive_tag(master):
    w = DanbooruWorker("rem", 10, "rating:q", [], NET_CONFIG)
    assert w.api_tag == "rem rating:q"
    assert w.rating_display == "Questionable"
    assert w.rating_allowed == {"q"}


def test_multi_rating_joins_comma(master):
    # danbooru ORs comma lists in the rating metatag (verified live)
    w = DanbooruWorker("rem", 10, "rating:g rating:q", [], NET_CONFIG)
    assert w.api_tag == "rem rating:g,q"
    assert w.rating_display == "Safe, Questionable"
    assert w.rating_allowed == {"g", "q"}


def test_three_ratings_stay_positive(master):
    w = DanbooruWorker("rem", 10, "rating:g rating:s rating:q", [], NET_CONFIG)
    assert w.api_tag == "rem rating:g,s,q"
    assert w.rating_display == "Safe, Sensitive, Questionable"
    assert w.rating_allowed == {"g", "s", "q"}


def test_all_ratings_means_no_filter(master):
    w = DanbooruWorker("rem", 10, "rating:g rating:s rating:q rating:e", [], NET_CONFIG)
    assert w.api_tag == "rem"
    assert w.rating_allowed == {"g", "s", "q", "e"}


def test_unknown_rating_words_ignored(master):
    w = DanbooruWorker("rem", 10, "rating:bogus rating:g", [], NET_CONFIG)
    assert w.api_tag == "rem rating:g"
    assert w.rating_allowed == {"g"}
