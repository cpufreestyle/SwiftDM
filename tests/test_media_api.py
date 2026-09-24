import pytest

import app as appmod
from media_service import media_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    media_registry._tabs.clear()
    appmod.BROWSER_CAPTURE_ENABLED = True
    yield
    appmod.BROWSER_CAPTURE_ENABLED = True
    media_registry._tabs.clear()


@pytest.fixture()
def client():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def _item(url, kind="hls", **kw):
    d = {"url": url, "kind": kind}
    d.update(kw)
    return d


def test_discover_accepts_items_and_reports_count(client):
    resp = client.post("/api/media/discover", json={
        "tab_id": "7", "page_url": "https://site/v", "title": "视频页",
        "items": [_item("https://c/a.m3u8", quality_hint="1080P", bytes=2048)],
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"ok": True, "added": 1, "count": 1}
    assert resp.headers["Access-Control-Allow-Origin"] == "*"


def test_discover_requires_tab_id(client):
    resp = client.post("/api/media/discover", json={"page_url": "p", "items": []})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_discover_rejects_non_list_items(client):
    resp = client.post("/api/media/discover", json={"tab_id": "1", "items": "oops"})
    assert resp.status_code == 400


def test_discover_short_circuits_when_monitoring_disabled(client):
    appmod.BROWSER_CAPTURE_ENABLED = False
    resp = client.post("/api/media/discover", json={"tab_id": "1", "items": [_item("https://c/a.m3u8")]})
    assert resp.get_json() == {"ok": False, "reason": "disabled"}
    assert media_registry.count() == 0


def test_list_returns_items_for_tab(client):
    client.post("/api/media/discover", json={
        "tab_id": "9", "page_url": "https://site/v", "title": "T",
        "items": [_item("https://c/a.m3u8"), _item("https://c/b.mpd", kind="dash")],
    })
    resp = client.get("/api/media/list?tabId=9")
    body = resp.get_json()
    assert body["ok"] is True
    assert body["page_url"] == "https://site/v"
    assert [i["kind"] for i in body["items"]] == ["hls", "dash"]


def test_list_unknown_tab_is_empty_ok(client):
    resp = client.get("/api/media/list?tabId=404")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "tab_id": "404", "page_url": "", "title": "", "items": []}


def test_list_requires_tab_id(client):
    assert client.get("/api/media/list").status_code == 400


def test_options_preflight_allowed(client):
    resp = client.open("/api/media/discover", method="OPTIONS")
    assert resp.status_code == 200
    assert "POST" in resp.headers["Access-Control-Allow-Methods"]
