import pytest

from scheduler import COUNTDOWN_SECONDS, Scheduler, build_command


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeTask:
    def __init__(self, task_id, status="pending"):
        self.task_id = task_id
        self.status = status
        self.started = 0

    def start(self):
        self.started += 1
        self.status = "downloading"


class FakeManager:
    def __init__(self, tasks=()):
        self.tasks = {t.task_id: t for t in tasks}

    def get_all_tasks(self):
        return list(self.tasks.values())

    def get_task(self, task_id):
        return self.tasks.get(task_id)


def make(tasks=(), start=1000.0):
    """返回 (scheduler, clock, 命令记录, 提示音记录, 休眠记录)，全部副作用都是假的。"""
    clock = Clock(start)
    cmds, beeps, suspends = [], [], []
    s = Scheduler(manager=FakeManager(tasks), runner=cmds.append,
                  beeper=lambda: beeps.append(1), suspender=lambda: suspends.append(1),
                  clock=clock)
    return s, clock, cmds, beeps, suspends


# ---------------- 定时开始 ----------------
def test_scan_starts_only_due_tasks():
    a, b = FakeTask("t_1"), FakeTask("t_2")
    s, clock, *_ = make([a, b])
    s.schedule("t_1", 1000.0)
    s.schedule("t_2", 2000.0)
    clock.t = 1001.0
    out = s.scan()
    assert out["started"] == ["t_1"]
    assert a.started == 1 and a.status == "downloading"
    assert b.started == 0
    assert s.pending_at("t_1") is None and s.pending_at("t_2") == 2000.0


def test_scan_drops_tasks_that_are_no_longer_pending():
    a = FakeTask("t_1", status="cancelled")
    s, clock, *_ = make([a])
    s.schedule("t_1", 1000.0)
    clock.t = 1005.0
    assert s.scan()["started"] == []
    assert a.started == 0 and s.pending_at("t_1") is None


def test_scan_ignores_unknown_task_id():
    s, clock, *_ = make([])
    s.schedule("ghost", 1000.0)
    clock.t = 1001.0
    assert s.scan()["started"] == []


def test_unschedule_reports_whether_it_removed():
    s, *_ = make([])
    s.schedule("t_1", 10.0)
    assert s.unschedule("t_1") is True
    assert s.unschedule("t_1") is False


def test_list_pending_is_sorted_by_start_at():
    s, *_ = make([])
    s.schedule("t_2", 30.0)
    s.schedule("t_1", 10.0)
    assert s.list_pending() == [{"task_id": "t_1", "start_at": 10.0},
                                {"task_id": "t_2", "start_at": 30.0}]


# ---------------- 完成后动作 ----------------
def test_finish_action_arms_after_all_tasks_leave_busy_state():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, beeps, _ = make([t])
    s.set_finish_action("beep")
    s.scan()
    assert s.countdown_remaining() == 0          # 还在下载，不该倒计时
    t.status = "completed"
    s.scan()
    assert s.countdown_remaining() == COUNTDOWN_SECONDS
    clock.t += COUNTDOWN_SECONDS
    assert s.scan()["fired"] == "beep"
    assert beeps == [1] and cmds == []
    assert s.countdown_remaining() == 0


def test_finish_action_does_not_fire_when_nothing_ever_ran():
    done = FakeTask("t_1", status="completed")
    s, clock, cmds, _, suspends = make([done])
    s.set_finish_action("shutdown")
    s.scan()
    clock.t += COUNTDOWN_SECONDS * 2
    s.scan()
    assert cmds == [] and suspends == []          # 没见过「忙」，就不该突然关机
    assert s.countdown_remaining() == 0


def test_pause_counts_as_busy():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, _, _ = make([t])
    s.set_finish_action("shutdown")
    t.status = "paused"
    s.scan()
    clock.t += COUNTDOWN_SECONDS * 2
    s.scan()
    assert cmds == [] and s.countdown_remaining() == 0


def test_cancel_finish_action_stops_countdown():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, _, _ = make([t])
    s.set_finish_action("shutdown")
    t.status = "completed"
    s.scan()
    assert s.countdown_remaining() == COUNTDOWN_SECONDS
    res = s.cancel_finish_action()
    assert res == {"action": "shutdown", "cancelled": True}
    assert s.get_finish_action() == "none"
    clock.t += COUNTDOWN_SECONDS
    s.scan()
    assert cmds == []


def test_new_task_during_countdown_cancels_the_action():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, _, _ = make([t])
    s.set_finish_action("shutdown")
    t.status = "completed"
    s.scan()
    assert s.countdown_remaining() == COUNTDOWN_SECONDS
    t.status = "downloading"                 # 缓冲期内任务又跑起来了（重试 / 新添加）
    assert s.scan()["disarmed"] is True
    assert s.countdown_remaining() == 0
    clock.t += COUNTDOWN_SECONDS
    s.scan()
    assert cmds == []


def test_watch_stops_after_one_fire():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, _, _ = make([t])
    s.set_finish_action("shutdown")
    t.status = "completed"
    s.scan()
    clock.t += COUNTDOWN_SECONDS
    s.scan()
    assert len(cmds) == 1
    clock.t += COUNTDOWN_SECONDS * 2
    s.scan()
    assert len(cmds) == 1                         # 一次勾选只管一批任务


def test_suspend_uses_injected_suspender():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, _, suspends = make([t])
    s.set_finish_action("suspend")
    t.status = "completed"
    s.scan()
    clock.t += COUNTDOWN_SECONDS
    assert s.scan()["fired"] == "suspend"
    assert suspends == [1] and cmds == []


def test_scan_survives_failing_runner():
    t = FakeTask("t_1", status="downloading")
    clock = Clock()

    def boom(cmd):
        raise OSError("系统拒绝访问")

    s = Scheduler(manager=FakeManager([t]), runner=boom, clock=clock)
    s.set_finish_action("shutdown")
    t.status = "completed"
    s.scan()
    clock.t += COUNTDOWN_SECONDS
    assert s.scan()["fired"] == "shutdown"        # 异常被记录，不再向上抛
    assert s.get_finish_action() == "none"


def test_unknown_action_falls_back_to_none():
    s, *_ = make([])
    assert s.set_finish_action("reboot") == "none"
    assert s.get_finish_action() == "none"


def test_start_is_idempotent():
    s, *_ = make([])
    assert s.start() is True
    assert s.start() is False


# ---------------- 平台命令 ----------------
@pytest.mark.parametrize("plat,expected", [
    ("win32", ["shutdown", "/s", "/t", "5"]),
    ("darwin", ["osascript", "-e", 'tell app "System Events" to shut down']),
    ("linux", ["shutdown", "-h", "+1"]),
])
def test_build_command_shutdown(plat, expected):
    assert build_command("shutdown", plat) == expected


def test_build_command_suspend_and_beep():
    assert build_command("suspend", "win32") is None      # Windows 走 SetSuspendState
    assert build_command("suspend", "linux") == ["systemctl", "suspend"]
    assert build_command("beep", "win32") is None
    assert build_command("none", "win32") is None
