"""失败自动重试的单元测试：退避间隔、调度条件、用户操作让渡、次数上限。

不触网：用 FakeTimer 捕获 threading.Timer（不真正等待，由测试手动 fire），
任务对象用鸭子类型替身；管理器经 object.__new__ 绕过 __init__，
避免动到真实的 ~/.swiftdm/history.json 与常驻保存线程。
"""
import threading
import time

import pytest

import config
import downloader


class FakeTimer:
    """threading.Timer 替身：记录计划中的重试，由测试手动触发。"""

    created = []

    def __init__(self, delay, fn, args=None, kwargs=None):
        self.delay = delay
        self.fn = fn
        self.args = args or ()
        self.kwargs = kwargs or {}
        self.alive = True
        self.cancelled = False
        FakeTimer.created.append(self)

    def start(self):
        pass  # 不真正计时，fire 前一直视为存活

    def is_alive(self):
        return self.alive and not self.cancelled

    def cancel(self):
        self.cancelled = True

    def fire(self):
        if self.cancelled:
            return
        self.alive = False
        self.fn(*self.args, **self.kwargs)


class FakeTask:
    """最小任务替身：只有自动重试路径会用到的字段。"""

    def __init__(self, task_id="t1", filename="x.bin", status="failed"):
        self.task_id = task_id
        self.filename = filename
        self.status = status
        self.retry_calls = 0
        self.auto_retry_at = 0.0
        self.invalidations = 0

    def _invalidate_cache(self):
        self.invalidations += 1

    def retry(self):
        self.retry_calls += 1
        self.status = "downloading"
        return True


@pytest.fixture
def fake_timer(monkeypatch):
    FakeTimer.created = []
    monkeypatch.setattr(threading, "Timer", FakeTimer)
    return FakeTimer


def _make_manager(monkeypatch, limit):
    """半初始化管理器：只装自动重试路径依赖的字段。"""
    mgr = object.__new__(downloader.DownloadManager)
    mgr._tasks = {}
    mgr._counter = 0
    mgr._lock = threading.Lock()
    mgr._auto_retry_lock = threading.Lock()
    mgr._auto_retry_state = {}
    monkeypatch.setattr(downloader.config, "get",
                        lambda key: limit if key == "auto_retry" else None)
    return mgr


def test_auto_retry_delay_grows_then_caps():
    assert downloader.auto_retry_delay(0) == 30.0
    assert downloader.auto_retry_delay(1) == 60.0
    assert downloader.auto_retry_delay(2) == 120.0
    assert downloader.auto_retry_delay(3) == 300.0
    assert downloader.auto_retry_delay(99) == 300.0   # 封顶 5 分钟
    assert downloader.auto_retry_delay(-1) == 30.0    # 非法取值钳制到首档


@pytest.mark.parametrize("raw,expected", [
    (None, 0), ("", 0), ("abc", 0), (-3, 0), ("0", 0),
    ("2", 2), (3, 3), (99, downloader.AUTO_RETRY_MAX),
])
def test_auto_retry_limit_clamps_config(monkeypatch, raw, expected):
    mgr = object.__new__(downloader.DownloadManager)
    monkeypatch.setattr(config, "get", lambda key: raw if key == "auto_retry" else None)
    assert mgr._auto_retry_limit() == expected


def test_disabled_by_default_schedules_nothing(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=0)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    assert fake_timer.created == []
    assert mgr._auto_retry_state == {}


def test_schedule_ignores_non_failed_tasks(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=3)
    for status in ("pending", "downloading", "paused", "completed", "cancelled"):
        task = FakeTask("t_" + status, status=status)
        mgr._schedule_auto_retry(task)
    assert fake_timer.created == []
    assert mgr._auto_retry_state == {}


def test_first_failure_schedules_retry_with_base_backoff(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=3)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    assert len(fake_timer.created) == 1
    timer = fake_timer.created[0]
    assert timer.delay == 30.0
    assert timer.is_alive()
    assert mgr._auto_retry_state["t1"]["attempts"] == 0


def test_repeated_failure_notifications_do_not_stack_timers(fake_timer, monkeypatch):
    """多个分段线程几乎同时失败时只排一次重试。"""
    mgr = _make_manager(monkeypatch, limit=3)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    mgr._schedule_auto_retry(task)
    mgr._schedule_auto_retry(task)
    assert len(fake_timer.created) == 1


def test_firing_retries_failed_task(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=2)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    fake_timer.created[0].fire()
    assert task.retry_calls == 1
    assert mgr._auto_retry_state["t1"]["attempts"] == 1


def test_second_failure_uses_longer_backoff(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=3)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    fake_timer.created[0].fire()          # 第 1 次重试
    task.status = "failed"                # 重试后又失败（替身 retry 已把状态改为 downloading，这里手动改回）
    mgr._schedule_auto_retry(task)
    assert len(fake_timer.created) == 2
    assert fake_timer.created[1].delay == 60.0


def test_attempts_are_capped_by_limit(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=1)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    fake_timer.created[0].fire()          # 唯一一次机会
    assert task.retry_calls == 1
    task.status = "failed"
    mgr._schedule_auto_retry(task)        # 次数用尽：不再排程
    assert len(fake_timer.created) == 1


def test_firing_after_user_took_over_resets_counter(fake_timer, monkeypatch):
    """用户手动重试后定时器才到点：让渡，不重复重试，并清零计数。"""
    mgr = _make_manager(monkeypatch, limit=3)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    task.status = "downloading"           # 用户手动重试成功
    fake_timer.created[0].fire()
    assert task.retry_calls == 0
    assert "t1" not in mgr._auto_retry_state


def test_firing_after_task_removed_clears_state(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=3)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    mgr._tasks.pop("t1")                  # 任务被移除（不经过 remove_task，直接测守卫）
    fake_timer.created[0].fire()
    assert "t1" not in mgr._auto_retry_state
    assert task.retry_calls == 0


def test_remove_task_cancels_pending_timer(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=3)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    timer = fake_timer.created[0]
    mgr.remove_task("t1")
    assert timer.cancelled
    assert "t1" not in mgr._auto_retry_state
    timer.fire()
    assert task.retry_calls == 0


def test_clear_completed_cancels_pending_timers(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=3)
    mgr.save_history = lambda: None       # 避免写真实历史文件
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    timer = fake_timer.created[0]
    assert mgr.clear_completed() == 1     # failed 任务属于可清除范围
    assert timer.cancelled
    assert mgr._auto_retry_state == {}


def test_fire_on_failed_tolerates_missing_or_broken_callback():
    class Bare:
        task_id = "t"
        filename = "f"
        status = "failed"

    class Broken:
        task_id = "t"
        filename = "f"
        status = "failed"

        def on_failed(self, task):
            raise RuntimeError("boom")

    downloader.fire_on_failed(Bare())     # 没挂回调：静默跳过
    downloader.fire_on_failed(Broken())   # 回调抛异常：吞掉，不搞崩下载线程


def test_create_task_wires_on_failed_callback(fake_timer, monkeypatch, tmp_path):
    """真实 create_task 必须挂上回调，且失败时能排定一次自动重试。"""
    mgr = _make_manager(monkeypatch, limit=2)
    task = mgr.create_task("https://example.invalid/x.bin", str(tmp_path), segments=1)
    assert task.on_failed == mgr._schedule_auto_retry
    task.status = "failed"
    downloader.fire_on_failed(task)
    assert len(fake_timer.created) == 1
    calls = []

    def fake_retry():
        calls.append(task.task_id)
        task.status = "downloading"
        return True

    task.retry = fake_retry               # 打桩，避免真实联网探测
    fake_timer.created[0].fire()
    assert calls == [task.task_id]
    assert mgr._auto_retry_state[task.task_id]["attempts"] == 1
def test_schedule_sets_countdown_and_invalidates_cache(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=3)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    before = time.time()
    mgr._schedule_auto_retry(task)
    assert before + 29 <= task.auto_retry_at <= time.time() + 31
    assert task.invalidations >= 1          # to_dict 缓存必须失效，否则 UI 看不到倒计时


def test_fire_clears_countdown_before_retry(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=3)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    seen = []

    def recording_retry():
        seen.append(task.auto_retry_at)
        task.status = "downloading"
        return True

    task.retry = recording_retry
    fake_timer.created[0].fire()
    assert seen == [0.0]                    # 重试开始时倒计时应已清除
    assert task.auto_retry_at == 0.0


def test_cancel_auto_retry_clears_countdown(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=3)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    assert task.auto_retry_at > 0
    mgr._cancel_auto_retry("t1")
    assert task.auto_retry_at == 0.0


def test_fire_after_user_took_over_clears_countdown(fake_timer, monkeypatch):
    mgr = _make_manager(monkeypatch, limit=3)
    task = FakeTask("t1")
    mgr._tasks["t1"] = task
    mgr._schedule_auto_retry(task)
    task.status = "downloading"             # 用户手动重试
    fake_timer.created[0].fire()
    assert task.auto_retry_at == 0.0


def test_to_dict_exposes_auto_retry_at():
    import downloader
    t = downloader.DownloadTask("t1", "https://x/y.bin", "C:/tmp", "y.bin", 1)
    assert t.to_dict()["auto_retry_at"] == 0.0
    t.auto_retry_at = 123.5
    assert t.to_dict()["auto_retry_at"] == 123.5
