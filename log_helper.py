"""
SwiftDM 日志工具 —— 统一日志配置（文件 + 控制台），并提供把日志推送到 UI 的 Handler
"""
import logging
import os

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "swiftdm.log")
LOG_FORMAT = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

_configured = False


def setup_logging(level=logging.INFO):
    """配置 SwiftDM 日志：同时输出到控制台和 swiftdm.log 文件（幂等）。"""
    global _configured
    logger = logging.getLogger("SwiftDM")
    if _configured:
        return logger
    _configured = True

    logger.setLevel(level)
    logger.propagate = False

    # 控制台
    ch = logging.StreamHandler()
    ch.setFormatter(LOG_FORMAT)
    logger.addHandler(ch)

    # 文件
    try:
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(LOG_FORMAT)
        logger.addHandler(fh)
    except Exception:
        pass

    logger.info("SwiftDM 日志已初始化 -> %s", LOG_FILE)
    return logger


class QtLogHandler(logging.Handler):
    """把日志通过 Qt 信号推送到界面（用于在 UI 里实时显示）。"""

    def __init__(self, signal):
        super().__init__()
        self.signal = signal

    def emit(self, record):
        try:
            msg = self.format(record)
            self.signal.emit(msg)
        except Exception:
            pass
