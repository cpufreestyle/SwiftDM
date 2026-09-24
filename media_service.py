"""
流媒体嗅探结果缓存 —— 记录「哪个标签页的哪个网页上发现了哪些媒体资源」。

发现 ≠ 下载：这里只存清单，真正的下载由 /api/add 显式发起。
"""
import hashlib
import threading
import time
from collections import OrderedDict
from urllib.parse import urlsplit, urlunsplit

# 只剥掉纯统计类参数；带签名语义的参数（sign/sig/token/range/key/expires/...）一律保留，
# 否则归一化后的 URL 会失去下载权限。
_TRACKING_KEYS = {"spm", "from", "utm_source", "utm_medium", "utm_campaign", "scm", "__dna"}


def dedup_key_media_url(url):
    """去重键：同路径不同 query（签名/统计参数）视为同一媒体，只保留 scheme/host/path。"""
    parts = urlsplit((url or "").strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, "", ""))


def normalize_media_url(url):
    """归一化媒体 URL 用于去重：小写 scheme/host、去 fragment、丢统计参数、query 排序。"""
    parts = urlsplit((url or "").strip())
    netloc = parts.netloc.lower()
    if not parts.query:
        return urlunsplit((parts.scheme.lower(), netloc, parts.path, "", ""))
    pairs = []
    for chunk in parts.query.split("&"):
        if not chunk:
            continue
        key, _, value = chunk.partition("=")
        if key.lower() in _TRACKING_KEYS:
            continue
        pairs.append(f"{key}={value}" if value else key)
    query = "&".join(sorted(pairs))
    return urlunsplit((parts.scheme.lower(), netloc, parts.path, query, ""))


class MediaRegistry:
    """按标签页缓存嗅探结果，带每页上限、全局上限（LRU）与 TTL。"""

    def __init__(self, max_per_tab=50, max_total=2000, tab_ttl=60.0, clock=time.monotonic):
        self.max_per_tab = max_per_tab
        self.max_total = max_total
        self.tab_ttl = tab_ttl
        self._clock = clock
        self._tabs = OrderedDict()   # tab_id -> {"page_url", "seen", "items": OrderedDict[normalized_url, dict]}
        self._lock = threading.Lock()

    @staticmethod
    def _media_id(normalized_url):
        return hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()[:12]

    def _build_item(self, raw, page_url, title, now):
        url = (raw.get("url") or "").strip()
        if not url:
            return None
        is_mse = url.startswith("blob:") or url.startswith("mediastream:")
        norm = url if is_mse else normalize_media_url(url)
        try:
            nbytes = int(raw.get("bytes") or 0)
        except (TypeError, ValueError):
            nbytes = 0
        return {
            "media_id": self._media_id(norm),
            "url": url,
            "kind": (raw.get("kind") or "hls").strip().lower(),
            "quality_hint": (raw.get("quality_hint") or "").strip(),
            "bytes": nbytes,
            "is_mse": bool(raw.get("is_mse")) or is_mse,
            "page_url": page_url or "",
            "title": title or "",
            "detected_at": now,
        }

    def record(self, tab_id, page_url, title="", items=()):
        """写入一个标签页的嗅探结果；页面变了就整批替换。返回新增条数。"""
        tab_id = str(tab_id)
        page_url = page_url or ""
        now = self._clock()
        self._expire(now)
        with self._lock:
            tab = self._tabs.get(tab_id)
            if tab is not None and tab["page_url"] != page_url:
                self._tabs.pop(tab_id, None)
                tab = None
            if tab is None:
                tab = {"page_url": page_url, "seen": now, "items": OrderedDict()}
                self._tabs[tab_id] = tab
            tab["seen"] = now
            self._tabs.move_to_end(tab_id)
            added = 0
            for raw in items or ():
                if not isinstance(raw, dict):
                    continue
                item = self._build_item(raw, page_url, title, now)
                if item is None:
                    continue
                key = item["url"] if item["is_mse"] else dedup_key_media_url(item["url"])
                old = tab["items"].get(key)
                if old is None:
                    tab["items"][key] = item
                    added += 1
                    if len(tab["items"]) > self.max_per_tab:
                        tab["items"].popitem(last=False)
                else:
                    merged = dict(old)
                    for field in ("quality_hint", "bytes", "title"):
                        if item[field]:
                            merged[field] = item[field]
                    merged["detected_at"] = now
                    tab["items"][key] = merged
                    tab["items"].move_to_end(key)
            self._enforce_total()
            return added

    def list_for_tab(self, tab_id):
        tab_id = str(tab_id)
        self._expire(self._clock())
        with self._lock:
            tab = self._tabs.get(tab_id)
            if not tab:
                return []
            return [dict(i) for i in tab["items"].values()]

    def count(self):
        self._expire(self._clock())
        with self._lock:
            return sum(len(t["items"]) for t in self._tabs.values())

    def _enforce_total(self):
        """全局上限：从最久未活动的标签页开始丢条目（调用方已持锁）。"""
        while sum(len(t["items"]) for t in self._tabs.values()) > self.max_total:
            oldest_id = min(self._tabs, key=lambda k: self._tabs[k]["seen"])
            oldest = self._tabs[oldest_id]
            if not oldest["items"]:
                del self._tabs[oldest_id]
                continue
            victim = next(iter(oldest["items"]))
            del oldest["items"][victim]
            if len(oldest["items"]) < max(self.max_per_tab // 4, 1):
                # 丢到临界就让它自然过期，避免在总量上限附近反复抖动
                oldest["seen"] = self._clock() - self.tab_ttl

    def _expire(self, now):
        with self._lock:
            stale = [tid for tid, t in self._tabs.items() if now - t["seen"] > self.tab_ttl]
            for tid in stale:
                self._tabs.pop(tid, None)


media_registry = MediaRegistry()
