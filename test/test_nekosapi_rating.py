import os

import pytest

from workers.nekosapi import NekosApiWorker


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
    w = NekosApiWorker("catgirl", 10, "", NET_CONFIG)
    assert w.api_rating == ""
    assert w.rating_display == ""
    assert w.rating_allowed == set()
    assert "rating" not in w._query_params(50, 0)


def test_missing_rating_means_all_ratings(master):
    w = NekosApiWorker("catgirl", 10, None, NET_CONFIG)
    assert w.api_rating == ""
    assert w.rating_allowed == set()
    assert "rating" not in w._query_params(50, 0)


def test_single_rating(master):
    w = NekosApiWorker("catgirl", 10, "safe", NET_CONFIG)
    assert w.api_rating == "safe"
    assert w.rating_display == "Safe"
    assert w.rating_allowed == {"safe"}
    assert w._query_params(50, 0)["rating"] == "safe"


def test_multi_rating_joins_comma(master):
    # nekosapi natively ORs comma lists in the rating param (verified live)
    w = NekosApiWorker("catgirl", 10, "safe explicit", NET_CONFIG)
    assert w.api_rating == "safe,explicit"
    assert w.rating_display == "Safe, NSFW"
    assert w.rating_allowed == {"safe", "explicit"}
    assert w._query_params(50, 0)["rating"] == "safe,explicit"


def test_rating_prefix_accepted(master):
    w = NekosApiWorker("catgirl", 10, "rating:borderline", NET_CONFIG)
    assert w.api_rating == "borderline"
    assert w.rating_display == "Questionable"
    assert w.rating_allowed == {"borderline"}


def test_all_ratings_means_no_server_filter(master):
    w = NekosApiWorker("catgirl", 10, "safe suggestive borderline explicit", NET_CONFIG)
    assert w.api_rating == ""
    assert w.rating_allowed == {"safe", "suggestive", "borderline", "explicit"}
    assert "rating" not in w._query_params(50, 0)


def test_unknown_rating_words_ignored(master):
    w = NekosApiWorker("catgirl", 10, "bogus rating:safe", NET_CONFIG)
    assert w.api_rating == "safe"
    assert w.rating_allowed == {"safe"}


def test_rating_folder_names(master):
    single = NekosApiWorker("catgirl", 10, "safe", NET_CONFIG)
    assert os.path.basename(single.rating_dir) == "Safe"
    multi = NekosApiWorker("catgirl", 10, "safe explicit", NET_CONFIG)
    assert os.path.basename(multi.rating_dir) == "Safe_NSFW"
    all_ratings = NekosApiWorker("catgirl", 10, "", NET_CONFIG)
    assert os.path.basename(all_ratings.rating_dir) == "All Ratings"


def test_tags_and_exclusions_still_query(master):
    w = NekosApiWorker("catgirl,-ai_generated", 10, "explicit", NET_CONFIG)
    params = w._query_params(50, 10)
    assert params == {"limit": 50, "offset": 10, "tags": "catgirl",
                      "without_tags": "ai_generated", "rating": "explicit"}
