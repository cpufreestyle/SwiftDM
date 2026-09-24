"""
全局下载限速 —— 单令牌桶，所有分段线程与流媒体任务共用。

单位：字节/秒；0 表示不限速。设计成「调用方拿到多少字节就喂多少」，
不在这里做分片，也不持有任何任务引用。
"""
import threading
import time

_BURST_SECONDS = 0.5          # 允许的最大突发（半秒额度），避免空闲后一次冲出去

_lock = threading.Lock()
_rate = 0
_tokens = 0.0
_last = 0.0

_monotonic = time.monotonic   # 测试注入假时钟
_sleep = time.sleep


def set_rate(bytes_per_sec):
    """设置全局限速（字节/秒）。0 = 不限速；负数按 0 处理。"""
    global _rate, _tokens, _last
    try:
        value = int(bytes_per_sec)
    except (TypeError, ValueError):
        value = 0
    with _lock:
        _rate = value if value > 0 else 0
        _tokens = 0.0
        _last = 0.0
    return _rate


def get_rate():
    return _rate


def write_granularity():
    """限速时的建议写入粒度（半秒突发额度）；0 = 不限速。

    分段下载按此粒度切片写入：令牌桶总额不变，但监控线程每次采样看到的都是
    「一份粒度」的字节，避免 256KB 整块写入把瞬时速度读数放大成
    chunk_size / 0.5s 倍（限速 64KB/s 却在 UI 上显示 512KB/s）。
    """
    if _rate <= 0:
        return 0
    return max(1, int(_rate * _BURST_SECONDS))


def consume(nbytes):
    """消费 nbytes 字节的额度，不足则阻塞到该睡的时间点。必须在任务锁之外调用。"""
    global _tokens, _last
    if _rate <= 0 or nbytes <= 0:
        return
    wait = 0.0
    with _lock:
        now = _monotonic()
        if _last == 0.0:
            _last = now
        burst = _rate * _BURST_SECONDS
        _tokens = min(burst, _tokens + (now - _last) * _rate)
        _last = now
        if _tokens >= nbytes:
            _tokens -= nbytes
        else:
            wait = (nbytes - _tokens) / _rate
            _tokens = 0.0
            # 预支等待时间，避免多线程同时排队时重复计息
            _last += wait
    if wait > 0:
        _sleep(wait)
