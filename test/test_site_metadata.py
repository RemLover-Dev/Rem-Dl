"""Site attribution lives in the file's own metadata: every download writes
site:<name>, crosspost site tags become extra site: lines, and the scan
reads them back so a tags directory moved out of its site folder still
attributes the image to the right site."""
import os

import pytest

_ENV_KEYS = [
    "API_TIMEOUT", "RETRY_WAIT", "ANTI_BAN_PAUSE", "DOWNLOAD_RETRIES",
    "USE_PROXY", "PROXY_URL", "VERIFY_TLS",
]
_env_before = {k: os.environ.get(k) for k in _ENV_KEYS}

import Rems_Dl

for _k, _v in _env_before.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

from PIL import Image

from core import shared

H = {"User-Agent": "RemsDlDesktopApp/1.0"}


@pytest.fixture
def lib(tmp_path, monkeypatch):
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    monkeypatch.setattr(shared, "_gallery_cache",
                        {"data": {"images": []}, "filenames": set(), "by_fn": {}})
    return tmp_path


@pytest.fixture
def img(tmp_path):
    p = tmp_path / "x.jpg"
    Image.new("RGB", (16, 16)).save(str(p))
    return str(p)


def test_write_and_parse_multiple_sites(img):
    shared.write_image_metadata(img, ["1girl"], [], "danbooru", extra_sites=["Twitter", "pixiv"])
    sites, tags = Rems_Dl._tags_from_file(img)
    assert sites == ["danbooru", "Twitter", "pixiv"]
    assert tags["tag"] == ["1girl"]


def test_rewrites_keep_extra_sites_only_when_forwarded(img):
    shared.write_image_metadata(img, ["1girl"], [], "danbooru", extra_sites=["Twitter"])
    shared.write_image_metadata(img, ["1girl", "2girls"], [], "danbooru")
    sites, tags = Rems_Dl._tags_from_file(img)
    assert sites == ["danbooru"] and tags["tag"] == ["1girl", "2girls"]
    shared.write_image_metadata(img, ["1girl"], [], "danbooru", extra_sites=["Twitter"])
    assert Rems_Dl._tags_from_file(img)[0] == ["danbooru", "Twitter"]


def test_extract_site_tags():
    assert shared.extract_site_tags(["twitter", "1girl", "Pixiv"], ["fanbox"], "danbooru") \
        == ["twitter", "Pixiv", "fanbox"]
    assert shared.extract_site_tags(["twitter"], None, "twitter") == []
    assert shared.extract_site_tags([], []) == []


def test_bounded_reader_matches_full_reader(img, tmp_path):
    big = tmp_path / "big.jpg"
    noise = os.urandom(400_000)  # larger than the read window
    Image.new("RGB", (300, 300)).save(str(big))
    with open(big, "r+b") as f:
        f.seek(2)
        f.write(noise[:200])  # junk between SOI and the rest
    for p, ext in ((img, "jpg"),):
        shared.write_image_metadata(p, ["1girl"], [], "danbooru", extra_sites=["Twitter"])
        full = shared.read_image_metadata(p)
        assert shared.read_image_metadata_bounded(p) == full
    # tail-appended container: webp
    wp = tmp_path / "w.webp"
    Image.new("RGB", (64, 64)).save(str(wp), "WEBP")
    shared.write_image_metadata(str(wp), ["1girl"], [], "gelbooru", extra_sites=["twitter"])
    assert shared.read_image_metadata_bounded(str(wp)) == shared.read_image_metadata(str(wp))
    # png and gif head formats
    for name, fmt in (("p.png", "PNG"), ("g.gif", "GIF")):
        q = tmp_path / name
        Image.new("RGB", (32, 32)).save(str(q), fmt)
        shared.write_image_metadata(str(q), ["1girl"], [], "danbooru")
        assert shared.read_image_metadata_bounded(str(q)) == shared.read_image_metadata(str(q))
    # no metadata -> None either way
    bare = tmp_path / "bare.png"
    Image.new("RGB", (8, 8)).save(str(bare), "PNG")
    assert shared.read_image_metadata_bounded(str(bare)) is None


def test_scan_attributes_from_embedded_site(lib):
    d = lib / "Elizabeth" / "Images"
    d.mkdir(parents=True)
    p = d / "moved.jpg"
    Image.new("RGB", (16, 16)).save(str(p))
    shared.write_image_metadata(str(p), ["1girl"], [], "danbooru")
    gallery, added, _fixed, _checked = Rems_Dl._scan_and_merge_gallery()
    entry = next(i for i in gallery["images"] if i["filename"] == "moved.jpg")
    assert added == 1 and entry["site"] == "danbooru"


def test_scan_without_embedded_site_keeps_folder_name(lib):
    d = lib / "someartist_(x)"
    d.mkdir(parents=True)
    (d / "bare.jpg").write_bytes(b"\xff\xd8\xff\xd9")
    gallery, added, _fixed, _checked = Rems_Dl._scan_and_merge_gallery()
    entry = next(i for i in gallery["images"] if i["filename"] == "bare.jpg")
    assert added == 1 and entry["site"] == "someartist_(x)"


def test_embedded_site_endpoint(lib):
    d = lib / "Elizabeth" / "Images"
    d.mkdir(parents=True)
    p = d / "moved.jpg"
    Image.new("RGB", (16, 16)).save(str(p))
    shared.write_image_metadata(str(p), ["1girl"], [], "danbooru")
    with Rems_Dl.app.test_client() as c:
        r = c.get("/api/gallery/embedded_site?filepath=Elizabeth/Images/moved.jpg", headers=H)
        assert r.status_code == 200 and r.get_json()["site"] == "danbooru"
        for bad in ("../../etc/passwd", "Elizabeth/Images/gone.jpg"):
            r = c.get("/api/gallery/embedded_site?filepath=" + bad, headers=H)
            assert r.status_code == 200 and r.get_json()["site"] == ""


def test_scan_bare_row_heals_site(lib, monkeypatch):
    d = lib / "Elizabeth" / "Images"
    d.mkdir(parents=True)
    p = d / "moved.jpg"
    Image.new("RGB", (16, 16)).save(str(p))
    shared.write_image_metadata(str(p), ["1girl"], [], "danbooru")
    monkeypatch.setattr(shared, "load_gallery", lambda: {"images": [{
        "id": "x", "filename": "moved.jpg", "filepath": "Elizabeth/Images/moved.jpg",
        "site": "Elizabeth", "tags": {}, "favourite": False}]})
    gallery, _added, fixed, _checked = Rems_Dl._scan_and_merge_gallery()
    row = next(i for i in gallery["images"] if i["filename"] == "moved.jpg")
    assert fixed >= 1
    assert row["site"] == "danbooru"
    assert {k: v for k, v in row["tags"].items() if v} == {"tag": ["1girl"]}
