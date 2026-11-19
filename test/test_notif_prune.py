import pytest

from core import notifications

pytestmark = pytest.mark.usefixtures("bypass_access_guard")

MD5 = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = str(tmp_path / "notifications.json")
    monkeypatch.setattr(notifications, "NOTIF_FILE", path)
    monkeypatch.setattr(notifications, "LEGACY_PIXIV_FILE", str(tmp_path / "legacy_missing.json"))
    monkeypatch.setattr(notifications, "_migration_done", True)
    return path


def _ingest(source, external_id, url="", thumb="", post_id=None):
    return notifications.ingest(source, [{
        "external_id": str(external_id),
        "metadata": {"url": url, "thumb": thumb,
                     "post_id": str(post_id if post_id is not None else external_id)},
    }])


def test_booru_hash_in_url_matches_served_md5(store):
    _ingest("gelbooru", 111, url=f"https://img3.gelbooru.com/images/aa/bb/{MD5}.jpg")
    n = notifications.drop_downloaded("gelbooru", f"https://img3.gelbooru.com/images/aa/bb/{MD5}.jpg",
                                      f"{MD5}.jpg", MD5)
    assert n == 1
    assert notifications.items("gelbooru") == []
    assert len(notifications.seen_keys("gelbooru")) == 1


def test_booru_hash_in_filename_when_served_bytes_differ(store):
    _ingest("gelbooru", 111, url=f"https://img3.gelbooru.com/images/aa/bb/{MD5}.jpg")
    n = notifications.drop_downloaded("gelbooru", "https://cdn.example/recompressed.jpg",
                                      f"{MD5}.jpg", "f" * 32)
    assert n == 1


def test_unrelated_download_keeps_notification(store):
    _ingest("gelbooru", 111, url=f"https://img3.gelbooru.com/images/aa/bb/{MD5}.jpg")
    n = notifications.drop_downloaded("gelbooru", "https://img3.gelbooru.com/images/cc/dd/" + "a" * 32 + ".jpg",
                                      "a" * 32 + ".jpg", "a" * 32)
    assert n == 0
    assert len(notifications.items("gelbooru")) == 1


def test_pixiv_work_id_in_filename(store):
    _ingest("pixiv", 12345678, thumb="https://i.pximg.net/c/360x360_70/img-master/img/2024/01/02/03/04/05/12345678_p0_square1200.jpg")
    n = notifications.drop_downloaded("pixiv", "https://i.pximg.net/img-original/img/2024/01/02/03/04/05/12345678_p0.jpg",
                                      "12345678_p0.jpg")
    assert n == 1


def test_pixiv_id_must_be_a_whole_token(store):
    _ingest("pixiv", 234, thumb="https://i.pximg.net/c/360x360_70/img-master/img/2024/01/02/03/04/05/234_p0_square1200.jpg")
    n = notifications.drop_downloaded("pixiv", "https://i.pximg.net/img-original/img/2024/01/02/03/04/05/12345678_p0.jpg",
                                      "12345678_p0.jpg")
    assert n == 0


def test_other_source_untouched(store):
    _ingest("gelbooru", 111, url=f"https://img3.gelbooru.com/images/aa/bb/{MD5}.jpg")
    n = notifications.drop_downloaded("danbooru", f"https://img3.gelbooru.com/images/aa/bb/{MD5}.jpg",
                                      f"{MD5}.jpg", MD5)
    assert n == 0
    assert len(notifications.items("gelbooru")) == 1


def test_dropped_key_moves_to_seen(store):
    _ingest("gelbooru", 111, url=f"https://img3.gelbooru.com/images/aa/bb/{MD5}.jpg")
    key = notifications.items("gelbooru")[0]["key"]
    notifications.drop_downloaded("gelbooru", "", f"{MD5}.jpg", MD5)
    assert key in notifications.seen_keys("gelbooru")
    assert notifications.ingest("gelbooru", [{
        "external_id": "111", "metadata": {"url": ""}}]) == []


def test_worker_name_mapping():
    assert notifications.source_for_worker("kona") == "konachan"
    assert notifications.source_for_worker("dan") == "danbooru"
    assert notifications.source_for_worker("safe") == "safebooru"
    assert notifications.source_for_worker("zero") == "zerochan"
    assert notifications.source_for_worker("gelbooru") == "gelbooru"
