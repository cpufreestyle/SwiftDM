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
- test_retry_start_survives_concurrent_cancel：retry() 在联网探测期间被并发 cancel，
  启动令牌必须让这次启动作废——已取消的任务不能被拉回 downloading。
- test_concurrent_transitions_settle_without_deadlock：5 个线程对同一任务疯狂
  pause/resume/cancel/start/retry，验证不出现死锁，且最终文件没有被并发写花。
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


def test_retry_start_survives_concurrent_cancel(tmp_path, monkeypatch):
    """retry() 重置字段后要联网探测（不能持锁），期间并发的 cancel 必须作废这次启动。

    retry() 需要先重置 _seg_done / _completion_handled 等字段再重启下载，而
    start() 内部要联网探测文件信息（最长约 45s），不能抱着状态锁干等。于是存在
    一个窗口：状态还是 failed/cancelled，start() 紧接着就要把它声明成 downloading。
    若此时并发 cancel 抢先，没有额外机制的话 cancel 会被 start() 覆盖，
    已被取消的任务又被拉回 downloading。实现用 _start_token 堵住这个窗口。
    """
    task = downloader.DownloadTask("tokenprobe", "http://127.0.0.1/probe",
                                   str(tmp_path), filename="probe.bin", segments=1)
    task.status = "failed"
    task.segments = 1
    task._seg_done = [False]
    task._segment_progress = [0]

    probing = threading.Event()
    release = threading.Event()

    def _slow_fetch():
        probing.set()                 # 已越过 start() 的状态声明，正在联网探测
        release.wait(5)
        return True

    monkeypatch.setattr(task, "_fetch_info", _slow_fetch)

    result = {}

    def _do_retry():
        result["ok"] = task.retry()

    threading.Thread(target=_do_retry, daemon=True).start()

    assert probing.wait(5), "retry 没有进入 start() 的联网探测段"
    task.cancel()                     # 探测期间取消
    release.set()                     # 放行探测，让 start() 继续走完

    deadline = time.time() + 10
    while "ok" not in result and time.time() < deadline:
        time.sleep(0.01)
    assert "ok" in result, "retry 没有返回（死锁？）"
    assert result["ok"] is False, "启动令牌已失效，start() 不应报告启动成功"
    assert task.status == "cancelled", "cancel 的状态被 start() 覆盖，任务被复活了"
    assert not task._threads, "启动令牌失效后不应拉起任何线程"


def test_concurrent_transitions_settle_without_deadlock(tmp_path, base_url):
    """5 个线程对同一任务疯狂 pause/resume/cancel/start/retry：不得死锁、不得写坏文件。

    同时压三类不变量：
      - 所有状态迁移（pause/resume/cancel/retry/start/完成）互斥，只有一种生效；
      - resume()/start() 只在 _xlock 之外 join 旧线程，不会被旧监控线程反锁；
      - monitor 完成块先在锁外让位，pause/cancel 不会被文件组装阻塞到超时。
    """
    task = downloader.DownloadTask("storm", base_url, str(tmp_path),
                                   filename="storm.bin", segments=4)
    throttle.set_rate(256 * 1024)
    stop = threading.Event()
    failures = []

    def _hammer(i):
        ops = [task.pause, task.resume, task.start, task.retry, task.cancel]
        n = 0
        while not stop.is_set():
            try:
                ops[(i + n) % len(ops)]()
            except Exception as e:
                failures.append(e)
            n += 1
            time.sleep(0.003)

    task.start()
    assert _spin_until(lambda: task.status == "downloading" and task.downloaded > 0, 15)

    workers = [threading.Thread(target=_hammer, args=(i,), daemon=True) for i in range(5)]
    for t in workers:
        t.start()
    time.sleep(1.5)
    stop.set()
    for t in workers:
        t.join(20)
    assert not any(t.is_alive() for t in workers), "并发状态转换导致死锁"
    assert not failures, "状态转换抛出异常: %r" % (failures[:3],)

    # 收尾：先 cancel 收敛到干净起点，再重试一次完整下载，校验字节没被并发写花
    task.cancel()
    assert task.status == "cancelled"
    assert task.retry() is True
    assert _spin_until(
        lambda: task.status in ("completed", "failed", "cancelled"), 60), task.status
    assert task.status == "completed", "status=%s err=%s" % (task.status, task.error)
    assert os.path.getsize(task.filepath) == SIZE, "文件大小不符：分段被并发写花"
    with open(task.filepath, "rb") as f:
        got = hashlib.sha256(f.read()).hexdigest()
    assert got == EXPECTED_SHA, "文件内容损坏：分段被并发写花"


def test_resume_invalidates_to_dict_cache(tmp_path):
    """resume() 翻转 status 后必须立即使 to_dict() 缓存失效。

    pause 与 resume 之间任何一次 to_dict（桌面 UI 500ms 刷新 / SSE 推送）
    都会把 status="paused" 缓存下来；resume 不失效缓存时，
    /api/resume 随响应返回的仍是旧 paused 快照，Web 端点完
    「继续」又显示暂停（二进制冒演里间歇性复现）。
    """
    task = downloader.DownloadTask("cacheprobe", "http://127.0.0.1/probe",
                                   str(tmp_path), filename="probe.bin", segments=1)
    task.status = "paused"
    task.segments = 1
    task._seg_done = [False]
    task._segment_progress = [0]
    task._run_segment = lambda idx: None
    task._monitor_progress = lambda: None

    assert task.to_dict()["status"] == "paused"  # 暂停态已被读取者缓存
    task.resume()
    assert task.to_dict()["status"] == "downloading"
