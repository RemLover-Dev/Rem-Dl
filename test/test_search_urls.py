import urllib.parse

from core.search_urls import build_search_url

RESOLVED = {"kuroneko": "823", "touhou": "42"}


def esh_resolver(word, timeout):
    return RESOLVED.get(word, "")


def q(params):
    return urllib.parse.parse_qs(params)


# --- booru family ---
def test_danbooru_single_rating():
    url, warns = build_search_url("dan", "rem blue_hair -maid", "rating:s")
    assert q(url.split("?")[1])["tags"] == ["rem blue_hair -maid rating:s"]
    assert warns == []


def test_danbooru_multi_rating_comma():
    url, _ = build_search_url("dan", "rem", "rating:s rating:q")
    assert q(url.split("?")[1])["tags"] == ["rem rating:s,q"]


def test_danbooru_all_ratings_plain():
    url, warns = build_search_url("dan", "rem", "rating:g rating:s rating:q rating:e")
    assert q(url.split("?")[1])["tags"] == ["rem"]
    assert warns == []


def test_gelbooru_negation_complement():
    url, _ = build_search_url("gelbooru", "rem", "rating:g rating:e")
    assert "tags=" in url
    body = url.split("tags=")[1].replace("%20", " ")
    assert "-rating:sensitive" in body and "-rating:questionable" in body
    assert "page=post&s=list" in url


def test_gelbooru_single_word_rating():
    url, warns = build_search_url("gelbooru", "rem", "rating:q")
    assert "rating:questionable" in url
    assert warns == []


def test_gsbooru_comma_and_e_drop():
    url, warns = build_search_url("gsbooru", "rem", "rating:g rating:s rating:e")
    assert "rating:g,s" in url
    assert len(warns) == 1 and "rating 'e'" in warns[0]


# --- moebooru ---
def test_konachan_single_rating():
    url, warns = build_search_url("kona", "rem", "rating:e")
    assert q(url.split("?")[1])["tags"] == ["rem rating:e"]
    assert warns == []


def test_yande_single_rating():
    url, warns = build_search_url("yande", "rem", "rating:s")
    assert url == "https://yande.re/post?tags=rem+rating:s"
    assert warns == []


def test_moebooru_multi_rating_not_applied():
    for site in ("kona", "yande", "sankaku"):
        url, warns = build_search_url(site, "rem", "rating:s rating:e")
        assert "rating:" not in url
        assert len(warns) == 1 and "single rating" in warns[0]


def test_sankaku_shape():
    url, warns = build_search_url("sankaku", "touhou_(series)", "")
    assert url == "https://sankaku.app/?tags=touhou_(series)"
    assert warns == []


# --- no-rating sites ---
def test_safebooru_url():
    url, warns = build_search_url("safe", "rem", "")
    assert url == "https://safebooru.org/index.php?page=post&s=list&tags=rem"
    assert warns == []


def test_rule34_url():
    url, warns = build_search_url("rule34", "rem 1girl", "")
    assert url == "https://rule34.xxx/index.php?page=post&s=list&tags=rem+1girl"
    assert warns == []


def test_zerocchan_negation_dropped():
    url, warns = build_search_url("zero", "reze (chainsaw man) -outfit", "")
    assert url.startswith("https://www.zerochan.net/reze,")
    assert "-outfit" not in url
    assert len(warns) == 1 and "zerochan" in warns[0]


def test_anime_pictures_negation_dropped():
    url, warns = build_search_url("anime_dl", "tsumugi -cosplay", "")
    assert "search_tag=tsumugi" == url.split("?")[1].split("&")[1]
    assert len(warns) == 1 and "anime-pictures" in warns[0]


def test_rating_ignored_for_safe_rule34():
    assert "rating" not in build_search_url("safe", "rem", "rating:e")[0]
    warns = build_search_url("safe", "rem", "rating:e")[1]
    assert warns and "no rating filter" in warns[0]


# --- pixiv ---
def test_pixiv_plain_tags_search():
    url, warns = build_search_url("pixiv", "rem tsuki", "")
    assert url == "https://www.pixiv.net/en/tags/rem+tsuki"
    assert warns == []


def test_pixiv_search_mode_with_value():
    url, warns = build_search_url("pixiv", "search:rem tsuki", "rating:r18")
    assert url == "https://www.pixiv.net/en/tags/rem+tsuki"
    assert warns and "rating filter" in warns[0]


def test_pixiv_artworks_user_and_bookmarks():
    url, _ = build_search_url("pixiv", "artworks:9886", "")
    assert url == "https://www.pixiv.net/en/users/9886"
    url, _ = build_search_url("pixiv", "bookmark:222", "")
    assert url == "https://www.pixiv.net/en/users/222/bookmarks/artworks"


def test_pixiv_artworks_nondigit_falls_back():
    url, warns = build_search_url("pixiv", "artworks:rem", "")
    assert "/en/tags/rem" in url
    assert any("user id" in w for w in warns)


def test_pixiv_ranking():
    url, warns = build_search_url("pixiv", "ranking:day", "")
    assert url == "https://www.pixiv.net/ranking.php?mode=day"
    assert warns == []


def test_pixiv_negation_dropped_plain_words():
    url, warns = build_search_url("pixiv", "rem -manga", "")
    assert "manga" not in url.replace("en/tags/", "")
    assert len(warns) == 1 and "negation" in warns[0]


# --- other ---
def test_pinterest_and_x():
    assert "https://www.pinterest.com/search/pins/?q=girl+art" == \
        build_search_url("pinterest", "girl art", "")[0]
    assert "https://x.com/search?q=touhou+pixiv" == build_search_url("x", "touhou pixiv", "")[0]


def test_api_only_sites_open_homepage():
    url, warns = build_search_url("nekosapi", "tag", "")
    assert url == "https://nekosapi.com"
    assert warns and "no tag-search page" in warns[0]


def test_eshuushuu_resolved_and_unresolved():
    url, warns = build_search_url("eshuushuu", "kuroneko", "", esh_resolver=esh_resolver)
    assert url == "https://e-shuushuu.net/search?tags=823"
    url, warns = build_search_url("eshuushuu", "kuroneko touhou", "", esh_resolver=esh_resolver)
    assert "tags=823" in url
    assert any("one tag per query" in w for w in warns)
    url, warns = build_search_url("eshuushuu", "nope_404", "", esh_resolver=esh_resolver)
    assert url is None
    assert warns and "could not be resolved" in warns[0]


def test_empty_and_unknown_site():
    url, warns = build_search_url("dan", "", "")
    assert url is None and "no tags" in warns[0]
    url, warns = build_search_url("bogus", "x", "")
    assert url is None and warns
