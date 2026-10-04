import pytest
from core.database import DatabaseManager
import os
import tempfile
import shutil
import unittest.mock as mock

class TestDatabaseManager:
    @pytest.fixture
    def db_env(self):
        # Override the database path to a temp dir for testing
        test_db_dir = tempfile.mkdtemp()
        
        # Patch the paths in DatabaseManager for testing
        with mock.patch("core.database.TAG_HISTORY_FILE", os.path.join(test_db_dir, "tag_history.json")), \
             mock.patch("core.database.FAV_TAGS_FILE", os.path.join(test_db_dir, "fav_tags.json")), \
             mock.patch("core.database.IMAGE_HISTORY_FILE", os.path.join(test_db_dir, "image_history.json")), \
             mock.patch("core.database.UI_CONFIG_FILE", os.path.join(test_db_dir, "ui_config.json")):
            yield test_db_dir
            
        # Clean up
        shutil.rmtree(test_db_dir)

    def test_ui_config_default(self, db_env):
        config = DatabaseManager.load_ui_config()
        
        assert "wallpapers" in config
        assert "Pixiv" in config["wallpapers"]
        assert "Gsbooru" in config["wallpapers"]
        
        assert config["wallpapers"]["Pixiv"]["dark"] == "Rem_Pixiv_d.jpg"
        assert config["wallpapers"]["Gsbooru"]["dark"] == "Rem_Gsbooru_d.jpg"

    def test_ui_config_drops_clone_orphan_wallpapers(self, db_env):
        import json
        DatabaseManager.load_ui_config()  # seeds the defaults file
        path = os.path.join(db_env, "ui_config.json")
        with open(path) as f:
            data = json.load(f)
        wp = data["wallpapers"]
        wp["waifu"] = dict(wp["Waifu"])   # legacy lowercase clone
        wp["zero"] = dict(wp["Zero"])
        wp["myupload"] = {"dark": "user_a.png", "light": "user_a_l.png"}  # real orphan upload
        with open(path, "w") as f:
            json.dump(data, f)

        out = DatabaseManager.load_ui_config()["wallpapers"]
        assert "waifu" not in out and "zero" not in out
        assert "Waifu" in out and "Zero" in out
        assert out["myupload"] == {"dark": "user_a.png", "light": "user_a_l.png"}

    def test_tag_history_operations(self, db_env):
        DatabaseManager.add_tag_history("zero", "test_tag")
        
        hist = DatabaseManager.load_tag_history()
        assert len(hist) == 1
        assert hist[0]["site"] == "zero"
        assert hist[0]["tag"] == "test_tag"
        assert hist[0]["searched_at"] > 0

        # re-searching the same tag keeps one entry and refreshes it
        DatabaseManager.add_tag_history("zero", "other_tag")
        DatabaseManager.add_tag_history("zero", "test_tag")
        hist = DatabaseManager.load_tag_history()
        assert len(hist) == 2
        assert hist[0]["tag"] == "test_tag"

        DatabaseManager.remove_tag_history("zero", "test_tag")
        hist2 = DatabaseManager.load_tag_history()
        assert len(hist2) == 1
        DatabaseManager.remove_tag_history("zero", "other_tag")
        assert len(DatabaseManager.load_tag_history()) == 0

    def test_tag_history_exclude_ai_flag(self, db_env):
        DatabaseManager.add_tag_history("rule34", "rem")
        assert DatabaseManager.load_tag_history()[0]["exclude_ai"] is False

        DatabaseManager.add_tag_history("rule34", "rem", "", True)
        hist = DatabaseManager.load_tag_history()
        assert len(hist) == 1
        assert hist[0]["exclude_ai"] is True

        DatabaseManager.add_tag_history("rule34", "rem", "", False)
        hist = DatabaseManager.load_tag_history()
        assert len(hist) == 1
        assert hist[0]["exclude_ai"] is False

    def test_favorites_operations(self, db_env):
        # Add to favorites
        DatabaseManager.toggle_favorite("safe", "cute")
        favs = DatabaseManager.load_favorites()
        assert len(favs) == 1
        assert favs[0]["site"] == "safe"
        assert favs[0]["tag"] == "cute"
        
        # Remove from favorites (toggle again)
        DatabaseManager.toggle_favorite("safe", "cute")
        favs2 = DatabaseManager.load_favorites()
        assert len(favs2) == 0

    def test_image_history_keeps_entries_past_100(self, db_env):
        # the old [:100] trim deleted the oldest entries on every download
        seed = [{"site": "zero", "filename": f"old{i}.png", "tags": {}, "downloaded_at": float(i)}
                for i in range(150)]
        DatabaseManager.save_image_history(seed)

        DatabaseManager.add_image_history("zero", "new.png", [], [])

        hist = DatabaseManager.load_image_history()
        assert len(hist) == 151
        assert hist[0]["filename"] == "new.png"
        assert hist[-1]["filename"] == "old149.png"
