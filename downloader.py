"""
IDM 风格下载引擎 —— 多线程分段下载、暂停/恢复
"""
import os
import re
import time
import shutil
import threading
import requests
from urllib.parse import urlparse, unquote


class DownloadTask:
    """单个下载任务"""

    def __init__(self, task_id, url, save_dir, filename=None, segments=8):
        self.task_id = task_id
        self.url = url
        self.save_dir = save_dir
        self.segments = segments  # 分段数
        self.status = "pending"   # pending | downloading | paused | completed | failed | cancelled
        self.progress = 0.0       # 0-100
        self.total_size = 0
        self.downloaded = 0
        self.speed = 0.0          # bytes/s
        self.eta = ""             # 预计剩余时间
        self.error = ""
        self.added_at = time.time()

        # 确定文件名
        if filename:
            self.filename = filename
        else:
            self.filename = self._extract_filename(url)

        self.filepath = os.path.join(save_dir, self.filename)
        self._tmp_dir = os.path.join(save_dir, f".{self.filename}.parts")

        # 内部控制
        self._lock = threading.Lock()
        self._pause_event = threading.Event()
        self._pause_event.set()  # 初始不暂停
        self._threads = []
        self._segment_progress = []   # 每段的已下载字节
        self._segment_offsets = []    # 每段的 [(start, end), ...]
        self._start_time = 0
        self._last_check_bytes = 0
        self._last_check_time = 0

    def _extract_filename(self, url):
        """从 URL 提取文件名"""
        parsed = urlparse(url)
        path = unquote(parsed.path)
        name = os.path.basename(path)
        if not name or "." not in name:
            # 尝试从 Content-Disposition 获取（在实际下载时）
            name = f"download_{int(time.time())}"
        return name

    def _fetch_info(self):
        """获取文件大小和最终文件名"""
        try:
            resp = requests.head(self.url, timeout=15, allow_redirects=True,
                                 headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            content_length = resp.headers.get("Content-Length")
            if content_length:
                self.total_size = int(content_length)

            # 尝试从 Content-Disposition 获取文件名
            cd = resp.headers.get("Content-Disposition", "")
            match = re.search(r'filename[*]?=["\']?([^"\';]+)', cd, re.IGNORECASE)
            if match:
                cd_name = unquote(match.group(1).strip())
                if cd_name:
                    self.filename = cd_name
                    self.filepath = os.path.join(self.save_dir, self.filename)
                    self._tmp_dir = os.path.join(self.save_dir, f".{self.filename}.parts")

            return True
        except Exception as e:
            self.error = str(e)
            return False

    def _calc_segments(self):
        """计算分段区间"""
        if self.total_size <= 0:
            # 未知大小，单段下载
            self.segments = 1
            self._segment_offsets = [(0, 0)]
            self._segment_progress = [0]
            return

        seg_size = self.total_size // self.segments
        self._segment_offsets = []
        for i in range(self.segments):
            start = i * seg_size
            end = start + seg_size - 1 if i < self.segments - 1 else self.total_size - 1
            self._segment_offsets.append((start, end))
        self._segment_progress = [0] * self.segments

    def _download_segment(self, idx):
        """下载单个分段"""
        start, end = self._segment_offsets[idx]
        seg_file = os.path.join(self._tmp_dir, f"part_{idx:04d}")

        # 恢复：从已有部分继续
        existing = 0
        restore_from = start
        if os.path.exists(seg_file):
            existing = os.path.getsize(seg_file)
            restore_from = start + existing

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        }
        if self.total_size > 0:
            headers["Range"] = f"bytes={restore_from}-{end}"

        try:
            resp = requests.get(self.url, headers=headers, stream=True, timeout=30,
                                allow_redirects=True)
            if resp.status_code not in (200, 206):
                raise Exception(f"HTTP {resp.status_code}")

            mode = "ab" if existing > 0 else "wb"
            with open(seg_file, mode) as f:
                for chunk in resp.iter_content(chunk_size=256 * 1024):
                    if not self._pause_event.is_set():
                        # 暂停中，等待恢复或取消
                        while not self._pause_event.is_set():
                            time.sleep(0.2)
                            if self.status in ("cancelled", "failed"):
                                return

                    if self.status in ("cancelled", "failed"):
                        return

                    f.write(chunk)
                    with self._lock:
                        self._segment_progress[idx] += len(chunk)
        except Exception as e:
            if self.status not in ("cancelled", "paused"):
                raise e

    def _all_segments_done(self):
        """检查所有分段是否下载完成"""
        for idx in range(self.segments):
            start, end = self._segment_offsets[idx]
            seg_file = os.path.join(self._tmp_dir, f"part_{idx:04d}")
            if not os.path.exists(seg_file):
                return False
            seg_size = os.path.getsize(seg_file)
            expected = (end - start + 1) if self.total_size > 0 else 0
            if expected > 0 and seg_size < expected:
                return False
        return True

    def _assemble_file(self):
        """合并分段文件"""
        with open(self.filepath, "wb") as out:
            for idx in range(self.segments):
                seg_file = os.path.join(self._tmp_dir, f"part_{idx:04d}")
                if os.path.exists(seg_file):
                    with open(seg_file, "rb") as inf:
                        while True:
                            data = inf.read(1024 * 1024)
                            if not data:
                                break
                            out.write(data)

        # 清理临时文件
        if os.path.exists(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def _monitor_progress(self):
        """监控下载进度和速度"""
        while self.status == "downloading":
            time.sleep(0.5)
            with self._lock:
                total_downloaded = sum(self._segment_progress)

            now = time.time()
            elapsed = now - self._start_time
            if elapsed > 0:
                if self._last_check_time > 0:
                    delta_bytes = total_downloaded - self._last_check_bytes
                    delta_time = now - self._last_check_time
                    self.speed = delta_bytes / delta_time if delta_time > 0 else 0
                else:
                    self.speed = total_downloaded / elapsed

            self._last_check_bytes = total_downloaded
            self._last_check_time = now
            self.downloaded = total_downloaded

            if self.total_size > 0:
                self.progress = min(100, (total_downloaded / self.total_size) * 100)
                if self.speed > 0:
                    remain_bytes = self.total_size - total_downloaded
                    remain_secs = remain_bytes / self.speed
                    if remain_secs < 60:
                        self.eta = f"{int(remain_secs)}s"
                    elif remain_secs < 3600:
                        self.eta = f"{int(remain_secs // 60)}m {int(remain_secs % 60)}s"
                    else:
                        h = int(remain_secs // 3600)
                        m = int((remain_secs % 3600) // 60)
                        self.eta = f"{h}h {m}m"
            else:
                self.progress = 0

            # 检查是否所有分段都已下载完成
            if self._all_segments_done():
                try:
                    self._assemble_file()
                    self.progress = 100.0
                    self.downloaded = self.total_size if self.total_size > 0 else total_downloaded
                    self.status = "completed"
                    self.speed = 0.0
                    self.eta = ""
                except Exception as e:
                    self.status = "failed"
                    self.error = f"合并文件失败: {e}"
                return

    def start(self):
        """启动下载"""
        if self.status in ("downloading", "completed"):
            return

        self.status = "downloading"
        self._pause_event.set()

        # 获取文件信息
        if not self._fetch_info():
            self.status = "failed"
            return

        # 计算分段
        self._calc_segments()

        # 创建临时目录
        os.makedirs(self._tmp_dir, exist_ok=True)

        # 如果文件已完整下载
        if self.total_size > 0 and os.path.exists(self.filepath):
            existing_size = os.path.getsize(self.filepath)
            if existing_size >= self.total_size:
                self.progress = 100
                self.status = "completed"
                self.downloaded = self.total_size
                return

        self._start_time = time.time()
        self._last_check_bytes = 0
        self._last_check_time = 0

        # 启动分段下载线程
        self._threads = []
        for idx in range(self.segments):
            t = threading.Thread(target=self._run_segment, args=(idx,), daemon=True)
            t.start()
            self._threads.append(t)

        # 启动进度监控线程
        monitor = threading.Thread(target=self._monitor_progress, daemon=True)
        monitor.start()
        self._threads.append(monitor)

    def _run_segment(self, idx):
        """在线程中运行分段下载"""
        try:
            self._download_segment(idx)
        except Exception as e:
            with self._lock:
                if self.status not in ("paused", "cancelled", "completed"):
                    self.status = "failed"
                    self.error = str(e)

    def pause(self):
        """暂停下载"""
        self.status = "paused"
        self._pause_event.clear()

    def resume(self):
        """恢复下载"""
        if self.status != "paused":
            return
        self.status = "downloading"
        self._pause_event.set()

        # 重新启动分段线程
        self._threads = []
        for idx in range(self.segments):
            t = threading.Thread(target=self._run_segment, args=(idx,), daemon=True)
            t.start()
            self._threads.append(t)

        # 进度监控
        monitor = threading.Thread(target=self._monitor_progress, daemon=True)
        monitor.start()
        self._threads.append(monitor)

    def cancel(self):
        """取消下载"""
        self.status = "cancelled"
        self._pause_event.set()  # 唤醒等待的线程

        # 清理临时文件
        if os.path.exists(self._tmp_dir):
            shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "filename": self.filename,
            "url": self.url,
            "status": self.status,
            "progress": round(self.progress, 1),
            "total_size": self.total_size,
            "downloaded": self.downloaded,
            "speed": self.speed,
            "eta": self.eta,
            "error": self.error,
            "segments": self.segments,
        }


class DownloadManager:
    """下载管理器"""

    def __init__(self):
        self._tasks = {}      # task_id -> DownloadTask
        self._counter = 0
        self._lock = threading.Lock()

    def create_task(self, url, save_dir, filename=None, segments=8):
        with self._lock:
            self._counter += 1
            task_id = f"dl_{self._counter}"
            task = DownloadTask(task_id, url, save_dir, filename, segments)
            self._tasks[task_id] = task
            return task

    def get_task(self, task_id):
        return self._tasks.get(task_id)

    def get_all_tasks(self):
        return list(self._tasks.values())

    def remove_task(self, task_id):
        with self._lock:
            task = self._tasks.get(task_id)
            if task and task.status in ("downloading", "paused"):
                task.cancel()
            self._tasks.pop(task_id, None)

    def clear_completed(self):
        with self._lock:
            completed = [tid for tid, t in self._tasks.items() if t.status in ("completed", "cancelled", "failed")]
            for tid in completed:
                self._tasks.pop(tid, None)

    def get_stats(self):
        tasks = self.get_all_tasks()
        total_speed = sum(t.speed for t in tasks if t.status == "downloading")
        active = sum(1 for t in tasks if t.status == "downloading")
        completed = sum(1 for t in tasks if t.status == "completed")
        failed = sum(1 for t in tasks if t.status == "failed")
        paused = sum(1 for t in tasks if t.status == "paused")
        return {
            "total_speed": total_speed,
            "active": active,
            "completed": completed,
            "failed": failed,
            "paused": paused,
            "total": len(tasks),
        }


# 全局实例
manager = DownloadManager()
