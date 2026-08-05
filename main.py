"""
SwiftDM 入口 —— 启动浏览器监控 + PyQt6 桌面 UI / Web 模式

502 修复要点：
- 系统若设置了 HTTP_PROXY，浏览器会把 127.0.0.1 也送进代理导致 502。
  这里在启动时把 localhost / 127.0.0.1 加入 NO_PROXY，
  并自动用「禁用代理」的浏览器实例打开界面，避免被代理拦截。
"""
import os
import sys
import shutil
import threading
import subprocess
import webbrowser

# 确保当前目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---- 关键：让本地回环地址绕过系统代理，避免 502 ----
for _k in ("no_proxy", "NO_PROXY"):
    _existing = os.environ.get(_k, "")
    _addrs = {"127.0.0.1", "localhost", "[::1]", "*.local"}
    _cur = {a.strip() for a in _existing.split(",") if a.strip()}
    _cur |= _addrs
    os.environ[_k] = ",".join(sorted(_cur))

# 日志
from log_helper import setup_logging
setup_logging()

# 服务监听配置（host 默认 0.0.0.0 以便预览代理/局域网可达；访问 URL 仍用 127.0.0.1）
SWIFTDM_HOST = os.environ.get("SWIFTDM_HOST", "0.0.0.0")
SWIFTDM_PORT = int(os.environ.get("SWIFTDM_PORT", "5000"))
SWIFTDM_MONITOR_PORT = int(os.environ.get("SWIFTDM_MONITOR_PORT", "5001"))


def _parse_args():
    """解析简单命令行参数，支持 --web-only / --no-ui（不启动桌面 UI）"""
    web_only = False
    for a in sys.argv[1:]:
        if a in ("--web-only", "--no-ui", "-w"):
            web_only = True
    return web_only


def _find_browser_exe():
    """查找本地的 Chrome / Edge 可执行文件，用于以禁用代理方式打开"""
    candidates = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        shutil.which("chrome"),
        shutil.which("msedge"),
        shutil.which("google-chrome"),
        shutil.which("chromium"),
    ]
    for c in candidates:
        if c and os.path.isfile(c):
            return c
    return None


def open_browser(url):
    """打开浏览器界面。优先用「禁用代理」的独立 Chrome/Edge 实例，避免 502。

    关键点：使用独立的临时用户目录(--user-data-dir)，确保拉起的是「新进程」，
    这样 --no-proxy-server 才会生效（否则会复用已打开的 Chrome 老进程，代理设置被忽略）。
    """
    exe = _find_browser_exe()
    if exe:
        try:
            import tempfile
            profile = os.path.join(tempfile.gettempdir(), "swiftdm_browser_profile")
            # 独立 profile + 禁用代理，直连 localhost，绕过系统代理导致的 502
            subprocess.Popen(
                [exe, f"--user-data-dir={profile}", "--no-proxy-server",
                 "--no-first-run", "--new-window", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print(f"  已用禁用代理的浏览器打开: {url}")
            print(f"  （若仍见 502，说明浏览器走了系统代理；可改用 start.bat --web 后手动访问，")
            print(f"    或在浏览器设置里把 127.0.0.1 / localhost 加入「代理绕过列表」）")
            return
        except Exception as e:
            print(f"  (专用浏览器启动失败，回退默认浏览器: {e})")
    # 回退：默认浏览器（用户若遇 502，请确认默认浏览器已把 localhost 加入代理例外）
    try:
        webbrowser.open(url)
        print(f"  已用默认浏览器打开: {url}")
    except Exception as e:
        print(f"  (自动打开浏览器失败，请手动访问: {url} — {e})")


def main():
    web_only = _parse_args()

    print("\n" + "=" * 55)
    print("  SwiftDM - 高速多线程下载管理器")
    print("  IDM 风格 | 浏览器监控 | 多线程分段下载")
    print("=" * 55)

    # 1. 安装依赖（如果需要）
    try:
        import PyQt6
    except ImportError:
        if not web_only:
            print("\n正在安装 PyQt6...")
            subprocess.check_call([sys.executable, "-m", "pip", "install", "PyQt6", "pyperclip", "-q"])
            print("依赖安装完成，请重新运行")
            return

    # 2. 启动浏览器监控 HTTP 服务器（端口 5001）
    from browser_monitor import BrowserMonitor

    monitor = BrowserMonitor(port=SWIFTDM_MONITOR_PORT)

    def on_url_captured(url, filename):
        """浏览器捕获到 URL 时的回调"""
        from downloader import manager
        save_dir = os.path.join(os.path.expanduser("~"), "Downloads", "IDM_Downloads")
        os.makedirs(save_dir, exist_ok=True)
        for t in manager.get_all_tasks():
            if t.url == url and t.status in ("downloading", "paused", "pending"):
                print(f"[Monitor] URL 已存在任务中，跳过: {url[:60]}...")
                return
        task = manager.create_task(url, save_dir, filename, 8)
        task.start()
        print(f"[Monitor] 浏览器捕获下载: {task.filename}")

    monitor.on_url_captured = on_url_captured
    monitor.start()

    # 3. 启动 Flask Web 服务器（后台线程，独立于 UI，UI 崩溃也不影响服务）
    from app import app as flask_app

    def run_flask():
        flask_app.run(host=SWIFTDM_HOST, port=SWIFTDM_PORT, debug=False,
                      threaded=True, use_reloader=False)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"  Web UI: http://127.0.0.1:{SWIFTDM_PORT}")

    # 4. 自动打开浏览器（禁用代理，规避 502）
    if not web_only:
        threading.Timer(1.5, open_browser, args=[f"http://127.0.0.1:{SWIFTDM_PORT}/"]).start()

    print(f"  浏览器监控端口: {SWIFTDM_MONITOR_PORT}")
    print("=" * 55 + "\n")

    if web_only:
        print("[OK] SwiftDM Web 模式已启动（无桌面 UI）。按 Ctrl+C 退出。\n")
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n正在退出...")
            monitor.stop()
        return

    # 5. 启动 PyQt6 桌面应用
    try:
        from PyQt6.QtWidgets import QApplication
        from main_window import MainWindow

        app = QApplication(sys.argv)
        app.setApplicationName("SwiftDM")
        app.setQuitOnLastWindowClosed(False)  # 关闭窗口时隐藏到托盘

        window = MainWindow(http_port=SWIFTDM_PORT)
        window.show()

        print("[OK] SwiftDM 已启动！\n")

        def cleanup():
            monitor.stop()

        app.aboutToQuit.connect(cleanup)
        sys.exit(app.exec())
    except Exception as e:
        print(f"  [警告] 桌面 UI 启动失败，但 Web 服务仍在运行: {e}")
        print(f"  请通过浏览器访问: http://127.0.0.1:{SWIFTDM_PORT}\n")
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            monitor.stop()


if __name__ == "__main__":
    main()
