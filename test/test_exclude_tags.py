import asyncio

import pytest

from workers.anime_dl import AnimeDlWorker
from workers.eshuushuu import EShuushuuWorker
from workers.gsbooru import GsbooruWorker
from workers.pixiv import PixivWorker
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


def test_gsbooru_dash_tags_stripped_from_query(master):
    # live-verified: gsbooru's tags param has no '-' exclusion
    # ("1girl -bogus_xyz123" -> 0 posts), so dashes never reach the API
    w = GsbooruWorker("solo -smile -gloves", 10, "", [], NET_CONFIG)
    assert w.api_tag == "solo"
    assert w.exclude_tags == ["smile", "gloves"]
    assert w.safe_tag_name == "solo"


def test_gsbooru_exclusion_only_query(master):
    w = GsbooruWorker("-smile", 10, "", [], NET_CONFIG)
    assert w.api_tag == ""
    assert w.exclude_tags == ["smile"]
    assert w.safe_tag_name == "all"


def test_gsbooru_post_filter_matches_tag_string(master):
    w = GsbooruWorker("solo -smile", 10, "", [], NET_CONFIG)
    assert w._excluded(["smile", "solo"]) is True
    assert w._excluded(["solo", "short_hair"]) is False
    assert w._excluded(["solo"]) is False


def test_pixiv_search_dash_tags_stripped(master):
    # pixiv's word param is a plain AND search — dashes are client-side only
    w = PixivWorker("search:smile -tears -blush", 10, "", [], NET_CONFIG)
    assert w.mode == "search"
    assert w.value == "smile"
    assert w.exclude_tags == ["tears", "blush"]


def test_pixiv_process_work_drops_excluded(master):
    w = PixivWorker("search:smile -tears", 10, "", [], NET_CONFIG)
    excluded = {"id": 1, "tags": [{"name": "smile"}, {"name": "tears"}]}
    assert asyncio.run(w._process_work(excluded)) == 0


def test_pixiv_search_without_dashes_untouched(master):
    w = PixivWorker("search:smile", 10, "", [], NET_CONFIG)
    assert w.value == "smile"
    assert w.exclude_tags == []


def test_pixiv_non_search_modes_ignore_dashes(master):
    w = PixivWorker("artworks:12345", 10, "", [], NET_CONFIG)
    assert w.value == "12345"
    assert w.exclude_tags == []


def test_sankaku_dash_tags_reach_query(master):
    # live-verified: sankaku API honors exclusion ("1girl -long_hair" ->
    # 0/18 results with long_hair vs 26/28 plain)
    w = SankakuWorker("solo -smile", 10, "", [], NET_CONFIG)
    assert w._tags_param() == "solo -smile"


def test_sankaku_rating_and_format_still_appended(master):
    w = SankakuWorker("solo -smile", 10, "rating:s", ["-video"], NET_CONFIG)
    assert w._tags_param() == "solo -smile rating:s file_type:image"


def test_eshuushuu_dash_tags_stripped_from_query(master, monkeypatch):
    # site search has no negation — dashed tokens become client-side filters
    monkeypatch.setattr(EShuushuuWorker, "_resolve_many", lambda self, toks: ([], []))
    w = EShuushuuWorker("solo -smile", 10, [], "", NET_CONFIG)
    assert w.tag_names == ["solo"]
    assert w.exclude_names == ["smile"]
    assert w._excluded(["smile"]) is True
    assert w._excluded(["solo"]) is False


def test_eshuushuu_exclusion_normalizes_spaces_and_case(master, monkeypatch):
    monkeypatch.setattr(EShuushuuWorker, "_resolve_many", lambda self, toks: ([], []))
    w = EShuushuuWorker("-long_hair", 10, [], "", NET_CONFIG)
    assert w.exclude_names == ["long_hair"]
    assert w._excluded(["Long Hair"]) is True
    assert w._excluded(["long_hair"]) is True
    assert w._excluded(["short hair"]) is False
    assert w.tag_ids == []


def test_anime_dl_dash_tags_stripped_from_search(master):
    # live-verified: site has no '-' operator ("-x" is just ANDed as "x")
    w = AnimeDlWorker("blue_hair -landscape", 10, NET_CONFIG)
    assert w.tag == "blue_hair"
    assert w.exclude_tags == ["landscape"]
    assert w.tag_slug == "blue_hair"


def test_anime_dl_exclusion_filter_normalizes_underscores(master):
    w = AnimeDlWorker("blue_hair -landscape", 10, NET_CONFIG)
    assert w._excluded(["blue hair", "landscape"]) is True
    assert w._excluded(["landscape"]) is True
    assert w._excluded(["blue_hair", "hatsune_miku"]) is False


def test_anime_dl_exclusion_only_query(master):
    w = AnimeDlWorker("-landscape", 10, NET_CONFIG)
    assert w.tag == ""
    assert w.exclude_tags == ["landscape"]
    assert w.tag_slug == "anime_pictures"
