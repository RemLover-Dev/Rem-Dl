import os
import tempfile

import pytest

# Rems_Dl's load_dotenv() pollutes os.environ, which SettingsManager reads for
# its defaults — snapshot and restore so test_settings stays isolated.
_ENV_KEYS = [
    "API_TIMEOUT", "RETRY_WAIT", "ANTI_BAN_PAUSE", "DOWNLOAD_RETRIES",
    "USE_PROXY", "PROXY_URL", "VERIFY_TLS",
]
_env_before = {k: os.environ.get(k) for k in _ENV_KEYS}

import Rems_Dl  # noqa: E402

for _k, _v in _env_before.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v

from core import shared  # noqa: E402

H = {"User-Agent": "RemsDlDesktopApp/1.0"}


@pytest.fixture
def client():
    Rems_Dl.app.config["TESTING"] = True
    with Rems_Dl.app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def restore_master_folder():
    orig = Rems_Dl.MASTER_FOLDER
    yield
    Rems_Dl.MASTER_FOLDER = orig
    shared.MASTER_FOLDER = orig
    Rems_Dl._invalidate_fp_cache()


def test_folder_rejects_empty(client):
    assert client.post("/api/folder", json={"folder": ""}, headers=H).status_code == 400


def test_folder_rejects_relative(client):
    assert client.post("/api/folder", json={"folder": "some/dir"}, headers=H).status_code == 400


def test_folder_rejects_nonexistent(client):
    path = os.path.join(os.path.expanduser("~"), "definitely-not-a-real-dir-xyz")
    assert client.post("/api/folder", json={"folder": path}, headers=H).status_code == 400


def test_folder_rejects_outside_home(client):
    assert client.post("/api/folder", json={"folder": "/etc"}, headers=H).status_code == 400


def test_folder_rejects_traversal_outside_home(client):
    path = os.path.dirname(os.path.expanduser("~"))
    assert client.post("/api/folder", json={"folder": path}, headers=H).status_code == 400


def test_folder_accepts_existing_home_subdir(client):
    home = os.path.expanduser("~")
    with tempfile.TemporaryDirectory(dir=home) as d:
        r = client.post("/api/folder", json={"folder": d}, headers=H)
        assert r.status_code == 200
        assert r.get_json()["folder"] == os.path.realpath(d)
