"""downloader 暂停/恢复线程竞态回归测试。

背景：pause() 只把 _gen+1、不 join 旧线程，resume() 曾直接 self._threads=[] 丢弃
旧线程引用就拉起新线程。限速下旧分段线程仍在写同一分片、旧监控线程仍在跑，会并发
写坏分片，或在合并后 rmtree 分片目录，最终表现为 resume 返回非 downloading、
文件大小/哈希不符。修复见 downloader.py resume()：先以有界 deadline join 旧线程，
再置 status="downloading" 并重启分段/监控线程。

本文件两类测试：
- test_resume_holds_status_until_stale_thread_exits：确定性地校验修复不变量——
  resume() 必须先 join 旧线程、再翻转 status 并重启线程（无修复时该断言必失败）。
- test_pause_then_immediate_resume_completes_intact：限速下把暂停点卡在下载中段，
  连续「暂停 -> 立即恢复（无间隙）」，校验任务最终 completed 且字节数/sha256 与源一致。
  原始竞态在打包二进制（test_binary）里才稳定复现、与调度时序相关，这里作为同类路径的
  完整性回归覆盖。
"""
import hashlib
import http.server
import os
import threading
import time

import pytest

import downloader
import throttle

SIZE = 1024 * 1024
BLOB = os.urandom(SIZE)
EXPECTED_SHA = hashlib.sha256(BLOB).hexdigest()


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _write(self, status, body, extra):
        self.send_response(status)
        for k, v in extra.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        rng = self.headers.get("Range", "")
        if rng.startswith("bytes="):
            spec = rng.split("=", 1)[1]
            s, _, e = spec.partition("-")
            start = int(s) if s else 0
            end = int(e) if e else SIZE - 1
            end = min(end, SIZE - 1)
            if start > end or start >= SIZE:
                self._write(416, b"", {"Content-Range": "bytes */%d" % SIZE})
                return
            self._write(206, BLOB[start:end + 1],
                        {"Content-Range": "bytes %d-%d/%d" % (start, end, SIZE)})
        else:
            self._write(200, BLOB, {})


@pytest.fixture(scope="module")
def base_url():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d/blob.bin" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def _direct_no_proxy(monkeypatch):
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    downloader.set_proxy_mode("direct")
    yield
    downloader.set_proxy_mode("env")
    throttle.set_rate(0)


def _spin_until(fn, deadline):
    end = time.time() + deadline
    while time.time() < end:
        if fn():
            return True
        time.sleep(0.005)
    return fn()


def _mid_download(task):
    # 用分段粒度的 _segment_progress（分片线程按片实时更新）判断是否真在下载中段，
    # 比监控线程 0.5s 才更新一次的 task.downloaded 更及时，避免小文件被下载完而错过暂停点。
    if task.status != "downloading":
        return False
    prog = sum(task._segment_progress)
    return SIZE * 0.05 <= prog <= SIZE * 0.85


@pytest.mark.parametrize("segments", [4, 6])
def test_pause_then_immediate_resume_completes_intact(base_url, tmp_path, segments):
    # 256KB/s -> write_granularity=128KB < 256KB chunk：分片内层多片写入，
    # 让「旧线程尚未退出」的竞态窗口真实存在，而不是一秒内就跑完。
    throttle.set_rate(256 * 1024)
    try:
        for i in range(2):
            save_dir = tmp_path / ("run%d_%d" % (segments, i))
            save_dir.mkdir()
            task = downloader.DownloadTask("rr_%d_%d" % (segments, i), base_url,
                                           str(save_dir), filename="blob.bin",
                                           segments=segments)
            try:
                task.start()
                mid = _spin_until(lambda: _mid_download(task), 15)
                assert task.status == "downloading" and mid, "未能在下载中段暂停：status=%s err=%s" % (task.status, task.error)

                task.pause()
                assert task.status == "paused", task.error
                task.resume()                       # 立即恢复、无间隙：触发竞态窗口
                assert task.status == "downloading", task.error

                assert _spin_until(
                    lambda: task.status in ("completed", "failed", "cancelled"), 60)
                assert task.status == "completed", "第 %d 轮：status=%s err=%s" % (i, task.status, task.error)
                assert os.path.getsize(task.filepath) == SIZE, "文件大小不符"
                with open(task.filepath, "rb") as f:
                    got = hashlib.sha256(f.read()).hexdigest()
                assert got == EXPECTED_SHA, "第 %d 轮：分段字节被并发写坏" % i
            finally:
                if task.status not in ("completed", "failed", "cancelled"):
                    task.cancel()
    finally:
        throttle.set_rate(0)


def test_resume_holds_status_until_stale_thread_exits(tmp_path):
    """确定性不变量：resume() 必须先把旧线程 join 掉，再翻转 status、重启分段/监控线程。

    旧实现（无 join）会立刻 status=downloading 并拉起新线程；这里塞一个阻塞在 gate 上的
    「旧线程」，resume 只有在它退出后才允许翻转 status。无修复时第一步断言即失败。
    """
    task = downloader.DownloadTask("joinprobe", "http://127.0.0.1/probe",
                                   str(tmp_path), filename="probe.bin", segments=1)
    task.status = "paused"
    task.segments = 1
    task._seg_done = [False]
    task._segment_progress = [0]
    task._run_segment = lambda idx: None
    task._monitor_progress = lambda: None

    gate = threading.Event()
    stale_exited = threading.Event()

    def _stale():
        gate.wait(5)
        stale_exited.set()

    stale = threading.Thread(target=_stale, daemon=True, name="stale-seg")
    stale.start()
    task._threads = [stale]

    resumed = threading.Event()

    def _do_resume():
        task.resume()
        resumed.set()

    threading.Thread(target=_do_resume, daemon=True).start()

    time.sleep(0.2)
    assert not stale_exited.is_set()
    assert task.status == "paused", "resume 未等待旧线程退出就翻转了 status（竞态回归）"
    assert not resumed.is_set(), "resume 未等待旧线程退出就返回了"

    gate.set()
    assert resumed.wait(3), "旧线程退出后 resume 未完成"
    assert stale_exited.is_set(), "resume 返回前旧线程尚未退出"
    assert task.status == "downloading"


def test_resume_bails_if_cancelled_during_join(tmp_path):
    """resume() join 旧线程期间若被并发 cancel，不得再翻回 downloading 覆盖取消状态。

    对应修复：join 之后、置 status="downloading" 之前重新校验 status 仍为 paused。
    无该校验时，cancel 设的 cancelled 会被 resume 覆盖成 downloading（竞态回归）。
    """
    task = downloader.DownloadTask("cancelprobe", "http://127.0.0.1/probe",
                                   str(tmp_path), filename="probe.bin", segments=1)
    task.status = "paused"
    task.segments = 1
    task._seg_done = [False]
    task._segment_progress = [0]
    task._run_segment = lambda idx: None
    task._monitor_progress = lambda: None

    gate = threading.Event()

    def _stale():
        gate.wait(5)

    stale = threading.Thread(target=_stale, daemon=True, name="stale-seg")
    stale.start()
    task._threads = [stale]

    resumed = threading.Event()

    def _do_resume():
        task.resume()
        resumed.set()

    threading.Thread(target=_do_resume, daemon=True).start()

    time.sleep(0.1)                  # resume 此刻应卡在 join 旧线程上
    assert task.status == "paused"
    task.status = "cancelled"        # 模拟并发 cancel 落在 join 窗口内
    gate.set()                       # 放开旧线程，让 join 返回

    assert resumed.wait(3), "resume 未在旧线程退出后返回"
    assert task.status == "cancelled", "resume 覆盖了并发取消（竞态回归）"
