"""
IDM 风格下载管理器 —— Flask Web 后端
"""
import os
import json
import sys
import time
import subprocess
from flask import Flask, render_template, request, jsonify, Response
from flask_cors import CORS
from downloader import manager, DownloadManager, get_proxy_mode, set_proxy_mode
from media_service import media_registry
from throttle import get_rate, set_rate
from scheduler import scheduler
from media import ffmpeg_status, ytdlp_available
import config

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

# 默认下载目录：统一从共享配置读取（桌面 UI 设置的目录对 Web 端同样生效）
DEFAULT_DOWNLOAD_DIR = config.get_download_dir()
os.makedirs(DEFAULT_DOWNLOAD_DIR, exist_ok=True)


def _with_schedule(task):
    """把定时信息并到任务字典里，UI 才能显示「定时 …」而不是干等的等待中。"""
    d = task.to_dict()
    d["scheduled_at"] = scheduler.pending_at(task.task_id)
    return d

# 剪贴板 URL 暂存
_clipboard_url = ""


@app.route("/")
def index():
    return render_template("index.html")


# ==================== API 路由 ====================

@app.route("/api/tasks", methods=["GET"])
def get_tasks():
    """获取所有任务"""
    tasks = [_with_schedule(t) for t in manager.get_all_tasks()]
    stats = manager.get_stats()
    return jsonify({"tasks": tasks, "stats": stats})


@app.route("/api/add", methods=["POST"])
def add_task():
    """添加下载任务（普通直链 / 磁力 / 种子 / HLS / DASH / 网页视频解析）"""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    filename = (data.get("filename") or "").strip() or None
    try:
        segments = int(data.get("segments") or 8)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "线程数必须是 1-32 的整数"}), 400
    # 钳制到 1-32：无上限线程数会造成资源耗尽
    segments = max(1, min(32, segments))
    save_dir = data.get("save_dir") or config.get_download_dir()
    kind = (data.get("kind") or "auto").strip().lower()
    referer = (data.get("referer") or "").strip() or None
    cookies_netscape = data.get("cookies_netscape") or ""
    resolution = data.get("resolution") or None
    start_at = data.get("start_at") or None

    if not url:
        return jsonify({"success": False, "reason": "invalid_url",
                        "error": "URL 不能为空"}), 400
    is_http = url.startswith(("http://", "https://"))
    is_magnet = url.lower().startswith("magnet:")
    is_torrent = is_http and url.lower().endswith(".torrent")
    if not (is_http or is_magnet or is_torrent):
        return jsonify({"success": False, "reason": "invalid_url",
                        "error": "请输入有效的下载链接（HTTP/HTTPS、磁力链接或 .torrent 种子）"}), 400
    from media import VALID_KINDS, MediaError
    if kind not in VALID_KINDS:
        return jsonify({"success": False, "reason": "invalid_kind",
                        "error": f"不支持的下载类型: {kind}"}), 400
    if cookies_netscape and len(cookies_netscape) > 512 * 1024:
        return jsonify({"success": False, "reason": "invalid_url",
                        "error": "Cookie 数据过大，请清理浏览器 Cookie 后重试"}), 400

    os.makedirs(save_dir, exist_ok=True)
    try:
        task = manager.create_task(url, save_dir, filename, segments, kind,
                                   referer, cookies_netscape, resolution)
    except ValueError as e:
        return jsonify({"success": False, "reason": "invalid_kind", "error": str(e)}), 400

    try:
        preflight = getattr(task, "preflight", None)
        if preflight:
            preflight()
    except MediaError as e:
        manager.remove_task(task.task_id)          # 被拒绝的任务不留残骸
        return jsonify({"success": False, "reason": e.reason, "error": str(e)}), 409

    if start_at:
        scheduler.schedule(task.task_id, float(start_at))
    else:
        task.start()

    return jsonify({"success": True, "task": _with_schedule(task)})


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
    scheduler.unschedule(task_id)
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


@app.route("/api/retry_all", methods=["POST"])
def retry_all_tasks():
    """批量重试全部失败/已取消任务，返回重试成功数量。"""
    n = 0
    for t in manager.get_all_tasks():
        if t.status in ("failed", "cancelled") and t.retry():
            n += 1
    return jsonify({"success": True, "retried": n})


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


def _reveal_in_file_manager(path):
    """跳到系统文件管理器中的指定路径（跨平台）。"""
    if sys.platform == "win32":
        os.startfile(path)  # noqa: P201
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


@app.route("/api/open_download_dir", methods=["POST"])
def open_download_dir():
    """在系统文件管理器中打开当前下载目录（目录不存在时先创建）。"""
    path = config.get_download_dir()
    try:
        os.makedirs(path, exist_ok=True)
        _reveal_in_file_manager(path)
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
    return jsonify({"success": True, "path": path})


@app.route("/api/open_folder/<task_id>", methods=["POST"])
def open_folder(task_id):
    """Reveal the task file in the OS file manager (cross-platform)."""
    task = manager.get_task(task_id)
    if not task:
        return jsonify({"success": False, "error": "task not found"}), 404
    filepath = task.filepath
    folder = os.path.dirname(filepath) if filepath else ""
    if not folder or not os.path.isdir(folder):
        return jsonify({"success": False, "error": "folder not found"}), 404
    try:
        _reveal_in_file_manager(folder)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/api/remove/<task_id>", methods=["DELETE"])
def remove_task(task_id):
    scheduler.unschedule(task_id)
    manager.remove_task(task_id)
    return jsonify({"success": True})


@app.route("/api/clear_completed", methods=["POST"])
def clear_completed():
    n = manager.clear_completed()
    return jsonify({"success": True, "cleared": n})


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


@app.route("/api/settings", methods=["GET", "POST", "OPTIONS"])
def settings():
    if request.method == "OPTIONS":
        return _cors(app.make_default_options_response(), "GET, POST, OPTIONS")
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        # 代理模式: env(系统代理) / direct(直连) / 自定义地址
        if "proxy_mode" in data:
            set_proxy_mode(data["proxy_mode"])
            config.set("proxy_mode", data["proxy_mode"])
        if "rate_limit" in data:
            rl = set_rate(data["rate_limit"])
            config.set("rate_limit", rl)  # 持久化，重启后限速仍生效
        if "finish_action" in data:
            scheduler.set_finish_action(data["finish_action"])
        # 下载目录：持久化到共享配置，Web / 桌面 / 浏览器捕获三端统一生效
        if "download_dir" in data:
            new_dir = str(data["download_dir"]).strip()
            if new_dir and os.path.isdir(os.path.expanduser(new_dir)):
                config.set_download_dir(os.path.expanduser(new_dir))
            elif new_dir:
                return jsonify({"success": False,
                                "error": f"目录不存在: {new_dir}"}), 400
        return jsonify({
            "success": True,
            "proxy_mode": get_proxy_mode(),
            "download_dir": config.get_download_dir(),
            "rate_limit": get_rate(),
            "finish_action": scheduler.get_finish_action(),
        })
    st = scheduler.status()
    return jsonify({
        "download_dir": config.get_download_dir(),
        "default_segments": config.get("segments"),
        "proxy_mode": get_proxy_mode(),
        "proxy_modes": ["env", "direct", "custom"],
        "rate_limit": get_rate(),
        "finish_action": st["finish_action"],
        "finish_countdown": st["remaining"],
        "scheduled": st["scheduled"],
        "capabilities": {"ffmpeg": ffmpeg_status(), "ytdlp": ytdlp_available()},
    })


@app.route("/api/finish_action/cancel", methods=["POST"])
def cancel_finish_action():
    """取消「全部下载完成后 …」的倒计时。"""
    res = scheduler.cancel_finish_action()
    payload = {"success": True, "action": res["action"], "cancelled": res["cancelled"]}
    return jsonify(payload)


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

    save_dir = os.path.join(config.get_download_dir(), "_selftest")
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

def _stream_payload():
    """一帧 SSE 的内容：任务 + 统计 + 完成后动作状态。抽出来是为了能单测。"""
    st = scheduler.status()
    return {
        "tasks": [_with_schedule(t) for t in manager.get_all_tasks()],
        "stats": manager.get_stats(),
        "finish": {"action": st["finish_action"], "remaining": st["remaining"]},
    }


@app.route("/api/stream")
def stream():
    """Server-Sent Events 实时推送任务状态。

    无数据变化时每 15s 发送 keepalive 注释行，防止代理/负载均衡器因空闲超时断开连接。
    """
    def generate():
        last_stats = None
        idle_since = 0
        while True:
            # 仅在有变化时推送
            payload_str = json.dumps(_stream_payload())
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


# 浏览器接管总开关（由桌面 GUI “浏览器监控” 设置同步；关闭时不接管浏览器下载）
BROWSER_CAPTURE_ENABLED = True


def _cors(resp, methods="POST, OPTIONS"):
    """扩展从 chrome-extension:// 源访问本地端口，必须逐条放行。"""
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = methods
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
    return resp


def _media_options():
    return _cors(app.make_default_options_response())


@app.route("/api/media/discover", methods=["POST", "OPTIONS"])
def media_discover():
    """接收扩展嗅探到的媒体清单（只登记，不下载）。"""
    if request.method == "OPTIONS":
        return _media_options()
    if not BROWSER_CAPTURE_ENABLED:
        return _cors(jsonify({"ok": False, "reason": "disabled"}))

    data = request.get_json(force=True, silent=True) or {}
    tab_id = str(data.get("tab_id", "")).strip()
    items = data.get("items", [])
    if not tab_id or not isinstance(items, list):
        return _cors(jsonify({"ok": False,
                              "error": "缺少 tab_id 或 items 不是数组"})), 400
    added = media_registry.record(tab_id, data.get("page_url", ""),
                                  data.get("title", ""), items)
    return _cors(jsonify({"ok": True, "added": added, "count": media_registry.count()}))


@app.route("/api/media/list", methods=["GET", "OPTIONS"])
def media_list():
    """popup 拉取当前标签页嗅到的媒体列表。"""
    if request.method == "OPTIONS":
        return _media_options()
    tab_id = (request.args.get("tabId") or "").strip()
    if not tab_id:
        return _cors(jsonify({"ok": False, "error": "缺少 tabId"}), "GET, OPTIONS"), 400
    items = media_registry.list_for_tab(tab_id)
    return _cors(jsonify({
        "ok": True,
        "tab_id": tab_id,
        "page_url": items[0]["page_url"] if items else "",
        "title": items[0]["title"] if items else "",
        "items": items,
    }), "GET, OPTIONS")



@app.route("/api/browser-capture", methods=["POST", "OPTIONS"])
def browser_capture():
    """浏览器扩展捕获端点 —— 接收 Chrome 扩展发送的下载 URL"""
    if not BROWSER_CAPTURE_ENABLED:
        resp = jsonify({"success": False, "reason": "disabled"})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    if request.method == "OPTIONS":
        resp = app.make_default_options_response()
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        return resp

    data = request.get_json(force=True, silent=True) or {}
    url = data.get("url", "").strip()
    filename = data.get("filename", "").strip() or None

    # 允许 HTTP(S)、磁力链接、.torrent 种子
    is_http = url.startswith(("http://", "https://"))
    is_magnet = url.lower().startswith("magnet:")
    if not url or (not is_http and not is_magnet):
        return jsonify({"success": False, "error": "无效 URL"}), 400

    # 检查重复
    for t in manager.get_all_tasks():
        if t.url == url and t.status in ("downloading", "paused", "pending"):
            return jsonify({"success": True, "task": t.to_dict(), "duplicate": True})

    task = manager.create_task(url, config.get_download_dir(), filename, 8)
    task.start()

    resp = jsonify({"success": True, "task": task.to_dict()})
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


if __name__ == "__main__":
    print("\n" + "=" * 50)
    print("  IDM 风格下载管理器已启动")
    print(f"  下载目录: {config.get_download_dir()}")
    print(f"  打开浏览器访问: http://127.0.0.1:{SWIFTDM_PORT}")
    print("=" * 50 + "\n")
    scheduler.start()
    app.run(host=SWIFTDM_HOST, port=SWIFTDM_PORT, debug=False, threaded=True)
