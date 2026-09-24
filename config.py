"""
SwiftDM 共享配置 —— 各模块（Web API / 桌面 UI / 浏览器监控）统一读写，
并持久化到 ~/.swiftdm/config.json，重启后仍生效。

线程安全：多个下载线程与 UI 线程并发读写，内部加锁 + 原子写盘。
"""
import json
import os
import tempfile
import threading

_CONFIG_DIR = os.environ.get(
    "SWIFTDM_CONFIG_DIR",
    os.path.join(os.path.expanduser("~"), ".swiftdm"),
)
CONFIG_PATH = os.path.join(_CONFIG_DIR, "config.json")

_lock = threading.Lock()
_cache = None  # 惰性加载的完整配置字典

_DEFAULTS = {
    "download_dir": os.path.join(os.path.expanduser("~"), "Downloads", "IDM_Downloads"),
    "proxy_mode": "env",
    "segments": 8,
    "monitor_enabled": True,
}


def _load():
    """读取磁盘配置并与默认值合并（坏文件/缺字段都安全回退）。"""
    global _cache
    if _cache is None:
        merged = dict(_DEFAULTS)
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                for k in _DEFAULTS:
                    if k in stored and stored[k] is not None:
                        merged[k] = stored[k]
        except (OSError, ValueError):
            pass  # 首次运行或文件损坏：使用默认值
        _cache = merged
    return _cache


def get(key):
    """读取配置项（自动合并默认值）。"""
    with _lock:
        return _load().get(key, _DEFAULTS.get(key))


def set(key, value):
    """写入单个配置项并持久化（原子写：临时文件 + rename）。"""
    with _lock:
        cfg = _load()
        cfg[key] = value
        try:
            os.makedirs(_CONFIG_DIR, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=_CONFIG_DIR, prefix=".config-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)  # 原子替换，避免写一半崩溃留下坏文件
        except OSError as e:
            # 持久化失败不阻塞运行（只影响重启后的记忆）
            print(f"[Config] 配置写入失败（仅本次会话生效）: {e}")
    return value


def get_download_dir():
    """当前下载目录（所有入口统一走这里：Web 添加 / 浏览器捕获 / 桌面 UI）。"""
    return get("download_dir")


def set_download_dir(path):
    return set("download_dir", path)
