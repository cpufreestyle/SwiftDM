"""
SwiftDM 的 BT / PT 下载支持 —— 基于 libtorrent 的 TorrentTask。

与 downloader.py 的 DownloadTask 保持完全一致的生命周期接口
（start / pause / resume / cancel / retry / to_dict，以及相同的
status / progress / total_size / downloaded / speed / eta / error 字段），
这样 API 层、Web UI、桌面 UI 无需任何改动即可直接驱动 BT 任务。

支持的链接形式：
  - magnet:?xt=urn:btih:...                  （磁力链接，可带 &tr= 私有 Tracker）
  - http(s)://.../xxx.torrent                 （.torrent 文件，PT 私有种子的 passkey
                                               已内嵌在 announce 里，直接下载即可用）

说明：
  - 所有 BT 任务共享一个 libtorrent session（进程级单例），便于做全局连接/速度管理。
  - 对等连接（peer）一律直连，不走 HTTP 代理；只有 Tracker 的 HTTP announce 在设置了
    SWIFTDM_TORRENT_PROXY 时才走代理（多数家庭网络下 Tracker 也可直连，默认直连）。
  - 私有种子（PT）的 DHT/PEX/LSD 由 libtorrent 依据 torrent 的 private 标志自动禁用，
    仅通过 .torrent / magnet 中携带的 Tracker 做种源交换，符合 PT 规则。
"""
import os
import re
import time
import threading
import logging
import requests
from urllib.parse import urlparse

import libtorrent as lt

logger = logging.getLogger("SwiftDM")

# 共享 session（进程级单例）
_session = None
_session_lock = threading.Lock()


def _apply_session_settings(s):
    """配置全局 BT session 的参数。"""
    settings = {
        "enable_dht": True,
        "enable_lsd": True,
        "enable_upnp": True,
        "enable_natpmp": True,
        "announce_to_all_trackers": True,
        "num_optimistic_unchoke_slots": 4,
        "cache_size": -1,
        "active_downloads": -1,
        "active_seeds": -1,
        "active_limit": -1,
    }
    # 可选：仅 Tracker announce 走代理（peer 仍直连）。格式如 http://127.0.0.1:7897
    proxy = os.environ.get("SWIFTDM_TORRENT_PROXY", "").strip()
    if proxy:
        try:
            parsed = urlparse(proxy)
            ptype = 2 if proxy.startswith("socks") else 1  # 2=socks5, 1=http
            settings.update({
                "proxy_type": ptype,
                "proxy_hostname": parsed.hostname or "",
                "proxy_port": parsed.port or 0,
                "proxy_username": "",
                "proxy_password": "",
                "proxy_peer_connections": False,  # peer 直连，仅 tracker 经代理
            })
            logger.info("BT Tracker 代理已启用: %s", proxy)
        except Exception as e:
            logger.warning("解析 SWIFTDM_TORRENT_PROXY 失败，忽略代理: %s", e)
    s.apply_settings(settings)


def _get_session():
    """获取（或创建）共享的 libtorrent session。"""
    global _session
    with _session_lock:
        if _session is None:
            _session = lt.session()
            _apply_session_settings(_session)
        return _session


def _fetch_torrent_bytes(url):
    """下载 .torrent 文件字节（走系统/自定义代理，与下载引擎一致）。

    PT 站点通常要求通过代理访问，这里继承 downloader 的代理模式判断。
    """
    from downloader import get_proxy_mode
    mode = get_proxy_mode()
    proxies = None
    if mode == "direct":
        proxies = {"http": None, "https": None}
    elif mode not in ("env", "", "system"):
        proxies = {"http": mode, "https": mode}
    # 127.0.0.1/localhost 强制不走代理
    no_proxy = "127.0.0.1,localhost"
    resp = requests.get(
        url, timeout=30, proxies=proxies,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    if resp.status_code != 200:
        raise Exception(f"下载 .torrent 失败: HTTP {resp.status_code}")
    return resp.content


def _hash_from_magnet(url):
    m = re.search(r"btih:([0-9a-fA-F]{40})", url)
    if m:
        return m.group(1).lower()
    return "unknown"


class TorrentTask:
    """BT / PT 下载任务（接口对齐 DownloadTask）。"""

    def __init__(self, task_id, url, save_dir, filename=None, segments=0):
        self.task_id = task_id
        self.url = url
        self.save_dir = save_dir
        self.segments = segments  # 兼容字段：BT 下填充为文件数
        self.status = "pending"
        self.progress = 0.0
        self.total_size = 0
        self.downloaded = 0
        self.speed = 0.0
        self.eta = ""
        self.error = ""
        self.filename = filename or f"magnet_{_hash_from_magnet(url)[:8]}"
        self.filepath = os.path.join(save_dir, self.filename)

        self.protocol = "torrent"
        self.seeds = 0
        self.peers = 0

        self._handle = None          # libtorrent torrent_handle
        self._session = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = None
        self._meta_ready = False
        self._started_at = 0
        self._last_total = 0

    # ---------------- 控制接口 ----------------

    def start(self):
        # guard 必须在锁内：否则 cancel 落在「guard 通过」与「持锁」之间时，
        # 会被下面覆写成 downloading，把已取消的任务复活
        with self._lock:
            if self.status in ("downloading", "completed"):
                return
            if self._handle is not None:
                # 已存在句柄（retry 场景）：恢复即可
                self._handle.resume()
                self.status = "downloading"
                self._stop.clear()
                self._ensure_monitor()
                return
            try:
                self._session = _get_session()
                if self.url.startswith("magnet:"):
                    self._handle = lt.add_magnet_uri(
                        self._session, self.url,
                        {"save_path": self.save_dir,
                         "storage_mode": lt.storage_mode_t.storage_mode_sparse},
                    )
                else:
                    # .torrent 文件（http/https）
                    data = _fetch_torrent_bytes(self.url)
                    ti = lt.torrent_info(data)
                    name = ti.name() or self.filename
                    if not self.filename or self.filename.startswith("magnet_"):
                        self.filename = name
                    self.filepath = os.path.join(self.save_dir, self.filename)
                    self.segments = max(ti.num_files(), 1)
                    self.total_size = ti.total_size()
                    self._meta_ready = True
                    self._handle = self._session.add_torrent(
                        {"save_path": self.save_dir, "ti": ti,
                         "storage_mode": lt.storage_mode_t.storage_mode_sparse},
                    )
                self.status = "downloading"
                self._started_at = time.time()
                self._stop.clear()
                self._ensure_monitor()
                logger.info("BT 任务已添加: %s (save=%s)", self.url, self.save_dir)
            except Exception as e:
                self.status = "failed"
                self.error = str(e)
                logger.error("BT 任务启动失败: %s | %s", self.url, e)

    def pause(self):
        with self._lock:
            if self.status != "downloading":
                return
            if self._handle is not None:
                self._handle.pause()
            self.status = "paused"

    def resume(self):
        with self._lock:
            if self.status != "paused":
                return
            if self._handle is not None:
                self._handle.resume()
            self.status = "downloading"

    def cancel(self):
        with self._lock:
            self._stop.set()
            if self._handle is not None and self._session is not None:
                try:
                    self._session.remove_torrent(self._handle,
                                                 lt.options_t.delete_files)
                except Exception:
                    pass
                self._handle = None
            self.status = "cancelled"

    def retry(self):
        # start() 的整个临界区（含拉取 .torrent 的网络 IO）都在 self._lock 内，
        # 这里同样持锁调用（RLock 可重入），保证「重置 + 启动」原子完成：否则并发
        # cancel 落在重置之后、start() 之前，会被 start() 覆写成 downloading
        with self._lock:
            if self.status not in ("failed", "cancelled"):
                return False
            self.error = ""
            self.downloaded = 0
            self.progress = 0.0
            self.total_size = 0
            self._meta_ready = False
            self._handle = None
            self.status = "pending"
            self.start()
        return True

    # ---------------- 监控线程 ----------------

    def _ensure_monitor(self):
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._monitor, daemon=True)
            self._thread.start()

    def _monitor(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:
                logger.warning("BT 监控异常: %s", e)
            # 仅在有活动下载时高频刷新；完成/失败后退出监控线程
            if self.status in ("completed", "failed", "cancelled"):
                break
            time.sleep(1.0)

    def _tick(self):
        if self._handle is None:
            return
        st = self._handle.status()

        # 元数据就绪后补全文件名 / 大小（磁力链接场景）
        if not self._meta_ready and st.name:
            self._meta_ready = True
            if not self.filename or self.filename.startswith("magnet_"):
                self.filename = st.name
            self.filepath = os.path.join(self.save_dir, self.filename)
            try:
                self.segments = max(self._handle.get_torrent_info().num_files(), 1)
            except Exception:
                pass

        self.total_size = st.total if st.total else 0
        self.downloaded = st.total_done
        self.speed = st.download_rate
        self.seeds = st.num_seeds
        self.peers = st.num_peers

        if st.total and st.total > 0 and st.download_rate > 0 and st.total_done < st.total:
            remain = (st.total - st.total_done) / st.download_rate
            if remain < 60:
                self.eta = f"{int(remain)}s"
            elif remain < 3600:
                self.eta = f"{int(remain // 60)}m {int(remain % 60)}s"
            else:
                self.eta = f"{int(remain // 3600)}h {int((remain % 3600) // 60)}m"
        else:
            self.eta = ""

        if st.total and st.total > 0:
            self.progress = round(min(100.0, st.progress * 100.0), 1)
        else:
            self.progress = 0.0

        # 完成判定：所有分片已下载完成（含校验）
        if st.is_finished or (st.total and st.total > 0 and st.progress >= 1.0
                              and not st.need_save_resume_data):
            self.status = "completed"
            self.progress = 100.0
            return

        # 错误判定：种子元数据拿到了、但完全无 peer 且无进展且 libtorrent 报了错误，
        # 视为真正失败（仅显示 tracker 报错但不影响下载的不直接判失败）。
        err = getattr(st, "error", "") or ""
        if err and self.progress <= 0 and st.num_connections == 0 \
                and (time.time() - self._started_at) > 20:
            self.status = "failed"
            self.error = err
            return

        # 把 tracker 报错作为提示（不阻塞下载）
        if err and not self.error:
            self.error = err

    # ---------------- 状态导出 ----------------

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
            "protocol": self.protocol,
            "seeds": self.seeds,
            "peers": self.peers,
        }
