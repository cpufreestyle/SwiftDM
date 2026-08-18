#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""对发布的 SwiftDM 二进制做严格端到端测试。

不依赖外网：本地起一个支持 Range 的 HTTP 服务器，用 sha256 校验下载完整性。
覆盖：分段下载 / 文件完整性 / 暂停-继续-取消 / 浏览器捕获端点。

用法：
    python test_binary.py
"""
import hashlib
import http.server
import functools
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "SwiftDM.exe")

FAILS = []


def fail(msg):
    FAILS.append(msg)
    print(f"  [FAIL] {msg}")


def ok(msg):
    print(f"  [ OK ] {msg}")


def wait_port(host, port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), 1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def find_free_port():
    """让 OS 分配一个当前空闲端口，避免残留进程占用默认 5000 造成干扰。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    # 测试客户端本身不走系统代理（本机代理 7897 当前宕机）
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    if not os.path.isfile(EXE):
        print(f"未找到二进制：{EXE}，请先 python build_exe.py")
        sys.exit(1)

    # 1) 准备本地测试文件（支持 Range 的静态服务器）
    tmp = tempfile.mkdtemp(prefix="swiftdm_test_")
    src = os.path.join(tmp, "sample.bin")
    data = os.urandom(3 * 1024 * 1024)  # 3MB 随机
    with open(src, "wb") as f:
        f.write(data)
    src_sha = sha256_of(src)

    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 8000),
        functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp),
    )
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    ok(f"本地服务器已启动 127.0.0.1:8000，源文件 sha256={src_sha[:12]}...")

    # 2) 启动二进制（强制直连，绕开宕机代理）
    # 先清理可能残留的旧进程（PyInstaller 单文件会 spawn 子进程，需按名清理）
    subprocess.run(["taskkill", "/F", "/IM", "SwiftDM.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    time.sleep(1)
    port = find_free_port()  # 用空闲端口，避开残留实例占用的 5000
    env = os.environ.copy()
    env["SWIFTDM_USE_PROXY"] = "direct"
    env["SWIFTDM_HOST"] = "127.0.0.1"
    env["SWIFTDM_PORT"] = str(port)
    proc = subprocess.Popen([EXE], env=env)
    try:
        if not wait_port("127.0.0.1", 5000, 30):
            fail("Flask 服务未在 30s 内启动")
            return
        ok("SwiftDM 二进制启动，Flask 服务就绪")

        base = f"http://127.0.0.1:{port}"

        # 3) 添加分段下载任务并校验完整性
        save_dir = os.path.join(tmp, "out")
        os.makedirs(save_dir, exist_ok=True)
        r = requests.post(f"{base}/api/add",
                          json={"url": "http://127.0.0.1:8000/sample.bin",
                                "filename": "sample.bin", "segments": 8,
                                "save_dir": save_dir}, timeout=15)
        if r.status_code != 200 or not r.json().get("success"):
            # 诊断：看看 5000 上到底是哪一个 app
            try:
                rt = requests.get(f"{base}/api/tasks", timeout=5)
                print(f"  [DIAG] GET /api/tasks -> {rt.status_code} {rt.text[:120]}")
            except Exception as e:
                print(f"  [DIAG] /api/tasks ERR {e}")
            try:
                rr = requests.get(f"{base}/", timeout=5)
                print(f"  [DIAG] GET / -> {rr.status_code} {rr.text[:120]}")
            except Exception as e:
                print(f"  [DIAG] / ERR {e}")
            fail(f"/api/add 失败: {r.status_code} {r.text[:120]}")
            return
        task_id = r.json()["task"]["task_id"]
        ok(f"/api/add 成功，task_id={task_id[:8]}")

        # 等待完成
        final = None
        for i in range(120):
            r = requests.get(f"{base}/api/tasks", timeout=10)
            for tk in r.json()["tasks"]:
                if tk["task_id"] == task_id:
                    final = tk
                    if tk["status"] in ("completed", "failed"):
                        break
            if final and final["status"] in ("completed", "failed"):
                break
            if i % 10 == 0:
                print(f"  ... 等待下载 ({(i // 2)}s) 当前状态={final['status'] if final else 'n/a'}")
            time.sleep(0.5)

        if not final:
            fail("任务状态查询失败")
        elif final["status"] != "completed":
            fail(f"下载未完成，状态={final['status']} 错误={final.get('error')}")
        else:
            out = os.path.join(save_dir, "sample.bin")
            if os.path.isfile(out) and sha256_of(out) == src_sha:
                ok(f"分段下载完成且文件完整 (sha256 一致, {final.get('size')} bytes)")
            else:
                fail("下载文件 sha256 与源不一致（分段/Range 处理有误）")

        # 4) 暂停-继续-取消
        r = requests.post(f"{base}/api/add",
                          json={"url": "http://127.0.0.1:8000/sample.bin",
                                "filename": "p2.bin", "segments": 4,
                                "save_dir": save_dir}, timeout=15)
        tid2 = r.json()["task"]["task_id"]
        time.sleep(0.3)
        pr = requests.post(f"{base}/api/pause/{tid2}", timeout=10)
        if pr.json().get("task", {}).get("status") != "paused":
            fail("暂停失败")
        else:
            ok("暂停成功")
        rr = requests.post(f"{base}/api/resume/{tid2}", timeout=10)
        if rr.json().get("task", {}).get("status") != "downloading":
            fail("继续失败")
        else:
            ok("继续成功")
        cr = requests.post(f"{base}/api/cancel/{tid2}", timeout=10)
        if not cr.json().get("success"):
            fail("取消失败")
        else:
            ok("取消成功")

        # 5) 浏览器捕获端点 (5001)
        if not wait_port("127.0.0.1", 5001, 10):
            fail("浏览器监控端口 5001 未启动")
        else:
            ok("浏览器监控端口 5001 就绪")
            cr2 = requests.post("http://127.0.0.1:5001/capture",
                                json={"url": "http://127.0.0.1:8000/sample.bin",
                                      "filename": "captured.bin"}, timeout=10)
            j = cr2.json()
            if j.get("success"):
                ok(f"浏览器捕获成功: {j.get('message')}")
            else:
                fail(f"浏览器捕获失败: {j}")

        # 汇总
        print()
        if FAILS:
            print(f"=== 测试未通过，{len(FAILS)} 项失败 ===")
            for f in FAILS:
                print(" -", f)
            sys.exit(1)
        else:
            print("=== 全部测试通过 ===")
    finally:
        # 按进程名杀掉真正的 app 子进程（PyInstaller 单文件会 spawn 子进程，
        # Popen 跟踪的引导器 PID 退出后子进程仍驻留，必须按名清理）
        subprocess.run(["taskkill", "/F", "/IM", "SwiftDM.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        try:
            proc.kill()
        except Exception:
            pass
        srv.shutdown()


if __name__ == "__main__":
    main()
