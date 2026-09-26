"""
下载完成提示音 —— 桌面端任务完成时的一声提醒（可关闭）。

与 throttle 一样的单例式模块：调用方负责持久化（config），这里只持有
当前选择与实际播放。fail-safe：任何异常都不能影响下载流程。
"""
import os
import sys
import threading

NOTIFY_SOUNDS = ("none", "beep", "system")
NOTIFY_SOUND_LABELS = {"none": "关闭", "beep": "提示音", "system": "系统音"}

_lock = threading.Lock()
_sound = "none"


def set_sound(name):
    """设置完成提示音；none/beep/system 之外的取值按 none（关闭）处理。"""
    global _sound
    value = name if name in NOTIFY_SOUNDS else "none"
    with _lock:
        _sound = value
    return value


def get_sound():
    with _lock:
        return _sound


def play():
    """按当前选择播放提示音；返回是否真的发声。任何平台/音频失败都静默降级。"""
    kind = get_sound()
    if kind == "none":
        return False
    try:
        if os.name == "nt":
            import winsound
            if kind == "system":
                winsound.PlaySound("SystemAsterisk", winsound.SND_ALIAS)
            else:
                for freq in (880, 660, 880):
                    winsound.Beep(freq, 220)
        else:
            sys.stdout.write("\a")
            sys.stdout.flush()
        return True
    except Exception:
        return False
