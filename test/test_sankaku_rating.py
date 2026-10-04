import pytest

from workers.sankaku import SankakuWorker


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
    w = SankakuWorker("rem", 10, "", [], NET_CONFIG)
    assert w.api_tag == "rem"
    assert w.rating_display == ""
    assert w.rating_allowed == set()


def test_single_rating_stays_server_side(master):
    w = SankakuWorker("rem", 10, "rating:q", [], NET_CONFIG)
    assert w.api_tag == "rem rating:q"
    assert w.rating_display == "Questionable"
    assert w.rating_allowed == {"q"}


def test_multi_rating_filters_locally(master):
    # sankaku has no comma-OR in rating (verified live: 'rating:s,rating:e' is
    # ignored, 'rating:s rating:e' = last wins) — the query stays clean and
    # the rating set filters posts locally
    w = SankakuWorker("rem", 10, "rating:s rating:e", [], NET_CONFIG)
    assert w.api_tag == "rem"
    assert w.rating_display == "Safe, NSFW"
    assert w.rating_allowed == {"s", "e"}


def test_all_ratings_means_no_filter(master):
    # the dropdown collapses a full set to All; worker treats it the same
    w = SankakuWorker("rem", 10, "rating:s rating:q rating:e", [], NET_CONFIG)
    assert w.api_tag == "rem"
    assert w.rating_display == ""
    assert w.rating_allowed == set()


def test_unknown_rating_is_ignored(master):
    w = SankakuWorker("rem", 10, "rating:banana", [], NET_CONFIG)
    assert w.api_tag == "rem"
    assert w.rating_allowed == set()
