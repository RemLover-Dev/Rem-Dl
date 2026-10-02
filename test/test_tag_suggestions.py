import pytest
from unittest.mock import MagicMock

HEADERS = {"User-Agent": "RemsDlDesktopApp/1.0"}


@pytest.fixture
def client():
    import Rems_Dl
    Rems_Dl.app.config["TESTING"] = True
    with Rems_Dl.app.test_client() as c:
        yield c


class TestEshuushuuSuggestions:
    def test_short_query_returns_empty(self, client):
        resp = client.post("/api/tags/eshuushuu", json={"query": "a"}, headers=HEADERS)
        assert resp.status_code == 200
        assert resp.get_json() == []

        resp = client.post("/api/tags/eshuushuu", json={"query": ""}, headers=HEADERS)
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_valid_query_hits_mapping(self, client, monkeypatch):
        import Rems_Dl

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "hits": [
                {"title": "Rem", "type": 1},
                {"name": "Ram", "type": 1},
                {"title": "maid dress", "type": 0},
                {"invalid": "entry"},
            ]
        }
        fake_session = MagicMock()
        fake_session.get.return_value = fake_resp

        monkeypatch.setattr(Rems_Dl, "get_session", lambda *a, **k: fake_session)

        resp = client.post("/api/tags/eshuushuu", json={"query": "rem"}, headers=HEADERS)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 3
        assert data[0] == {"title": "Rem", "type": 1}
        assert data[1] == {"title": "Ram", "type": 1}
        assert data[2] == {"title": "maid dress", "type": 0}

    def test_network_failure_returns_empty_safely(self, client, monkeypatch):
        import Rems_Dl

        fake_session = MagicMock()
        fake_session.get.side_effect = Exception("Connection timeout")
        monkeypatch.setattr(Rems_Dl, "get_session", lambda *a, **k: fake_session)

        resp = client.post("/api/tags/eshuushuu", json={"query": "rem"}, headers=HEADERS)
        assert resp.status_code == 200
        assert resp.get_json() == []


class TestNekosapiSuggestions:
    def test_short_query_returns_empty(self, client):
        resp = client.post("/api/tags/nekosapi", json={"query": "n"}, headers=HEADERS)
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_query_sanitization_and_strip(self, client, monkeypatch):
        import Rems_Dl

        monkeypatch.setattr(
            Rems_Dl,
            "_refresh_nekosapi_live_tags",
            lambda net_config: ["catgirl", "neko", "maid", "foxgirl"],
        )

        # Leading/trailing whitespace should be stripped cleanly
        resp = client.post("/api/tags/nekosapi", json={"query": "  cat  "}, headers=HEADERS)
        assert resp.status_code == 200
        assert resp.get_json() == ["catgirl"]

    def test_prefix_matching(self, client, monkeypatch):
        import Rems_Dl

        monkeypatch.setattr(
            Rems_Dl,
            "_refresh_nekosapi_live_tags",
            lambda net_config: ["neko_ears", "neko_tail", "nekomimi", "kitsune", "rem"],
        )

        resp = client.post("/api/tags/nekosapi", json={"query": "neko"}, headers=HEADERS)
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 3
        assert "neko_ears" in data
        assert "neko_tail" in data
        assert "nekomimi" in data

    def test_live_tags_harvesting_safeguard(self, monkeypatch, tmp_path):
        import Rems_Dl

        # Ensure live tags file path is in temporary directory
        monkeypatch.setattr(Rems_Dl, "DATABASE_DIR", str(tmp_path))
        monkeypatch.setattr(Rems_Dl, "_nekosapi_live_cache", {"tags": [], "at": 0.0})

        fake_session = MagicMock()
        # Simulate network error on images endpoint
        fake_session.get.side_effect = Exception("Network unreachable")
        monkeypatch.setattr(Rems_Dl, "get_session", lambda *a, **k: fake_session)

        tags = Rems_Dl._refresh_nekosapi_live_tags({})
        assert isinstance(tags, list)
        assert tags == []


class TestNekosiaSuggestions:
    def test_short_query_returns_empty(self, client):
        resp = client.post("/api/tags/nekosia", json={"query": "k"}, headers=HEADERS)
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_prefix_matching_from_cache(self, client, monkeypatch):
        import Rems_Dl
        import time

        monkeypatch.setattr(
            Rems_Dl,
            "_nekosia_tags_cache",
            {"tags": ["kitsune", "kemono", "neko", "blue_hair"], "at": time.time()},
        )

        resp = client.post("/api/tags/nekosia", json={"query": "k"}, headers=HEADERS)
        # 1 character query returns empty because threshold is < 2
        assert resp.status_code == 200
        assert resp.get_json() == []

        resp = client.post("/api/tags/nekosia", json={"query": "ki"}, headers=HEADERS)
        assert resp.status_code == 200
        assert resp.get_json() == ["kitsune"]

    def test_live_network_fetch_and_cache(self, client, monkeypatch):
        import Rems_Dl

        monkeypatch.setattr(Rems_Dl, "_nekosia_tags_cache", {"tags": [], "at": 0.0})

        fake_resp = MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {"tags": ["kitsune", "kemonomimi", "neko"]}
        fake_session = MagicMock()
        fake_session.get.return_value = fake_resp

        monkeypatch.setattr(Rems_Dl, "get_session", lambda *a, **k: fake_session)

        resp = client.post("/api/tags/nekosia", json={"query": "kem"}, headers=HEADERS)
        assert resp.status_code == 200
        assert resp.get_json() == ["kemonomimi"]
        # Verify cached
        assert "kemonomimi" in Rems_Dl._nekosia_tags_cache["tags"]
