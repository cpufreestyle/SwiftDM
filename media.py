"""
流媒体下载 —— kind 判定、ffmpeg / yt-dlp 依赖探测、MediaTask（yt-dlp 适配器）。

设计约束（见 docs/superpowers/specs/2026-09-23-media-sniffing-design.md）：
  - 只检测 DRM，绝不尝试解密；
  - 不打包 ffmpeg，缺失时明确报「需要 ffmpeg」；
  - MediaTask 与 DownloadTask / TorrentTask 接口兼容，可直接被 SSE 与历史记录复用。
"""
import os
import shutil
import time
import logging
import threading

logger = logging.getLogger("SwiftDM")

KIND_AUTO = "auto"
KIND_HTTP = "http"
KIND_HLS = "hls"
KIND_DASH = "dash"
KIND_VIDEO_PAGE = "video_page"
VALID_KINDS = (KIND_AUTO, KIND_HTTP, KIND_HLS, KIND_DASH, KIND_VIDEO_PAGE)
MEDIA_KINDS = (KIND_HLS, KIND_DASH, KIND_VIDEO_PAGE)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class MediaError(Exception):
    """流媒体任务的可归类错误。API 层按 .reason 映射 HTTP 状态码。"""
    reason = "media_error"

    def __init__(self, message="", reason=None):
        super().__init__(message or "流媒体任务失败")
        self.reason = reason or type(self).reason


class DrmProtectedError(MediaError):
    reason = "drm_protected"


class NeedsFfmpegError(MediaError):
    reason = "needs_ffmpeg"


def classify_kind(url, kind=KIND_AUTO):
    """决定用哪个引擎。显式 kind（含 video_page）原样返回；auto 只看 URL 特征。"""
    kind = (kind or KIND_AUTO).strip().lower()
    if kind not in VALID_KINDS:
        raise MediaError(f"不支持的下载类型: {kind}", "invalid_kind")
    if kind != KIND_AUTO:
        return kind
    path = (url or "").strip().lower()
    if path.endswith(".m3u8") or "/format=m3u8" in path or ".m3u8?" in path:
        return KIND_HLS
    if path.endswith(".mpd") or ".mpd?" in path:
        return KIND_DASH
    return KIND_HTTP


def ffmpeg_status():
    """探测系统 ffmpeg：SWIFTDM_FFMPEG 优先，其次 PATH。绝不自动下载。"""
    override = (os.environ.get("SWIFTDM_FFMPEG") or "").strip()
    if override and os.path.isfile(override):
        return {"available": True, "path": override}
    found = shutil.which("ffmpeg")
    if found and os.path.isfile(found):
        return {"available": True, "path": found}
    return {"available": False, "path": None}


# ---------------- yt-dlp 延迟导入 ----------------
_YTDLP = None


class _YdlLogger:
    """把 yt-dlp 的日志桥接到 logging。

    不传 logger 时 yt-dlp 的错误上报会往 stderr flush；打包后的 GUI 进程
    （windowed 模式 / 父进程控制台已退出导致 stderr 变成坏管道）里那次 flush
    会抛 OSError，把真实错误（如「This video is DRM protected」）掩埋成
    [Errno 22]。桥接后所有消息完整落入 swiftdm.log，错误归类不再失真。
    """

    def debug(self, msg, once=False):
        logger.debug(msg)

    def info(self, msg, once=False):
        logger.debug(msg)

    def warning(self, msg, once=False):
        logger.warning(msg)

    def error(self, msg, once=False):
        logger.error(msg)


_YDL_LOGGER = _YdlLogger()


def set_ytdlp_module(module):
    """注入 yt_dlp 模块（测试用假模块，或用户自带版本）。传 None 表示恢复自动探测。"""
    global _YTDLP
    _YTDLP = module


def _ytdlp():
    global _YTDLP
    if _YTDLP is not None:
        return _YTDLP
    try:
        import yt_dlp
    except ImportError as e:
        raise MediaError("未安装 yt-dlp，请执行 pip install yt-dlp 后重启 SwiftDM",
                         "needs_ytdlp") from e
    _YTDLP = yt_dlp
    return _YTDLP


def ytdlp_available():
    try:
        _ytdlp()
        return True
    except MediaError:
        return False


class MediaTask:
    """基于 yt-dlp 的流媒体任务（HLS / DASH / 网页视频解析）。

    与 DownloadTask 同接口，交给 DownloadManager / SSE / 历史记录时无需特殊分支。
    网络与磁盘写操作都在工作线程里跑，不阻塞 Flask 请求线程。
    """

    def __init__(self, task_id, url, save_dir, filename=None, segments=8,
                 kind=KIND_HLS, referer=None, cookies_netscape=None, resolution=None):
        self.task_id = task_id
        self.url = (url or "").strip()
        self.save_dir = save_dir
        self.filename = (filename or "").strip() or _guess_name(self.url, kind)
        self.segments = max(int(segments or 0), 1)
        self.kind = kind if kind in MEDIA_KINDS else KIND_HLS
        self.referer = (referer or "").strip()
        self.resolution = int(resolution) if resolution else None
        self._cookies_netscape = cookies_netscape or ""

        self.status = "pending"
        self.progress = 0.0
        self.total_size = 0
        self.downloaded = 0
        self.speed = 0.0
        self.eta = ""
        self.error = ""
        self.error_reason = ""
        self.added_at = time.time()
        self.filepath = os.path.join(save_dir, self.filename)
        self._lock = threading.Lock()
        # 生命周期转换锁：start/pause/resume/cancel/retry/完成等状态迁移互斥。
        # _lock 仍只保护进度字段；嵌套顺序固定 _xlock -> _lock，不会反转。
        self._xlock = threading.RLock()
        self._start_token = 0  # 启动令牌：retry 申请，并发 cancel 使其失效
        self._gen = 0
        self._info = None
        self._worker = None

    # ---------------- 前置检查 ----------------

    def _extract(self):
        """拉一次元数据（不下载）。失败或结构不认识时返回 None，交给 start() 报真实错误。"""
        ydl = _ytdlp()
        opts = self._base_opts(None)
        try:
            with ydl.YoutubeDL(opts) as inst:
                return inst.extract_info(self.url, download=False) or {}
        except DrmProtectedError:
            raise
        except MediaError:
            raise
        except Exception as e:
            # DRM 保护是提取期错误（yt-dlp 拿不到就报错，没有 info 可查），翻译成
            # DrmProtectedError 让 preflight 硬拒绝（409），而不是建完任务再失败
            if _reason_from_text(str(e)) == "drm_protected":
                raise DrmProtectedError("该资源受 DRM 保护，SwiftDM 不支持下载")
            logger.info("媒体元数据预取失败（稍后由下载重试）: %s | %s",
                        self.filename, e, exc_info=True)
            return None

    def preflight(self):
        """下载前的硬性门槛：DRM 直接拒绝；只有「元数据确认音视频分离且本机没 ffmpeg」才拒绝。

        抛 MediaError 子类 → API 层转 409（不创建任务）。不做「没 ffmpeg 就别下 HLS」的一刀切：
        纯 TS 序列由 yt-dlp 内置合并，取不到元数据时放行，缺 ffmpeg 的真实错误留给
        `_run()` 按 `needs_ffmpeg` 上报（spec §2「MediaTask 行为」ffmpeg 一条）。
        """
        info = self._extract()
        if info:
            self._info = info
            _assert_no_drm(info)
            if not ffmpeg_status()["available"] and _needs_merge(info):
                raise NeedsFfmpegError("该资源的音视频是分离的，需要 ffmpeg 合并，"
                                       "请安装 ffmpeg 并加入系统 PATH（或设置 SWIFTDM_FFMPEG）")

    # ---------------- 生命周期 ----------------

    def start(self, token=None):
        """启动 yt-dlp 工作线程。

        token: retry() 申请的启动令牌，用于堵住「retry 已重置字段、但 start() 还没
        拉起线程」这段窗口里并发 cancel 被覆盖、任务被复活的问题。
        """
        if self.status in ("downloading", "completed"):
            return False
        if token is None and self.status == "cancelled":
            return False           # 只有 retry 持令牌才允许从 cancelled 重新启动
        if token is not None and token != self._start_token:
            return False
        logger.info("开始流媒体任务: %s  [%s] %s", self.filename, self.kind, self.url)
        # start() 也可能被用于从 paused 续跑：先等旧 worker 退出，否则两个
        # yt-dlp 实例会并发写同一个 .part，互相覆盖导致文件损坏
        self._drain_worker()
        with self._xlock:
            if self.status in ("downloading", "completed"):
                return False
            if token is None and self.status == "cancelled":
                return False
            if token is not None and token != self._start_token:
                return False
            self.status = "downloading"
            self.error = ""
            self.error_reason = ""
            gen = self._gen
            worker = threading.Thread(target=self._run, args=(gen,), daemon=True)
            worker.start()
            # 必须先 start 再发布：否则并发的 _drain_worker 可能 join 一个还没
            # 启动的 Thread（RuntimeError: cannot join thread before it is started）
            self._worker = worker
        return True

    def _drain_worker(self, timeout=5.0):
        """有界地等待旧的 yt-dlp worker 退出。

        pause() 只切状态并对 _gen + 1，worker 不会立即消失（要等 progress hook 触发
        DownloadCancelled 再层层 unwind）；重新拉起前必须先等它退出，否则新旧两个
        yt-dlp 实例会并发写同一个 .part 文件。

        必须在 _xlock 之外调用：worker 在完成路径上要取 _xlock，持锁 join 会与
        还没退出的旧 worker 死锁。
        """
        worker = self._worker
        if worker is not None:
            worker.join(timeout=timeout)

    def _run(self, gen):
        cookie_path = self._write_cookie_file()
        try:
            ydl = _ytdlp()
        except MediaError as e:
            self._cleanup_cookie(cookie_path)
            self._fail(e, reason=e.reason)
            return
        cancelled_cls = getattr(getattr(ydl, "utils", None), "DownloadCancelled", Exception)
        try:
            with ydl.YoutubeDL(self._build_opts(gen)) as inst:
                inst.download([self.url])
            if self._gen != gen or self.status != "downloading":
                return                       # 已被暂停/取消：保留调用方设置的状态
            # 持 _xlock 复核后再置 completed：与 cancel 的状态迁移互斥，
            # 否则可能把已取消的任务写成 completed
            with self._xlock:
                if self._gen != gen or self.status != "downloading":
                    return
                with self._lock:
                    self.status = "completed"
                    self.progress = 100.0
                    self.speed = 0.0
                    self.eta = ""
                    if self.downloaded > 0:
                        # 已下载字节才是最可信的完成量：yt-dlp 的 total_bytes_estimate 在分片阶段
                        # 会大幅抖动（实测 26 万字节的任务估到过 29 万+），拿它当总量会把进度写爆
                        self.total_size = self.downloaded
                    elif self.total_size > 0:
                        self.downloaded = self.total_size
            logger.info("流媒体下载完成: %s", self.filename)
        except cancelled_cls as e:           # 暂停/取消的协作式中断，不是失败
            if self._gen != gen or self.status in ("paused", "cancelled"):
                logger.info("流媒体任务已中断: %s", self.filename)
                return
            self._fail(e)
        except MediaError as e:
            if self._gen != gen or self.status != "downloading":
                return               # 已被暂停/取消/重启：保留调用方设置的状态
            self._fail(e, reason=e.reason)
        except Exception as e:               # 底层库异常种类多，统一归类成用户可读提示
            if self._gen != gen or self.status in ("paused", "cancelled"):
                return
            self._fail(e)
        finally:
            self.speed = 0.0
            self._cleanup_cookie(cookie_path)

    @staticmethod
    def _cleanup_cookie(path):
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            logger.debug("临时 Cookie 文件清理失败: %s", path)

    def _fail(self, exc, reason=None):
        msg = str(exc)
        with self._xlock:
            # 已被暂停/取消/新一代 worker 接管时不覆写状态
            if self.status != "downloading":
                return
            self.status = "failed"
            self.error_reason = reason or _reason_from_text(msg)
            self.error = _friendly_media_error(self.error_reason, msg)
        logger.error("流媒体任务失败: %s | %s | %s", self.filename, self.error_reason, msg)
        from downloader import fire_on_failed
        fire_on_failed(self)

    def pause(self):
        # 持 _xlock 让「代数 +1」和「状态置 paused」原子完成：否则并发的 cancel
        # 可能落在这两步之间，随后又被 pause 覆写成 paused
        with self._xlock:
            if self.status != "downloading":
                return
            self._gen += 1          # 工作线程在 progress hook 里看到代数变化即抛 DownloadCancelled
            self.status = "paused"
            self.speed = 0.0
            self.eta = ""

    def resume(self):
        if self.status != "paused":
            return
        # start() 会先 drain 旧 worker 再在 _xlock 下复核状态；cancelled 的任务
        # 没有启动令牌，start() 会拒绝，不会被 resume 复活
        self.start()                # continuedl=True，yt-dlp 自行断点续传 .part

    def cancel(self):
        # 持锁迁移状态：与 _run 的「置 completed」「_fail 置 failed」互斥，
        # 否则 cancel 可能被这两条路径覆写
        with self._xlock:
            self._gen += 1
            self._start_token += 1  # 作废 retry 已申请但尚未生效的启动令牌
            self.status = "cancelled"
            self.speed = 0.0
        self._drop_partials()

    def retry(self):
        with self._xlock:
            if self.status not in ("failed", "cancelled"):
                return False
            self.error = ""
            self.error_reason = ""
            self.progress = 0.0
            self.status = "pending"
            # start() 内部要跑 yt-dlp（worker 可能长时间占住），不能在锁内调用，
            # 否则并发的 pause/cancel 会被长时间阻塞。改用启动令牌：若期间状态被
            # 并发操作改动，start() 会看到令牌失效而直接返回，不会复活已取消的任务
            self._start_token += 1
            token = self._start_token
        return self.start(token)

    # ---------------- yt-dlp 选项 ----------------

    def _base_opts(self, gen):
        opts = {
            "outtmpl": os.path.join(self.save_dir, _stem(self.filename) + ".%(ext)s"),
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "continuedl": True,
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": self.segments,
            "http_headers": {"User-Agent": _UA},
            "logger": _YDL_LOGGER,
        }
        if gen is not None:
            opts["progress_hooks"] = [lambda d, g=gen: self._on_progress(d, g)]
        if self.referer:
            opts["http_headers"]["Referer"] = self.referer
        if self.kind == KIND_VIDEO_PAGE and self.resolution:
            opts["format"] = f"bv*[height<={self.resolution}]+ba/b"
        else:
            opts["format"] = "bv*+ba/b"
        from throttle import get_rate
        rate = get_rate()
        if rate > 0:
            opts["ratelimit"] = rate
        return opts

    def _build_opts(self, gen):
        opts = self._base_opts(gen)
        if self._cookies_netscape:
            path = self._cookie_path()
            opts["cookiefile"] = path
        return opts

    # ---------------- Cookie 临时文件 ----------------

    def _cookie_path(self):
        data_dir = os.path.join(os.path.expanduser("~"), ".swiftdm")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, f"ck_{self.task_id}_{os.getpid()}.txt")

    def _write_cookie_file(self):
        if not self._cookies_netscape.strip():
            return None
        path = self._cookie_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._cookies_netscape.rstrip() + "\n")
        return path

    def _drop_partials(self):
        stem = _stem(self.filename)
        try:
            names = os.listdir(self.save_dir)
        except OError:
            return
        for name in names:
            if name.startswith(stem) and name.endswith((".ytdl", ".part")):
                try:
                    os.remove(os.path.join(self.save_dir, name))
                except OSError:
                    pass

    # ---------------- 进度 ----------------

    def _on_progress(self, d, gen):
        if self._gen != gen:
            raise _ytdlp().utils.DownloadCancelled("任务已暂停或取消")
        status = d.get("status")
        if status == "downloading":
            done = int(d.get("downloaded_bytes") or 0)
            total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
            with self._lock:
                self.downloaded = max(self.downloaded, done)
                if total > 0:
                    self.total_size = total
                    self.progress = min(99.5, self.downloaded * 100.0 / total)
                self.speed = float(d.get("speed") or 0.0)
                eta = d.get("eta")
                self.eta = f"{int(eta)}s" if eta else ""
        elif status == "finished":
            name = d.get("filename") or ""
            done = int(d.get("downloaded_bytes") or 0)
            total = int(d.get("total_bytes") or 0)      # finished 事件的 total_bytes 是精确值
            with self._lock:
                if total > 0:
                    self.total_size = total
                if done > 0:
                    self.downloaded = max(self.downloaded, done)
                if name:
                    self.filepath = name
                    self.filename = os.path.basename(name)

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "filename": self.filename,
            "filepath": self.filepath,
            "url": self.url,
            "save_dir": self.save_dir,
            "status": self.status,
            "progress": round(self.progress, 1),
            "total_size": self.total_size,
            "downloaded": self.downloaded,
            "speed": self.speed,
            "eta": self.eta,
            "error": self.error,
            "error_reason": self.error_reason,
            "segments": self.segments,
            "kind": self.kind,
            "resolution": self.resolution or 0,
            "referer": self.referer,
        }


def _stem(filename):
    base = os.path.basename(filename or "video")
    return os.path.splitext(base)[0] or "video"


def _guess_name(url, kind):
    from urllib.parse import unquote, urlsplit
    path = urlsplit(url or "").path
    last = unquote(os.path.basename(path)) if path else ""
    if last and "." in last and len(last) < 120:
        return last
    stamp = int(time.time())
    ext = {"hls": "mp4", "dash": "mp4"}.get(kind, "mp4")
    return f"media_{stamp}.{ext}"


def _assert_no_drm(info):
    """只要元数据里出现 DRM 标记就拒绝（AES-128 由 yt-dlp 用清单里的明文 key 处理，不算 DRM）。"""
    if info.get("drm") or info.get("has_drm"):
        raise DrmProtectedError("该资源受 DRM 保护，SwiftDM 不支持下载")
    for fmt in info.get("formats") or []:
        if isinstance(fmt, dict) and (fmt.get("has_drm") or fmt.get("drm")):
            raise DrmProtectedError("该资源受 DRM 保护，SwiftDM 不支持下载")


def _needs_merge(info):
    """音视频确认分离（必须 ffmpeg 合并）时返回 True。

    看不出来就不返回 True —— 误判成「需要合并」会让没装 ffmpeg 的机器下不了纯 TS 的 HLS，
    而漏判只是把同样的错误推迟到 `_run()` 里由 yt-dlp 报出来，代价小得多。
    """
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    if not formats:
        return False
    v = [f for f in formats if f.get("vcodec") not in (None, "none")]
    a = [f for f in formats if f.get("acodec") not in (None, "none")]
    muxed = [f for f in formats if f.get("vcodec") not in (None, "none")
             and f.get("acodec") not in (None, "none")]
    return bool(v and a and not muxed)


_DRM_HINTS = ("drm", "widevine", "fairplay", "playready", "protected")
_COOKIE_HINTS = ("login", "cookies", "account", "401", "403",
                 "age-restricted", "age restriction", "confirm your age")


def _reason_from_text(text):
    low = (text or "").lower()
    if any(h in low for h in _DRM_HINTS):
        return "drm_protected"
    if "ffmpeg" in low and ("not found" in low or "missing" in low or "is required" in low):
        return "needs_ffmpeg"
    if any(h in low for h in _COOKIE_HINTS):
        return "cookies_required"
    return "parse_failed"


_MEDIA_HINTS = {
    "drm_protected": "资源受 DRM 保护，无法下载（SwiftDM 不解密受保护内容）",
    "needs_ffmpeg": "缺少 ffmpeg：请安装 ffmpeg 并加入系统 PATH 后重试",
    "cookies_required": "该站点需要登录：请在浏览器登录后重试，或手动复制下载链接",
    "needs_ytdlp": "未安装 yt-dlp：请执行 pip install yt-dlp 后重启 SwiftDM",
    "parse_failed": "解析失败：链接可能已过期或站点暂不支持，可稍后重试",
}


def _friendly_media_error(reason, raw=""):
    return _MEDIA_HINTS.get(reason, f"流媒体下载失败: {raw[:120]}")
