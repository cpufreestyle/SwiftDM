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
from requests.adapters import HTTPAdapter
from urllib.parse import urlparse, unquote

logger = logging.getLogger("SwiftDM")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# 代理模式（运行时可通过 set_proxy_mode() 切换）：
#   "env"    —— 继承系统代理（HTTP_PROXY/HTTPS_PROXY），默认。GitHub 等资源必须走代理。
#   "direct" —— 彻底忽略代理，强制直连（系统代理宕机时用）。
#   其它值   —— 视为自定义代理地址（如 "http://127.0.0.1:7890" 或 "socks5://..."），
#              同时作为 http/https 代理。
# 环境变量 SWIFTDM_USE_PROXY 可预设初始模式。
_PROXY_MODE = os.environ.get("SWIFTDM_USE_PROXY", "env").strip().lower()

# 全局 Session（所有下载共用，便于连接复用；连接池加大以支持多线程分段并发）
_SESSION = requests.Session()
_POOL = HTTPAdapter(pool_connections=32, pool_maxsize=32, max_retries=0)
_SESSION.mount("http://", _POOL)
_SESSION.mount("https://", _POOL)


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
        self._threads = []
        self._segment_progress = []   # 每段的已下载字节
        self._segment_offsets = []    # 每段的 [(start, end), ...]
        self._seg_done = []           # 每段是否真正下载完成（由分段线程置位）
        self._gen = 0                 # 代数计数器：暂停/取消时 +1，旧线程据此退出，避免新旧线程同时写同一文件
        self._range_supported = False  # 服务器是否支持 Range 分段/续传
        self._completion_handled = False  # 防止监控线程重复合并文件
        self._start_time = 0
        self._last_check_bytes = 0
        self._last_check_time = 0
        self._dict_cache = None  # to_dict() 缓存，避免每 500ms 重复构造 dict（UI/SSE 高频调用）

    def _invalidate_cache(self):
        """字段变化时使 to_dict() 缓存失效。"""
        self._dict_cache = None

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
        """探测文件信息：大小、最终文件名、是否支持 Range 分段。

        用 GET + Range: bytes=0-0 代替 HEAD（部分服务器不支持 HEAD 或谎报大小）：
          - 206 + Content-Range  → 支持 Range，可精确拿到总大小
          - 200                 → 不支持 Range，只能单线程整文件下载（此时若仍多段并发，
                                  每段都会拿到整个文件，合并后文件损坏/体积翻倍）
        """
        resp = _SESSION.get(self.url, headers={"User-Agent": _UA, "Range": "bytes=0-0"},
                            stream=True, timeout=15, allow_redirects=True)
        try:
            if resp.status_code == 416:
                # 个别服务器对 Range 探测返回 416：退回普通 GET 探测
                resp.close()
                resp = _SESSION.get(self.url, headers={"User-Agent": _UA},
                                    stream=True, timeout=15, allow_redirects=True)

            if resp.status_code in (401, 403):
                raise Exception("HTTP 403")  # 链接签名过期/无权限，由 _friendly_error 转译
            if resp.status_code not in (200, 206):
                raise Exception(f"HTTP {resp.status_code}")

            if resp.status_code == 206:
                self._range_supported = True
                # Content-Range: bytes 0-0/12345 → 最后一段是总大小
                m = re.match(r"bytes\s+\d+-\d+/(\d+)", resp.headers.get("Content-Range", ""))
                if m:
                    self.total_size = int(m.group(1))
            else:
                self._range_supported = False
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
        finally:
            resp.close()

        return True

    def _calc_segments(self):
        """计算分段区间。

        未知大小或服务器不支持 Range 时强制单段：
          - 未知大小：无法分段；
          - 不支持 Range：多段请求都会返回整个文件，必须单段下载。
        """
        if self.total_size <= 0 or not self._range_supported:
            self.segments = 1
            self._segment_offsets = [(0, max(self.total_size - 1, 0))]
            self._segment_progress = [0]
            return

        seg_size = self.total_size // self.segments
        self._segment_offsets = []
        for i in range(self.segments):
            start = i * seg_size
            end = start + seg_size - 1 if i < self.segments - 1 else self.total_size - 1
            self._segment_offsets.append((start, end))
        self._segment_progress = [0] * self.segments

    def _segment_complete(self, idx):
        """检查某个分段文件是否已下载完整（未知大小时无法判断，返回 False，靠流正常结束判定）。"""
        if self.total_size <= 0:
            return False
        start, end = self._segment_offsets[idx]
        seg_file = os.path.join(self._tmp_dir, f"part_{idx:04d}")
        expected = end - start + 1
        return os.path.exists(seg_file) and os.path.getsize(seg_file) >= expected

    def _download_segment(self, idx, gen):
        """下载单个分段。返回 True=流正常结束，False=被暂停/取消/重启而中止。"""
        start, end = self._segment_offsets[idx]
        seg_file = os.path.join(self._tmp_dir, f"part_{idx:04d}")

        # 该分段已完整（上次下载遗留）：直接置位进度，跳过网络请求
        if self._segment_complete(idx):
            with self._lock:
                self._segment_progress[idx] = end - start + 1
            return True

        # 恢复：从已有部分继续（不支持 Range 的服务器只能从头重下）
        existing = 0
        if os.path.exists(seg_file):
            existing = os.path.getsize(seg_file) if self._range_supported else 0
        restore_from = start + existing

        # 恢复进度计数：否则暂停后进度条从 0 重新计算
        with self._lock:
            self._segment_progress[idx] = existing

        headers = {"User-Agent": _UA}
        if self.total_size > 0 and self._range_supported:
            headers["Range"] = f"bytes={restore_from}-{end}"

        # 主模式先试；失败（状态码不可达 / 连接错误）再用互补模式重试一次
        try:
            return self._stream_segment(idx, headers, seg_file, existing, start, end, gen)
        except Exception as e:
            if _is_proxy_down_error(e):
                # 代理宕机：整个会话切到直连，后续分段不再重复代理重试
                logger.warning("分段 %d 检测到代理不可用(%s)，切换为直连: %s",
                               idx, _PROXY_MODE, e)
                set_proxy_mode("direct")
                try:
                    ok = self._stream_segment(idx, headers, seg_file, existing, start, end, gen)
                    logger.info("分段 %d 经直连下载成功", idx)
                    return ok
                except Exception as e2:
                    logger.error("分段 %d 直连仍失败: %s", idx, e2)
                    if self.status not in ("cancelled", "paused", "failed"):
                        raise Exception(_friendly_error(e2))
                    return False
            alt = _alt_proxy_mode()
            logger.warning("分段 %d 主模式(%s)失败: %s，尝试互补模式 %s",
                           idx, _PROXY_MODE, e, alt)
            try:
                with _SwitchedProxy(alt):
                    ok = self._stream_segment(idx, headers, seg_file, existing, start, end, gen)
                logger.info("分段 %d 经互补模式 %s 下载成功", idx, alt)
                return ok
            except Exception as e2:
                logger.error("分段 %d 互补模式 %s 仍失败: %s", idx, alt, e2)
                if self.status not in ("cancelled", "paused", "failed"):
                    raise Exception(_friendly_error(e2))
                return False

    def _stream_segment(self, idx, headers, seg_file, existing, start, end, gen):
        """用当前全局 Session 的代理模式下载并写入一个分段（不含失败重试）。

        返回 True=流正常结束；False=任务被暂停/取消/重启，调用方应静默退出。
        """
        resp = _SESSION.get(self.url, headers=headers, stream=True, timeout=30,
                            allow_redirects=True)
        try:
            if resp.status_code in (401, 403):
                raise Exception("HTTP 403")  # 链接签名过期/无权限
            if resp.status_code not in (200, 206):
                raise Exception(f"HTTP {resp.status_code}")
            # 请求了续传区间但服务器返回 200 整文件：继续写入会破坏分段布局
            if "Range" in headers and existing > 0 and resp.status_code == 200:
                raise Exception("服务器不支持 Range 续传（返回 200），无法从断点继续")

            # 本分段还应下载的字节数（已知大小且支持 Range 时）；
            # 个别服务器会忽略 Range 的结束偏移多发数据，按需截断，防止分片超长
            expected = None
            if self.total_size > 0 and self._range_supported:
                expected = end - (start + existing) + 1

            mode = "ab" if existing > 0 else "wb"
            written = 0
            with open(seg_file, mode) as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    # 暂停/取消/重启（代数变化）：旧线程立即退出，由 resume 重新拉起，
                    # 避免新旧两组线程同时向同一分段文件写数据导致文件损坏
                    if self._gen != gen or self.status != "downloading":
                        return False
                    if expected is not None:
                        if written >= expected:
                            break
                        if written + len(chunk) > expected:
                            chunk = chunk[:expected - written]
                    f.write(chunk)
                    written += len(chunk)
                    with self._lock:
                        self._segment_progress[idx] += len(chunk)
            return True
        finally:
            resp.close()

    def _assemble_file(self):
        """合并分段文件"""
        with open(self.filepath, "wb") as out:
            for idx in range(self.segments):
                seg_file = os.path.join(self._tmp_dir, f"part_{idx:04d}")
                if os.path.exists(seg_file):
                    with open(seg_file, "rb") as inf:
                        shutil.copyfileobj(inf, out, length=4 * 1024 * 1024)

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

            self._invalidate_cache()

            # 检查是否所有分段线程都已置位完成标志（旧实现只看分段文件是否存在，
            # 未知大小任务一旦文件被创建就会被误判为完成）
            if (len(self._seg_done) == self.segments and all(self._seg_done)):
                with self._lock:
                    if self._completion_handled or self.status != "downloading":
                        return
                    self._completion_handled = True
                try:
                    self._assemble_file()
                    final_size = os.path.getsize(self.filepath) if os.path.exists(self.filepath) else 0
                    if self.total_size > 0 and final_size != self.total_size:
                        raise Exception(
                            f"文件大小校验失败（预期 {self.total_size} 字节，实际 {final_size} 字节）")
                    if self.total_size <= 0:
                        # 未知大小：完成后用实际大小回填，供 UI 显示
                        self.total_size = final_size
                        self.downloaded = final_size
                    else:
                        self.downloaded = self.total_size
                    self.progress = 100.0
                    self.status = "completed"
                    self.speed = 0.0
                    self.eta = ""
                    self._invalidate_cache()
                except Exception as e:
                    self.status = "failed"
                    self.error = f"合并文件失败: {e}"
                    self._invalidate_cache()
                    logger.error("合并文件失败: %s | 错误: %s", self.filename, e, exc_info=True)
                else:
                    logger.info("下载完成: %s (%.2f MB)", self.filename, final_size / 1024 / 1024)
                return

    def start(self):
        """启动下载"""
        if self.status in ("downloading", "completed"):
            return

        logger.info("开始下载任务: %s  (%s)", self.filename, self.url)
        self.status = "downloading"
        self._invalidate_cache()

        # 获取文件信息
        if not self._fetch_info():
            self.status = "failed"
            self._invalidate_cache()
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
                self._invalidate_cache()
                return

        with self._lock:
            self._seg_done = [False] * self.segments
            self._completion_handled = False
            self._start_time = time.time()
            self._last_check_time = time.time()
            self._last_check_bytes = 0

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
        """在线程中运行分段下载（含 3 次自动重试，覆盖连接中途断开导致的分段不完整）。"""
        gen = self._gen
        try:
            for _attempt in range(3):
                if self._gen != gen or self.status != "downloading":
                    return
                streamed = self._download_segment(idx, gen)
                # 未知大小：流正常结束即完成；已知大小：校验分段文件确实完整
                if streamed and (self.total_size <= 0 or self._segment_complete(idx)):
                    break
                # 否则不完整（如连接提前断开），自动重试
            else:
                if self._gen == gen and self.status == "downloading":
                    raise Exception("分段下载不完整（已自动重试 3 次仍不完整），请暂停后恢复重试")
                return
            if self._gen == gen and self.status == "downloading":
                with self._lock:
                    self._seg_done[idx] = True
        except Exception as e:
            with self._lock:
                if self.status == "downloading":
                    self.status = "failed"
                    self.error = str(e)
                    self._invalidate_cache()
            logger.error("任务失败: %s | 分段%d 错误: %s", self.filename, idx, e)

    def pause(self):
        """暂停下载。代数 +1 让所有分段线程尽快退出（旧实现线程挂起等待，
        resume 再拉起新线程后新旧两组同时写同一文件，会导致文件损坏）。"""
        if self.status != "downloading":
            return
        self.status = "paused"
        self._gen += 1
        self.speed = 0.0
        self.eta = ""
        self._invalidate_cache()

    def resume(self):
        """恢复下载"""
        if self.status != "paused":
            return
        self.status = "downloading"

        with self._lock:
            self._seg_done = [False] * self.segments
            self._completion_handled = False
            self._start_time = time.time()
            # 以当前累计字节为基线，避免恢复瞬间速度计算出现负值/尖峰
            self._last_check_time = time.time()
            self._last_check_bytes = sum(self._segment_progress)

        # 重新启动分段线程（会从各分段已有字节断点续传）
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
        self._gen += 1  # 唤醒/终止所有分段线程
        self._invalidate_cache()

        # 清理临时文件
        if os.path.exists(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def retry(self):
        """重试失败/已取消的任务。

        失败任务的分片文件仍在磁盘上（cancel 才会清理），重试会从各分片
        已有字节断点续传，不会从头下载。已取消任务因分片被清理则从头开始。
        """
        if self.status not in ("failed", "cancelled"):
            return False
        self.error = ""
        self._completion_handled = False
        self._seg_done = [False] * max(self.segments, 1)
        self.status = "pending"
        self._invalidate_cache()
        self.start()
        return True

    def to_dict(self):
        """序列化任务状态。结果缓存到下次字段变化时失效，
        避免每 500ms 的 UI 刷新和 SSE 推送重复构造 dict。"""
        if self._dict_cache is not None:
            return self._dict_cache
        self._dict_cache = {
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
        return self._dict_cache


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
