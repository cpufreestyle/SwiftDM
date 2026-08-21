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


@app.route("/api/retry/<task_id>", methods=["POST"])
def retry_task(task_id):
    """重试失败/已取消的任务（失败任务从已有分片断点续传）"""
    task = manager.get_task(task_id)
    if not task:
        return jsonify({"success": False, "error": "任务不存在"}), 404
    ok = task.retry()
    if not ok:
        return jsonify({"success": False, "error": f"当前状态不支持重试: {task.status}"}), 400
    return jsonify({"success": True, "task": task.to_dict()})


@app.route("/api/open/<task_id>", methods=["POST"])
def open_file(task_id):
    """用系统默认程序打开已下载的文件（跨平台）"""
    import sys as _sys
    import subprocess as _sp
    task = manager.get_task(task_id)
    if not task:
        return jsonify({"success": False, "error": "任务不存在"}), 404
    filepath = task.filepath
    if not os.path.exists(filepath):
        return jsonify({"success": False, "error": "文件不存在"}), 404
    try:
        if _sys.platform == "win32":
            os.startfile(filepath)  # noqa: P201
        elif _sys.platform == "darwin":
            _sp.Popen(["open", filepath])
        else:
            _sp.Popen(["xdg-open", filepath])
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


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


# 本地测试文件内容（构建一次后缓存，多分段并发请求时避免重复生成 5MB 数据）
_LOCAL_TEST_DATA = None


def _local_test_bytes():
    global _LOCAL_TEST_DATA
    if _LOCAL_TEST_DATA is None:
        chunk = b"SwiftDM-test-payload-line\n"
        size = 5 * 1024 * 1024
        _LOCAL_TEST_DATA = (chunk * (size // len(chunk) + 1))[:size]
    return _LOCAL_TEST_DATA


@app.route("/api/local-test-file")
def local_test_file():
    """返回一个支持 HTTP Range 的本地测试文件（用于无外网时验证多分段下载链路）。

    支持 Range 请求（206 + Content-Range），与真实 CDN 行为一致，
    使 /api/self-test 能真正走多线程分段路径。
    """
    import re as _re
    data = _local_test_bytes()
    size = len(data)

    range_header = request.headers.get("Range", "")
    m = _re.match(r"^bytes=(\d*)-(\d*)$", range_header.strip())
    if m:
        start = int(m.group(1)) if m.group(1) else 0
        end = int(m.group(2)) if m.group(2) else size - 1
        end = min(end, size - 1)
        if start > end or start >= size:
            return Response(status=416, headers={"Content-Range": f"bytes */{size}"})
        piece = data[start:end + 1]
        return Response(
            piece, status=206,
            headers={
                "Content-Disposition": 'attachment; filename="swiftdm_local_test.bin"',
                "Content-Length": str(len(piece)),
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Accept-Ranges": "bytes",
            },
        )

    return Response(
        data,
        mimetype="application/octet-stream",
        headers={
            "Content-Disposition": 'attachment; filename="swiftdm_local_test.bin"',
            "Content-Length": str(size),
            "Accept-Ranges": "bytes",
        },
    )


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
    """Server-Sent Events 实时推送任务状态。

    无数据变化时每 15s 发送 keepalive 注释行，防止代理/负载均衡器因空闲超时断开连接。
    """
    def generate():
        last_stats = None
        idle_since = 0
        while True:
            tasks = [t.to_dict() for t in manager.get_all_tasks()]
            stats = manager.get_stats()
            payload = {"tasks": tasks, "stats": stats}

            # 仅在有变化时推送
            payload_str = json.dumps(payload)
            if payload_str != last_stats:
                last_stats = payload_str
                idle_since = 0
                yield f"data: {payload_str}\n\n"
            else:
                idle_since += 1  # 0.5s per tick

            # 15s 无数据变化时发送 keepalive 注释（SSE 规范: 以 : 开头的行被客户端忽略）
            if idle_since >= 30:
                yield ": keepalive\n\n"
                idle_since = 0

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
