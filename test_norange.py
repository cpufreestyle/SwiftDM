"""验证 P0-1 修复：不支持 Range 的服务器上「暂停→继续」不应损坏文件。

使用 Python 标准 http.server（不支持 Range 分段），下载中暂停再继续，
最终文件 sha256 必须与源一致。
"""
import hashlib
import os
import shutil
import socket
import subprocess
import sys
import time
import threading
import functools
import http.server
import logging
import requests

# 压制 http.server 的访问日志噪音（包括客户端主动 close 的连接异常）
http.server.SimpleHTTPRequestHandler.log_message = lambda self, *a, **k: None
logging.getLogger("http.server").setLevel(logging.CRITICAL)


def _silent_handle_error(self, request, client_address):
    # 客户端主动 close 导致的 ConnectionAbortedError 属预期，静默
    pass


http.server.ThreadingHTTPServer.handle_error = _silent_handle_error

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = os.path.join(HERE, "dist", "SwiftDM.exe")


def trust(s):
    s.proxies = {}  # 强制不走系统代理


def find_free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        os.environ.pop(k, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    if not os.path.isfile(EXE):
        print("未找到二进制，请先 python build_exe.py")
        sys.exit(1)

    # 生成测试源文件（4MB）
    tmp = os.path.join(HERE, "_tnr")
    if os.path.isdir(tmp):
        shutil.rmtree(tmp)
    os.makedirs(tmp, exist_ok=True)
    src = os.path.join(tmp, "src.bin")
    data = os.urandom(4 * 1024 * 1024)
    with open(src, "wb") as f:
        f.write(data)
    exp_sha = hashlib.sha256(data).hexdigest()

    # 启动不支持 Range 的本地服务器
    os.chdir(tmp)
    srv = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 8000),
        functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp),
    )
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    port = find_free_port()
    env = os.environ.copy()
    env["SWIFTDM_USE_PROXY"] = "direct"
    env["SWIFTDM_HOST"] = "127.0.0.1"
    env["SWIFTDM_PORT"] = str(port)
    subprocess.run(["taskkill", "/F", "/IM", "SwiftDM.exe"],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
    time.sleep(1)
    if os.environ.get("USE_SRC"):
        # 用源码版（python main.py --web-only）隔离「是否是打包问题」
        proc = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "main.py"), "--web-only"],
            env=env, cwd=HERE)
    else:
        proc = subprocess.Popen([EXE], env=env)
    base = f"http://127.0.0.1:{port}"

    # 等待启动
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), 1):
                break
        except OSError:
            time.sleep(0.3)

    try:
        save_dir = os.path.join(tmp, "out")
        os.makedirs(save_dir, exist_ok=True)
        r = requests.post(f"{base}/api/add",
                          json={"url": "http://127.0.0.1:8000/src.bin",
                                "filename": "src.bin", "segments": 6,
                                "save_dir": save_dir}, timeout=15)
        trust(r)
        tid = r.json()["task"]["task_id"]

        # 下载一会儿后暂停
        time.sleep(1.0)
        requests.post(f"{base}/api/pause/{tid}", timeout=10)
        time.sleep(0.8)
        # 继续
        requests.post(f"{base}/api/resume/{tid}", timeout=10)

        # 等待完成
        final = None
        for _ in range(120):
            r = requests.get(f"{base}/api/tasks", timeout=10)
            trust(r)
            for tk in r.json()["tasks"]:
                if tk["task_id"] == tid:
                    final = tk
                    if tk["status"] in ("completed", "failed"):
                        break
            if final and final["status"] in ("completed", "failed"):
                break
            time.sleep(0.5)

        ok = final and final["status"] == "completed"
        out = os.path.join(save_dir, "src.bin")
        got_sha = None
        if os.path.exists(out):
            h = hashlib.sha256()
            with open(out, "rb") as f:
                for b in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(b)
            got_sha = h.hexdigest()
        ok = ok and got_sha == exp_sha
        diag = ""
        if os.path.exists(out):
            sz = os.path.getsize(out)
            diag += f" out_size={sz} exp_size={len(data)}"
            if sz == len(data) and got_sha != exp_sha:
                # 找出第一处差异偏移
                with open(out, "rb") as f, open(src, "rb") as g:
                    off = 0
                    while True:
                        a = f.read(1 << 20)
                        b = g.read(1 << 20)
                        if not a and not b:
                            break
                        for i in range(min(len(a), len(b))):
                            if a[i] != b[i]:
                                diag += f" first_diff=at {off + i}"
                                break
                        else:
                            off += len(a)
                            continue
                        break
                # 额外：打印前 32 字节 hex，便于判断是否为 HTTP 响应头污染
                head = open(out, "rb").read(32)
                diag += " got_head=" + head.hex()
        # 列出残留的临时段目录
        parts = os.path.join(save_dir, ".src.bin.parts")
        if os.path.isdir(parts):
            diag += f" parts_left={os.listdir(parts)}"
        print(f"[{'OK' if ok else 'FAIL'}] 不支持Range + 暂停继续: "
              f"status={final['status'] if final else 'n/a'} "
              f"sha={'match' if got_sha == exp_sha else 'MISMATCH'} "
              f"(exp={exp_sha[:8]} got={got_sha[:8] if got_sha else 'none'}){diag}")
        with open(os.path.join(HERE, "_tnr_result.txt"), "w") as rf:
            rf.write(f"[{'OK' if ok else 'FAIL'}] status={final['status'] if final else 'n/a'} "
                     f"sha={'match' if got_sha == exp_sha else 'MISMATCH'} "
                     f"exp={exp_sha[:8]} got={got_sha[:8] if got_sha else 'none'}{diag}")
        sys.exit(0 if ok else 1)
    finally:
        subprocess.run(["taskkill", "/F", "/IM", "SwiftDM.exe"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        try:
            proc.kill()
        except Exception:
            pass
        srv.shutdown()


if __name__ == "__main__":
    main()
