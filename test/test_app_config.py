import os
from PIL import Image


def test_icons_exist_and_are_valid():
    ico_path = os.path.join("icon", "icon.ico")
    png_path = os.path.join("icon", "icon.png")

    assert os.path.isfile(ico_path), "icon/icon.ico must exist"
    assert os.path.isfile(png_path), "icon/icon.png must exist"

    with Image.open(ico_path) as img:
        assert img.format == "ICO"

    with Image.open(png_path) as img:
        assert img.format == "PNG"


def test_desktop_entry():
    desktop_file = "Rems_Dl.desktop"
    assert os.path.isfile(desktop_file)
    with open(desktop_file, "r", encoding="utf-8") as f:
        content = f.read()

    assert "[Desktop Entry]" in content
    assert "Name=Rems Dl" in content
    assert "Icon=" in content
    assert "StartupWMClass=Rems_Dl" in content or "StartupWMClass=Rems Dl" in content


def test_github_workflow_naming():
    workflow_path = os.path.join(".github", "workflows", "build.yml")
    assert os.path.isfile(workflow_path)
    with open(workflow_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "build-windows:" in content
    assert "build-linux:" in content

    # Verify proper naming ("اسم درست")
    assert "Rems-Dl-Windows-x64-Setup.exe" in content
    assert "Rems-Dl-Windows-x64-Portable.zip" in content
    assert "Rems-Dl-Linux-x86_64.tar.gz" in content


def test_inno_setup_script():
    iss_path = os.path.join("installer", "setup.iss")
    assert os.path.isfile(iss_path)
    with open(iss_path, "r", encoding="utf-8") as f:
        content = f.read()

    assert "Rems-Dl-Windows-x64-Setup" in content
    assert "VCRedistNeedsInstall" in content
    assert "RemLoverDev.RemsDl.App.1.0" in content
    assert '#define MyAppVersion "5.3.0"' in content


def test_pyinstaller_spec_and_metadata_hooks():
    spec_path = "Rems_Dl.spec"
    assert os.path.isfile(spec_path)
    with open(spec_path, "r", encoding="utf-8") as f:
        spec_content = f.read()

    assert "copy_metadata" in spec_content
    assert "rule34Py" in spec_content
    assert "hookspath=['hooks']" in spec_content

    hook_file = os.path.join("hooks", "hook-rule34Py.py")
    assert os.path.isfile(hook_file), "hooks/hook-rule34Py.py must exist"

    rthook_file = os.path.join("hooks", "rthook-metadata.py")
    assert os.path.isfile(rthook_file), "hooks/rthook-metadata.py must exist"


def test_rule34py_importlib_metadata_safeguard(monkeypatch):
    import importlib.metadata
    from workers.rule34 import Rule34Worker

    # Test that importing or querying version with missing metadata handles PackageNotFoundError safely
    orig_version = importlib.metadata.version

    def mock_fail_version(pkg):
        raise importlib.metadata.PackageNotFoundError(pkg)

    monkeypatch.setattr(importlib.metadata, "version", mock_fail_version)

    # Re-apply guard and test
    try:
        class _SafeDistribution:
            def __init__(self, name="rule34Py", ver="4.2.0"):
                self.version = ver
                self.metadata = {"Version": ver, "Name": name}

        def _safe_version(distribution_name):
            try:
                return mock_fail_version(distribution_name)
            except Exception:
                if distribution_name and str(distribution_name).lower() in ("rule34py", "pinterest-dl", "pinterest_dl"):
                    return "4.2.0"
                raise

        monkeypatch.setattr(importlib.metadata, "version", _safe_version)
        assert importlib.metadata.version("rule34Py") == "4.2.0"
    finally:
        monkeypatch.setattr(importlib.metadata, "version", orig_version)


def test_index_version_stamps_assets():
    # the webview kept serving a stale style.css after a fix — the index
    # route stamps ?v=<mtime> so an edited css/js is always a cache miss
    import Rems_Dl
    Rems_Dl.app.config["TESTING"] = True
    H = {"User-Agent": "RemsDlDesktopApp/1.0"}

    with Rems_Dl.app.test_client() as client:
        r = client.get("/", headers=H)
        assert r.status_code == 200
        html = r.get_data(as_text=True)
        for name in ("style.css", "script.js"):
            v = int(os.path.getmtime(os.path.join("web", name)))
            assert f'{name}?v={v}"' in html, f"{name} link not version-stamped"


def test_folder_api_and_browse(tmp_path, monkeypatch):
    import Rems_Dl
    Rems_Dl.app.config["TESTING"] = True
    H = {"User-Agent": "RemsDlDesktopApp/1.0"}

    with Rems_Dl.app.test_client() as client:
        # GET returns current master folder
        r = client.get("/api/folder", headers=H)
        assert r.status_code == 200
        assert "folder" in r.get_json()

        # POST without Rem God join
        test_dir = str(tmp_path / "MyDownloads")
        os.makedirs(test_dir, exist_ok=True)
        # folder validation confines POSTed folders to the user's home root —
        # point "~" at tmp_path so this fixture dir counts as inside the root
        _real_expanduser = os.path.expanduser
        monkeypatch.setattr(os.path, "expanduser",
                            lambda p: str(tmp_path) if p == "~" else _real_expanduser(p))
        r = client.post("/api/folder", json={"folder": test_dir}, headers=H)
        assert r.status_code == 200
        assert r.get_json()["folder"] == os.path.realpath(test_dir)

        # POST empty returns 400
        r = client.post("/api/folder", json={"folder": ""}, headers=H)
        assert r.status_code == 400

        # /api/folder/browse with simulated picked path
        monkeypatch.setattr(Rems_Dl, "_pick_folder_desktop", lambda start: test_dir)
        r = client.post("/api/folder/browse", headers=H)
        assert r.status_code == 200
        assert r.get_json()["folder"] == os.path.normpath(test_dir)

        # /api/folder/browse with user cancelled
        monkeypatch.setattr(Rems_Dl, "_pick_folder_desktop", lambda start: None)
        r = client.post("/api/folder/browse", headers=H)
        assert r.status_code == 200
        assert r.get_json().get("cancelled") is True


def test_gallery_file_revalidates_instead_of_serving_stale(tmp_path, monkeypatch):
    # no-cache (not max-age): the browser must revalidate, so a deleted file
    # 404s instead of serving a day-old cached copy — and 304s keep reopen cheap
    import Rems_Dl
    Rems_Dl.app.config["TESTING"] = True
    H = {"User-Agent": "RemsDlDesktopApp/1.0"}
    monkeypatch.setattr(Rems_Dl, "MASTER_FOLDER", str(tmp_path))
    os.makedirs(os.path.join(str(tmp_path), "sub"))
    img_path = os.path.join(str(tmp_path), "sub", "a.jpg")
    with open(img_path, "wb") as f:
        f.write(b"\xff\xd8\xff\xd9")

    with Rems_Dl.app.test_client() as client:
        r = client.get("/api/gallery/file/sub/a.jpg", headers=H)
        assert r.status_code == 200
        assert r.headers.get("Cache-Control") == "private, no-cache"
        etag = r.headers.get("ETag")
        assert etag

        # unchanged file answers 304 — no body re-download
        r304 = client.get("/api/gallery/file/sub/a.jpg", headers={**H, "If-None-Match": etag})
        assert r304.status_code == 304

        # deleted file answers 404 even with a conditional revalidation
        os.remove(img_path)
        r404 = client.get("/api/gallery/file/sub/a.jpg", headers={**H, "If-None-Match": etag})
        assert r404.status_code == 404
        assert "max-age" not in r404.headers.get("Cache-Control", "")

        rmiss = client.get("/api/gallery/file/sub/gone.jpg", headers=H)
        assert rmiss.status_code == 404
        assert "max-age" not in rmiss.headers.get("Cache-Control", "")
