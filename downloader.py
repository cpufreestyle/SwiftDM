"""
IDM 风格下载引擎 —— 多线程分段下载、暂停/恢复
"""
import os
import re
import time
import shutil
import threading
import logging
import requests
from urllib.parse import urlparse, unquote

logger = logging.getLogger("SwiftDM")

# 代理模式（运行时可通过 set_proxy_mode() 切换）：
#   "env"    —— 继承系统代理（HTTP_PROXY/HTTPS_PROXY），默认。GitHub 等资源必须走代理。
#   "direct" —— 彻底忽略代理，强制直连（系统代理宕机时用）。
#   其它值   —— 视为自定义代理地址（如 "http://127.0.0.1:7890" 或 "socks5://..."），
#              同时作为 http/https 代理。
# 环境变量 SWIFTDM_USE_PROXY 可预设初始模式。
_PROXY_MODE = os.environ.get("SWIFTDM_USE_PROXY", "env").strip().lower()

# 全局 Session（所有下载共用，便于连接复用）
_SESSION = requests.Session()


def _apply_proxy_mode(mode):
    """根据模式配置全局 Session 的代理行为。"""
    global _PROXY_MODE
    _PROXY_MODE = mode.strip().lower() if isinstance(mode, str) else "env"
    _SESSION.proxies.clear()
    if _PROXY_MODE == "direct":
        # 强制直连：忽略环境变量代理
        _SESSION.trust_env = False
        _SESSION.proxies.update({"http": None, "https": None})
    elif _PROXY_MODE in ("env", "", "system"):
        # 走系统代理（继承 HTTP_PROXY/HTTPS_PROXY）
        _SESSION.trust_env = True
    else:
        # 自定义代理地址
        _SESSION.trust_env = False
        _SESSION.proxies.update({"http": _PROXY_MODE, "https": _PROXY_MODE})


def set_proxy_mode(mode):
    """运行时切换代理模式（供 UI / API 调用）。"""
    _apply_proxy_mode(mode)
    logger.info("下载代理模式已切换为: %s", _PROXY_MODE)


def get_proxy_mode():
    return _PROXY_MODE


# 初始化
_apply_proxy_mode(_PROXY_MODE)


def _alt_proxy_mode():
    """返回与当前模式互补的模式，用于连接失败时自动回退尝试。"""
    if _PROXY_MODE == "direct":
        return "env"
    return "direct"


def _is_proxy_down_error(exc):
    """判断异常是否为「代理服务器未启动/不可达」（连接被拒绝）。"""
    s = str(exc)
    return ("ProxyError" in s and
            ("10061" in s or "refused" in s or "Unable to connect to proxy" in s
             or "errno 61" in s or "Connection refused" in s))


def _friendly_error(exc):
    """把底层异常转成用户可读的中文错误描述。"""
    s = str(exc)
    if _is_proxy_down_error(exc):
        return ("代理服务器未启动或不可达（默认 127.0.0.1:7897）。"
                "请开启 Clash，或在设置中把「下载代理」改为「直连(direct)」。")
    if "403" in s or "401" in s:
        return "链接已失效（签名过期/无权限）。请从下载源重新复制最新链接后再试。"
    if ("ConnectionError" in s or "Timeout" in s or "ConnectTimeout" in s
            or "远程主机" in s or "timed out" in s or "NameResolutionError" in s
            or "getaddrinfo" in s):
        return "无法连接下载服务器，请检查网络是否连通或代理设置是否正确。"
    return s


class DownloadTask:
    """单个下载任务"""

    def __init__(self, task_id, url, save_dir, filename=None, segments=8):
        self.task_id = task_id
        self.url = url
        self.save_dir = save_dir
        self.segments = segments  # 分段数
        self.status = "pending"   # pending | downloading | paused | completed | failed | cancelled
        self.progress = 0.0       # 0-100
        self.total_size = 0
        self.downloaded = 0
        self.speed = 0.0          # bytes/s
        self.eta = ""             # 预计剩余时间
        self.error = ""
        self.added_at = time.time()

        # 确定文件名
        if filename:
            self.filename = filename
        else:
            self.filename = self._extract_filename(url)

        self.filepath = os.path.join(save_dir, self.filename)
        self._tmp_dir = os.path.join(save_dir, f".{self.filename}.parts")

        # 内部控制
        self._lock = threading.Lock()
        self._pause_event = threading.Event()
        self._pause_event.set()  # 初始不暂停
        self._threads = []
        self._segment_progress = []   # 每段的已下载字节
        self._segment_offsets = []    # 每段的 [(start, end), ...]
        self._start_time = 0
        self._last_check_bytes = 0
        self._last_check_time = 0

    def _extract_filename(self, url):
        """从 URL 提取文件名"""
        parsed = urlparse(url)
        path = unquote(parsed.path)
        name = os.path.basename(path)
        if not name or "." not in name:
            # 尝试从 Content-Disposition 获取（在实际下载时）
            name = f"download_{int(time.time())}"
        return name

    def _fetch_info(self):
        """获取文件大小和最终文件名。

        主代理模式失败时的处理：
          - 若失败原因是「代理服务器未启动/不可达」，本次会话直接切到直连(direct)，
            避免后续每个分段都先浪费一次代理重试；若直连仍失败则给出清晰报错。
          - 否则用互补模式再尝试一次。
        """
        try:
            return self._do_fetch_info()
        except Exception as e:
            if _is_proxy_down_error(e):
                logger.warning("检测到代理不可用(%s)，本次会话自动切换为直连: %s", _PROXY_MODE, e)
                set_proxy_mode("direct")
                try:
                    return self._do_fetch_info()
                except Exception as e2:
                    self.error = _friendly_error(e2)
                    logger.error("获取文件信息失败(直连): %s | 错误: %s", self.url, e2)
                    return False
            alt = _alt_proxy_mode()
            logger.warning("_fetch_info 主模式(%s)失败: %s，尝试互补模式 %s",
                           _PROXY_MODE, e, alt)
            try:
                with _SwitchedProxy(alt):
                    return self._do_fetch_info()
            except Exception as e2:
                self.error = _friendly_error(e2)
                logger.error("获取文件信息失败(含回退): %s | 错误: %s", self.url, e2)
                return False

    def _do_fetch_info(self):
        """用当前全局 Session 的代理模式做 HEAD 探测，提取大小/文件名。"""
        resp = _SESSION.head(self.url, timeout=15, allow_redirects=True,
                             headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        if resp.status_code in (401, 403):
            raise Exception("HTTP 403")  # 链接签名过期/无权限，由 _friendly_error 转译
        if resp.status_code not in (200, 206):
            raise Exception(f"HTTP {resp.status_code}")
        content_length = resp.headers.get("Content-Length")
        if content_length:
            self.total_size = int(content_length)

        # 尝试从 Content-Disposition 获取文件名
        cd = resp.headers.get("Content-Disposition", "")
        match = re.search(r'filename[*]?=["\']?([^"\';]+)', cd, re.IGNORECASE)
        if match:
            cd_name = unquote(match.group(1).strip())
            if cd_name:
                self.filename = cd_name
                self.filepath = os.path.join(self.save_dir, self.filename)
                self._tmp_dir = os.path.join(self.save_dir, f".{self.filename}.parts")

        return True

    def _calc_segments(self):
        """计算分段区间"""
        if self.total_size <= 0:
            # 未知大小，单段下载
            self.segments = 1
            self._segment_offsets = [(0, 0)]
            self._segment_progress = [0]
            return

        seg_size = self.total_size // self.segments
        self._segment_offsets = []
        for i in range(self.segments):
            start = i * seg_size
            end = start + seg_size - 1 if i < self.segments - 1 else self.total_size - 1
            self._segment_offsets.append((start, end))
        self._segment_progress = [0] * self.segments

    def _download_segment(self, idx):
        """下载单个分段"""
        start, end = self._segment_offsets[idx]
        seg_file = os.path.join(self._tmp_dir, f"part_{idx:04d}")

        # 恢复：从已有部分继续
        existing = 0
        restore_from = start
        if os.path.exists(seg_file):
            existing = os.path.getsize(seg_file)
            restore_from = start + existing

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if self.total_size > 0:
            headers["Range"] = f"bytes={restore_from}-{end}"

        # 主模式先试；失败（状态码不可达 / 连接错误）再用互补模式重试一次
        try:
            self._stream_segment(idx, headers, seg_file, existing, start, end)
        except Exception as e:
            if _is_proxy_down_error(e):
                # 代理宕机：整个会话切到直连，后续分段不再重复代理重试
                logger.warning("分段 %d 检测到代理不可用(%s)，切换为直连: %s",
                               idx, _PROXY_MODE, e)
                set_proxy_mode("direct")
                try:
                    self._stream_segment(idx, headers, seg_file, existing, start, end)
                    logger.info("分段 %d 经直连下载成功", idx)
                    return
                except Exception as e2:
                    logger.error("分段 %d 直连仍失败: %s", idx, e2)
                    if self.status not in ("cancelled", "paused"):
                        raise Exception(_friendly_error(e2))
            alt = _alt_proxy_mode()
            logger.warning("分段 %d 主模式(%s)失败: %s，尝试互补模式 %s",
                           idx, _PROXY_MODE, e, alt)
            try:
                with _SwitchedProxy(alt):
                    self._stream_segment(idx, headers, seg_file, existing, start, end)
                logger.info("分段 %d 经互补模式 %s 下载成功", idx, alt)
                return
            except Exception as e2:
                logger.error("分段 %d 互补模式 %s 仍失败: %s", idx, alt, e2)
                if self.status not in ("cancelled", "paused"):
                    raise Exception(_friendly_error(e2))

    def _stream_segment(self, idx, headers, seg_file, existing, start, end):
        """用当前全局 Session 的代理模式下载并写入一个分段（不含失败重试）。"""
        resp = _SESSION.get(self.url, headers=headers, stream=True, timeout=30,
                            allow_redirects=True)
        if resp.status_code in (401, 403):
            raise Exception("HTTP 403")  # 链接签名过期/无权限
        if resp.status_code not in (200, 206):
            raise Exception(f"HTTP {resp.status_code}")
        mode = "ab" if existing > 0 else "wb"
        with open(seg_file, mode) as f:
            for chunk in resp.iter_content(chunk_size=256 * 1024):
                if not self._pause_event.is_set():
                    # 暂停中，等待恢复或取消
                    while not self._pause_event.is_set():
                        time.sleep(0.2)
                        if self.status in ("cancelled", "failed"):
                            return
                if self.status in ("cancelled", "failed"):
                    return
                f.write(chunk)
                with self._lock:
                    self._segment_progress[idx] += len(chunk)

    def _all_segments_done(self):
        """检查所有分段是否下载完成"""
        for idx in range(self.segments):
            start, end = self._segment_offsets[idx]
            seg_file = os.path.join(self._tmp_dir, f"part_{idx:04d}")
            if not os.path.exists(seg_file):
                return False
            seg_size = os.path.getsize(seg_file)
            expected = (end - start + 1) if self.total_size > 0 else 0
            if expected > 0 and seg_size < expected:
                return False
        return True

    def _assemble_file(self):
        """合并分段文件"""
        with open(self.filepath, "wb") as out:
            for idx in range(self.segments):
                seg_file = os.path.join(self._tmp_dir, f"part_{idx:04d}")
                if os.path.exists(seg_file):
                    with open(seg_file, "rb") as inf:
                        while True:
                            data = inf.read(1024 * 1024)
                            if not data:
                                break
                            out.write(data)

        # 清理临时文件
        if os.path.exists(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _monitor_progress(self):
        """监控下载进度和速度"""
        while self.status == "downloading":
            time.sleep(0.5)
            with self._lock:
                total_downloaded = sum(self._segment_progress)

            now = time.time()
            elapsed = now - self._start_time
            if elapsed > 0:
                if self._last_check_time > 0:
                    delta_bytes = total_downloaded - self._last_check_bytes
                    delta_time = now - self._last_check_time
                    self.speed = delta_bytes / delta_time if delta_time > 0 else 0
                else:
                    self.speed = total_downloaded / elapsed

            self._last_check_bytes = total_downloaded
            self._last_check_time = now
            self.downloaded = total_downloaded

            if self.total_size > 0:
                self.progress = min(100, (total_downloaded / self.total_size) * 100)
                if self.speed > 0:
                    remain_bytes = self.total_size - total_downloaded
                    remain_secs = remain_bytes / self.speed
                    if remain_secs < 60:
                        self.eta = f"{int(remain_secs)}s"
                    elif remain_secs < 3600:
                        self.eta = f"{int(remain_secs // 60)}m {int(remain_secs % 60)}s"
                    else:
                        h = int(remain_secs // 3600)
                        m = int((remain_secs % 3600) // 60)
                        self.eta = f"{h}h {m}m"
            else:
                self.progress = 0

            # 检查是否所有分段都已下载完成
            if self._all_segments_done():
                try:
                    self._assemble_file()
                    self.progress = 100.0
                    self.downloaded = self.total_size if self.total_size > 0 else total_downloaded
                    self.status = "completed"
                    self.speed = 0.0
                    self.eta = ""
                except Exception as e:
                    self.status = "failed"
                    self.error = f"合并文件失败: {e}"
                    logger.error("合并文件失败: %s | 错误: %s", self.filename, e, exc_info=True)
                else:
                    logger.info("下载完成: %s (%.2f MB)", self.filename,
                                (self.total_size if self.total_size > 0 else total_downloaded) / 1024 / 1024)
                return

    def start(self):
        """启动下载"""
        if self.status in ("downloading", "completed"):
            return

        logger.info("开始下载任务: %s  (%s)", self.filename, self.url)
        self.status = "downloading"
        self._pause_event.set()

        # 获取文件信息
        if not self._fetch_info():
            self.status = "failed"
            logger.error("任务初始化失败，已停止: %s | 原因: %s", self.filename, self.error)
            return

        # 计算分段
        self._calc_segments()

        # 创建临时目录
        os.makedirs(self._tmp_dir, exist_ok=True)

        # 如果文件已完整下载
        if self.total_size > 0 and os.path.exists(self.filepath):
            existing_size = os.path.getsize(self.filepath)
            if existing_size >= self.total_size:
                self.progress = 100
                self.status = "completed"
                self.downloaded = self.total_size
                return

        self._start_time = time.time()
        self._last_check_bytes = 0
        self._last_check_time = 0

        # 启动分段下载线程
        self._threads = []
        for idx in range(self.segments):
            t = threading.Thread(target=self._run_segment, args=(idx,), daemon=True)
            t.start()
            self._threads.append(t)

        # 启动进度监控线程
        monitor = threading.Thread(target=self._monitor_progress, daemon=True)
        monitor.start()
        self._threads.append(monitor)

    def _run_segment(self, idx):
        """在线程中运行分段下载"""
        try:
            self._download_segment(idx)
        except Exception as e:
            with self._lock:
                if self.status not in ("paused", "cancelled", "completed"):
                    self.status = "failed"
                    self.error = str(e)
                    logger.error("任务失败: %s | 分段%d 错误: %s", self.filename, idx, e)

    def pause(self):
        """暂停下载"""
        self.status = "paused"
        self._pause_event.clear()

    def resume(self):
        """恢复下载"""
        if self.status != "paused":
            return
        self.status = "downloading"
        self._pause_event.set()

        # 重新启动分段线程
        self._threads = []
        for idx in range(self.segments):
            t = threading.Thread(target=self._run_segment, args=(idx,), daemon=True)
            t.start()
            self._threads.append(t)

        # 进度监控
        monitor = threading.Thread(target=self._monitor_progress, daemon=True)
        monitor.start()
        self._threads.append(monitor)

    def cancel(self):
        """取消下载"""
        self.status = "cancelled"
        self._pause_event.set()  # 唤醒等待的线程

        # 清理临时文件
        if os.path.exists(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "filename": self.filename,
            "url": self.url,
            "status": self.status,
            "progress": round(self.progress, 1),
            "total_size": self.total_size,
            "downloaded": self.downloaded,
            "speed": self.speed,
            "eta": self.eta,
            "error": self.error,
            "segments": self.segments,
        }


class DownloadManager:
    """下载管理器"""

    def __init__(self):
        self._tasks = {}      # task_id -> DownloadTask
        self._counter = 0
        self._lock = threading.Lock()

    def create_task(self, url, save_dir, filename=None, segments=8):
        with self._lock:
            self._counter += 1
            task_id = f"dl_{self._counter}"
            task = DownloadTask(task_id, url, save_dir, filename, segments)
            self._tasks[task_id] = task
            return task

    def get_task(self, task_id):
        return self._tasks.get(task_id)

    def get_all_tasks(self):
        return list(self._tasks.values())

    def remove_task(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if task and task.status in ("downloading", "paused"):
                task.cancel()
            self._tasks.pop(task_id, None)

    def clear_completed(self):
        with self._lock:
            completed = [tid for tid, t in self._tasks.items() if t.status in ("completed", "cancelled", "failed")]
            for tid in completed:
                self._tasks.pop(tid, None)

    def get_stats(self):
        tasks = self.get_all_tasks()
        total_speed = sum(t.speed for t in tasks if t.status == "downloading")
        active = sum(1 for t in tasks if t.status == "downloading")
        completed = sum(1 for t in tasks if t.status == "completed")
        failed = sum(1 for t in tasks if t.status == "failed")
        paused = sum(1 for t in tasks if t.status == "paused")
        return {
            "total_speed": total_speed,
            "active": active,
            "completed": completed,
            "failed": failed,
            "paused": paused,
            "total": len(tasks),
        }


class _SwitchedProxy:
    """上下文管理器：临时把全局 Session 切到指定代理模式，退出时恢复原模式。"""

    def __init__(self, temp_mode):
        self.temp_mode = temp_mode
        self._prev = _PROXY_MODE

    def __enter__(self):
        _apply_proxy_mode(self.temp_mode)
        return self

    def __exit__(self, *exc):
        _apply_proxy_mode(self._prev)
        return False


# 全局实例
manager = DownloadManager()
