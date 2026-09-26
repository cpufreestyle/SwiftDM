import json

import pytest

import app as appmod
import downloader
import media
from downloader import DownloadManager, DownloadTask
from media import MediaTask

_REAL_LOAD_HISTORY = downloader.DownloadManager._load_history


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


def _segments_stub_manager():
    """Manager stub: records the thread count, aborts before any real start."""

    class _Stop(Exception):
        pass

    class _StubManager:
        def __init__(self):
            self.calls = []

        def get_all_tasks(self):
            return []

        def create_task(self, url, save_dir, filename, segments, *args, **kwargs):
            self.calls.append((url, save_dir, filename, segments))
            raise _Stop

    return _StubManager(), _Stop


@pytest.mark.parametrize("value,expected", [(3, 3), (16, 16), (99, 32), (0, 8)])
def test_browser_capture_reads_the_segments_setting(tmp_path, monkeypatch, value,
                                                    expected):
    """The browser-capture entry point must follow the "segments" setting.

    This handler used to pass a literal 8, so captures silently ignored the
    thread count configured in both settings UIs.
    """
    import config
    from browser_monitor import BrowserCaptureHandler

    manager, stop = _segments_stub_manager()
    handler = BrowserCaptureHandler.__new__(BrowserCaptureHandler)
    handler.manager = manager
    monkeypatch.setattr(config, "get_download_dir", lambda: str(tmp_path))
    saved = config.get("segments")
    try:
        config.set("segments", value)
        try:
            handler._add_download("https://c/v/a.bin", None)
        except stop:
            pass
        assert manager.calls == [("https://c/v/a.bin", str(tmp_path), None, expected)]
    finally:
        config.set("segments", saved)


@pytest.mark.parametrize("value,expected", [(3, 3), (16, 16), (99, 32), (0, 8)])
def test_monitor_auto_add_reads_the_segments_setting(tmp_path, monkeypatch, value,
                                                      expected):
    """The monitor thread auto-add path must follow "segments" as well."""
    import config
    import downloader
    from browser_monitor import BrowserMonitor

    class _Task:
        filename = "a.bin"

        def start(self):
            pass

    created = []

    def create_task(url, save_dir, filename, segments, *args, **kwargs):
        created.append((url, save_dir, filename, segments))
        return _Task()

    monkeypatch.setattr(config, "get_download_dir", lambda: str(tmp_path))
    monkeypatch.setattr(downloader.manager, "create_task", create_task)
    saved = config.get("segments")
    try:
        config.set("segments", value)
        BrowserMonitor()._auto_add("https://c/v/a.bin")
        assert created == [("https://c/v/a.bin", str(tmp_path), None, expected)]
    finally:
        config.set("segments", saved)


def test_no_entry_point_hard_codes_the_thread_count():
    """No create_task call site may pass a literal thread count.

    app.py, main_window.py and browser_monitor.py all resolve the count from
    config now, so a bare literal in any call site would silently fork the
    setting again.  The one legitimate literal is downloader.py signature
    default (segments=8), which the "def " head check below skips.
    """
    import os
    import re
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, check=True)
        paths = [p for p in listed.stdout.decode("utf-8", "replace").splitlines()
                 if p.endswith((".py", ".js", ".html"))]
    except (OSError, subprocess.CalledProcessError):
        paths = []
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "__pycache__", "build", "dist")]
            for name in files:
                if name.startswith("_") or not name.endswith((".py", ".js", ".html")):
                    continue
                paths.append(os.path.relpath(os.path.join(base, name), root)
                             .replace(os.sep, "/"))

    bare_int = re.compile("(?<![A-Za-z0-9_.])[0-9]+(?![A-Za-z0-9_.])")
    offenders = []
    for path in paths:
        if path.startswith(("tests/", "docs/")):
            continue
        with open(os.path.join(root, path), encoding="utf-8") as handle:
            src = handle.read()
        for match in re.finditer(r"create_task\(", src):
            head = src[max(0, match.start() - 20):match.start()].splitlines()[-1]
            if "def " in head:
                continue
            depth, end = 1, match.end()
            while end < len(src) and depth:
                if src[end] == "(":
                    depth += 1
                elif src[end] == ")":
                    depth -= 1
                end += 1
            args = src[match.end():end - 1]
            if bare_int.search(args):
                offenders.append("%s: %s" % (path, " ".join(args.split())[:60]))
    assert offenders == [], "hard-coded thread count in create_task: " + " | ".join(offenders)


def _tracked_source_files():
    """受版本控制 + 未忽略的文本源文件，和 create_task 守卫同一份清单。"""
    import os
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        listed = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
            cwd=root, capture_output=True, check=True)
        raw_paths = listed.stdout.decode("utf-8", "replace").splitlines()
    except (OSError, subprocess.CalledProcessError):
        raw_paths = []
        for base, dirs, files in os.walk(root):
            dirs[:] = [d for d in dirs
                       if d not in (".git", "__pycache__", "build", "dist")]
            for name in files:
                raw_paths.append(os.path.relpath(os.path.join(base, name), root)
                                 .replace(os.sep, "/"))
    return [p for p in raw_paths
            if p.endswith((".py", ".js", ".html", ".css", ".md", ".txt"))]


def test_no_stray_bom_lands_inside_a_source_file():
    """UTF-8 BOM 只能出现在文件开头，绝不能掉在文件中间。

    templates/index.html 里曾掉进三个游离 BOM：一个把 .toast.info 这条 CSS 规则
    整条打死（U+FEFF 成了选择器的一部分，匹配不上 .toast.info），另两个落在
    HTML 注释和 <script> 注释前面。这类损坏极难用肉眼看出来——编辑器把它显示成
    零宽空格，肉眼和 git diff 都未必察觉，但 CSS/JS 语义已经变了。
    """
    offenders = []
    for path in _tracked_source_files():
        with open(path, "rb") as handle:
            blob = handle.read()
        body = blob[3:] if blob.startswith(b"\xef\xbb\xbf") else blob
        if b"\xef\xbb\xbf" in body:
            first = body.index(b"\xef\xbb\xbf")
            line = body[:first].count(b"\n") + 1
            offenders.append("%s:%d" % (path, line))
    assert offenders == [], "文件中间出现游离 BOM: " + ", ".join(offenders)


def test_create_task_without_segments_follows_the_setting(tmp_path):
    """Omitting segments must resolve the shared setting, not a bare 8.

    Every entry point (API, capture, monitor, drag-and-drop) passes an
    explicit clamped count now, so the signature default is the last place a
    stale literal could hide.
    """
    import config

    m = DownloadManager()
    saved = config.get("segments")
    try:
        config.set("segments", 12)
        assert m.create_task("https://c/v/a.bin", str(tmp_path)).segments == 12
        config.set("segments", 0)  # not configured -> built-in default wins
        assert m.create_task("https://c/v/a.bin", str(tmp_path)).segments == 8
    finally:
        config.set("segments", saved)
def test_scheduled_pending_task_survives_restart(tmp_path, monkeypatch):
    """重启不能把定时任务当中断处理。

    历史里的 pending 任务向被标成「重启后中断」（不自动续传半成品），
    但定时等待的任务不算中断：scheduler.restore() 只恢复状态仍是
    pending 的任务，这里一改成已取消，重启后定时下载就永远起不来。
    """
    import config

    monkeypatch.setattr(config, "get",
                        lambda key, default=None: {"dl_7": 900.0} if key == "scheduled" else default)
    m = DownloadManager()
    back = m._reconstruct_task({"task_id": "dl_7", "url": "https://c/a.zip",
                                "status": "pending", "filename": "a.zip"})
    assert back.status == "pending", "定时等待中的任务不能被标记为重启后中断"
    assert back.error == ""


def test_plain_pending_task_is_still_cancelled_on_restart(tmp_path, monkeypatch):
    """没有登记定时的 pending 任务该怎样还怎样：仍然当中断处理。"""
    import config

    monkeypatch.setattr(config, "get",
                        lambda key, default=None: {} if key == "scheduled" else default)
    m = DownloadManager()
    back = m._reconstruct_task({"task_id": "dl_8", "url": "https://c/a.zip",
                                "status": "pending", "filename": "a.zip"})
    assert back.status == "cancelled"
    assert back.error == "重启后中断（未自动续传）"


def test_scheduled_task_ids_tolerates_broken_table(monkeypatch):
    """配置里的定时表丢了/格式错了，不能把整个历史加载带崩。"""
    import config

    for bad in (None, "oops", [], 42):
        monkeypatch.setattr(
            config, "get",
            lambda key, default=None, _b=bad: _b if key == "scheduled" else default)
        assert downloader._scheduled_task_ids() == set()


def test_load_history_keeps_scheduled_and_cancels_plain_pending(tmp_path, monkeypatch):
    """整条链路：写盘的历史里同时有定时与非定时的 pending。"""
    import config

    monkeypatch.setattr(config, "get",
                        lambda key, default=None: {"dl_7": 900.0} if key == "scheduled" else default)
    hist = tmp_path / "history.json"
    hist.write_text(json.dumps({"tasks": [
        {"task_id": "dl_7", "url": "https://c/a.zip", "status": "pending",
         "filename": "a.zip", "filepath": str(tmp_path / "a.zip")},
        {"task_id": "dl_8", "url": "https://c/b.zip", "status": "pending",
         "filename": "b.zip", "filepath": str(tmp_path / "b.zip")},
    ]}), encoding="utf-8")

    real_load = _REAL_LOAD_HISTORY          # import 时忘记截下真身，避开 autouse fixture 的空桩
    m = DownloadManager()
    monkeypatch.setattr(m, "_history_path", str(hist))
    m._load_history = lambda: real_load(m)   # 实例属性阴影：只对这一个实例生效
    m._load_history()
    assert m.get_task("dl_7").status == "pending"
    assert m.get_task("dl_8").status == "cancelled"
def test_shared_config_writes_never_reach_the_real_file():
    """跑测试不能改用户真实的 ~/.swiftdm/config.json。

    曾经 Scheduler._persist() 在测试进程里把空调度表写回共享配置，
    用户真实的定时下载表被冲成测试数据，重启后全部变成「已取消」。
    """
    import config

    with open(config.CONFIG_PATH, "r", encoding="utf-8") as fh:
        before = json.load(fh)

    config.set("scheduled", {"dl_999": 1.0})
    assert config.get("scheduled") == {"dl_999": 1.0}      # 覆写层照常生效

    with open(config.CONFIG_PATH, "r", encoding="utf-8") as fh:
        after = json.load(fh)
    assert after == before, "测试把配置写回真实文件了"
