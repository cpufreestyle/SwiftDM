"""
浏览器监控 —— 剪贴板监听 + HTTP 端点（供浏览器扩展调用）
"""
import re
import time
import threading
import json
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse


# path 末段命中这些词才算下载动作。用集合而非子串匹配：
# 子串会把 /downloads/page/2、/blog/how-to-download-files 全算成下载。
# 只收 download/dl 这类语义明确的词：get、attachment 太通用，
# /get?id=123 这样的 API 端点会被误判成下载。
_DOWNLOAD_ACTION_WORDS = {
    "download", "dl", "downloadfile", "filedownload",
}


# URL 正则
URL_PATTERN = re.compile(
    r'https?://[^\s<>"{}|\\^`\[\]]+',
    re.IGNORECASE
)

# 文件扩展名（常见的下载类型）
DOWNLOAD_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".iso",
    ".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".apk", ".appx",
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m3u8",
    ".mp3", ".aac", ".flac", ".wav", ".ogg", ".wma", ".m4a",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg", ".psd",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".txt",
    ".torrent", ".bin", ".dat", ".dll", ".jar", ".py", ".js", ".ts",
    ".crx", ".xpi", ".safariextz",
}

# 下载类 MIME 类型
DOWNLOAD_MIMES = {
    "application/zip", "application/x-rar-compressed", "application/x-7z-compressed",
    "application/x-tar", "application/gzip", "application/x-bzip2",
    "application/x-msdownload", "application/x-msi", "application/octet-stream",
    "application/x-apple-diskimage", "application/x-iso9660-image",
    "video/", "audio/",
    "application/pdf",
    "application/vnd.android.package-archive",
}


class CaptureLog:
    """浏览器捕获事件流水：监控线程写，桌面面板读。

    以前只有剪贴板那一路会给提示，扩展端点加的活是静默的：
    任务往往平空出现，用户不知道它从哪来、也不知道是否被去重。
    流水只增不减（有上限），每条事件带序号；UI 记住自己渲染到第几条，
    后面只拿新的那些，不需要让两个线程共享一份列表。
    """

    def __init__(self, limit=200):
        self.limit = limit
        self._events = []
        self._seq = 0
        self._lock = threading.Lock()

    def record(self, url, filename, source, outcome):
        """追加一条事件，返回它本身（UI 据此去重渲染）。"""
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "time": time.strftime("%H:%M:%S"),
                "url": url or "",
                "filename": filename or "",
                "source": source,
                "outcome": outcome,
            }
            self._events.append(event)
            if len(self._events) > self.limit:
                del self._events[:len(self._events) - self.limit]
            return dict(event)

    def since(self, seq):
        """取序号之后的事件；被缓冲挤掉的早期事件补不回来。"""
        with self._lock:
            return [dict(ev) for ev in self._events if ev["seq"] > seq]


capture_log = CaptureLog()


def add_capture(url, filename=None, source="clipboard", manager=None):
    """捕获落地：去重 → 建任务 → 启动 → 记流水。

    扩展端点和剪贴板以前各写一遍去重 + 建任务，口径散在两处；
    现在共用这一条路，顺带把事件写进 capture_log，桌面面板才有东西可看。
    返回 (task, outcome)：同一 URL 已有活跃任务时 outcome 为 duplicate，不重建（面板里看得见「这次为什么没动静」）。
    """
    import os
    import config
    if manager is None:
        from downloader import manager as manager
    for task in manager.get_all_tasks():
        if task.url == url and task.status in ("downloading", "paused", "pending"):
            capture_log.record(url, filename, source, "duplicate")
            return None, "duplicate"
    save_dir = config.get_download_dir()
    os.makedirs(save_dir, exist_ok=True)
    task = manager.create_task(
        url, save_dir, filename, config.clamp_segments(config.get("segments")))
    task.start()
    capture_log.record(url, filename, source, "added")
    return task, "added"


class BrowserCaptureHandler(BaseHTTPRequestHandler):
    """处理浏览器扩展发送的下载捕获请求"""
    manager = None  # 由外部设置

    def log_message(self, format, *args):
        pass  # 静默日志

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_POST(self):
        if self.path == "/capture":
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            try:
                data = json.loads(body)
                url = data.get("url", "").strip()
                filename = data.get("filename", "").strip()
                referrer = data.get("referrer", "")

                if url and url.startswith("http"):
                    self._add_download(url, filename or None)
                    resp = {"success": True, "message": "已捕获"}
                else:
                    capture_log.record(url, filename, "extension", "invalid")
                    resp = {"success": False, "message": "无效 URL"}
            except Exception as e:
                resp = {"success": False, "message": str(e)}

            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def _add_download(self, url, filename):
        if self.manager is None:
            from downloader import manager as mgr
            self.__class__.manager = mgr
        # 去重 / 建任务 / 记流水都在 add_capture 里，与剪贴板和 Web 端同口径
        _task, outcome = add_capture(url, filename, "extension", manager=self.manager)
        print(f"[Monitor] 捕获 {url[:60]} -> {outcome}")


class BrowserMonitor:
    """浏览器监控器：剪贴板 + HTTP 端点"""

    def __init__(self, port=5001, on_url_captured=None):
        self.port = port
        self.on_url_captured = on_url_captured  # 回调: (url, filename)
        self._server = None
        self._thread = None
        self._running = False
        self._clipboard_thread = None
        self._last_clipboard = ""
        self._enabled = True
        self._started = False  # 幂等保护：设置面板反复保存不会重复拉起线程

    def start(self):
        """启动 HTTP 服务器和剪贴板监听（幂等：已启动时直接返回）。"""
        if self._started:
            return
        self._started = True
        self._running = True

        # HTTP 服务器
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()

        # 剪贴板监听
        self._clipboard_thread = threading.Thread(target=self._run_clipboard_watcher, daemon=True)
        self._clipboard_thread.start()

        print(f"[Monitor] 浏览器监控已启动 (端口 {self.port})")

    def stop(self):
        """停止监控（幂等：未启动/已停止时直接返回）。

        shutdown() 只结束 serve_forever 循环，还需 server_close() 关闭监听
        socket 并 join 线程，否则立刻重启会因端口未释放而绑定失败。
        """
        if not self._started:
            return
        self._started = False
        self._running = False
        if self._server:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        self._thread = None

    def _run_server(self):
        try:
            # ThreadingHTTPServer：并发处理多个扩展请求，避免单请求阻塞监控端点
            self._server = ThreadingHTTPServer(("127.0.0.1", self.port), BrowserCaptureHandler)
            self._server.serve_forever()
        except OSError as e:
            print(f"[Monitor] 端口 {self.port} 被占用: {e}")

    def _run_clipboard_watcher(self):
        """剪贴板监控（检测 URL）"""
        try:
            import pyperclip
        except ImportError:
            print("[Monitor] pyperclip 未安装，剪贴板监控不可用")
            return

        recent = set()
        while self._running:
            try:
                text = pyperclip.paste()
                if text and text != self._last_clipboard:
                    self._last_clipboard = text
                    urls = URL_PATTERN.findall(text)
                    for url in urls:
                        url = url.rstrip(".,;:!?")
                        if url not in recent and self._is_download_url(url):
                            recent.add(url)
                            if len(recent) > 50:
                                recent.clear()
                            print(f"[Monitor] 剪贴板捕获: {url[:80]}...")
                            if self.on_url_captured:
                                self.on_url_captured(url, None)
                            else:
                                self._auto_add(url)
            except Exception:
                pass
            time.sleep(1)

    def _is_download_url(self, url):
        """判断 URL 是否为下载链接。

        旧规则「路径里出现 download 子串就算」误判极多：复制一个
        https://example.com/downloads 这样的普通页面分享链接，剪贴板监听
        就会凭空建出一个下载任务。而真实下载站大量使用无扩展名直链和
        query 带文件名的形式，旧规则又漏掉。

        现在按三条依次判断，任一命中即算下载：
        1. path 末段带已知下载扩展名；
        2. query 的 file/filename 等参数带已知下载扩展名；
        3. path 末段本身是 download 类动作词，且不带页面扩展名。
           末段判定避免把 /downloads/page/2 这类导航路径当成下载。
        """
        parsed = urlparse(url)
        path = parsed.path.lower()
        last_segment = path.rstrip("/").rsplit("/", 1)[-1]

        # 1. path 末段带下载扩展名
        if any(path.endswith(ext) for ext in DOWNLOAD_EXTENSIONS):
            return True

        # 2. query 里的文件名参数带下载扩展名（?file=report.pdf 这类）
        query = parse_qs(parsed.query.lower())
        for key in ("file", "filename", "name", "attachment", "download"):
            for value in query.get(key, []):
                if any(value.endswith(ext) for ext in DOWNLOAD_EXTENSIONS):
                    return True

        # 3. path 末段是下载动作词，且不像页面
        page_exts = (".html", ".htm", ".php", ".asp", ".aspx", ".jsp", ".shtml")
        if last_segment and not last_segment.endswith(page_exts):
            stem = last_segment.rsplit(".", 1)[0]
            if stem in _DOWNLOAD_ACTION_WORDS:
                return True
        return False

    def _auto_add(self, url):
        """自动添加下载（没有回调时由剪贴板监听直接走这条）"""
        task, _outcome = add_capture(url, None, "clipboard")
        if task is not None:
            print(f"[Monitor] 自动添加下载: {task.filename}")
