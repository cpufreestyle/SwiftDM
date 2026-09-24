import json

import pytest

import app as appmod
import downloader
import media
from downloader import DownloadManager, DownloadTask
from media import MediaTask


@pytest.fixture(autouse=True)
def _clean_history(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader.DownloadManager, "_load_history", lambda self: None)
    monkeypatch.setattr(downloader.DownloadManager, "_start_saver", lambda self: None)
    monkeypatch.setattr(downloader.DownloadManager, "save_history", lambda self: None)
    media.set_ytdlp_module(None)
    yield
    media.set_ytdlp_module(None)


def test_auto_routes_m3u8_to_media_task(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://c/v/a.m3u8", str(tmp_path), None, 8)
    assert isinstance(t, MediaTask) and t.kind == "hls"


def test_auto_routes_plain_file_to_download_task(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://c/v/a.zip", str(tmp_path), None, 8)
    assert isinstance(t, DownloadTask)


def test_explicit_video_page_routes_to_media_task(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://site/watch?v=1", str(tmp_path), None, 8, kind="video_page",
                      referer="https://site/", resolution=720)
    assert isinstance(t, MediaTask) and t.kind == "video_page" and t.resolution == 720
    assert t.referer == "https://site/"


def test_kind_http_overrides_media_extension(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://c/v/a.m3u8", str(tmp_path), None, 8, kind="http")
    assert isinstance(t, DownloadTask)


def test_torrent_still_wins_over_media(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://c/t/a.torrent", str(tmp_path), None, 8)
    assert type(t).__name__ == "TorrentTask"


def test_history_roundtrip_keeps_media_task(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://c/v/a.m3u8", str(tmp_path), "a.mp4", 8, kind="hls",
                      referer="https://site/v")
    t.status = "completed"
    d = json.loads(json.dumps(t.to_dict()))      # 必须可 JSON 序列化
    back = m._reconstruct_task(d)
    assert isinstance(back, MediaTask)
    assert back.kind == "hls" and back.referer == "https://site/v"
    assert back.save_dir == str(tmp_path) and back.status == "completed"


def test_reconstruct_tolerates_legacy_records_without_kind(tmp_path):
    m = DownloadManager()
    legacy = {"task_id": "dl_9", "url": "https://c/a.zip", "status": "completed",
              "filename": "a.zip", "filepath": str(tmp_path / "a.zip")}
    back = m._reconstruct_task(legacy)
    assert isinstance(back, DownloadTask) and back.status == "completed"


@pytest.fixture()
def client():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def test_api_add_accepts_media_kind_and_returns_task(client, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(MediaTask, "start", lambda self: started.append(self.task_id))
    monkeypatch.setattr(MediaTask, "preflight", lambda self: None)
    resp = client.post("/api/add", json={"url": "https://c/v/a.m3u8", "kind": "hls",
                                        "save_dir": str(tmp_path), "segments": 12})
    body = resp.get_json()
    assert resp.status_code == 200 and body["success"] is True
    assert body["task"]["kind"] == "hls" and body["task"]["segments"] == 12
    assert started == [body["task"]["task_id"]]


def test_api_add_maps_drm_preflight_to_409(client, monkeypatch):
    monkeypatch.setattr(MediaTask, "preflight",
                        lambda self: (_ for _ in ()).throw(
                            media.DrmProtectedError("该资源受 DRM 保护，SwiftDM 不支持下载")))
    resp = client.post("/api/add", json={"url": "https://c/v/a.m3u8"})
    body = resp.get_json()
    assert resp.status_code == 409 and body["reason"] == "drm_protected"
    assert body["success"] is False


def test_api_add_dropped_task_is_not_left_in_manager(client, monkeypatch):
    monkeypatch.setattr(MediaTask, "preflight",
                        lambda self: (_ for _ in ()).throw(media.NeedsFfmpegError("需要 ffmpeg")))
    before = len(appmod.manager.get_all_tasks())
    client.post("/api/add", json={"url": "https://c/v/a.m3u8"})
    assert len(appmod.manager.get_all_tasks()) == before


def test_api_add_rejects_unknown_kind(client):
    resp = client.post("/api/add", json={"url": "https://c/a.zip", "kind": "flash"})
    assert resp.status_code == 400
    assert resp.get_json()["reason"] == "invalid_kind"


def test_api_add_start_at_defers_start(client, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(MediaTask, "start", lambda self: calls.append("start"))
    monkeypatch.setattr(MediaTask, "preflight", lambda self: None)
    from scheduler import scheduler
    real_schedule = scheduler.schedule
    monkeypatch.setattr(scheduler, "schedule",
                        lambda tid, when: (calls.append(("sched", tid, when)),
                                           real_schedule(tid, when)))
    resp = client.post("/api/add", json={"url": "https://c/v/a.m3u8",
                                        "save_dir": str(tmp_path), "start_at": 2000000000})
    assert resp.status_code == 200
    assert calls and calls[0][0] == "sched" and calls[0][2] == 2000000000
    assert resp.get_json()["task"]["scheduled_at"] == 2000000000
