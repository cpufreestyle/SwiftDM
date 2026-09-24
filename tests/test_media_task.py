import os
import threading
import time
import types

import pytest

import media


@pytest.fixture()
def fake_ytdlp(monkeypatch):
    """假 yt_dlp 模块：记录 opts、按脚本回放进度事件、可注入异常。"""
    captured = {}

    class _Inst:
        def __init__(self, opts):
            captured["opts"] = opts
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            captured["extract"] = (url, download)
            if captured.get("extract_raise") is not None:
                raise captured["extract_raise"]
            return captured.get("info", {})

        def download(self, urls):
            captured["download"] = urls
            captured["hold"].wait(timeout=10.0)   # 让测试能稳住「下载中」这个中间态
            for st, payload in captured["events"]:
                for hook in self.opts["progress_hooks"]:
                    hook(dict(payload, status=st))
            if captured.get("raise") is not None:
                raise captured["raise"]

    mod = types.SimpleNamespace(
        YoutubeDL=_Inst,
        utils=types.SimpleNamespace(DownloadCancelled=type("DownloadCancelled", (Exception,), {})))
    media.set_ytdlp_module(mod)
    captured["events"] = [("downloading", {"downloaded_bytes": 500, "total_bytes": 1000,
                                           "speed": 250.0, "eta": 2}),
                          ("finished", {"downloaded_bytes": 1000, "total_bytes": 1000,
                                        "filename": ""})]
    captured["hold"] = threading.Event()
    captured["hold"].set()
    monkeypatch.setattr(media, "ffmpeg_status", lambda: {"available": True, "path": "ffmpeg"})
    yield captured
    media.set_ytdlp_module(None)


def _mk(tmp_path, kind="hls", segments=8, **kw):
    return media.MediaTask("dl_1", "https://c/v/index.m3u8", str(tmp_path),
                           filename=kw.pop("filename", "movie.mp4"), segments=segments,
                           kind=kind, referer=kw.pop("referer", None),
                           cookies_netscape=kw.pop("cookies_netscape", None),
                           resolution=kw.pop("resolution", None))


def _wait_done(task, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline and task.status in ("pending", "downloading"):
        time.sleep(0.02)
    return task.status


def _wait_pred(pred, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return False


def test_fields_and_to_dict_contract(tmp_path, fake_ytdlp):
    t = _mk(tmp_path, kind="hls", referer="https://site/v")
    d = t.to_dict()
    for key in ("task_id", "filename", "filepath", "url", "status", "progress", "total_size",
                "downloaded", "speed", "eta", "error", "error_reason", "segments", "kind",
                "resolution", "referer", "save_dir"):
        assert key in d, key
    assert d["status"] == "pending" and d["kind"] == "hls" and d["referer"] == "https://site/v"
    assert "cookies" not in str(d).lower()           # Cookie 绝不能进状态导出


def test_start_downloads_and_completes(tmp_path, fake_ytdlp):
    fake_ytdlp["events"][1][1]["filename"] = os.path.join(str(tmp_path), "movie.mp4")
    t = _mk(tmp_path)
    t.start()
    assert _wait_done(t) == "completed", t.error
    assert t.progress == 100.0
    assert t.total_size == 1000 and t.downloaded == 1000
    assert t.filepath == os.path.join(str(tmp_path), "movie.mp4")
    assert fake_ytdlp["download"] == ["https://c/v/index.m3u8"]


def test_opts_for_hls(tmp_path, fake_ytdlp):
    t = _mk(tmp_path, kind="hls", referer="https://site/v", segments=16)
    t.start()
    _wait_done(t)
    opts = fake_ytdlp["opts"]
    assert opts["concurrent_fragment_downloads"] == 16
    assert opts["http_headers"]["Referer"] == "https://site/v"
    assert opts["http_headers"]["User-Agent"]
    assert opts["merge_output_format"] == "mp4"
    assert opts["continuedl"] is True and opts["noprogress"] is True
    assert "cookiefile" not in opts
    assert "ratelimit" not in opts                   # 未限速时不塞 0
    assert opts["format"] == "bv*+ba/b"


def test_opts_resolution_and_cookies(tmp_path, fake_ytdlp):
    t = _mk(tmp_path, kind="video_page", resolution=720,
            cookies_netscape="# Netscape HTTP Cookie File\n"
                             ".example.com\tTRUE\t/\tFALSE\t0\tsid\tabc\n")
    fake_ytdlp["hold"].clear()                       # 停在「下载中」，才好检查 Cookie 临时文件
    t.start()
    assert _wait_pred(lambda: "opts" in fake_ytdlp)
    opts = fake_ytdlp["opts"]
    assert opts["format"] == "bv*[height<=720]+ba/b"
    path = opts["cookiefile"]
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as f:
        assert "sid" in f.read()
    fake_ytdlp["hold"].set()
    assert _wait_done(t) == "completed"
    assert not os.path.exists(path)                  # 任务结束必须删掉临时 Cookie 文件


def test_ratelimit_is_taken_from_global_throttle(tmp_path, fake_ytdlp):
    import throttle
    throttle.set_rate(2 * 1024 * 1024)
    try:
        t = _mk(tmp_path)
        t.start()
        _wait_done(t)
        assert fake_ytdlp["opts"]["ratelimit"] == 2 * 1024 * 1024
    finally:
        throttle.set_rate(0)


def test_pause_cancels_worker_and_resume_restarts(tmp_path, fake_ytdlp):
    fake_ytdlp["events"] = [("downloading", {"downloaded_bytes": 300, "total_bytes": 1000})]
    fake_ytdlp["hold"].clear()                       # 卡在「下载中」，让 pause 有确定性
    t = _mk(tmp_path)
    t.start()
    assert _wait_pred(lambda: fake_ytdlp.get("download") is not None)
    t.pause()
    assert t.status == "paused" and t.speed == 0.0
    first_opts = fake_ytdlp["opts"]
    fake_ytdlp["events"].append(("finished", {"downloaded_bytes": 1000, "total_bytes": 1000,
                                              "filename": os.path.join(str(tmp_path), "movie.mp4")}))
    fake_ytdlp["hold"].set()                         # 旧线程醒来后应自己静默退出
    assert _wait_pred(lambda: t._worker is None or not t._worker.is_alive())
    t.resume()
    assert _wait_done(t) == "completed", t.error
    assert fake_ytdlp["opts"] is not first_opts


def test_failure_from_ytdlp_marks_failed_with_reason(tmp_path, fake_ytdlp):
    fake_ytdlp["raise"] = Exception("ERROR: [generic] Unable to download webpage: timed out")
    t = _mk(tmp_path)
    t.start()
    assert _wait_done(t) == "failed"
    assert t.error_reason == "parse_failed"
    assert "解析失败" in t.error                      # 给用户看的中文提示，不暴露原始栈


def test_drm_error_from_ytdlp_maps_to_drm_reason(tmp_path, fake_ytdlp):
    fake_ytdlp["raise"] = Exception("ERROR: This video is protected by Widevine DRM")
    t = _mk(tmp_path)
    t.start()
    assert _wait_done(t) == "failed"
    assert t.error_reason == "drm_protected"
    assert "DRM" in t.error


def test_cookie_required_error_maps_to_cookies_reason(tmp_path, fake_ytdlp):
    fake_ytdlp["raise"] = Exception("ERROR: Login required. Cookies are needed")
    t = _mk(tmp_path)
    t.start()
    assert _wait_done(t) == "failed"
    assert t.error_reason == "cookies_required"
    assert "登录" in t.error


def test_preflight_rejects_drm_metadata(tmp_path, fake_ytdlp):
    fake_ytdlp["info"] = {"formats": [{"format_id": "1", "has_drm": True},
                                      {"format_id": "2", "vcodec": "avc1"}]}
    t = _mk(tmp_path)
    with pytest.raises(media.DrmProtectedError):
        t.preflight()


def test_preflight_allows_pure_ts_hls_without_ffmpeg(tmp_path, fake_ytdlp, monkeypatch):
    """没装 ffmpeg 也必须能下 HLS：纯 TS 序列由 yt-dlp 内置合并（spec §2「MediaTask 行为」）。"""
    monkeypatch.setattr(media, "ffmpeg_status", lambda: {"available": False, "path": None})
    fake_ytdlp["info"] = {"ext": "mp4", "format_id": "__all__"}          # 无 formats 的已混流清单
    t = _mk(tmp_path, kind="hls")
    assert t.preflight() is None


def test_preflight_rejects_split_dash_without_ffmpeg(tmp_path, fake_ytdlp, monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_status", lambda: {"available": False, "path": None})
    fake_ytdlp["info"] = {"formats": [{"format_id": "v", "vcodec": "avc1", "acodec": "none"},
                                      {"format_id": "a", "vcodec": "none", "acodec": "mp4a"}]}
    t = _mk(tmp_path, kind="dash")
    with pytest.raises(media.NeedsFfmpegError):
        t.preflight()


def test_preflight_passes_muxed_video_without_ffmpeg(tmp_path, fake_ytdlp, monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_status", lambda: {"available": False, "path": None})
    fake_ytdlp["info"] = {"formats": [{"format_id": "18", "vcodec": "avc1", "acodec": "mp4a"}]}
    t = _mk(tmp_path, kind="video_page")
    assert t.preflight() is None                     # 音视频同轨，不需要 ffmpeg


def test_preflight_rejects_split_streams_without_ffmpeg(tmp_path, fake_ytdlp, monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_status", lambda: {"available": False, "path": None})
    fake_ytdlp["info"] = {"formats": [{"format_id": "137", "vcodec": "avc1", "acodec": "none"},
                                      {"format_id": "140", "vcodec": "none", "acodec": "mp4a"}]}
    t = _mk(tmp_path, kind="video_page")
    with pytest.raises(media.NeedsFfmpegError):
        t.preflight()


def test_cancel_keeps_cancelled_state(tmp_path, fake_ytdlp):
    fake_ytdlp["events"] = []
    t = _mk(tmp_path)
    t.start()
    time.sleep(0.05)
    t.cancel()
    time.sleep(0.15)
    assert t.status == "cancelled"                   # 工作线程不得把它改回 completed


def test_retry_resets_error_and_restarts(tmp_path, fake_ytdlp):
    t = _mk(tmp_path)
    t.status = "failed"
    t.error = "x"
    t.error_reason = "parse_failed"
    assert t.retry() is True
    assert _wait_done(t) == "completed"
    assert t.error == "" and t.error_reason == ""
    assert t.retry() is False                        # 已完成不可重试


def test_progress_accumulates_fragment_events(tmp_path, fake_ytdlp):
    """yt-dlp 的分片进度是「全局累计」口径：每片到达时会重复上一次的累计值。

    实测（yt-dlp 2026.8.19 + 合成 HLS）事件形如
      {fragment_index:0, fragment_count:3, downloaded_bytes:100}
      {fragment_index:0, fragment_count:3, downloaded_bytes:300}   # 第 0 片完成
      {fragment_index:1, fragment_count:3, downloaded_bytes:300}   # 第 1 片开始，累计值重复
      ... 最后 finished 事件才带精确 total_bytes。
    换算后 downloaded 必须等于全部分片之和，不能被抖动的 total_bytes_estimate 带偏。
    """
    fake_ytdlp["events"] = [
        ("downloading", {"fragment_index": 0, "fragment_count": 3, "downloaded_bytes": 100,
                         "total_bytes_estimate": 300}),
        ("downloading", {"fragment_index": 0, "fragment_count": 3, "downloaded_bytes": 300}),
        ("downloading", {"fragment_index": 1, "fragment_count": 3, "downloaded_bytes": 300}),
        ("downloading", {"fragment_index": 1, "fragment_count": 3, "downloaded_bytes": 600}),
        ("downloading", {"fragment_index": 2, "fragment_count": 3, "downloaded_bytes": 600}),
        ("downloading", {"fragment_index": 2, "fragment_count": 3, "downloaded_bytes": 900,
                         "total_bytes_estimate": 294912}),
        ("finished", {"downloaded_bytes": 900, "total_bytes": 900,
                      "filename": os.path.join(str(tmp_path), "movie.mp4")}),
    ]
    t = _mk(tmp_path)
    t.start()
    assert _wait_done(t) == "completed", t.error
    assert t.downloaded == 900 and t.total_size == 900
    assert t.progress == 100.0 and t.filepath.endswith("movie.mp4")


def test_preflight_rejects_drm_from_extraction_error(fake_ytdlp, tmp_path):
    """yt-dlp 对 DRM 是提取期报错（拿不到 info），必须翻成 409 级的硬拒绝。"""
    fake_ytdlp["extract_raise"] = Exception(
        "ERROR: [generic] drm_master: This video is DRM protected")
    task = _mk(tmp_path, kind="hls")
    with pytest.raises(media.DrmProtectedError):
        task.preflight()


def test_base_opts_uses_logger_bridge_not_stderr(fake_ytdlp, tmp_path):
    """logger 桥存在：yt-dlp 的错误上报不写 stderr（GUI 打包进程里 flush 会失败）。"""
    task = _mk(tmp_path, kind="hls")
    opts = task._base_opts(0)
    assert opts["logger"] is media._YDL_LOGGER
    # 桥接方法存在且可用
    opts["logger"].debug("x")
    opts["logger"].info("x")
    opts["logger"].warning("x")
    opts["logger"].error("x")

def test_media_start_drains_stale_worker_before_respawn(tmp_path, fake_ytdlp):
    """start() 从 paused 续跑前必须先等旧 worker 退出。

    yt-dlp 的 worker 收到 _gen 变化后，要等 progress hook 触发 DownloadCancelled
    才层层 unwind，期间仍在写 .part。若不 join 就重新拉起，两个 yt-dlp 实例会
    并发写同一个 .part，互相覆盖导致文件损坏。对应 DownloadTask._drain_threads。
    """
    fake_ytdlp["events"] = [("downloading", {"downloaded_bytes": 300, "total_bytes": 1000})]
    fake_ytdlp["hold"].clear()                   # 卡在「下载中」，让 pause 有确定性
    t = _mk(tmp_path)
    t.start()
    assert _wait_pred(lambda: fake_ytdlp.get("download") is not None)
    t.pause()
    assert t.status == "paused"

    gate = threading.Event()
    stale_exited = threading.Event()

    def _stale():
        gate.wait(5)
        stale_exited.set()

    stale = threading.Thread(target=_stale, daemon=True, name="stale-ydl")
    stale.start()
    t._worker = stale                            # 用可控的假 worker 顶替真实 worker

    fake_ytdlp["events"].append(
        ("finished", {"downloaded_bytes": 1000, "total_bytes": 1000,
                      "filename": os.path.join(str(tmp_path), "movie.mp4")}))
    resumed = threading.Event()

    def _do_start():
        t.start()
        resumed.set()

    threading.Thread(target=_do_start, daemon=True).start()

    time.sleep(0.2)
    assert not stale_exited.is_set(), "start() 没等旧 worker 退出就继续了"
    assert not resumed.is_set(), "start() 没等旧 worker 退出就返回了"

    gate.set()
    assert resumed.wait(5), "旧 worker 退出后 start() 没有返回"
    fake_ytdlp["hold"].set()                     # 放行新 worker，让它尽快跑完
    assert t.status == "downloading"


def test_media_retry_start_survives_concurrent_cancel(tmp_path, fake_ytdlp, monkeypatch):
    """retry() 重置字段与 start() 拉起 worker 之间，并发 cancel 必须作废这次启动。

    retry() 要在持锁段里重置字段，而 start() 里要跑 yt-dlp（不能持锁），中间有
    一个状态还是 pending 的窗口；没有启动令牌的话并发 cancel 会被 start() 覆盖，
    已取消的任务又被拉起来。对应 DownloadTask 的同名测试。
    """
    t = _mk(tmp_path)
    t.status = "failed"

    entered = threading.Event()
    release = threading.Event()

    def _blocked_drain(timeout=5.0):
        entered.set()                 # 已越过 start() 的前置 guard，正在 drain
        release.wait(5)

    monkeypatch.setattr(t, "_drain_worker", _blocked_drain)

    result = {}

    def _do_retry():
        result["ok"] = t.retry()

    threading.Thread(target=_do_retry, daemon=True).start()
    assert entered.wait(5), "retry 没有走到 start() 的 drain 前"
    t.cancel()                         # drain 期间取消
    release.set()

    deadline = time.time() + 10
    while "ok" not in result and time.time() < deadline:
        time.sleep(0.01)
    assert "ok" in result, "retry 没有返回（死锁？）"
    assert result["ok"] is False, "启动令牌已失效，start() 不应报告启动成功"
    assert t.status == "cancelled", "cancel 的状态被 start() 覆盖，任务被复活了"
    assert t._worker is None, "启动令牌失效后不应拉起 worker"


def test_media_concurrent_transitions_settle_without_deadlock(tmp_path, fake_ytdlp):
    """4 个线程对同一任务疯狂 pause/resume/cancel/start/retry：不得死锁、不得抛异常。

    与 DownloadTask 的同类压测对应：验证 MediaTask 的状态迁移互斥、
    _drain_worker 只在 _xlock 之外 join、完成路径与 cancel 不会互相覆写。
    """
    fake_ytdlp["events"] = [("downloading", {"downloaded_bytes": 200, "total_bytes": 1000})]
    t = _mk(tmp_path)
    stop = threading.Event()
    failures = []

    def _hammer(i):
        ops = [t.pause, t.resume, t.start, t.retry, t.cancel]
        n = 0
        while not stop.is_set():
            try:
                ops[(i + n) % len(ops)]()
            except Exception as e:
                failures.append(e)
            n += 1
            time.sleep(0.002)

    t.start()
    assert _wait_pred(lambda: fake_ytdlp.get("download") is not None)
    workers = [threading.Thread(target=_hammer, args=(i,), daemon=True) for i in range(4)]
    for w in workers:
        w.start()
    time.sleep(0.5)
    stop.set()
    for w in workers:
        w.join(15)
    assert not any(w.is_alive() for w in workers), "并发状态转换导致死锁"
    assert not failures, "状态转换抛出异常: %r" % (failures[:3],)

    # 收尾：等最后一个 worker 退出，cancel 收敛后重试一次完整下载
    fake_ytdlp["events"].append(
        ("finished", {"downloaded_bytes": 1000, "total_bytes": 1000,
                      "filename": os.path.join(str(tmp_path), "movie.mp4")}))
    assert _wait_pred(lambda: t._worker is None or not t._worker.is_alive(), 10)
    t.cancel()
    assert _wait_pred(lambda: t.status == "cancelled", 5)
    assert t.retry() is True
    assert _wait_done(t, 20) == "completed", t.error
