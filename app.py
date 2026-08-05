"""
IDM 风格下载管理器 —— Flask Web 后端
"""
import os
import json
import time
from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
from downloader import manager, DownloadManager, get_proxy_mode, set_proxy_mode

# 让本地回环地址绕过系统代理，避免浏览器经代理访问 127.0.0.1 出现 502
for _k in ("no_proxy", "NO_PROXY"):
    _existing = os.environ.get(_k, "")
    _cur = {a.strip() for a in _existing.split(",") if a.strip()}
    _cur |= {"127.0.0.1", "localhost", "[::1]"}
    os.environ[_k] = ",".join(sorted(_cur))

# 日志：文件 + 控制台（UI 不可用时也能留存错误）
from log_helper import setup_logging
setup_logging()

# 监听配置（可被环境变量覆盖，便于预览代理 / 局域网访问）
SWIFTDM_HOST = os.environ.get("SWIFTDM_HOST", "0.0.0.0")
SWIFTDM_PORT = int(os.environ.get("SWIFTDM_PORT", "5000"))

app = Flask(__name__)
CORS(app)

# 默认下载目录
DEFAULT_DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads", "IDM_Downloads")
os.makedirs(DEFAULT_DOWNLOAD_DIR, exist_ok=True)

# 剪贴板 URL 暂存
_clipboard_url = ""


@app.route("/")
def index():
    return render_template("index.html")


# ==================== API 路由 ====================

@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    """获取所有任务"""
    tasks = [t.to_dict() for t in manager.get_all_tasks()]
    stats = manager.get_stats()
    return jsonify({"tasks": tasks, "stats": stats})


@app.route("/api/add", methods=["POST"])
def add_task():
    """添加下载任务"""
    data = request.get_json()
    url = data.get("url", "").strip()
    filename = data.get("filename", "").strip() or None
    segments = int(data.get("segments", 8))
    save_dir = data.get("save_dir", DEFAULT_DOWNLOAD_DIR)

    if not url:
        return jsonify({"success": False, "error": "URL 不能为空"}), 400

    if not url.startswith(("http://", "https://")):
        return jsonify({"success": False, "error": "请输入有效的 HTTP/HTTPS 链接"}), 400

    os.makedirs(save_dir, exist_ok=True)
    task = manager.create_task(url, save_dir, filename, segments)
    task.start()

    return jsonify({"success": True, "task": task.to_dict()})


@app.route("/api/pause/<task_id>", methods=["POST"])
def pause_task(task_id):
    task = manager.get_task(task_id)
    if task:
        task.pause()
        return jsonify({"success": True, "task": task.to_dict()})
    return jsonify({"success": False, "error": "任务不存在"}), 404


@app.route("/api/resume/<task_id>", methods=["POST"])
def resume_task(task_id):
    task = manager.get_task(task_id)
    if task:
        task.resume()
        return jsonify({"success": True, "task": task.to_dict()})
    return jsonify({"success": False, "error": "任务不存在"}), 404


@app.route("/api/cancel/<task_id>", methods=["POST"])
def cancel_task(task_id):
    task = manager.get_task(task_id)
    if task:
        task.cancel()
    return jsonify({"success": True})


@app.route("/api/remove/<task_id>", methods=["DELETE"])
def remove_task(task_id):
    manager.remove_task(task_id)
    return jsonify({"success": True})


@app.route("/api/clear_completed", methods=["POST"])
def clear_completed():
    manager.clear_completed()
    return jsonify({"success": True})


@app.route("/api/pause_all", methods=["POST"])
def pause_all():
    for t in manager.get_all_tasks():
        if t.status == "downloading":
            t.pause()
    return jsonify({"success": True})


@app.route("/api/resume_all", methods=["POST"])
def resume_all():
    paused = [t for t in manager.get_all_tasks() if t.status == "paused"]
    for t in paused:
        t.resume()
    return jsonify({"success": True})


@app.route("/api/settings", methods=["GET", "POST"])
def settings():
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        # 代理模式: env(系统代理) / direct(直连) / 自定义地址
        if "proxy_mode" in data:
            set_proxy_mode(data["proxy_mode"])
        # 可扩展: 保存设置
        return jsonify({"success": True, "proxy_mode": get_proxy_mode()})
    return jsonify({
        "download_dir": DEFAULT_DOWNLOAD_DIR,
        "default_segments": 8,
        "proxy_mode": get_proxy_mode(),
        "proxy_modes": ["env", "direct"],
    })


@app.route("/api/local-test-file")
def local_test_file():
    """返回一个本地生成的多分段测试文件（用于无外网时验证下载链路）。

    文件内容可校验：由 'SwiftDM-test-' 重复填充，大小为 ~5MB，便于分 8 段下载。
    """
    import io
    size = 5 * 1024 * 1024
    chunk = b"SwiftDM-test-payload-line\n"
    data = (chunk * (size // len(chunk) + 1))[:size]
    bio = io.BytesIO(data)
    resp = Response(
        bio.getvalue(),
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="swiftdm_local_test.bin"',
            "Content-Length": str(len(data)),
            "Accept-Ranges": "bytes",
        },
    )
    return resp


@app.route("/api/self-test")
def self_test():
    """本机回环下载自测：用下载引擎下载 /api/local-test-file，验证全链路。"""
    import threading
    import time as _time
    from downloader import manager

    save_dir = os.path.join(DEFAULT_DOWNLOAD_DIR, "_selftest")
    os.makedirs(save_dir, exist_ok=True)
    url = request.host_url.rstrip("/") + "/api/local-test-file"

    task = manager.create_task(url, save_dir, "swiftdm_local_test.bin", segments=8)
    task.start()

    # 等待完成或失败（最多 30s）
    for _ in range(60):
        _time.sleep(0.5)
        if task.status in ("completed", "failed"):
            break

    ok = task.status == "completed"
    # 清理临时文件与已下载文件
    try:
        if os.path.exists(task.filepath):
            os.remove(task.filepath)
        if os.path.exists(task._tmp_dir):
            import shutil as _sh
            _sh.rmtree(task._tmp_dir, ignore_errors=True)
    except Exception:
        pass
    manager.remove_task(task.task_id)

    return jsonify({
        "success": ok,
        "status": task.status,
        "error": task.error,
        "proxy_mode": get_proxy_mode(),
    })


# ==================== SSE 实时推送 ====================

@app.route("/api/stream")
def stream():
    """Server-Sent Events 实时推送任务状态"""
    def generate():
        last_stats = None
        while True:
            tasks = [t.to_dict() for t in manager.get_all_tasks()]
            stats = manager.get_stats()
            payload = {"tasks": tasks, "stats": stats}

            # 仅在有变化时推送
            payload_str = json.dumps(payload)
            if payload_str != last_stats:
                last_stats = payload_str
                yield f"data: {payload_str}\n\n"

            time.sleep(0.5)
    return Response(generate(), mimetype="text/event-stream")


@app.route("/api/clipboard", methods=["POST"])
def set_clipboard():
    """接收从浏览器粘贴的 URL"""
    global _clipboard_url
    data = request.get_json()
    _clipboard_url = data.get("url", "")
    return jsonify({"success": True})


@app.route("/api/browser-capture", methods=["POST", "OPTIONS"])
def browser_capture():
    """浏览器扩展捕获端点 —— 接收 Chrome 扩展发送的下载 URL"""
    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url", "").strip()
    filename = data.get("filename", "").strip() or None

    if not url or not url.startswith("http"):
        return jsonify({"success": False, "error": "无效 URL"}), 400

    # 检查重复
    for t in manager.get_all_tasks():
        if t.url == url and t.status in ("downloading", "paused", "pending"):
            return jsonify({"success": True, "task": t.to_dict(), "duplicate": True})

    task = manager.create_task(url, DEFAULT_DOWNLOAD_DIR, filename, 8)
    task.start()

    resp = jsonify({"success": True, "task": task.to_dict()})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  IDM 风格下载管理器已启动")
    print(f"  下载目录: {DEFAULT_DOWNLOAD_DIR}")
    print(f"  打开浏览器访问: http://127.0.0.1:{SWIFTDM_PORT}")
    print("=" * 50 + "\n")
    app.run(host=SWIFTDM_HOST, port=SWIFTDM_PORT, debug=False, threaded=True)
