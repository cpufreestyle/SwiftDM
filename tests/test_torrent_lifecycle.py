"""torrent.TorrentTask 生命周期转换的回归测试。

TorrentTask 的 retry() 早先只把「重置字段 + status=pending」放在锁外、再调
start()。并发 cancel 落在重置之后、start() 之前时会被 start() 覆写，已取消的
任务又被拉回 downloading。修复把整段放进 self._lock（RLock 可重入），并让
start() 的 guard 也回到锁内，与 DownloadTask / MediaTask 的转换锁保持一致。
"""
import threading
import time
import types

import pytest

torrent = pytest.importorskip("torrent")


def test_retry_holds_lock_across_start_so_cancel_cannot_interleave(tmp_path):
    """retry() 持锁调用 start()：锁释放前 cancel 无法插入。

    无修复时 retry 的重置段不持锁，cancel 可以在 start() 之前落地，随后被
    start() 覆写成 downloading。修复后 cancel 必须等到 start() 结束才能执行，
    最终状态由 cancel 决定。
    """
    t = torrent.TorrentTask("bt_lock", "magnet:?xt=urn:btih:" + "0" * 40,
                            str(tmp_path), "movie", 0)
    t.status = "failed"
    t.error = "boom"

    entered = threading.Event()
    release = threading.Event()

    def _blocked_start():
        entered.set()                 # 已进入 start()，此时 retry 仍持有 _lock
        release.wait(5)
        return True

    t.start = _blocked_start          # 顶替真实 start，只用来占住锁

    result = {}

    def _do_retry():
        result["ok"] = t.retry()

    threading.Thread(target=_do_retry, daemon=True).start()
    assert entered.wait(5), "retry 没有进入 start()"

    cancelled = threading.Event()

    def _do_cancel():
        t.cancel()
        cancelled.set()

    threading.Thread(target=_do_cancel, daemon=True).start()
    # 锁被 retry 持有：cancel 必然无法完成
    assert not cancelled.wait(0.5), "cancel 插进了 retry 的临界区（状态会被覆写）"

    release.set()
    assert cancelled.wait(5), "锁释放后 cancel 没有执行"
    deadline = time.time() + 10
    while "ok" not in result and time.time() < deadline:
        time.sleep(0.01)
    assert "ok" in result, "retry 没有返回（死锁？）"
    assert t.status == "cancelled", "retry 期间并发 cancel 的状态被覆盖了"


def test_cancel_cannot_interleave_into_start_critical_section(tmp_path, monkeypatch):
    """cancel 不能插进 start() 的临界区。

    start() 会持锁拉取 .torrent 并建 session（网络 IO 也在这把锁里），这段期间
    cancel 必须等待，否则会出现「任务已取消却又重新开始」。本测试把这条不变量
    钉住：后续若有人把锁拆小或挪走，这里会立刻失败。
    """
    t = torrent.TorrentTask("bt_guard", "magnet:?xt=urn:btih:" + "0" * 40,
                            str(tmp_path), "movie", 0)
    t.status = "pending"

    entered = threading.Event()
    release = threading.Event()

    def _blocked_session():
        entered.set()
        release.wait(5)
        raise RuntimeError("测试里不建真实 session")

    monkeypatch.setattr(torrent, "_get_session", _blocked_session)

    started = threading.Event()

    def _do_start():
        t.start()
        started.set()

    threading.Thread(target=_do_start, daemon=True).start()
    assert entered.wait(5), "start() 没有进入临界区"

    cancelled = threading.Event()

    def _do_cancel():
        t.cancel()
        cancelled.set()

    threading.Thread(target=_do_cancel, daemon=True).start()
    assert not cancelled.wait(0.5), "cancel 插进了 start() 的临界区"

    release.set()
    assert started.wait(5), "临界区释放后 start() 没有返回"
    assert cancelled.wait(5), "锁释放后 cancel 没有执行"
    # start() 内部 _get_session 抛错会把状态置 failed，但紧随其后的 cancel 必须
    # 成为最终状态，而不是被 start() 的失败分支抢先
    assert t.status == "cancelled", t.status
    assert t._handle is None
class _FakeStatus:
    def __init__(self, finished=False, total=1000, done=1000):
        self.name = "movie.mkv"
        self.total = total
        self.total_done = done
        self.download_rate = 0
        self.num_seeds = 2
        self.num_peers = 3
        self.num_connections = 0
        self.progress = 1.0 if finished else 0.5
        self.is_finished = finished
        self.need_save_resume_data = False
        self.error = ""


class _FakeHandle:
    """只实现 TorrentTask 用到的那几个方法的假 torrent_handle。"""

    def __init__(self, finished=False):
        self._finished = finished
        self.paused = False

    def status(self):
        return _FakeStatus(finished=self._finished)

    def get_torrent_info(self):
        return types.SimpleNamespace(num_files=lambda: 1)

    def pause(self):
        self.paused = True

    def resume(self):
        self.paused = False


def test_cancel_cannot_be_overwritten_by_monitor_tick(tmp_path):
    """cancel() 删掉分片后，监控线程的 _tick() 不得再把状态改回 completed。

    _tick() 的完成判定来自 libtorrent 的 status()，它不知道调用方已经取消并删了
    文件。修复把整段刷新放进 self._lock，与 cancel/pause/start 互斥。
    """
    t = torrent.TorrentTask("bt_tick", "magnet:?xt=urn:btih:" + "0" * 40,
                            str(tmp_path), "movie", 0)
    t.status = "downloading"
    t._started_at = time.time()
    handle = _FakeHandle(finished=True)
    t._handle = handle
    t._session = types.SimpleNamespace(remove_torrent=lambda *a, **k: None)

    entered = threading.Event()
    release = threading.Event()
    real_status = handle.status

    def _blocking_status():
        entered.set()                 # 已进入 _tick 临界区，还没写 status
        release.wait(5)
        return real_status()

    handle.status = _blocking_status

    ticked = threading.Event()

    def _do_tick():
        t._tick()
        ticked.set()

    threading.Thread(target=_do_tick, daemon=True).start()
    assert entered.wait(5), "_tick 没有进入临界区"

    cancelled = threading.Event()

    def _do_cancel():
        t.cancel()
        cancelled.set()

    threading.Thread(target=_do_cancel, daemon=True).start()
    # cancel 已删分片并置 cancelled，必须等 _tick 让出临界区
    assert not cancelled.wait(0.5), "cancel 插进了 _tick 的临界区"
    assert t.status == "cancelled" or t.status == "downloading", t.status

    release.set()
    assert ticked.wait(5), "锁释放后 _tick 没有返回"
    assert cancelled.wait(5), "锁释放后 cancel 没有执行"
    assert t.status == "cancelled", "监控线程把已取消的任务改回了 completed"
    assert t._handle is None


def test_tick_reports_progress_and_completion(tmp_path):
    """_tick 锁内仍能正常刷新进度 / 完成态 / eta（防回归）。"""
    t = torrent.TorrentTask("bt_progress", "magnet:?xt=urn:btih:" + "0" * 40,
                            str(tmp_path), None, 0)
    t.status = "downloading"
    t._started_at = time.time()
    t._handle = _FakeHandle(finished=False)

    t._tick()
    assert t.status == "downloading"
    assert t.downloaded == 1000 and t.total_size == 1000
    assert t.speed == 0 and t.seeds == 2 and t.peers == 3
    assert t.eta == ""
    assert t.filename == "movie.mkv" and t.segments == 1

    t._handle = _FakeHandle(finished=True)
    t._tick()
    assert t.status == "completed" and t.progress == 100.0
