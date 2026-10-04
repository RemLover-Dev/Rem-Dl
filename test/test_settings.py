import os
import pytest
from core.database import SettingsManager
import tempfile
import shutil

class TestSettingsManager:
    @pytest.fixture
    def settings_env(self):
        # Create a temporary directory for tests
        test_dir = tempfile.mkdtemp()
        manager = SettingsManager(test_dir)
        yield manager
        # Clean up
        shutil.rmtree(test_dir)

    def test_default_config_loading(self, settings_env):
        assert settings_env.get("api_timeout") == 10
        assert settings_env.get("retry_wait") == 5
        assert settings_env.get("anti_ban_pause") == 3.0
        assert settings_env.get("download_retries") == 3

    def test_rate_limit_defaults_to_four(self, tmp_path, monkeypatch):
        # each ISP has a different cap — user-editable, default 4 req/s
        monkeypatch.delenv("REQ_RATE_LIMIT", raising=False)
        assert SettingsManager(str(tmp_path)).get("req_rate_limit") == 4

    def test_rate_limit_updates_live_and_persists(self, settings_env, monkeypatch):
        monkeypatch.setenv("REQ_RATE_LIMIT", "4")
        settings_env.update({"req_rate_limit": "7"})
        settings_env.save_config()
        # applied live (no restart) and written to .env for the next boot
        assert os.environ["REQ_RATE_LIMIT"] == "7"
        with open(settings_env._env_path(), encoding="utf-8") as f:
            assert "REQ_RATE_LIMIT=7" in f.read()

    def test_update_config(self, settings_env):
        settings_env.update({"api_timeout": 20})
        assert settings_env.get("api_timeout") == 20
        # Check it didn't update something else
        assert settings_env.get("retry_wait") == 5

    def test_save_and_load_api_settings(self, settings_env):
        api_data = {
            "rule34_api_key": "test_r34_key",
            "gelbooru_api_key": "test_gel_key",
            "pixiv_refresh_token": "test_pixiv_token",
            "gsbooru_api_key": "test_gs_key"
        }
        
        # Save settings
        settings_env.save_api_settings(api_data)
        
        # Load settings and verify
        loaded = settings_env.load_api_settings()
        assert loaded.get("rule34_api_key") == "test_r34_key"
        assert loaded.get("gelbooru_api_key") == "test_gel_key"
        assert loaded.get("pixiv_refresh_token") == "test_pixiv_token"
        assert loaded.get("gsbooru_api_key") == "test_gs_key"

    def test_secrets_persist_in_env_file_and_env_wins(self, settings_env):
        # keys must survive an app restart -> written to .env
        settings_env.save_api_settings({"rule34_api_key": "sekrit",
                                        "konachan_login": "alice",
                                        "pixiv_cookie": "legacy-cookie"})
        with open(settings_env._env_path(), encoding="utf-8") as f:
            content = f.read()
        assert "RULE34_API_KEY=sekrit" in content
        assert "PIXIV_COOKIE=legacy-cookie" in content
        assert "KONACHAN_USERNAME=alice" in content
        # runtime env wins over file values
        os.environ["KONACHAN_USERNAME"] = "envuser"
        try:
            assert settings_env.load_api_settings()["konachan_login"] == "envuser"
        finally:
            del os.environ["KONACHAN_USERNAME"]
