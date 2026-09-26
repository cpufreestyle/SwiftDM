"""
IDM 风格下载引擎 —— 多线程分段下载、暂停/恢复
"""
import os
import re
import time
import json
import shutil
import threading
import logging
import requests
from requests.adapters import HTTPAdapter
from urllib.parse import urlparse, unquote
from throttle import consume as _throttle_consume
from throttle import write_granularity as _throttle_granularity

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


def _windows_system_proxy():
    """读取 Windows 系统代理（Internet Settings），返回 {'http':..,'https':..} 或 None。

    requests 的 trust_env 只认 HTTP_PROXY/HTTPS_PROXY 环境变量，不会读 Windows 系统代理；
    很多用户只在系统设置里配了代理（本机即 127.0.0.1:7897），导致 env 模式等效直连、
    外网下载全部连不上（没速度 / 失败）。这里补上系统代理，使 env 模式在 Windows 上真正生效。
    """
    try:
        import winreg
    except Exception:
        return None
    proxy = None
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            with winreg.OpenKey(
                hive, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
            ) as k:
                if not winreg.QueryValueEx(k, "ProxyEnable")[0]:
                    continue
                server = winreg.QueryValueEx(k, "ProxyServer")[0]
        except Exception:
            continue
        if not server:
            continue
        proxies = {}
        if "=" in server:  # 形如 http=127.0.0.1:7897;https=127.0.0.1:7897
            for part in server.split(";"):
                if "=" in part:
                    scheme, addr = part.split("=", 1)
                    proxies[scheme.strip().lower()] = addr.strip()
        else:  # 形如 127.0.0.1:7897
            proxies = {"http": server, "https": server}
        if proxies:
            proxy = proxies
            break
    return proxy


_PROXY_ENV_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")
_EXPORTED_BEFORE = {}   # 导出前各键的值（None 表示原先不存在），供撤销时恢复


def _restore_proxy_env():
    """撤销之前写进环境变量的代理，恢复写入前的值。"""
    for k, before in _EXPORTED_BEFORE.items():
        if before is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = before
    _EXPORTED_BEFORE.clear()


def _export_proxy_env(proxies):
    """把当前生效的代理同步进环境变量。

    yt-dlp（流媒体解析/下载）等进程内客户端只认 HTTP_PROXY/HTTPS_PROXY，不会读 Windows
    系统代理；env 模式在只配了系统代理的机器上必须显式导出，否则媒体任务等效直连、外网
    全部连不上。传 None 表示全部抹掉（配合 _restore_proxy_env 实现可撤销）。
    """
    if not _EXPORTED_BEFORE:
        for k in _PROXY_ENV_KEYS:
            _EXPORTED_BEFORE[k] = os.environ.get(k)
    for k in _PROXY_ENV_KEYS:
        os.environ.pop(k, None)
    if not proxies:
        return
    http_v = proxies.get("http") or proxies.get("https")
    https_v = proxies.get("https") or proxies.get("http")
    if http_v:
        os.environ["HTTP_PROXY"] = http_v
        os.environ["http_proxy"] = http_v
    if https_v:
        os.environ["HTTPS_PROXY"] = https_v
        os.environ["https_proxy"] = https_v


def _apply_proxy_mode(mode):
    """根据模式配置全局 Session 的代理行为。"""
    global _PROXY_MODE
    _PROXY_MODE = mode.strip().lower() if isinstance(mode, str) else "env"
    _SESSION.proxies.clear()
    if _PROXY_MODE == "direct":
        # 强制直连：忽略环境变量代理
        _SESSION.trust_env = False
        _SESSION.proxies.update({"http": None, "https": None})
        # yt-dlp 不读 Session 配置，只认环境变量——直连模式必须把代理变量摘掉
        _export_proxy_env(None)
    elif _PROXY_MODE in ("env", "", "system"):
        # 走系统代理：优先用 HTTP_PROXY/HTTPS_PROXY 环境变量；
        # 环境变量缺失时补读 Windows 系统代理（requests 自身不会读，否则会等效直连）
        _SESSION.trust_env = True
        # 先还原此前被 direct/custom 抹掉的用户环境变量，再判断 env_has
        if _EXPORTED_BEFORE:
            _restore_proxy_env()
        env_has = (os.environ.get("HTTP_PROXY") or os.environ.get("HTTPS_PROXY")
                   or os.environ.get("http_proxy") or os.environ.get("https_proxy"))
        if not env_has:
            sys_proxy = _windows_system_proxy()
            if sys_proxy:
                _SESSION.proxies.update(sys_proxy)
                _export_proxy_env(sys_proxy)
    else:
        # 自定义代理地址
        _SESSION.trust_env = False
        _SESSION.proxies.update({"http": _PROXY_MODE, "https": _PROXY_MODE})
        _export_proxy_env({"http": _PROXY_MODE, "https": _PROXY_MODE})


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


def _sanitize_filename(name):
    """消毒外部来源的文件名（Content-Disposition / API / 扩展），阻断路径遍历。

    攻击面: filename=../../../.zshenv、绝对路径 /etc/x、反斜杠 ..\\..\\evil 等
    —— os.path.join 遇绝对路径会丢弃 save_dir，必须在这里拦下。
    """
    if not name:
        return ""
    # 只取最后一段（剥掉所有目录成分，兼容 / 与 \ 两种分隔符）
    name = name.replace("\\", "/")
    name = name.rstrip("/").split("/")[-1]
    # 剥离前导点（隐藏文件 + '.'/'..' 兜底）
    name = name.lstrip(".")
    # 剥离控制字符与 Windows 保留字符
    name = "".join(c for c in name if ord(c) >= 32 and c not in '<>:"|?*')
    name = name.strip()
    if name in ("", ".", ".."):
        return ""
    return name


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
        self.error_reason = ""
        self.added_at = time.time()

        # 确定文件名（外部输入必须消毒，防路径遍历）
        filename = _sanitize_filename(filename)
        if filename:
            self.filename = filename
        else:
            self.filename = self._extract_filename(url)

        self.filepath = os.path.join(save_dir, self.filename)
        self._tmp_dir = os.path.join(save_dir, f".{self.filename}.parts")

        # 内部控制
        self._lock = threading.Lock()
        # 生命周期转换锁：pause/resume/cancel/start/完成等状态迁移互斥。
        # 与 _lock 的分工：_lock 只保护字段读写（临界区短、绝不 join），
        # _xlock 只保护状态机迁移；嵌套顺序固定为 _xlock -> _lock，不会反转。
        self._xlock = threading.RLock()
        self._start_token = 0  # 启动令牌：retry 申请，并发 cancel/pause 使其失效
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
        """从 URL 提取文件名（经消毒，URL 解码后可能含 \\ 等路径成分）"""
        parsed = urlparse(url)
        path = unquote(parsed.path)
        name = _sanitize_filename(os.path.basename(path))
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
            # RFC 5987: filename*=UTF-8''%E5%90%8D.txt（带 charset 前缀）；旧式: filename="name.txt"
            cd = resp.headers.get("Content-Disposition", "")
            cd_name = ""
            m5987 = re.search(r"filename\*=(?:UTF-8|utf-8)''([^;\s]+)", cd)
            if m5987:
                cd_name = unquote(m5987.group(1).strip().strip('"'))
            else:
                m_plain = re.search(r'filename[*]?=["\']?([^"\';]+)', cd, re.IGNORECASE)
                if m_plain:
                    cd_name = unquote(m_plain.group(1).strip())
            # 消毒: 服务器可控，是路径遍历攻击的主入口（../../../x、绝对路径等）
            cd_name = _sanitize_filename(cd_name)
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
            gran = _throttle_granularity()
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
                    # 限速时按半秒额度切片写入：总额受令牌桶控制，UI 上的瞬时速度
                    # 也不会被整块写入放大数倍
                    off = 0
                    while off < len(chunk):
                        step = (len(chunk) - off) if gran <= 0 else min(gran, len(chunk) - off)
                        piece = chunk[off:off + step]
                        f.write(piece)
                        off += step
                        written += step
                        with self._lock:
                            self._segment_progress[idx] += step
                        _throttle_consume(step)
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
                # 锁外快速让位：完成已由别的线程处理，或任务已离开 downloading
                # （被 pause/cancel）时直接退出。这里刻意不取锁，保证 pause/cancel
                # 持锁迁移状态时不会被旧监控线程挡住，resume() join 旧监控线程
                # 时也不会死锁。
                if self._completion_handled or self.status != "downloading":
                    return
                # 持 _xlock 复核后再组装：与 cancel 的 rmtree、pause 的状态迁移互斥
                with self._xlock:
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
                        # 合并期间用户可能 cancel（分片已被 rmtree）：不覆写 cancelled 状态
                        if self.status == "downloading":
                            self.status = "failed"
                            self.error = f"合并文件失败: {e}"
                            self._invalidate_cache()
                        logger.error("合并文件失败: %s | 错误: %s", self.filename, e, exc_info=True)
                    else:
                        logger.info("下载完成: %s (%.2f MB)", self.filename, final_size / 1024 / 1024)
                return

    def _drain_threads(self, timeout=5.0):
        """有界地等待旧的下载/监控线程退出。

        pause() 只切状态并对 _gen + 1，线程不会立即消失；resume()/start() 重新
        拉起线程前必须先等它们退出，否则新旧两组线程会并发写同一个分段文件导致
        文件损坏，或同时存在两个监控线程。

        必须在 _xlock 之外调用：监控线程在完成路径上要取 _xlock，持锁 join
        会与还没退出的旧监控线程形成死锁。
        """
        deadline = time.time() + timeout
        for t in list(self._threads):
            remain = deadline - time.time()
            if remain > 0:
                t.join(timeout=remain)

    def start(self, token=None):
        """启动下载

        token: retry() 申请的启动令牌。retry() 需要在联网探测之前先重置字段，
        而 _fetch_info() 最长可能阻塞约 45s、不能持锁，因此用令牌保证并发的
        pause/cancel 抢先时本方法不会把已被取消的任务重新拉起来。
        """
        if self.status in ("downloading", "completed"):
            return False
        if token is not None and token != self._start_token:
            return False
        if token is None and self.status == "cancelled":
            return False           # 只有 retry 持令牌才允许从 cancelled 重新启动
        logger.info("开始下载任务: %s  (%s)", self.filename, self.url)
        # start() 也可能被用于从 paused 续跑：先等旧线程退出，避免新旧两组线程
        # 同时写同一分段文件、或出现双监控线程
        self._drain_threads()
        with self._xlock:
            if self.status in ("downloading", "completed"):
                return False
            if token is not None and token != self._start_token:
                return False
            if token is None and self.status == "cancelled":
                return False           # 只有 retry 持令牌才允许从 cancelled 重新启动
            self.status = "downloading"
            self._invalidate_cache()

        # 获取文件信息（联网，最长约 45s）。刻意不持 _xlock，
        # 否则并发的 pause/cancel 会被长时间阻塞
        if not self._fetch_info():
            with self._xlock:
                if self.status == "downloading":
                    self.status = "failed"
                    self._invalidate_cache()
            logger.error("任务初始化失败，已停止: %s | 原因: %s", self.filename, self.error)
            return False

        # 计算分段
        self._calc_segments()

        # 创建临时目录
        os.makedirs(self._tmp_dir, exist_ok=True)

        # 如果文件已完整下载
        if self.total_size > 0 and os.path.exists(self.filepath):
            existing_size = os.path.getsize(self.filepath)
            if existing_size >= self.total_size:
                with self._xlock:
                    # 探测期间可能已被取消，别覆盖 terminal 状态
                    if self.status != "downloading":
                        return False
                    self.progress = 100
                    self.status = "completed"
                    self.downloaded = self.total_size
                    self._invalidate_cache()
                return True

        with self._xlock:
            # 探测期间可能已被 pause/cancel：此时不能再拉起线程，保留当时状态
            if self.status != "downloading":
                return False
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
        return True

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
            with self._xlock:
                if self.status == "downloading":
                    self.status = "failed"
                    self.error = str(e)
                    self.error_reason = "download_failed"
                    self._invalidate_cache()
            logger.error("任务失败: %s | 分段%d 错误: %s", self.filename, idx, e)

    def pause(self):
        """暂停下载。代数 +1 让所有分段线程尽快退出（旧实现线程挂起等待，
        resume 再拉起新线程后新旧两组同时写同一文件，会导致文件损坏）。"""
        # 持 _xlock 复核后再迁移：与 monitor 的"组装并置 completed"互斥，
        # 否则可能把一个刚好完成的任务又改回 paused
        with self._xlock:
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
        # 先等待旧的下载/监控线程退出再切回 downloading：否则旧监控线程看到
        # status 又变回 downloading 会继续运行形成双监控；且暂停时 _gen 已 +1，
        # 旧分段线程可能仍在写同一分片文件，与随后新拉起的线程并发写会导致文件
        # 损坏、分段校验失败。暂停期间 status 仍为 paused，旧分段线程（_gen 已变）
        # 与旧监控线程（status 非 downloading）都会在此窗口内自然退出。
        # 注意：join 必须在 _xlock 之外做——监控线程在完成路径上要取 _xlock，
        # 持锁 join 会与还没退出的旧监控线程形成死锁。
        self._drain_threads()
        # 持 _xlock 复核后再迁移：join 期间可能有并发 cancel / 完成改了状态，
        # 若已不是 paused 就放弃，别覆盖取消
        with self._xlock:
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
        # 持锁迁移状态 + 清理临时文件：与 monitor 的"组装 + rmtree 分片目录"互斥
        with self._xlock:
            self.status = "cancelled"
            self._gen += 1  # 唤醒/终止所有分段线程
            self._start_token += 1  # 作废 retry 已申请但尚未生效的启动令牌
            self._invalidate_cache()

            # 清理临时文件
            if os.path.exists(self._tmp_dir):
                shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def retry(self):
        """重试失败/已取消的任务。

        失败任务的分片文件仍在磁盘上（cancel 才会清理），重试会从各分片
        已有字节断点续传，不会从头下载。已取消任务因分片被清理则从头开始。
        """
        with self._xlock:
            if self.status not in ("failed", "cancelled"):
                return False
            self.error = ""
            self._completion_handled = False
            self._seg_done = [False] * max(self.segments, 1)
            self.status = "pending"
            self._invalidate_cache()
            # start() 内部要联网探测（最长约 45s），不能在锁内调用，否则并发的
            # pause/cancel 会被长时间阻塞。改用启动令牌：若探测期间状态被并发操作
            # 改动，start() 会看到令牌失效而直接返回，不会复活已被取消的任务
            self._start_token += 1
            token = self._start_token
        return self.start(token)

    def to_dict(self):
        """序列化任务状态。结果缓存到下次字段变化时失效，
        避免每 500ms 的 UI 刷新和 SSE 推送重复构造 dict。"""
        if self._dict_cache is not None:
            return self._dict_cache
        self._dict_cache = {
            "task_id": self.task_id,
            "filename": self.filename,
            "filepath": self.filepath,
            "url": self.url,
            "status": self.status,
            "progress": round(self.progress, 1),
            "total_size": self.total_size,
            "downloaded": self.downloaded,
            "speed": self.speed,
            "eta": self.eta,
            "error": self.error,
            "save_dir": self.save_dir,
            "error_reason": getattr(self, "error_reason", ""),
            "kind": "http",
            "segments": self.segments,
        }
        return self._dict_cache


class DownloadManager:
    """下载管理器"""

    def __init__(self):
        self._tasks = {}      # task_id -> DownloadTask
        self._counter = 0
        self._lock = threading.Lock()

        # 历史记录持久化：把任务元数据落盘，重启后仍能看到「历史下载记录」
        data_dir = os.path.join(os.path.expanduser("~"), ".swiftdm")
        try:
            os.makedirs(data_dir, exist_ok=True)
        except Exception:
            data_dir = os.path.dirname(os.path.abspath(__file__))
        self._history_path = os.path.join(data_dir, "history.json")
        self._saver_stop = threading.Event()
        self._load_history()
        self._start_saver()

    def create_task(self, url, save_dir, filename=None, segments=8, kind="auto",
                    referer=None, cookies_netscape=None, resolution=None):
        with self._lock:
            self._counter += 1
            task_id = f"dl_{self._counter}"
            # 三种任务类型接口完全兼容：BT/PT → TorrentTask，
            # HLS/DASH/网页视频 → MediaTask，其余可 Range 的普通文件 → 多线程 DownloadTask。
            if self._is_torrent_url(url):
                from torrent import TorrentTask
                task = TorrentTask(task_id, url, save_dir, filename, segments or 0)
            else:
                from media import MediaTask, MEDIA_KINDS, classify_kind, MediaError
                try:
                    resolved = classify_kind(url, kind)
                except MediaError as e:
                    raise ValueError(str(e)) from e
                if resolved in MEDIA_KINDS:
                    task = MediaTask(task_id, url, save_dir, filename, segments, resolved,
                                     referer, cookies_netscape, resolution)
                else:
                    task = DownloadTask(task_id, url, save_dir, filename, segments)
            self._tasks[task_id] = task
            return task

    @staticmethod
    def _is_torrent_url(url):
        """判断是否为 BT/PT 下载链接（磁力链接或 .torrent 文件）。"""
        u = (url or "").strip().lower()
        if u.startswith("magnet:"):
            return True
        if "btih:" in u:
            return True
        # 以 .torrent 结尾的 http(s) 链接（如 PT 种子下载地址）
        if (u.startswith("http://") or u.startswith("https://")) and u.endswith(".torrent"):
            return True
        return False

    def get_task(self, task_id):
        with self._lock:
            return self._tasks.get(task_id)

    def get_all_tasks(self):
        with self._lock:
            return list(self._tasks.values())

    def remove_task(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if task and task.status in ("downloading", "paused"):
                task.cancel()
            self._tasks.pop(task_id, None)

    def clear_completed(self):
        """清除已完成/已失败/已取消的任务，返回清除数量。"""
        with self._lock:
            completed = [tid for tid, t in self._tasks.items() if t.status in ("completed", "cancelled", "failed")]
            for tid in completed:
                self._tasks.pop(tid, None)
        self.save_history()
        return len(completed)

    # ---------------- 历史记录持久化 ----------------
    def _reconstruct_task(self, d):
        """根据保存的字典重建任务对象（仅用于历史展示，不会启动下载）。"""
        url = d.get("url", "")
        filepath = d.get("filepath", "") or ""
        save_dir = d.get("save_dir") or (os.path.dirname(filepath) if filepath else "")
        filename = d.get("filename") or None
        segments = d.get("segments", 8)
        kind = d.get("kind") or ""
        referer = d.get("referer") or ""
        resolution = d.get("resolution") or None
        if self._is_torrent_url(url):
            from torrent import TorrentTask
            task = TorrentTask(d.get("task_id", ""), url, save_dir, filename, segments)
        elif kind in ("hls", "dash", "video_page"):
            from media import MediaTask
            task = MediaTask(d.get("task_id", ""), url, save_dir, filename, segments,
                             kind, referer, None, resolution)
        else:
            task = DownloadTask(d.get("task_id", ""), url, save_dir, filename, segments)
        task.status = d.get("status", "pending")
        task.progress = d.get("progress", 0.0)
        task.total_size = d.get("total_size", 0)
        task.downloaded = d.get("downloaded", 0)
        task.error = d.get("error", "")
        task.filepath = filepath
        # 重启时仍在下载/暂停/等待中的任务视为中断（不自动续传半成品文件），标记为已取消
        if task.status in ("downloading", "paused", "pending"):
            task.status = "cancelled"
            if not task.error:
                task.error = "重启后中断（未自动续传）"
        return task

    def _load_history(self):
        try:
            if not os.path.exists(self._history_path):
                return
            with open(self._history_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("读取历史下载记录失败: %s", e)
            return
        tasks = data.get("tasks", []) if isinstance(data, dict) else (data or [])
        max_n = 0
        with self._lock:
            for d in tasks:
                try:
                    task = self._reconstruct_task(d)
                except Exception as e:
                    logger.warning("重建历史任务失败: %s", e)
                    continue
                self._tasks[task.task_id] = task
                try:
                    n = int(str(task.task_id).split("_")[-1])
                    max_n = max(max_n, n)
                except Exception:
                    pass
        self._counter = max_n

    def save_history(self):
        with self._lock:
            tasks = [t.to_dict() for t in self._tasks.values()]
        tasks = tasks[-500:]  # 仅保留最近 500 条，避免无限增长
        payload = {"version": 1, "saved_at": time.time(), "tasks": tasks}
        try:
            tmp = self._history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
            os.replace(tmp, self._history_path)
        except Exception as e:
            logger.warning("保存历史下载记录失败: %s", e)

    def _start_saver(self):
        def _loop():
            while not self._saver_stop.wait(5):
                try:
                    self.save_history()
                except Exception:
                    pass
        threading.Thread(target=_loop, daemon=True).start()

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
