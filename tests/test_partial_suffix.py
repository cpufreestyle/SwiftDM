"""下载中文件使用 .part 后缀的回归测试。

背景：合并分段时原先直接写 self.filepath，合并期间（耗时与文件大小同量级）
用户从资源管理器看到的是一个只写了一半的成品文件，会当成完整文件打开或移动。
修复见 downloader.py：_assemble_file 先写 <name>.part，等写句柄关闭后再
os.replace 原子改名；delete_files 也清理 .part，避免删任务后留下永久残留。
"""
import hashlib
import http.server
import os
import threading
import time

import pytest

import downloader

SIZE = 256 * 1024
BLOB = os.urandom(SIZE)
EXPECTED_SHA = hashlib.sha256(BLOB).hexdigest()


class _RangeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_GET(self):
        rng = self.headers.get("Range")
        if rng:
            start, end = rng.replace("bytes=", "").split("-")
            start, end = int(start), int(end)
            body = BLOB[start:end + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{SIZE}")
        else:
            body = BLOB
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass


@pytest.fixture()
def server():
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _RangeHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/file.bin"
    srv.shutdown()
    srv.server_close()


def _wait_terminal(task, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if task.status in ("completed", "failed", "cancelled"):
            return
        time.sleep(0.05)
    raise AssertionError(f"任务未在 {timeout}s 内结束，status={task.status}")


def test_download_writes_part_then_renames(server, tmp_path):
    """下载中落盘 .part，完成后原子改名成正式文件。"""
    task = downloader.DownloadTask("t1", server, str(tmp_path), segments=4)
    partial = task._partial_path()

    assert partial.endswith(".part")
    assert not os.path.exists(partial)

    task.start()
    try:
        seen_partial = False
        deadline = time.time() + 10
        while time.time() < deadline and task.status == "downloading":
            if os.path.exists(partial):
                seen_partial = True
            assert not os.path.exists(task.filepath), "下载中不该出现成品文件"
            time.sleep(0.02)
        _wait_terminal(task)
    finally:
        # 只在还在跑时才取消：cancel() 无条件覆写 status，
        # 对已完成任务调用会把它从 completed 改成 cancelled。
        if task.status == "downloading":
            task.cancel()

    assert task.status == "completed"
    assert seen_partial, "整个下载过程从未见过 .part，没覆盖到目标路径"
    assert os.path.isfile(task.filepath)
    assert not os.path.exists(partial)
    assert os.path.getsize(task.filepath) == SIZE
    with open(task.filepath, "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == EXPECTED_SHA


def test_delete_files_removes_partial(server, tmp_path):
    """删除任务时必须连 .part 一起清掉。"""
    task = downloader.DownloadTask("t2", server, str(tmp_path), segments=4)
    partial = task._partial_path()

    os.makedirs(task._tmp_dir, exist_ok=True)
    with open(partial, "wb") as f:
        f.write(BLOB[:1024])

    removed = task.delete_files()

    assert partial in removed
    assert not os.path.exists(partial)
    assert not os.path.exists(task._tmp_dir)

