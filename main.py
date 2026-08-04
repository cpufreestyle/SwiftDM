"""
SwiftDM 入口 —— 启动浏览器监控 + PyQt6 桌面 UI
"""
import os
import sys
import threading
import signal

# 确保当前目录在 path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    print("\n" + "=" * 55)
    print("  SwiftDM - 高速多线程下载管理器")
    print("  IDM 风格 | 浏览器监控 | 多线程分段下载")
    print("=" * 55)

    # 1. 安装依赖（如果需要）
    try:
        import PyQt6
    except ImportError:
        print("\n正在安装 PyQt6...")
        import subprocess
        subprocess.check_call([sys.executable, "-m", "pip", "install", "PyQt6", "pyperclip", "-q"])
        print("依赖安装完成，请重新运行")
        return

    from PyQt6.QtWidgets import QApplication
    from PyQt6.QtCore import Qt

    # 2. 启动浏览器监控 HTTP 服务器
    from browser_monitor import BrowserMonitor

    monitor = BrowserMonitor(port=5001)

    def on_url_captured(url, filename):
        """浏览器捕获到 URL 时的回调"""
        from downloader import manager
        save_dir = os.path.join(os.path.expanduser("~"), "Downloads", "IDM_Downloads")
        os.makedirs(save_dir, exist_ok=True)
        # 检查是否已存在相同 URL 的任务
        for t in manager.get_all_tasks():
            if t.url == url and t.status in ("downloading", "paused", "pending"):
                print(f"[Monitor] URL 已存在任务中，跳过: {url[:60]}...")
                return
        task = manager.create_task(url, save_dir, filename, 8)
        task.start()
        print(f"[Monitor] 浏览器捕获下载: {task.filename}")

    monitor.on_url_captured = on_url_captured
    monitor.start()

    # 也可启动 Flask 服务器（保留 Web UI 访问）
    try:
        from app import app as flask_app

        def run_flask():
            flask_app.run(host="127.0.0.1", port=5000, debug=False, threaded=True, use_reloader=False)

        flask_thread = threading.Thread(target=run_flask, daemon=True)
        flask_thread.start()
        print("  Web UI: http://127.0.0.1:5000")
    except Exception as e:
        print(f"  (Web UI 未启动: {e})")

    # 3. 启动 PyQt6 桌面应用
    print("  浏览器监控端口: 5001")
    print("=" * 55 + "\n")

    app = QApplication(sys.argv)
    app.setApplicationName("SwiftDM")
    app.setQuitOnLastWindowClosed(False)  # 关闭窗口时隐藏到托盘

    from main_window import MainWindow
    window = MainWindow(http_port=5000)
    window.show()

    print("✓ SwiftDM 已启动！\n")

    # 退出时清理
    def cleanup():
        monitor.stop()

    app.aboutToQuit.connect(cleanup)

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
