import pytest

from workers.gelbooru import GelbooruWorker


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
    w = GelbooruWorker("rem", 10, "", [], NET_CONFIG)
    assert w.api_tag == "rem"
    assert w.rating_display == ""
    assert w.rating_allowed == set()


def test_single_rating_uses_positive_tag(master):
    w = GelbooruWorker("rem", 10, "rating:q", [], NET_CONFIG)
    assert w.api_tag == "rem rating:questionable"
    assert w.rating_display == "Questionable"
    assert w.rating_allowed == {"questionable"}


def test_multi_rating_negates_complement(master):
    # gelbooru ANDs rating tags, so {g,q} must be sent as "not s, not e"
    w = GelbooruWorker("rem", 10, "rating:g rating:q", [], NET_CONFIG)
    assert w.api_tag == "rem -rating:sensitive -rating:explicit"
    assert w.rating_display == "Safe, Questionable"
    assert w.rating_allowed == {"general", "questionable"}


def test_three_ratings_negate_the_one_left_out(master):
    w = GelbooruWorker("rem", 10, "rating:g rating:s rating:q", [], NET_CONFIG)
    assert w.api_tag == "rem -rating:explicit"
    assert w.rating_display == "Safe, Sensitive, Questionable"


def test_all_ratings_means_no_filter(master):
    w = GelbooruWorker("rem", 10, "rating:g rating:s rating:q rating:e", [], NET_CONFIG)
    assert w.api_tag == "rem"
    assert w.rating_allowed == {"general", "sensitive", "questionable", "explicit"}
