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


def test_categorize_tags_uses_cache(master):
    # API post tag_string is flat; categories come from the /api/tags
    # type cache (calibrated live: 0=general 1=artist 3=copyright
    # 4=character 5=meta — same ints as core.shared.TAG_TYPE_MAP)
    w = GsbooruWorker("rem", 10, "", [], NET_CONFIG)
    w.tag_cache = {
        "zaint_(zaintz4)": "artist",
        "zani_(wuthering_waves)": "character",
        "wuthering_waves": "copyright",
        "highres": "metadata",
    }
    general, artists, characters, copyrights, metadata_tags = w._categorize_tags(
        ["1girl", "zaint_(zaintz4)", "zani_(wuthering_waves)",
         "wuthering_waves", "highres"]
    )
    assert general == ["1girl"]
    assert artists == ["zaint_(zaintz4)"]
    assert characters == ["zani_(wuthering_waves)"]
    assert copyrights == ["wuthering_waves"]
    assert metadata_tags == ["highres"]
