"""合成 HLS 端到端：本地起服务器 → /api/add → MediaTask 真跑 yt-dlp → 校验字节与限速。

需要已安装 yt-dlp（requirements.txt 已列）；缺依赖时整个模块 skip，而不是 fail，
以免没装解析器的机器上跑全量测试被误判成功。
"""
import hashlib
import http.server
import json
import os
import threading
import time

import pytest

pytest.importorskip("yt_dlp", reason="流媒体集成测试需要已安装 yt-dlp（pip install -r requirements.txt）")

import app as appmod                       # noqa: E402
import downloader                          # noqa: E402
import media                               # noqa: E402
import throttle                            # noqa: E402
from downloader import manager             # noqa: E402

_REAL_LOAD_HISTORY = downloader.DownloadManager._load_history

SEG_COUNT = 8
SEG_SIZE = 32 * 1024                        # 每片 32KB，共 256KB：限速用例秒级可辨
TOTAL = SEG_COUNT * SEG_SIZE


def _segment_bytes(i):
    """伪 TS 分片：188 字节包对齐，内容可复现。"""
    packet = bytes([0x47]) + ("seg%02d" % i).encode("utf-8").ljust(187, b"\x00")
    return (packet * (SEG_SIZE // 188 + 1))[:SEG_SIZE]


PLAYLIST = (
    "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n"
    + "".join("#EXTINF:1.0,\nseg%02d.ts\n" % i for i in range(SEG_COUNT))
    + "#EXT-X-ENDLIST\n"
)
EXPECTED_SHA = hashlib.sha256(b"".join(_segment_bytes(i) for i in range(SEG_COUNT))).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _payload(self):
        path = self.path.split("?")[0]
        if path.endswith(".m3u8"):
            return 200, PLAYLIST.encode("utf-8"), "application/vnd.apple.mpegurl"
        name = path.rsplit("/", 1)[-1]
        if name.startswith("seg") and name.endswith(".ts"):
            return 200, _segment_bytes(int(name[3:5])), "video/mp2t"
        return 404, b"", "text/plain"

    def _respond(self, with_body):
        status, data, ctype = self._payload()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if with_body and data:
            self.wfile.write(data)

    def do_GET(self):
        self._respond(True)

    def do_HEAD(self):
        self._respond(False)


@pytest.fixture(scope="module")
def hls_base():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """本机回环不能被系统代理劫走（这台机器上有 Clash），并且不能污染真实历史文件。"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(downloader.DownloadManager, "_load_history", lambda self: None)
    monkeypatch.setattr(downloader.DownloadManager, "_start_saver", lambda self: None)
    monkeypatch.setattr(downloader.DownloadManager, "save_history", lambda self: None)
    media.set_ytdlp_module(None)
    throttle.set_rate(0)
    yield
    throttle.set_rate(0)


def _download(base, save_dir, segments=8, **extra):
    client = appmod.app.test_client()
    os.makedirs(save_dir, exist_ok=True)
    payload = {"url": base + "/index.m3u8", "kind": "hls",
               "save_dir": str(save_dir), "segments": segments}
    payload.update(extra)
    resp = client.post("/api/add", json=payload)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    task = manager.get_task(body["task"]["task_id"])
    started = time.time()
    deadline = started + 90
    while time.time() < deadline and task.status not in ("completed", "failed", "cancelled"):
        time.sleep(0.25)
    result = {"status": task.status, "elapsed": time.time() - started,
              "filepath": task.filepath, "error": task.error,
              "downloaded": task.downloaded}
    manager.remove_task(task.task_id)
    return result


needs_native_hls = pytest.mark.skipif(
    media.ffmpeg_status()["available"],
    reason="系统里有 ffmpeg 时 yt-dlp 会把 HLS 转封装成 mp4，字节序列不再等于分片拼接")


def test_local_hls_downloads_end_to_end(hls_base, tmp_path):
    got = _download(hls_base, tmp_path / "a")
    assert got["status"] == "completed", got["error"]
    assert os.path.isfile(got["filepath"])
    assert got["downloaded"] == TOTAL, "进度统计应等于所有分片之和"


@needs_native_hls
def test_concatenated_segments_match_source_bytes(hls_base, tmp_path):
    got = _download(hls_base, tmp_path / "b")
    assert got["status"] == "completed", got["error"]
    with open(got["filepath"], "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == EXPECTED_SHA, "分片顺序被搞乱了"


def test_global_rate_limit_applies_to_stream_download(hls_base, tmp_path):
    fast = _download(hls_base, tmp_path / "fast")
    assert fast["status"] == "completed", fast["error"]
    throttle.set_rate(128 * 1024)
    # segments=1 是必需的：yt-dlp 的 ratelimit 按「每条连接」限速，
    # 8 路并发分片会把总速率抬到 8 倍，那样时间断言毫无意义。
    slow = _download(hls_base, tmp_path / "slow", segments=1)     # 256KB / 128KBps ⇒ ≥2 秒
    assert slow["status"] == "completed", slow["error"]
    assert slow["elapsed"] >= 1.5, (fast, slow)
    assert slow["elapsed"] > fast["elapsed"]


def test_stream_payload_reports_media_progress(hls_base, tmp_path):
    """进度要沿 `/api/stream` 那一帧流出去 —— 前端复用同一条流，不新增轮询（spec §2、§6）。"""
    client = appmod.app.test_client()
    throttle.set_rate(64 * 1024)                                  # 256KB 单连接 ⇒ 约 4 秒采样窗口
    resp = client.post("/api/add", json={"url": hls_base + "/index.m3u8", "kind": "hls",
                                        "save_dir": str(tmp_path / "sse"), "segments": 1})
    assert resp.status_code == 200, resp.get_json()
    tid = resp.get_json()["task"]["task_id"]
    frames = []
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            mine = [t for t in appmod._stream_payload()["tasks"] if t["task_id"] == tid]
            assert mine, "SSE 帧里丢了刚添加的媒体任务"
            frames.append(mine[0])
            if mine[0]["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.1)
    finally:
        manager.remove_task(tid)
        throttle.set_rate(0)
    assert frames[-1]["status"] == "completed", frames[-1]["error"]
    assert any(0 < f["progress"] < 100 and f["downloaded"] > 0 for f in frames), frames
    assert frames[-1]["progress"] == 100.0 and frames[-1]["downloaded"] == TOTAL


def test_legacy_history_without_kind_still_loads_and_deletes(tmp_path, monkeypatch):
    """老版本没有 kind 字段，连 m3u8 也是按普通文件下的 —— 必须能加载并删除。"""
    legacy = {"version": 1, "saved_at": time.time(), "tasks": [
        {"task_id": "dl_1", "url": "https://c/a.zip", "filename": "a.zip",
         "status": "completed", "filepath": str(tmp_path / "a.zip"),
         "total_size": 10, "downloaded": 10, "progress": 100.0},
        {"task_id": "dl_2", "url": "https://c/v/index.m3u8", "filename": "index.m3u8",
         "status": "completed", "save_dir": str(tmp_path),
         "total_size": 20, "downloaded": 20, "progress": 100.0},
    ]}
    path = tmp_path / "history.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    monkeypatch.setattr(downloader.DownloadManager, "_start_saver", lambda self: None)
    real_load = _REAL_LOAD_HISTORY          # import 时抓到的真身，避开 fixture 的类级替换
    m = downloader.DownloadManager()
    m._history_path = str(path)
    m._load_history = lambda: real_load(m)  # 实例属性影子：只对这一个实例生效
    m._load_history()                       # 现在会真正读 history.json
    assert {t.task_id for t in m.get_all_tasks()} == {"dl_1", "dl_2"}
    m.remove_task("dl_1")
    assert {t.task_id for t in m.get_all_tasks()} == {"dl_2"}
