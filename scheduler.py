"""
下载调度 —— 定时开始 + 一批任务全部结束后的动作（关机 / 休眠 / 提示音）。

所有状态推进都集中在 scan()：扫描线程按点调它，测试直接调它。
这样倒计时不依赖真实 sleep，也不会有第二个地方偷偷改 fire_at。
"""
import logging
import sys
import threading
import time

FINISH_ACTIONS = ("none", "shutdown", "suspend", "beep")
COUNTDOWN_SECONDS = 60     # 全部完成后再等 60 秒，给人留出取消时间
SCAN_INTERVAL = 5          # 扫描节拍：定时开始的延迟不会超过它
OS_TAIL_SECONDS = 5        # 命令下发后操作系统自己的宽限（Windows shutdown /t）
BUSY_STATES = ("pending", "downloading", "paused")
DONE_STATES = ("completed", "failed", "cancelled")

logger = logging.getLogger("swiftdm.scheduler")


def build_command(action, platform=None):
    """把动作翻译成外部命令；返回 None 表示不需要命令（走系统 API 或本进程内处理）。"""
    plat = platform or sys.platform
    if action == "shutdown":
        if plat.startswith("win"):
            return ["shutdown", "/s", "/t", str(OS_TAIL_SECONDS)]
        if plat == "darwin":
            return ["osascript", "-e", 'tell app "System Events" to shut down']
        return ["shutdown", "-h", "+1"]
    if action == "suspend":
        if plat.startswith("win"):
            return None
        return ["systemctl", "suspend"]
    return None


def _default_runner(cmd):
    import os
    import subprocess
    kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW
    subprocess.Popen(cmd, **kwargs)


def _default_beeper():
    import os
    if os.name == "nt":
        import winsound
        for freq in (880, 660, 880):
            winsound.Beep(freq, 220)
    else:
        sys.stdout.write("\a")
        sys.stdout.flush()


def _default_suspender():
    import os
    if os.name == "nt":
        import ctypes
        ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
        return
    cmd = build_command("suspend", sys.platform)
    if cmd:
        _default_runner(cmd)


class Scheduler:
    def __init__(self, manager=None, runner=None, beeper=None, suspender=None,
                 clock=time.time):
        self._lock = threading.Lock()
        self._pending = {}           # task_id -> start_at（epoch 秒）
        self._finish_action = "none"
        self._watching = False       # 是否仍在等待「本批任务结束」
        self._saw_busy = False       # 勾选之后是否真的忙过
        self._fire_at = None         # 倒计时到点时刻
        self._manager = manager      # 惰性取全局 manager，测试可注入假的
        self._runner = runner or _default_runner
        self._beeper = beeper or _default_beeper
        self._suspender = suspender or _default_suspender
        self._clock = clock
        self._started = False

    # ---------------- 任务表 ----------------
    def _mgr(self):
        if self._manager is None:
            from downloader import manager
            self._manager = manager
        return self._manager

    # ---------------- 定时开始 ----------------
    def schedule(self, task_id, start_at):
        with self._lock:
            self._pending[str(task_id)] = float(start_at)

    def unschedule(self, task_id):
        with self._lock:
            return self._pending.pop(str(task_id), None) is not None

    def pending_at(self, task_id):
        with self._lock:
            return self._pending.get(str(task_id))

    def list_pending(self):
        with self._lock:
            return self._pending_list()

    def _pending_list(self):
        return sorted(({"task_id": k, "start_at": v} for k, v in self._pending.items()),
                      key=lambda d: d["start_at"])

    # ---------------- 完成后动作 ----------------
    def get_finish_action(self):
        with self._lock:
            return self._finish_action

    def set_finish_action(self, action):
        value = action if action in FINISH_ACTIONS else "none"
        # 勾选那一刻就已经在下载中的任务也算「忙过」：用户勾选时任务正在跑，
        # 全部结束后理应触发倒计时。反之列表里全是已完成任务时不武装，
        # 避免「任务早结束后才勾选」被误解成立即关机。
        saw_busy = self._any_busy()
        with self._lock:
            self._finish_action = value
            self._watching = value != "none"
            self._saw_busy = saw_busy
            self._fire_at = None
        return value

    def _any_busy(self):
        try:
            tasks = self._mgr().get_all_tasks()
        except Exception:
            return False
        return any(getattr(t, "status", "") in BUSY_STATES for t in tasks)

    def countdown_remaining(self, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            return self._remaining(now)

    def _remaining(self, now):
        if self._fire_at is None:
            return 0
        return max(0, int(round(self._fire_at - now)))

    def cancel_finish_action(self):
        with self._lock:
            action = self._finish_action
            was_armed = self._fire_at is not None
            self._finish_action = "none"
            self._watching = False
            self._saw_busy = False
            self._fire_at = None
        return {"action": action, "cancelled": was_armed}

    def status(self, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            return {
                "finish_action": self._finish_action,
                "fire_at": self._fire_at,
                "remaining": self._remaining(now),
                "scheduled": self._pending_list(),
            }

    # ---------------- 状态推进 ----------------
    def scan(self, now=None):
        now = self._clock() if now is None else now
        due, fire, armed, disarmed = [], None, False, False
        with self._lock:
            for tid, at in list(self._pending.items()):
                if at <= now:
                    self._pending.pop(tid, None)
                    due.append(tid)

            if self._watching:
                busy = done = False
                for t in self._mgr().get_all_tasks():
                    st = getattr(t, "status", "")
                    if st in BUSY_STATES:
                        busy = True
                    elif st in DONE_STATES:
                        done = True
                if busy:
                    self._saw_busy = True
                    if self._fire_at is not None:
                        self._fire_at = None        # 缓冲期内又来了新任务 -> 撤销倒计时
                        disarmed = True
                elif done and self._saw_busy and self._fire_at is None:
                    self._fire_at = now + COUNTDOWN_SECONDS
                    armed = True

            if self._fire_at is not None and now >= self._fire_at:
                fire = self._finish_action
                self._fire_at = None
                self._watching = False
                self._saw_busy = False
                self._finish_action = "none"

        started = self._start_now(due)          # 锁外执行副作用
        if fire:
            self._fire(fire)
        return {"started": started, "armed": armed, "disarmed": disarmed, "fired": fire}

    def _start_now(self, task_ids):
        started = []
        for tid in task_ids:
            task = self._mgr().get_task(tid)
            if task is None or getattr(task, "status", None) != "pending":
                continue                        # 已取消/已删除的到点任务直接丢弃登记
            try:
                task.start()
                started.append(tid)
            except Exception as e:
                logger.warning("定时启动任务 %s 失败: %s", tid, e)
        return started

    def _fire(self, action):
        try:
            if action == "beep":
                self._beeper()
                return
            if action == "suspend":
                self._suspender()
                return
            cmd = build_command(action, sys.platform)
            if cmd:
                self._runner(cmd)
        except Exception as e:
            logger.warning("完成后动作 %s 执行失败: %s", action, e)

    # ---------------- 扫描线程 ----------------
    def start(self):
        with self._lock:
            if self._started:
                return False
            self._started = True
        threading.Thread(target=self._loop, daemon=True, name="swiftdm-scheduler").start()
        return True

    def _loop(self):
        while True:
            time.sleep(SCAN_INTERVAL)
            try:
                self.scan()
            except Exception:
                logger.warning("定时扫描异常", exc_info=True)


scheduler = Scheduler()
