import json
import zipfile
import pytest

from core.extensions import ExtensionManager
from core.extension_downloader import _safe_extract_zip, SecurityError, install_from_local_path
from Rems_Dl import app


@pytest.fixture
def temp_ext_dir(tmp_path, monkeypatch):
    """Fixture to isolate extension directory during tests."""
    ext_dir = tmp_path / "extensions"
    cfg_file = tmp_path / "extensions.json"
    ext_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("core.extensions.EXTENSIONS_DIR", str(ext_dir))
    monkeypatch.setattr("core.extensions.EXTENSIONS_CONFIG_FILE", str(cfg_file))
    monkeypatch.setattr("core.extension_downloader.EXTENSIONS_DIR", str(ext_dir))

    manager = ExtensionManager()
    monkeypatch.setattr("core.extensions._extension_manager", manager)
    return ext_dir, manager


def test_extension_manager_catalog(temp_ext_dir):
    _, manager = temp_ext_dir
    catalog = manager.get_catalog()
    assert isinstance(catalog, list)
    assert len(catalog) >= 1

    watcher_entry = next((c for c in catalog if c["id"] == "rems-watcher"), None)
    assert watcher_entry is not None
    assert watcher_entry["name"] == "Rems Watcher"
    assert watcher_entry["installed"] is False


def test_zip_slip_security_prevention(tmp_path):
    """Verify that malicious archives attempting directory traversal are blocked."""
    evil_zip = tmp_path / "evil.zip"
    extract_target = tmp_path / "target"
    extract_target.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(str(evil_zip), "w") as zf:
        zf.writestr("../../evil.txt", "malicious payload")

    with pytest.raises(SecurityError, match="Malicious archive member detected"):
        _safe_extract_zip(str(evil_zip), str(extract_target))


def test_install_from_local_archive_and_lifecycle(temp_ext_dir, tmp_path):
    ext_dir, manager = temp_ext_dir

    # Create a valid test extension archive
    ext_src = tmp_path / "mock_ext"
    ext_src.mkdir(parents=True, exist_ok=True)

    manifest_data = {
        "id": "mock-addon",
        "name": "Mock Addon",
        "version": "1.2.0",
        "author": "Tester",
        "entry_point": "plugin.py",
        "nav_tab": {
            "id": "mock_tab",
            "label": "Mock Tab"
        }
    }

    with open(ext_src / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest_data, f)

    plugin_code = """
class MockPlugin:
    def __init__(self, app, socketio, context):
        self.context = context
        self.active = False
    def setup(self):
        self.active = True
    def teardown(self):
        self.active = False

def setup(app, socketio, context):
    p = MockPlugin(app, socketio, context)
    p.setup()
    return p
"""
    with open(ext_src / "plugin.py", "w", encoding="utf-8") as f:
        f.write(plugin_code)

    mock_zip = tmp_path / "mock-addon-1.2.0.zip"
    with zipfile.ZipFile(str(mock_zip), "w") as zf:
        zf.writestr("mock-addon-1.2.0/manifest.json", json.dumps(manifest_data))
        zf.writestr("mock-addon-1.2.0/plugin.py", plugin_code)

    # Install the extension
    ok, msg = install_from_local_path(str(mock_zip), "mock-addon")
    assert ok is True

    # Verify discovery
    installed = manager.discover_installed()
    assert len(installed) == 1
    assert installed[0]["id"] == "mock-addon"
    assert installed[0]["version"] == "1.2.0"
    assert installed[0]["enabled"] is True

    # Verify UI contributions
    contrib = manager.get_ui_contributions()
    assert len(contrib["nav_tabs"]) == 1
    assert contrib["nav_tabs"][0]["id"] == "mock_tab"

    # Test disable
    assert manager.disable_extension("mock-addon") is True
    assert manager.is_enabled("mock-addon") is False
    assert "mock-addon" not in manager.active_plugins

    # Test re-enable
    assert manager.enable_extension("mock-addon") is True
    assert manager.is_enabled("mock-addon") is True
    assert "mock-addon" in manager.active_plugins

    # Test uninstall
    assert manager.uninstall_extension("mock-addon") is True
    assert len(manager.discover_installed()) == 0


def test_api_extensions_endpoints():
    client = app.test_client()

    headers = {"User-Agent": "RemsDlDesktopApp/1.0"}
    resp = client.get("/api/extensions", headers=headers)
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["success"] is True
    assert "extensions" in data
    assert "contributions" in data
