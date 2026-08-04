"""
浏览器监控 —— 剪贴板监听 + HTTP 端点（供浏览器扩展调用）
"""
import re
import time
import threading
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse


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
        import os
        if self.manager is None:
            from downloader import manager as mgr
            self.__class__.manager = mgr
        save_dir = os.path.join(os.path.expanduser("~"), "Downloads", "IDM_Downloads")
        os.makedirs(save_dir, exist_ok=True)
        task = self.manager.create_task(url, save_dir, filename, 8)
        task.start()


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

    def start(self):
        """启动 HTTP 服务器和剪贴板监听"""
        self._running = True

        # HTTP 服务器
        self._thread = threading.Thread(target=self._run_server, daemon=True)
        self._thread.start()

        # 剪贴板监听
        self._clipboard_thread = threading.Thread(target=self._run_clipboard_watcher, daemon=True)
        self._clipboard_thread.start()

        print(f"[Monitor] 浏览器监控已启动 (端口 {self.port})")

    def stop(self):
        self._running = False
        if self._server:
            self._server.shutdown()

    def _run_server(self):
        try:
            self._server = HTTPServer(("127.0.0.1", self.port), BrowserCaptureHandler)
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
        """判断 URL 是否为下载链接"""
        parsed = urlparse(url)
        path = parsed.path.lower()
        if any(path.endswith(ext) for ext in DOWNLOAD_EXTENSIONS):
            return True
        # 包含 download 关键词
        if "download" in path:
            return True
        return False

    def _auto_add(self, url):
        """自动添加下载"""
        import os
        try:
            from downloader import manager
        except ImportError:
            return
        save_dir = os.path.join(os.path.expanduser("~"), "Downloads", "IDM_Downloads")
        os.makedirs(save_dir, exist_ok=True)
        task = manager.create_task(url, save_dir, None, 8)
        task.start()
        print(f"[Monitor] 自动添加下载: {task.filename}")
