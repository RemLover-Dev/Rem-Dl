import pytest

from workers.gsbooru import GsbooruWorker


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
    w = GsbooruWorker("rem", 10, "", [], NET_CONFIG)
    assert w.filter_code == ""
    assert w.filter_words == set()
    assert w.rating_display == ""


def test_single_rating_uses_code(master):
    w = GsbooruWorker("rem", 10, "rating:q", [], NET_CONFIG)
    assert w.filter_code == "q"
    assert w.filter_words == {"questionable"}
    assert w.rating_display == "Questionable"


def test_multi_rating_joins_comma(master):
    # gsbooru ORs comma lists in its rating param (verified live:
    # rating=g,s returns a page mixed g and s)
    w = GsbooruWorker("rem", 10, "rating:g rating:q", [], NET_CONFIG)
    assert w.filter_code == "g,q"
    assert w.filter_words == {"general", "questionable"}
    assert w.rating_display == "Safe, Questionable"


def test_all_three_ratings_means_no_filter(master):
    # the site only has these three (rating=e is empty live), so
    # selecting them all is exactly "All"
    w = GsbooruWorker("rem", 10, "rating:g rating:s rating:q", [], NET_CONFIG)
    assert w.filter_code == ""
    assert w.filter_words == set()
    assert w.rating_display == "Safe, Sensitive, Questionable"


def test_unknown_rating_words_ignored(master):
    w = GsbooruWorker("rem", 10, "rating:bogus rating:g", [], NET_CONFIG)
    assert w.filter_code == "g"
    assert w.filter_words == {"general"}
