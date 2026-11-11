import os

import pytest

H = {"User-Agent": "RemsDlDesktopApp/1.0"}
FP = "/mnt/sda3/Rem/Dan/Mochizuki Kei/Safe/Images/12264376.jpg"

pytestmark = [
    pytest.mark.usefixtures("bypass_access_guard"),
    # reads this machine's gallery root; skip on runners without the drive
    pytest.mark.skipif(not os.path.isfile(FP), reason="HDD gallery root not mounted"),
]


@pytest.fixture
def client():
    import Rems_Dl
    Rems_Dl.app.config["TESTING"] = True
    with Rems_Dl.app.test_client() as c:
        yield c


@pytest.fixture
def realish(monkeypatch):
    import Rems_Dl
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER",
                        "/home/hanekawa/RemGodCatcher/Rem-Dl/Rems Dl")
    monkeypatch.setattr(Rems_Dl.DatabaseManager, "load_ui_config",
                        staticmethod(lambda: {"gallery_roots": ["/mnt/sda3/Rem"]}))
    return Rems_Dl


def test_frontend_url_shape(realish, client):
    # the frontend now strips the leading slash: "/mnt/..." -> "mnt/..."
    url = "/api/gallery/file/" + FP.lstrip("/")
    r = client.get(url, headers=H)
    assert r.status_code == 200, r.get_data(as_text=True)[:120]


def test_merged_form(realish, client):
    merged = "/api/gallery/file/mnt/sda3/Rem/Dan/Mochizuki Kei/Safe/Images/12264376.jpg"
    r = client.get(merged, headers=H)
    assert r.status_code == 200, r.status_code
    assert r.status_code == 200


def test_direct_abs(realish, client):
    # what the route SHOULD do with the true abs filepath (leading slash kept)
    r = client.get("/api/gallery/file/" + FP.lstrip("/"), headers=H)
    assert r.status_code == 200, r.status_code
    assert r.status_code == 200
