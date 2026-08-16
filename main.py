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


def _port_bindable(host, port):
    """探测 (host, port) 是否可绑定（与 Werkzeug 一致使用 SO_REUSEADDR）。

    绑定 0.0.0.0 时同时探测 127.0.0.1：若回环地址已被其它进程以更精确地址占用，
    通配绑定虽能成功，但发往 127.0.0.1 的请求会被对端截走（如已启动的监控服务），
    因此两种情况都视为不可用。
    """
    import socket

    def _try_bind(h, p):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind((h, p))
                return True
            except OSError:
                return False

    if not _try_bind(host, port):
        return False
    if host in ("0.0.0.0", "") and not _try_bind("127.0.0.1", port):
        return False
    return True


def _pick_port(host, preferred, tries=5, avoid=()):
    """返回从 preferred 起第一个可用端口。

    macOS 的 AirPlay 接收（控制中心）默认占用 TCP 5000/7000，
    若不处理，Flask 会在启动时因端口冲突直接挂掉。这里自动向后找可用端口，
    并通过 avoid 排除本进程已占用的端口（避免 Web 与监控端口相互撞车）。
    """
    for cand in range(preferred, preferred + tries):
        if cand in avoid:
            continue
        if _port_bindable(host, cand):
            if cand != preferred:
                print(f"  [提示] 端口 {preferred} 被占用（macOS 的 AirPlay 接收功能默认占用 5000），"
                      f"已自动改用端口 {cand}")
                if sys.platform == "darwin" and preferred == 5000:
                    print("  （可在 系统设置 → 通用 → 隔空投送与接力 → 关闭「AirPlay 接收」释放 5000 端口）")
            return cand
    return preferred


def _parse_args():
    """解析简单命令行参数，支持 --web-only / --no-ui（不启动桌面 UI）"""
    web_only = False
    for a in sys.argv[1:]:
        if a in ("--web-only", "--no-ui", "-w"):
            web_only = True
    return web_only


def _find_browser_exe():
    """查找本地的 Chrome / Edge 可执行文件，用于以禁用代理方式打开（Windows / macOS / Linux）"""
    candidates = [
        # Windows
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        # macOS
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
        # Linux（PATH 查找，macOS Homebrew Cask 安装的浏览器也可能在 PATH 中）
        shutil.which("chrome"),
        shutil.which("msedge"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("brave-browser"),
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

    # 0. 解析实际可用端口（macOS AirPlay 默认占用 5000，自动向后寻找可用端口）
    # 监控端口优先固定在 5001（Chrome 扩展写死了 5001），Web 端口避开它
    monitor_port = _pick_port("127.0.0.1", SWIFTDM_MONITOR_PORT)
    web_port = _pick_port(SWIFTDM_HOST, SWIFTDM_PORT, avoid=(monitor_port,))

    # 1. 安装依赖（如果需要）—— 检查全部依赖，缺失则按 requirements.txt 补齐
    try:
        import PyQt6  # noqa: F401
        import flask  # noqa: F401
        import flask_cors  # noqa: F401
        import requests  # noqa: F401
        import pyperclip  # noqa: F401
    except ImportError:
        req = os.path.join(os.path.dirname(os.path.abspath(__file__)), "requirements.txt")
        print("\n检测到缺失依赖，正在安装 requirements.txt ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-r", req])
        print("依赖安装完成，请重新运行")
        return

    # 2. 启动浏览器监控 HTTP 服务器
    from browser_monitor import BrowserMonitor

    monitor = BrowserMonitor(port=monitor_port)

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
        flask_app.run(host=SWIFTDM_HOST, port=web_port, debug=False,
                      threaded=True, use_reloader=False)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print(f"  Web UI: http://127.0.0.1:{web_port}")

    # 4. 自动打开浏览器（禁用代理，规避 502）
    if not web_only:
        threading.Timer(1.5, open_browser, args=[f"http://127.0.0.1:{web_port}/"]).start()

    print(f"  浏览器监控端口: {monitor_port}")
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

        window = MainWindow(http_port=web_port)
        window.show()

        print("[OK] SwiftDM 已启动！\n")

        def cleanup():
            monitor.stop()

        app.aboutToQuit.connect(cleanup)
        sys.exit(app.exec())
    except Exception as e:
        print(f"  [警告] 桌面 UI 启动失败，但 Web 服务仍在运行: {e}")
        print(f"  请通过浏览器访问: http://127.0.0.1:{web_port}\n")
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            monitor.stop()


if __name__ == "__main__":
    main()
