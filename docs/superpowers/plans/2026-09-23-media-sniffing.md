# 流媒体嗅探与下载调度 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 SwiftDM 嗅探网页里的 HLS/DASH/直链媒体并调用 yt-dlp 下载，同时补齐限速、定时下载与完成后动作这三项主流下载软件的收费功能。

**Architecture:** 双引擎按 `kind` 分流（方案 A）——`DownloadTask` 继续负责可 Range 的普通文件，新增 `MediaTask` 用 yt-dlp 的 Python API 负责 m3u8/mpd/网页视频；两者接口（`status/progress/speed/to_dict/pause/resume/cancel/retry`）完全兼容，`DownloadManager` 与现有 SSE、历史记录直接复用（SSE 只往同一帧里追加 `scheduled_at / finish` 字段，不改协议）。嗅探在 Chrome 扩展侧完成（webRequest + content script 双层），结果写入后端 `MediaRegistry`（按标签页缓存、去重、TTL），popup 展示列表并把用户显式选中的条目连同 Referer/Cookie 发回 `/api/add`。限速与调度是两个独立的全局单例（`throttle.py` 令牌桶、`scheduler.py` 的 5 秒扫描线程 `SCAN_INTERVAL`），分别挂进分段读循环和 `pending` 任务队列。

**Tech Stack:** Python 3.13 / Flask（threaded）/ requests / pytest 9 / yt-dlp（Python API，新增依赖）/ 系统 ffmpeg（不打包，缺失时优雅降级）/ Chrome MV3 扩展（原生 JS）/ 内嵌单页 Web UI（原生 JS + SSE）。

**Spec:** `docs/superpowers/specs/2026-09-23-media-sniffing-design.md`（本计划逐条实现该 spec；冲突时以 spec 为准并在提交信息里说明）

## Global Constraints

- DRM 边界：只检测、只拒绝，绝不解密、绝不绕过 Widevine/FairPlay/SAMPLE-AES。AES-128（yt-dlp 可用明文 key 解密）允许。
- Cookie 只允许存在于单次任务的临时文件里，`finally` 删除；不得写入日志、`history.json`、`to_dict()` 或异常文本。
- 新增第三方依赖只有 `yt-dlp`（进 `requirements.txt` + `build_exe.py` 的 `--hidden-import`）；ffmpeg 走系统 PATH 或 `SWIFTDM_FFMPEG`，程序绝不自动下载二进制。
- 所有测试必须离线：不得访问外网（集成测试只连 `127.0.0.1` 的合成服务器）、不得真实关机/休眠、不得要求本机安装 ffmpeg 或 yt-dlp（单测用注入的假模块）。唯一例外是 Task 14 的集成测试：它用 `pytest.importorskip("yt_dlp")` 自行跳过，本机没装 yt-dlp/ffmpeg 时整套测试仍然是绿的。
- 任务对象必须保持接口兼容：`status ∈ {pending,downloading,paused,completed,failed,cancelled}`，`to_dict()` 至少含 `task_id,filename,filepath,url,status,progress,total_size,downloaded,speed,eta,error,segments,save_dir`。
- 界面文案沿用现有风格：简短中文、动词开头、不加句号；错误提示说明「怎么办」而不是抛异常栈。
- 提交信息用中文 + 现有前缀（`feat:` / `fix:` / `test:` / `docs:`）。**未经用户明确要求不要 push。**
- 扩展 JS 改动必须通过 `node --check`；`background.js` 是 MV3 service worker，里面不存在 `document`。
- 本机环境坑：`127.0.0.1:5000` 可能被无关进程占用（预览统一用 5080/5081）；系统代理是 Clash `127.0.0.1:7897`，访问本地端口需 `NO_PROXY` 含 `127.0.0.1`；读 JSON/文本要显式 `encoding="utf-8"`（默认 GBK 会炸）。
- 改 `downloader.py` 后必须重跑 `python test_norange.py`（需要 `dist/SwiftDM.exe`，先 `python build_exe.py`）。

---

## File Structure

| 文件 | 动作 | 职责 |
|------|------|------|
| `tests/conftest.py` | 新建 | 把仓库根加进 `sys.path`，让测试能 `import app/downloader/media` |
| `media_service.py` | 新建 | `MediaRegistry`：按标签页缓存嗅探结果（归一化去重、每页 50 条、全局 2000 条 LRU、60s TTL、换页即清空） |
| `media.py` | 新建 | `classify_kind()` 分流判据、`ffmpeg_status()`、`MediaError` 家族、`MediaTask`（yt-dlp 适配器） |
| `throttle.py` | 新建 | 全局令牌桶限速（`set_rate/get_rate/consume`），0 = 不限速 |
| `scheduler.py` | 新建 | 定时启动扫描线程 + 完成后动作（关机/休眠/提示音）与撤销 |
| `downloader.py` | 修改 | `create_task()` 按 kind 分流；历史记录带上 kind/resolution/referer；分段读循环调用 `throttle.consume()` |
| `app.py` | 修改 | `/api/add` 扩展字段 + 409 语义；`/api/media/discover` `/api/media/list`；`/api/settings` 扩展；SSE 注入 `scheduled_at` |
| `extension/sniff.js` | 新建 | 纯函数嗅探规则 + Cookie→Netscape 转换（service worker / content script / node 测试共用） |
| `extension/background.js` | 修改 | 加宽 webRequest 嗅探、上报 discover、popup 消息桥、Cookie 采集 |
| `extension/content.js` | 新建 | DOM 层 `<video>/<source>` 扫描 + MSE(blob) 标记 |
| `extension/manifest.json` | 修改 | `tabs`/`cookies` 权限 + `content_scripts` |
| `extension/popup.html` `.js` | 修改 | 媒体列表面板与「下载」按钮 |
| `templates/index.html` | 修改 | 添加任务的分辨率/定时控件、任务卡 kind/定时徽标、设置面板（限速 + 完成后动作 + ffmpeg 状态） |
| `main.py` | 修改 | 桌面模式启动时拉起 `scheduler.start()`（pytest 里绝不启动扫描线程） |
| `requirements.txt` / `build_exe.py` | 修改 | yt-dlp 依赖与打包隐藏导入 |
| `tests/test_media_registry.py` | 新建 | Task 1：`MediaRegistry` 归一化去重、上限、TTL |
| `tests/test_media_api.py` | 新建 | Task 2：`/api/media/discover`、`/api/media/list` |
| `tests/test_media_classify.py` | 新建 | Task 3：`classify_kind()` 分流判据 + `ffmpeg_status()` |
| `tests/test_throttle.py` | 新建 | Task 4：令牌桶速率、热更新、0=不限速 |
| `tests/test_media_task.py` | 新建 | Task 5：`MediaTask`（假 yt_dlp）进度、取消、DRM 预检、cookie 临时文件清理 |
| `tests/test_dispatch.py` | 新建 | Task 6：`create_task` 按 kind 分流、`/api/add` 扩展字段与 409、定时入队 |
| `tests/test_sniff.js` | 新建 | Task 7：嗅探规则纯函数 + Cookie→Netscape（Node 原生 assert） |
| `tests/test_background_load.js` | 新建 | Task 8：background.js 在 vm 沙箱里可加载、消息监听器唯一 |
| `tests/test_scheduler.py` | 新建 | Task 11：定时启动、倒计时撤销、完成后动作与幂等 |
| `tests/test_scheduler_api.py` | 新建 | Task 11：`/api/settings` 的 finish_action、撤销端点、SSE `finish` 载荷 |
| `tests/test_web_ui.py` | 新建 | Task 12：添加栏高级面板、kind/分辨率/定时控件、任务卡徽标 |
| `tests/test_settings_panel.py` | 新建 | Task 13：设置弹窗、限速输入、完成后动作与能力展示 |
| `tests/test_media_integration.py` | 新建 | Task 14：本机合成 HLS 服务器端到端（缺 yt-dlp/ffmpeg 时自动跳过） |
| `docs/superpowers/plans/2026-09-23-media-sniffing-acceptance.md` | 新建 | Task 15：exe 与浏览器手工验收记录 |

---

## Task 1: MediaRegistry —— 嗅探结果缓存

**Files:**
- Create: `tests/conftest.py`
- Create: `tests/test_media_registry.py`
- Create: `media_service.py`

**Interfaces:**
- Consumes: 无（纯标准库）
- Produces:
  - `media_service.normalize_media_url(url: str) -> str`
  - `class media_service.MediaRegistry(max_per_tab=50, max_total=2000, tab_ttl=60.0, clock=time.monotonic)`
    - `record(tab_id: str, page_url: str, title: str = "", items: Sequence[dict] = ()) -> int`（返回本次新增条数）
    - `list_for_tab(tab_id: str) -> list[dict]`（插入顺序；过期/未知 tab 返回 `[]`）
    - `count() -> int`
  - `media_service.media_registry`（模块级单例）
  - item 字典固定字段：`media_id,url,kind,quality_hint,bytes,is_mse,page_url,title,detected_at`

- [ ] **Step 1: 建测试目录脚手架**

Create `tests/conftest.py`:

```python
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
```

- [ ] **Step 2: 写失败的测试**

Create `tests/test_media_registry.py`:

```python
from media_service import MediaRegistry, normalize_media_url


def _item(url, **kw):
    d = {"url": url, "kind": "hls"}
    d.update(kw)
    return d


def test_normalize_drops_fragment_and_tracking_keys_and_sorts_query():
    got = normalize_media_url("https://c.example.com/v/a.m3u8?b=2&a=1&spm=x#frag")
    assert got == "https://c.example.com/v/a.m3u8?a=1&b=2"


def test_normalize_lowercases_scheme_and_host_only():
    assert normalize_media_url("https://CDN.Example.com/V/A.M3U8") == "https://cdn.example.com/V/A.M3U8"


def test_normalize_keeps_signed_params():
    u = "https://c/x.m3u8?sign=abc&ts=1"
    assert normalize_media_url(u) == "https://c/x.m3u8?sign=abc&ts=1"


def test_record_dedups_same_normalized_url_within_tab():
    r = MediaRegistry()
    added = r.record("7", "https://site/v", "标题",
                     [_item("https://c/x.m3u8?a=1"), _item("https://c/x.m3u8?a=2&spm=y")])
    assert added == 1
    assert len(r.list_for_tab("7")) == 1


def test_record_merges_richer_metadata_into_existing_item():
    r = MediaRegistry()
    r.record("7", "https://site/v", "", [_item("https://c/x.m3u8", quality_hint="", bytes=0)])
    r.record("7", "https://site/v", "新标题",
             [_item("https://c/x.m3u8", quality_hint="1080P", bytes=4096)])
    items = r.list_for_tab("7")
    assert items[0]["quality_hint"] == "1080P"
    assert items[0]["bytes"] == 4096
    assert items[0]["title"] == "新标题"


def test_page_change_replaces_tab_items():
    r = MediaRegistry()
    r.record("7", "https://site/a", "A", [_item("https://c/a.m3u8")])
    r.record("7", "https://site/b", "B", [_item("https://c/b.m3u8")])
    urls = [it["url"] for it in r.list_for_tab("7")]
    assert urls == ["https://c/b.m3u8"]


def test_per_tab_cap_evicts_oldest():
    r = MediaRegistry(max_per_tab=3)
    r.record("7", "p", "", [_item(f"https://c/{i}.m3u8") for i in range(5)])
    assert [it["url"] for it in r.list_for_tab("7")] == [
        "https://c/2.m3u8", "https://c/3.m3u8", "https://c/4.m3u8"]


def test_total_cap_evicts_oldest_tab_first():
    now = [1000.0]
    r = MediaRegistry(max_per_tab=2, max_total=3, clock=lambda: now[0])
    r.record("1", "p1", "", [_item("https://c/1a.m3u8"), _item("https://c/1b.m3u8")])
    now[0] += 1
    r.record("2", "p2", "", [_item("https://c/2a.m3u8"), _item("https://c/2b.m3u8")])
    assert r.count() == 3
    assert [it["url"] for it in r.list_for_tab("1")] == ["https://c/1b.m3u8"]
    assert [it["url"] for it in r.list_for_tab("2")] == ["https://c/2a.m3u8", "https://c/2b.m3u8"]


def test_tab_expires_after_ttl():
    now = [500.0]
    r = MediaRegistry(clock=lambda: now[0], tab_ttl=60.0)
    r.record("9", "p", "", [_item("https://c/x.m3u8")])
    now[0] += 61
    assert r.list_for_tab("9") == []
    assert r.count() == 0


def test_media_id_is_stable_across_tabs_and_refresh():
    r = MediaRegistry()
    r.record("1", "p1", "", [_item("https://c/x.m3u8")])
    r.record("2", "p2", "", [_item("https://c/x.m3u8")])
    assert r.list_for_tab("1")[0]["media_id"] == r.list_for_tab("2")[0]["media_id"]


def test_record_ignores_items_without_url_and_mse_has_no_url_requirement():
    r = MediaRegistry()
    added = r.record("7", "p", "", [{"kind": "hls"},
                                    {"url": "  ", "kind": "hls"},
                                    {"url": "blob:https://x/abc", "kind": "mse", "is_mse": True}])
    assert added == 1
    items = r.list_for_tab("7")
    assert items[0]["is_mse"] is True
    assert items[0]["url"] == "blob:https://x/abc"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `python -m pytest tests/test_media_registry.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'media_service'`

- [ ] **Step 4: 实现 `media_service.py`**

```python
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
                key = item["url"] if item["is_mse"] else normalize_media_url(item["url"])
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
```

- [ ] **Step 5: 跑测试确认通过**

Run: `python -m pytest tests/test_media_registry.py -q`
Expected: PASS（11 passed）

- [ ] **Step 6: 提交**

```bash
git add tests/conftest.py tests/test_media_registry.py media_service.py
git commit -m "feat: 新增按标签页缓存的流媒体嗅探清单 MediaRegistry"
```

---

## Task 2: 嗅探上报 / 查询 API

**Files:**
- Modify: `app.py`（在 `BROWSER_CAPTURE_ENABLED` 定义之后、`/api/browser-capture` 之前插入新路由）
- Create: `tests/test_media_api.py`

**Interfaces:**
- Consumes: `media_service.media_registry`
- Produces（HTTP 契约，供扩展 popup / background 使用）:
  - `POST /api/media/discover`，body `{tab_id, page_url, title, items:[{url,kind,quality_hint?,bytes?,is_mse?}]}` → `200 {"ok":true,"added":n,"count":m}`；参数非法 → `400 {"ok":false,"error":...}`；浏览器监控关闭 → `200 {"ok":false,"reason":"disabled"}`。全部响应带 `Access-Control-Allow-Origin: *`。
  - `GET /api/media/list?tabId=7` → `200 {"ok":true,"tab_id":"7","page_url":...,"title":...,"items":[...]}`
  - `POST /api/media/discover` / `GET /api/media/list` 的 `OPTIONS` 预检返回允许方法与头。

- [ ] **Step 1: 写失败的测试**

Create `tests/test_media_api.py`:

```python
import pytest

import app as appmod
from media_service import media_registry


@pytest.fixture(autouse=True)
def _clean_registry():
    media_registry._tabs.clear()
    appmod.BROWSER_CAPTURE_ENABLED = True
    yield
    appmod.BROWSER_CAPTURE_ENABLED = True
    media_registry._tabs.clear()


@pytest.fixture()
def client():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def _item(url, kind="hls", **kw):
    d = {"url": url, "kind": kind}
    d.update(kw)
    return d


def test_discover_accepts_items_and_reports_count(client):
    resp = client.post("/api/media/discover", json={
        "tab_id": "7", "page_url": "https://site/v", "title": "视频页",
        "items": [_item("https://c/a.m3u8", quality_hint="1080P", bytes=2048)],
    })
    assert resp.status_code == 200
    body = resp.get_json()
    assert body == {"ok": True, "added": 1, "count": 1}
    assert resp.headers["Access-Control-Allow-Origin"] == "*"


def test_discover_requires_tab_id(client):
    resp = client.post("/api/media/discover", json={"page_url": "p", "items": []})
    assert resp.status_code == 400
    assert resp.get_json()["ok"] is False


def test_discover_rejects_non_list_items(client):
    resp = client.post("/api/media/discover", json={"tab_id": "1", "items": "oops"})
    assert resp.status_code == 400


def test_discover_short_circuits_when_monitoring_disabled(client):
    appmod.BROWSER_CAPTURE_ENABLED = False
    resp = client.post("/api/media/discover", json={"tab_id": "1", "items": [_item("https://c/a.m3u8")]})
    assert resp.get_json() == {"ok": False, "reason": "disabled"}
    assert media_registry.count() == 0


def test_list_returns_items_for_tab(client):
    client.post("/api/media/discover", json={
        "tab_id": "9", "page_url": "https://site/v", "title": "T",
        "items": [_item("https://c/a.m3u8"), _item("https://c/b.mpd", kind="dash")],
    })
    resp = client.get("/api/media/list?tabId=9")
    body = resp.get_json()
    assert body["ok"] is True
    assert body["page_url"] == "https://site/v"
    assert [i["kind"] for i in body["items"]] == ["hls", "dash"]


def test_list_unknown_tab_is_empty_ok(client):
    resp = client.get("/api/media/list?tabId=404")
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "tab_id": "404", "page_url": "", "title": "", "items": []}


def test_list_requires_tab_id(client):
    assert client.get("/api/media/list").status_code == 400


def test_options_preflight_allowed(client):
    resp = client.open("/api/media/discover", method="OPTIONS")
    assert resp.status_code == 200
    assert "POST" in resp.headers["Access-Control-Allow-Methods"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_media_api.py -q`
Expected: FAIL — 404（路由不存在）

- [ ] **Step 3: 在 `app.py` 加路由**

`app.py` 顶部 import 区补一行（`from downloader import ...` 之后）：

```python
from media_service import media_registry
```

在 `BROWSER_CAPTURE_ENABLED = True` 之后插入：

```python
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
```

注意 `return _cors(...), 400` 的括号：`_cors()` 返回 response 对象，元组第二项是状态码，别写成 `_cors(jsonify(...), 400)`。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_media_api.py -q`
Expected: PASS（8 passed）

- [ ] **Step 5: 确认没打破既有 API**

Run: `python -m pytest tests -q && python -c "import app; print('import ok')"`
Expected: 全部 PASS，`import ok`

- [ ] **Step 6: 提交**

```bash
git add app.py tests/test_media_api.py
git commit -m "feat: 新增 /api/media/discover 与 /api/media/list 嗅探端点"
```

---

## Task 3: kind 判定、ffmpeg 探测与 yt-dlp 依赖接入

**Files:**
- Create: `tests/test_media_classify.py`
- Create: `media.py`
- Modify: `requirements.txt`
- Modify: `build_exe.py:186`（`--hidden-import` 列表末尾）

**Interfaces:**
- Consumes: 无
- Produces:
  - `media.KIND_AUTO/KIND_HTTP/KIND_HLS/KIND_DASH/KIND_VIDEO_PAGE`、`media.VALID_KINDS`、`media.MEDIA_KINDS`
  - `media.classify_kind(url: str, kind: str = "auto") -> str`
  - `media.MediaError(reason: str, message: str)` 基类（属性 `.reason`）+ 子类 `DrmProtectedError`("drm_protected")、`NeedsFfmpegError`("needs_ffmpeg")
  - `media.ffmpeg_status() -> {"available": bool, "path": str | None}`
  - `media.ytdlp_available() -> bool`、`media.set_ytdlp_module(module)`（测试注入假模块）、`media._ytdlp()`（返回模块或抛 `MediaError("yt_dlp_missing", ...)`）

- [ ] **Step 1: 写失败的测试**

Create `tests/test_media_classify.py`:

```python
import shutil

import pytest

import media


@pytest.mark.parametrize("url,expected", [
    ("https://c/v/index.m3u8?sign=1", "hls"),
    ("https://c/v/INDEX.MPD", "dash"),
    ("https://c/v/movie.mp4", "http"),
    ("https://c/v/blob-ish", "http"),
    ("magnet:?xt=urn:btih:abc", "http"),          # 磁力由 DownloadManager 先拦截，classify 不报错即可
])
def test_classify_kind_auto_by_url_feature(url, expected):
    assert media.classify_kind(url, "auto") == expected


def test_classify_kind_auto_never_guesses_video_page():
    # 任意网页地址不能自动当解析页——必须显式 kind=video_page
    assert media.classify_kind("https://site/watch?v=1", "auto") == "http"


@pytest.mark.parametrize("explicit", ["hls", "dash", "video_page", "http"])
def test_explicit_kind_wins(explicit):
    assert media.classify_kind("https://site/whatever", explicit) == explicit


def test_unknown_kind_raises_invalid_url():
    with pytest.raises(media.MediaError) as e:
        media.classify_kind("https://site/a", "flash")
    assert e.value.reason == "invalid_kind"


def test_ffmpeg_status_uses_path_and_env(monkeypatch, tmp_path):
    fake = tmp_path / "ffmpeg.exe"
    fake.write_bytes(b"x")
    monkeypatch.delenv("SWIFTDM_FFMPEG", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: str(fake) if name == "ffmpeg" else None)
    assert media.ffmpeg_status() == {"available": True, "path": str(fake)}
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert media.ffmpeg_status() == {"available": False, "path": None}
    monkeypatch.setenv("SWIFTDM_FFMPEG", str(fake))
    assert media.ffmpeg_status()["available"] is True
    monkeypatch.setenv("SWIFTDM_FFMPEG", str(tmp_path / "missing.exe"))
    monkeypatch.setattr(shutil, "which", lambda name: str(fake))
    assert media.ffmpeg_status() == {"available": True, "path": str(fake)}   # 覆盖路径无效时回落 PATH


def test_ytdlp_missing_raises_media_error(monkeypatch):
    media.set_ytdlp_module(None)
    if media.ytdlp_available():
        pytest.skip("本机已安装 yt-dlp，无法验证缺失分支")
    with pytest.raises(media.MediaError) as e:
        media._ytdlp()
    assert e.value.reason == "needs_ytdlp"


def test_injected_module_is_used(monkeypatch):
    sentinel = object()
    media.set_ytdlp_module(sentinel)
    assert media._ytdlp() is sentinel
    assert media.ytdlp_available() is True
    media.set_ytdlp_module(None)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_media_classify.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'media'`

- [ ] **Step 3: 实现 `media.py` 的判定与依赖层**

```python
"""
流媒体下载 —— kind 判定、ffmpeg / yt-dlp 依赖探测、MediaTask（yt-dlp 适配器）。

设计约束（见 docs/superpowers/specs/2026-09-23-media-sniffing-design.md）：
  - 只检测 DRM，绝不尝试解密；
  - 不打包 ffmpeg，缺失时明确报「需要 ffmpeg」；
  - MediaTask 与 DownloadTask / TorrentTask 接口兼容，可直接被 SSE 与历史记录复用。
"""
import os
import shutil
import time
import logging
import threading

logger = logging.getLogger("SwiftDM")

KIND_AUTO = "auto"
KIND_HTTP = "http"
KIND_HLS = "hls"
KIND_DASH = "dash"
KIND_VIDEO_PAGE = "video_page"
VALID_KINDS = (KIND_AUTO, KIND_HTTP, KIND_HLS, KIND_DASH, KIND_VIDEO_PAGE)
MEDIA_KINDS = (KIND_HLS, KIND_DASH, KIND_VIDEO_PAGE)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class MediaError(Exception):
    """流媒体任务的可归类错误。API 层按 .reason 映射 HTTP 状态码。"""
    reason = "media_error"

    def __init__(self, message="", reason=None):
        super().__init__(message or "流媒体任务失败")
        self.reason = reason or type(self).reason


class DrmProtectedError(MediaError):
    reason = "drm_protected"


class NeedsFfmpegError(MediaError):
    reason = "needs_ffmpeg"


def classify_kind(url, kind=KIND_AUTO):
    """决定用哪个引擎。显式 kind（含 video_page）原样返回；auto 只看 URL 特征。"""
    kind = (kind or KIND_AUTO).strip().lower()
    if kind not in VALID_KINDS:
        raise MediaError(f"不支持的下载类型: {kind}", "invalid_kind")
    if kind != KIND_AUTO:
        return kind
    path = (url or "").strip().lower()
    if path.endswith(".m3u8") or "/format=m3u8" in path or ".m3u8?" in path:
        return KIND_HLS
    if path.endswith(".mpd") or ".mpd?" in path:
        return KIND_DASH
    return KIND_HTTP


def ffmpeg_status():
    """探测系统 ffmpeg：SWIFTDM_FFMPEG 优先，其次 PATH。绝不自动下载。"""
    override = (os.environ.get("SWIFTDM_FFMPEG") or "").strip()
    if override and os.path.isfile(override):
        return {"available": True, "path": override}
    found = shutil.which("ffmpeg")
    if found and os.path.isfile(found):
        return {"available": True, "path": found}
    return {"available": False, "path": None}


# ---------------- yt-dlp 延迟导入 ----------------
_YTDLP = None


def set_ytdlp_module(module):
    """注入 yt_dlp 模块（测试用假模块，或用户自带版本）。传 None 表示恢复自动探测。"""
    global _YTDLP
    _YTDLP = module


def _ytdlp():
    global _YTDLP
    if _YTDLP is not None:
        return _YTDLP
    try:
        import yt_dlp
    except ImportError as e:
        raise MediaError("未安装 yt-dlp，请执行 pip install yt-dlp 后重启 SwiftDM",
                         "needs_ytdlp") from e
    _YTDLP = yt_dlp
    return _YTDLP


def ytdlp_available():
    try:
        _ytdlp()
        return True
    except MediaError:
        return False
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_media_classify.py -q`
Expected: PASS（14 passed；本机已装 yt-dlp 时其中 1 个 skip）

- [ ] **Step 5: 接入依赖**

`requirements.txt` 末行 `libtorrent>=2.0.0  # BT / PT 下载（磁力链接与 .torrent 种子）` 之后追加：

```
yt-dlp>=2025.1.15  # 流媒体（HLS/DASH/网页视频）解析下载
```

`build_exe.py` 的 `opts` 列表里，`"--hidden-import", "torrent",` 之后追加：

```python
        # 流媒体下载（yt-dlp 的 extractor 是运行时动态导入的，必须让 PyInstaller 收集）
        "--hidden-import", "yt_dlp",
        "--collect-all", "yt_dlp",
```

安装：`python -m pip install "yt-dlp>=2025.1.15"`，然后 `python -c "import yt_dlp,media; print(yt_dlp.version.__version__, media.ytdlp_available())"`
Expected: 打印版本号 + `True`

- [ ] **Step 6: 提交**

```bash
git add media.py tests/test_media_classify.py requirements.txt build_exe.py
git commit -m "feat: 新增媒体 kind 判定与 ffmpeg/yt-dlp 依赖探测"
```

---

## Task 4: throttle.py —— 全局限速

**Files:**
- Create: `tests/test_throttle.py`
- Create: `throttle.py`
- Modify: `downloader.py:382-396`（`_stream_segment` 分段写循环）
- Modify: `app.py:144-157`（`/api/settings` 增加 `rate_limit`）

**Interfaces:**
- Consumes: 无
- Produces:
  - `throttle.set_rate(bytes_per_sec: int) -> int`（0 = 不限速，返回生效值）
  - `throttle.get_rate() -> int`
  - `throttle.consume(nbytes: int) -> None`（按令牌桶节流，可能 sleep；**必须在锁外调用**）
  - 模块属性 `throttle._monotonic`、`throttle._sleep`（测试注入假时钟，绝不靠真实等待）
  - HTTP 契约：`GET /api/settings` 增加 `"rate_limit": <int bytes/s>`；`POST /api/settings {"rate_limit": n}` → 生效并回显

- [ ] **Step 1: 写失败的测试**

Create `tests/test_throttle.py`:

```python
import pytest

import throttle


@pytest.fixture(autouse=True)
def _reset():
    throttle.set_rate(0)
    yield
    throttle.set_rate(0)


def test_zero_rate_means_unlimited_and_never_sleeps():
    sleeps = []
    throttle._sleep = sleeps.append
    throttle.set_rate(0)
    throttle.consume(10 * 1024 * 1024)
    assert sleeps == []
    assert throttle.get_rate() == 0


def test_set_rate_clamps_negative_and_returns_applied():
    assert throttle.set_rate(-5) == 0
    assert throttle.set_rate(1024) == 1024


def test_consume_sleeps_for_missing_tokens():
    clock = [0.0]
    sleeps = []
    throttle._monotonic = lambda: clock[0]
    throttle._sleep = sleeps.append
    throttle.set_rate(1000)              # 1000 B/s，初始桶空
    throttle.consume(500)                # 无余额 → 需要 0.5s
    assert sleeps and abs(sleeps[0] - 0.5) < 1e-6
    sleeps.clear()
    clock[0] += 1.0                      # 过 1 秒，攒到上限（0.5s 突发 = 500B）
    throttle.consume(400)                # 余额够 → 不 sleep
    assert sleeps == []
    throttle.consume(400)                # 余额不足 → 按比例补时
    assert sleeps and 0.2 < sleeps[0] <= 0.6


def test_burst_is_capped_so_idle_cannot_cheat():
    clock = [0.0]
    sleeps = []
    throttle._monotonic = lambda: clock[0]
    throttle._sleep = sleeps.append
    throttle.set_rate(1000)
    clock[0] += 3600                     # 挂机一小时
    throttle.consume(100000)             # 不能一次白拿一小时额度
    assert sleeps[0] > 90                # ≈ (100000 - 500) / 1000
    assert sleeps[0] <= 100


def test_rate_change_takes_effect_immediately():
    clock = [0.0]
    sleeps = []
    throttle._monotonic = lambda: clock[0]
    throttle._sleep = sleeps.append
    throttle.set_rate(1000)
    throttle.consume(1000)               # ≈1s
    sleeps.clear()
    throttle.set_rate(0)
    throttle.consume(5_000_000)          # 立刻不限速
    assert sleeps == []


def test_consume_ignores_zero_and_negative_sizes():
    sleeps = []
    throttle._sleep = sleeps.append
    throttle.set_rate(1)
    throttle.consume(0)
    throttle.consume(-10)
    assert sleeps == []


def test_settings_api_roundtrip_rate_limit():
    import app as appmod
    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()

    got = client.get("/api/settings").get_json()
    assert got["rate_limit"] == 0

    posted = client.post("/api/settings", json={"rate_limit": 512 * 1024}).get_json()
    assert posted["success"] is True and posted["rate_limit"] == 512 * 1024
    assert throttle.get_rate() == 512 * 1024

    client.post("/api/settings", json={"rate_limit": 0})
    assert throttle.get_rate() == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_throttle.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'throttle'`

- [ ] **Step 3: 实现 `throttle.py`**

```python
"""
全局下载限速 —— 单令牌桶，所有分段线程与流媒体任务共用。

单位：字节/秒；0 表示不限速。设计成「调用方拿到多少字节就喂多少」，
不在这里做分片，也不持有任何任务引用。
"""
import threading
import time

_BURST_SECONDS = 0.5          # 允许的最大突发（半秒额度），避免空闲后一次冲出去

_lock = threading.Lock()
_rate = 0
_tokens = 0.0
_last = 0.0

_monotonic = time.monotonic   # 测试注入假时钟
_sleep = time.sleep


def set_rate(bytes_per_sec):
    """设置全局限速（字节/秒）。0 = 不限速；负数按 0 处理。"""
    global _rate, _tokens, _last
    try:
        value = int(bytes_per_sec)
    except (TypeError, ValueError):
        value = 0
    with _lock:
        _rate = value if value > 0 else 0
        _tokens = 0.0
        _last = 0.0
    return _rate


def get_rate():
    return _rate


def consume(nbytes):
    """消费 nbytes 字节的额度，不足则阻塞到该睡的时间点。必须在任务锁之外调用。"""
    global _tokens, _last
    if _rate <= 0 or nbytes <= 0:
        return
    wait = 0.0
    with _lock:
        now = _monotonic()
        if _last == 0.0:
            _last = now
        burst = _rate * _BURST_SECONDS
        _tokens = min(burst, _tokens + (now - _last) * _rate)
        _last = now
        if _tokens >= nbytes:
            _tokens -= nbytes
        else:
            wait = (nbytes - _tokens) / _rate
            _tokens = 0.0
            # 预支等待时间，避免多线程同时排队时重复计息
            _last += wait
    if wait > 0:
        _sleep(wait)
```

- [ ] **Step 4: 挂进 HTTP 分段读循环**

`downloader.py` 顶部 import 区加：

```python
from throttle import consume as _throttle_consume
```

`_stream_segment` 的写循环里，把进度更新与限速放在一起（注意在 `self._lock` 之外 sleep）：

```python
                    f.write(chunk)
                    written += len(chunk)
                    with self._lock:
                        self._segment_progress[idx] += len(chunk)
                    _throttle_consume(len(chunk))
```

- [ ] **Step 5: `/api/settings` 增加限速字段**

`app.py` 的 `settings()` 整体替换为：

```python
@app.route("/api/settings", methods=["GET", "POST", "OPTIONS"])
def settings():
    if request.method == "OPTIONS":
        return _cors(app.make_default_options_response(), "GET, POST, OPTIONS")
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        # 代理模式: env(系统代理) / direct(直连) / 自定义地址
        if "proxy_mode" in data:
            set_proxy_mode(data["proxy_mode"])
        if "rate_limit" in data:
            set_rate(data["rate_limit"])
        return jsonify({"success": True, "proxy_mode": get_proxy_mode(),
                        "rate_limit": get_rate()})
    return jsonify({
        "download_dir": DEFAULT_DOWNLOAD_DIR,
        "default_segments": 8,
        "proxy_mode": get_proxy_mode(),
        "proxy_modes": ["env", "direct"],
        "rate_limit": get_rate(),
    })
```

（`_cors()` 来自 Task 2；`from throttle import get_rate, set_rate` 加到 `app.py` 顶部 import 区。）

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_throttle.py -q && python -m pytest tests -q`
Expected: 新用例 PASS，全量仍 PASS

- [ ] **Step 7: 提交**

```bash
git add throttle.py downloader.py app.py tests/test_throttle.py
git commit -m "feat: 新增全局下载限速并接入分段下载循环"
```

---

## Task 5: MediaTask —— yt-dlp 下载任务

**Files:**
- Create: `tests/test_media_task.py`
- Modify: `media.py`（文件末尾追加 `MediaTask`）

**Interfaces:**
- Consumes: `media._ytdlp()`、`media.ffmpeg_status()`、`media.NeedsFfmpegError/DrmProtectedError/MediaError`、`KIND_*`
- Produces:
  - `media.MediaTask(task_id, url, save_dir, filename=None, segments=8, kind="hls", referer=None, cookies_netscape=None, resolution=None)`
  - 方法：`preflight()`（抛 `MediaError` 子类给 API 层转 409：DRM 一律拒绝；ffmpeg 只在元数据确认「音视频分离且本机没有 ffmpeg」时拒绝，纯 TS 的 HLS 不要求 ffmpeg）、`start()`、`pause()`、`resume()`、`cancel()`、`retry()`、`to_dict()`
  - 额外字段：`kind`、`resolution`、`referer`、`error_reason`（`""` 或 reason 字符串）
  - 假模块契约（后续任务的测试都靠它）：`module.YoutubeDL(opts)` 返回支持 `__enter__/__exit__/download(urls)/extract_info(url, download=False)` 的对象；`module.utils.DownloadCancelled` 是异常类；opts 键固定为 `format,outtmpl,concurrent_fragment_downloads,http_headers,merge_output_format,noprogress,quiet,no_warnings,progress_hooks,cookiefile,ratelimit,continuedl`

- [ ] **Step 1: 写失败的测试**

Create `tests/test_media_task.py`:

```python
import os
import time
import types

import pytest

import media


@pytest.fixture()
def fake_ytdlp(monkeypatch):
    """假 yt_dlp 模块：记录 opts、按脚本回放进度事件、可注入异常。"""
    captured = {}

    class _Inst:
        def __init__(self, opts):
            captured["opts"] = opts
            self.opts = opts

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            captured["extract"] = (url, download)
            return captured.get("info", {})

        def download(self, urls):
            captured["download"] = urls
            for st, payload in captured["events"]:
                for hook in self.opts["progress_hooks"]:
                    hook(dict(payload, status=st))
            if captured.get("raise") is not None:
                raise captured["raise"]

    mod = types.SimpleNamespace(
        YoutubeDL=_Inst,
        utils=types.SimpleNamespace(DownloadCancelled=type("DownloadCancelled", (Exception,), {})))
    media.set_ytdlp_module(mod)
    captured["events"] = [("downloading", {"downloaded_bytes": 500, "total_bytes": 1000,
                                           "speed": 250.0, "eta": 2}),
                          ("finished", {"downloaded_bytes": 1000, "total_bytes": 1000,
                                        "filename": ""})]
    monkeypatch.setattr(media, "ffmpeg_status", lambda: {"available": True, "path": "ffmpeg"})
    yield captured
    media.set_ytdlp_module(None)


def _mk(tmp_path, kind="hls", segments=8, **kw):
    return media.MediaTask("dl_1", "https://c/v/index.m3u8", str(tmp_path),
                           filename=kw.pop("filename", "movie.mp4"), segments=segments,
                           kind=kind, referer=kw.pop("referer", None),
                           cookies_netscape=kw.pop("cookies_netscape", None),
                           resolution=kw.pop("resolution", None))


def _wait_done(task, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline and task.status in ("pending", "downloading"):
        time.sleep(0.02)
    return task.status


def test_fields_and_to_dict_contract(tmp_path, fake_ytdlp):
    t = _mk(tmp_path, kind="hls", referer="https://site/v")
    d = t.to_dict()
    for key in ("task_id", "filename", "filepath", "url", "status", "progress", "total_size",
                "downloaded", "speed", "eta", "error", "error_reason", "segments", "kind",
                "resolution", "referer", "save_dir"):
        assert key in d, key
    assert d["status"] == "pending" and d["kind"] == "hls" and d["referer"] == "https://site/v"
    assert "cookies" not in str(d).lower()           # Cookie 绝不能进状态导出


def test_start_downloads_and_completes(tmp_path, fake_ytdlp):
    fake_ytdlp["events"][1][1]["filename"] = os.path.join(str(tmp_path), "movie.mp4")
    t = _mk(tmp_path)
    t.start()
    assert _wait_done(t) == "completed", t.error
    assert t.progress == 100.0
    assert t.total_size == 1000 and t.downloaded == 1000
    assert t.filepath == os.path.join(str(tmp_path), "movie.mp4")
    assert fake_ytdlp["download"] == ["https://c/v/index.m3u8"]


def test_opts_for_hls(tmp_path, fake_ytdlp):
    t = _mk(tmp_path, kind="hls", referer="https://site/v", segments=16)
    t.start()
    _wait_done(t)
    opts = fake_ytdlp["opts"]
    assert opts["concurrent_fragment_downloads"] == 16
    assert opts["http_headers"]["Referer"] == "https://site/v"
    assert opts["http_headers"]["User-Agent"]
    assert opts["merge_output_format"] == "mp4"
    assert opts["continuedl"] is True and opts["noprogress"] is True
    assert "cookiefile" not in opts
    assert "ratelimit" not in opts                   # 未限速时不塞 0
    assert opts["format"] == "bv*+ba/b"


def test_opts_resolution_and_cookies(tmp_path, fake_ytdlp):
    t = _mk(tmp_path, kind="video_page", resolution=720,
            cookies_netscape="# Netscape HTTP Cookie File\n"
                             ".example.com\tTRUE\t/\tFALSE\t0\tsid\tabc\n")
    t.start()
    opts = fake_ytdlp["opts"]
    assert opts["format"] == "bv*[height<=720]+ba/b"
    path = opts["cookiefile"]
    assert os.path.isfile(path)
    with open(path, encoding="utf-8") as f:
        assert "sid" in f.read()
    assert _wait_done(t) == "completed"
    assert not os.path.exists(path)                  # 任务结束必须删掉临时 Cookie 文件


def test_ratelimit_is_taken_from_global_throttle(tmp_path, fake_ytdlp):
    import throttle
    throttle.set_rate(2 * 1024 * 1024)
    try:
        t = _mk(tmp_path)
        t.start()
        _wait_done(t)
        assert fake_ytdlp["opts"]["ratelimit"] == 2 * 1024 * 1024
    finally:
        throttle.set_rate(0)


def test_pause_cancels_worker_and_resume_restarts(tmp_path, fake_ytdlp):
    fake_ytdlp["events"] = [("downloading", {"downloaded_bytes": 300, "total_bytes": 1000})]
    t = _mk(tmp_path)
    t.start()
    time.sleep(0.05)
    t.pause()
    assert t.status == "paused" and t.speed == 0.0
    first_opts = fake_ytdlp["opts"]
    fake_ytdlp["events"].append(("finished", {"downloaded_bytes": 1000, "total_bytes": 1000,
                                              "filename": os.path.join(str(tmp_path), "movie.mp4")}))
    t.resume()
    assert _wait_done(t) == "completed", t.error
    assert fake_ytdlp["opts"] is not first_opts


def test_failure_from_ytdlp_marks_failed_with_reason(tmp_path, fake_ytdlp):
    fake_ytdlp["raise"] = Exception("ERROR: [generic] Unable to download webpage: timed out")
    t = _mk(tmp_path)
    t.start()
    assert _wait_done(t) == "failed"
    assert t.error_reason == "parse_failed"
    assert "解析失败" in t.error                      # 给用户看的中文提示，不暴露原始栈


def test_drm_error_from_ytdlp_maps_to_drm_reason(tmp_path, fake_ytdlp):
    fake_ytdlp["raise"] = Exception("ERROR: This video is protected by Widevine DRM")
    t = _mk(tmp_path)
    t.start()
    assert _wait_done(t) == "failed"
    assert t.error_reason == "drm_protected"
    assert "DRM" in t.error


def test_cookie_required_error_maps_to_cookies_reason(tmp_path, fake_ytdlp):
    fake_ytdlp["raise"] = Exception("ERROR: Login required. Cookies are needed")
    t = _mk(tmp_path)
    t.start()
    assert _wait_done(t) == "failed"
    assert t.error_reason == "cookies_required"
    assert "登录" in t.error


def test_preflight_rejects_drm_metadata(tmp_path, fake_ytdlp):
    fake_ytdlp["info"] = {"formats": [{"format_id": "1", "has_drm": True},
                                      {"format_id": "2", "vcodec": "avc1"}]}
    t = _mk(tmp_path)
    with pytest.raises(media.DrmProtectedError):
        t.preflight()


def test_preflight_allows_pure_ts_hls_without_ffmpeg(tmp_path, fake_ytdlp, monkeypatch):
    """没装 ffmpeg 也必须能下 HLS：纯 TS 序列由 yt-dlp 内置合并（spec §2「MediaTask 行为」）。"""
    monkeypatch.setattr(media, "ffmpeg_status", lambda: {"available": False, "path": None})
    fake_ytdlp["info"] = {"ext": "mp4", "format_id": "__all__"}          # 无 formats 的已混流清单
    t = _mk(tmp_path, kind="hls")
    assert t.preflight() is None


def test_preflight_rejects_split_dash_without_ffmpeg(tmp_path, fake_ytdlp, monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_status", lambda: {"available": False, "path": None})
    fake_ytdlp["info"] = {"formats": [{"format_id": "v", "vcodec": "avc1", "acodec": "none"},
                                      {"format_id": "a", "vcodec": "none", "acodec": "mp4a"}]}
    t = _mk(tmp_path, kind="dash")
    with pytest.raises(media.NeedsFfmpegError):
        t.preflight()


def test_preflight_passes_muxed_video_without_ffmpeg(tmp_path, fake_ytdlp, monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_status", lambda: {"available": False, "path": None})
    fake_ytdlp["info"] = {"formats": [{"format_id": "18", "vcodec": "avc1", "acodec": "mp4a"}]}
    t = _mk(tmp_path, kind="video_page")
    assert t.preflight() is None                     # 音视频同轨，不需要 ffmpeg


def test_preflight_rejects_split_streams_without_ffmpeg(tmp_path, fake_ytdlp, monkeypatch):
    monkeypatch.setattr(media, "ffmpeg_status", lambda: {"available": False, "path": None})
    fake_ytdlp["info"] = {"formats": [{"format_id": "137", "vcodec": "avc1", "acodec": "none"},
                                      {"format_id": "140", "vcodec": "none", "acodec": "mp4a"}]}
    t = _mk(tmp_path, kind="video_page")
    with pytest.raises(media.NeedsFfmpegError):
        t.preflight()


def test_cancel_keeps_cancelled_state(tmp_path, fake_ytdlp):
    fake_ytdlp["events"] = []
    t = _mk(tmp_path)
    t.start()
    time.sleep(0.05)
    t.cancel()
    time.sleep(0.15)
    assert t.status == "cancelled"                   # 工作线程不得把它改回 completed


def test_retry_resets_error_and_restarts(tmp_path, fake_ytdlp):
    t = _mk(tmp_path)
    t.status = "failed"
    t.error = "x"
    t.error_reason = "parse_failed"
    assert t.retry() is True
    assert _wait_done(t) == "completed"
    assert t.error == "" and t.error_reason == ""
    assert t.retry() is False                        # 已完成不可重试
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_media_task.py -q`
Expected: FAIL — `AttributeError: module 'media' has no attribute 'MediaTask'`

- [ ] **Step 3: 实现 `MediaTask`**

Append to `media.py`:

```python
class MediaTask:
    """基于 yt-dlp 的流媒体任务（HLS / DASH / 网页视频解析）。

    与 DownloadTask 同接口，交给 DownloadManager / SSE / 历史记录时无需特殊分支。
    网络与磁盘写操作都在工作线程里跑，不阻塞 Flask 请求线程。
    """

    def __init__(self, task_id, url, save_dir, filename=None, segments=8,
                 kind=KIND_HLS, referer=None, cookies_netscape=None, resolution=None):
        self.task_id = task_id
        self.url = (url or "").strip()
        self.save_dir = save_dir
        self.filename = (filename or "").strip() or _guess_name(self.url, kind)
        self.segments = max(int(segments or 0), 1)
        self.kind = kind if kind in MEDIA_KINDS else KIND_HLS
        self.referer = (referer or "").strip()
        self.resolution = int(resolution) if resolution else None
        self._cookies_netscape = cookies_netscape or ""

        self.status = "pending"
        self.progress = 0.0
        self.total_size = 0
        self.downloaded = 0
        self.speed = 0.0
        self.eta = ""
        self.error = ""
        self.error_reason = ""
        self.added_at = time.time()
        self.filepath = os.path.join(save_dir, self.filename)
        self._lock = threading.Lock()
        self._gen = 0
        self._info = None
        self._worker = None

    # ---------------- 前置检查 ----------------

    def _extract(self):
        """拉一次元数据（不下载）。失败或结构不认识时返回 None，交给 start() 报真实错误。"""
        ydl = _ytdlp()
        opts = self._base_opts(None)
        try:
            with ydl.YoutubeDL(opts) as inst:
                return inst.extract_info(self.url, download=False) or {}
        except DrmProtectedError:
            raise
        except MediaError:
            raise
        except Exception as e:            # 网络/解析失败：不在此处判定，留给 _run
            logger.info("媒体元数据预取失败（稍后由下载重试）: %s | %s", self.filename, e)
            return None

    def preflight(self):
        """下载前的硬性门槛：DRM 直接拒绝；只有「元数据确认音视频分离且本机没 ffmpeg」才拒绝。

        抛 MediaError 子类 → API 层转 409（不创建任务）。不做「没 ffmpeg 就别下 HLS」的一刀切：
        纯 TS 序列由 yt-dlp 内置合并，取不到元数据时放行，缺 ffmpeg 的真实错误留给
        `_run()` 按 `needs_ffmpeg` 上报（spec §2「MediaTask 行为」ffmpeg 一条）。
        """
        info = self._extract()
        if info:
            self._info = info
            _assert_no_drm(info)
            if not ffmpeg_status()["available"] and _needs_merge(info):
                raise NeedsFfmpegError("该资源的音视频是分离的，需要 ffmpeg 合并，"
                                       "请安装 ffmpeg 并加入系统 PATH（或设置 SWIFTDM_FFMPEG）")

    # ---------------- 生命周期 ----------------

    def start(self):
        if self.status in ("downloading", "completed"):
            return
        logger.info("开始流媒体任务: %s  [%s] %s", self.filename, self.kind, self.url)
        self.status = "downloading"
        self.error = ""
        self.error_reason = ""
        gen = self._gen
        self._worker = threading.Thread(target=self._run, args=(gen,), daemon=True)
        self._worker.start()

    def _run(self, gen):
        cookie_path = self._write_cookie_file()
        try:
            ydl = _ytdlp()
        except MediaError as e:
            self._cleanup_cookie(cookie_path)
            self._fail(e, reason=e.reason)
            return
        cancelled_cls = getattr(getattr(ydl, "utils", None), "DownloadCancelled", Exception)
        try:
            with ydl.YoutubeDL(self._build_opts(gen)) as inst:
                inst.download([self.url])
            if self._gen != gen or self.status != "downloading":
                return                       # 已被暂停/取消：保留调用方设置的状态
            with self._lock:
                self.status = "completed"
                self.progress = 100.0
                self.speed = 0.0
                self.eta = ""
                if self.total_size > 0:
                    self.downloaded = self.total_size
            logger.info("流媒体下载完成: %s", self.filename)
        except cancelled_cls as e:           # 暂停/取消的协作式中断，不是失败
            if self._gen != gen or self.status in ("paused", "cancelled"):
                logger.info("流媒体任务已中断: %s", self.filename)
                return
            self._fail(e)
        except MediaError as e:
            self._fail(e, reason=e.reason)
        except Exception as e:               # 底层库异常种类多，统一归类成用户可读提示
            if self._gen != gen or self.status in ("paused", "cancelled"):
                return
            self._fail(e)
        finally:
            self.speed = 0.0
            self._cleanup_cookie(cookie_path)

    @staticmethod
    def _cleanup_cookie(path):
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            logger.debug("临时 Cookie 文件清理失败: %s", path)

    def _fail(self, exc, reason=None):
        msg = str(exc)
        self.status = "failed"
        self.error_reason = reason or _reason_from_text(msg)
        self.error = _friendly_media_error(self.error_reason, msg)
        logger.error("流媒体任务失败: %s | %s | %s", self.filename, self.error_reason, msg)

    def pause(self):
        if self.status != "downloading":
            return
        self._gen += 1                  # 工作线程在 progress hook 里看到代数变化即抛 DownloadCancelled
        self.status = "paused"
        self.speed = 0.0
        self.eta = ""

    def resume(self):
        if self.status != "paused":
            return
        self.start()                    # continuedl=True，yt-dlp 自行断点续传 .part

    def cancel(self):
        self._gen += 1
        self.status = "cancelled"
        self.speed = 0.0
        self._drop_partials()

    def retry(self):
        if self.status not in ("failed", "cancelled"):
            return False
        self.error = ""
        self.error_reason = ""
        self.progress = 0.0
        self.status = "pending"
        self.start()
        return True

    # ---------------- yt-dlp 选项 ----------------

    def _base_opts(self, gen):
        opts = {
            "outtmpl": os.path.join(self.save_dir, _stem(self.filename) + ".%(ext)s"),
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "continuedl": True,
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": self.segments,
            "http_headers": {"User-Agent": _UA},
        }
        if gen is not None:
            opts["progress_hooks"] = [lambda d, g=gen: self._on_progress(d, g)]
        if self.referer:
            opts["http_headers"]["Referer"] = self.referer
        if self.kind == KIND_VIDEO_PAGE and self.resolution:
            opts["format"] = f"bv*[height<={self.resolution}]+ba/b"
        else:
            opts["format"] = "bv*+ba/b"
        from throttle import get_rate
        rate = get_rate()
        if rate > 0:
            opts["ratelimit"] = rate
        return opts

    def _build_opts(self, gen):
        opts = self._base_opts(gen)
        if self._cookies_netscape:
            path = self._cookie_path()
            opts["cookiefile"] = path
        return opts

    # ---------------- Cookie 临时文件 ----------------

    def _cookie_path(self):
        data_dir = os.path.join(os.path.expanduser("~"), ".swiftdm")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, f"ck_{self.task_id}_{os.getpid()}.txt")

    def _write_cookie_file(self):
        if not self._cookies_netscape.strip():
            return None
        path = self._cookie_path()
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._cookies_netscape.rstrip() + "\n")
        return path

    def _drop_partials(self):
        stem = _stem(self.filename)
        try:
            names = os.listdir(self.save_dir)
        except OSError:
            return
        for name in names:
            if name.startswith(stem) and name.endswith((".ytdl", ".part")):
                try:
                    os.remove(os.path.join(self.save_dir, name))
                except OSError:
                    pass

    # ---------------- 进度 ----------------

    def _on_progress(self, d, gen):
        if self._gen != gen:
            raise _ytdlp().utils.DownloadCancelled("任务已暂停或取消")
        status = d.get("status")
        if status == "downloading":
            done = int(d.get("downloaded_bytes") or 0)
            total = int(d.get("total_bytes") or d.get("total_bytes_estimate") or 0)
            with self._lock:
                self.downloaded = max(self.downloaded, done)
                if total > 0:
                    self.total_size = total
                    self.progress = min(99.5, self.downloaded * 100.0 / total)
                self.speed = float(d.get("speed") or 0.0)
                eta = d.get("eta")
                self.eta = f"{int(eta)}s" if eta else ""
        elif status == "finished":
            name = d.get("filename") or ""
            if name:
                with self._lock:
                    self.filepath = name
                    self.filename = os.path.basename(name)

    def to_dict(self):
        return {
            "task_id": self.task_id,
            "filename": self.filename,
            "filepath": self.filepath,
            "url": self.url,
            "save_dir": self.save_dir,
            "status": self.status,
            "progress": round(self.progress, 1),
            "total_size": self.total_size,
            "downloaded": self.downloaded,
            "speed": self.speed,
            "eta": self.eta,
            "error": self.error,
            "error_reason": self.error_reason,
            "segments": self.segments,
            "kind": self.kind,
            "resolution": self.resolution or 0,
            "referer": self.referer,
        }
```

模块级辅助函数（追加在 `MediaTask` 之前或之后均可，测试直接调用）：

```python
def _stem(filename):
    base = os.path.basename(filename or "video")
    return os.path.splitext(base)[0] or "video"


def _guess_name(url, kind):
    from urllib.parse import unquote, urlsplit
    path = urlsplit(url or "").path
    last = unquote(os.path.basename(path)) if path else ""
    if last and "." in last and len(last) < 120:
        return last
    stamp = int(time.time())
    ext = {"hls": "mp4", "dash": "mp4"}.get(kind, "mp4")
    return f"media_{stamp}.{ext}"


def _assert_no_drm(info):
    """只要元数据里出现 DRM 标记就拒绝（AES-128 由 yt-dlp 用清单里的明文 key 处理，不算 DRM）。"""
    if info.get("drm") or info.get("has_drm"):
        raise DrmProtectedError("该资源受 DRM 保护，SwiftDM 不支持下载")
    for fmt in info.get("formats") or []:
        if isinstance(fmt, dict) and (fmt.get("has_drm") or fmt.get("drm")):
            raise DrmProtectedError("该资源受 DRM 保护，SwiftDM 不支持下载")


def _needs_merge(info):
    """音视频确认分离（必须 ffmpeg 合并）时返回 True。

    看不出来就不返回 True —— 误判成「需要合并」会让没装 ffmpeg 的机器下不了纯 TS 的 HLS，
    而漏判只是把同样的错误推迟到 `_run()` 里由 yt-dlp 报出来，代价小得多。
    """
    formats = [f for f in (info.get("formats") or []) if isinstance(f, dict)]
    if not formats:
        return False
    v = [f for f in formats if f.get("vcodec") not in (None, "none")]
    a = [f for f in formats if f.get("acodec") not in (None, "none")]
    muxed = [f for f in formats if f.get("vcodec") not in (None, "none")
             and f.get("acodec") not in (None, "none")]
    return bool(v and a and not muxed)


_DRM_HINTS = ("drm", "widevine", "fairplay", "playready", "protected")
_COOKIE_HINTS = ("login", "cookies", "account", "401", "403", "age")


def _reason_from_text(text):
    low = (text or "").lower()
    if any(h in low for h in _DRM_HINTS):
        return "drm_protected"
    if "ffmpeg" in low and ("not found" in low or "missing" in low or "is required" in low):
        return "needs_ffmpeg"
    if any(h in low for h in _COOKIE_HINTS):
        return "cookies_required"
    return "parse_failed"


_MEDIA_HINTS = {
    "drm_protected": "资源受 DRM 保护，无法下载（SwiftDM 不解密受保护内容）",
    "needs_ffmpeg": "缺少 ffmpeg：请安装 ffmpeg 并加入系统 PATH 后重试",
    "cookies_required": "该站点需要登录：请在浏览器登录后重试，或手动复制下载链接",
    "needs_ytdlp": "未安装 yt-dlp：请执行 pip install yt-dlp 后重启 SwiftDM",
    "parse_failed": "解析失败：链接可能已过期或站点暂不支持，可稍后重试",
}


def _friendly_media_error(reason, raw=""):
    return _MEDIA_HINTS.get(reason, f"流媒体下载失败: {raw[:120]}")
```

两点必须记住的 yt-dlp 语义（都会影响验收，不是可选项）：

1. `ratelimit` 是**每条连接**限速。`concurrent_fragment_downloads` 取 `self.segments`（默认 8），所以流媒体任务的实际总速率上限约为 `限速 × segments`，与 `DownloadTask` 那个全局令牌桶不等价。这一点写进 HANDOFF（Task 15），Task 14 的限速用例也因此固定用 `segments: 1`。
2. `progress_hooks` 的 `downloaded_bytes` 在分片下载时是**分片内偏移**还是全局累计，取决于 yt-dlp 版本；Task 5 只按假模块的「累计」契约实现，真实口径由 Task 14 的集成测试兜住。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_media_task.py -q`
Expected: PASS（16 passed）

- [ ] **Step 5: 提交**

```bash
git add media.py tests/test_media_task.py
git commit -m "feat: 新增 MediaTask —— 基于 yt-dlp 的流媒体下载任务"
```

---

## Task 6: 引擎分流 + `/api/add` 扩展

**Files:**
- Modify: `downloader.py:655-699`（`create_task` / `_reconstruct_task`）
- Modify: `app.py:52-75`（`add_task`）
- Create: `tests/test_dispatch.py`

**Interfaces:**
- Consumes: `media.MediaTask/classify_kind/MEDIA_KINDS/MediaError/KIND_*`
- Produces:
  - `DownloadManager.create_task(url, save_dir, filename=None, segments=8, kind="auto", referer=None, cookies_netscape=None, resolution=None)` → 任务对象（三种引擎任一）
  - `POST /api/add` body `{url, save_dir?, filename?, segments?, kind?, referer?, cookies_netscape?, resolution?, start_at?}` → `200 {"success":true,"task":{...}}`；`400 {"success":false,"error":...,"reason":"invalid_url"|"invalid_kind"}`；`409 {"success":false,"error":...,"reason":"drm_protected"|"needs_ffmpeg"}`

- [ ] **Step 1: 写失败的测试**

Create `tests/test_dispatch.py`:

```python
import json

import pytest

import app as appmod
import downloader
import media
from downloader import DownloadManager, DownloadTask
from media import MediaTask


@pytest.fixture(autouse=True)
def _clean_history(monkeypatch, tmp_path):
    monkeypatch.setattr(downloader.DownloadManager, "_load_history", lambda self: None)
    monkeypatch.setattr(downloader.DownloadManager, "_start_saver", lambda self: None)
    monkeypatch.setattr(downloader.DownloadManager, "save_history", lambda self: None)
    media.set_ytdlp_module(None)
    yield
    media.set_ytdlp_module(None)


def test_auto_routes_m3u8_to_media_task(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://c/v/a.m3u8", str(tmp_path), None, 8)
    assert isinstance(t, MediaTask) and t.kind == "hls"


def test_auto_routes_plain_file_to_download_task(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://c/v/a.zip", str(tmp_path), None, 8)
    assert isinstance(t, DownloadTask)


def test_explicit_video_page_routes_to_media_task(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://site/watch?v=1", str(tmp_path), None, 8, kind="video_page",
                      referer="https://site/", resolution=720)
    assert isinstance(t, MediaTask) and t.kind == "video_page" and t.resolution == 720
    assert t.referer == "https://site/"


def test_kind_http_overrides_media_extension(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://c/v/a.m3u8", str(tmp_path), None, 8, kind="http")
    assert isinstance(t, DownloadTask)


def test_torrent_still_wins_over_media(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://c/t/a.torrent", str(tmp_path), None, 8)
    assert type(t).__name__ == "TorrentTask"


def test_history_roundtrip_keeps_media_task(tmp_path):
    m = DownloadManager()
    t = m.create_task("https://c/v/a.m3u8", str(tmp_path), "a.mp4", 8, kind="hls",
                      referer="https://site/v")
    t.status = "completed"
    d = json.loads(json.dumps(t.to_dict()))      # 必须可 JSON 序列化
    back = m._reconstruct_task(d)
    assert isinstance(back, MediaTask)
    assert back.kind == "hls" and back.referer == "https://site/v"
    assert back.save_dir == str(tmp_path) and back.status == "completed"


def test_reconstruct_tolerates_legacy_records_without_kind(tmp_path):
    m = DownloadManager()
    legacy = {"task_id": "dl_9", "url": "https://c/a.zip", "status": "completed",
              "filename": "a.zip", "filepath": str(tmp_path / "a.zip")}
    back = m._reconstruct_task(legacy)
    assert isinstance(back, DownloadTask) and back.status == "completed"


@pytest.fixture()
def client():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client()


def test_api_add_accepts_media_kind_and_returns_task(client, tmp_path, monkeypatch):
    started = []
    monkeypatch.setattr(MediaTask, "start", lambda self: started.append(self.task_id))
    monkeypatch.setattr(MediaTask, "preflight", lambda self: None)
    resp = client.post("/api/add", json={"url": "https://c/v/a.m3u8", "kind": "hls",
                                        "save_dir": str(tmp_path), "segments": 12})
    body = resp.get_json()
    assert resp.status_code == 200 and body["success"] is True
    assert body["task"]["kind"] == "hls" and body["task"]["segments"] == 12
    assert started == [body["task"]["task_id"]]


def test_api_add_maps_drm_preflight_to_409(client, monkeypatch):
    monkeypatch.setattr(MediaTask, "preflight",
                        lambda self: (_ for _ in ()).throw(
                            media.DrmProtectedError("该资源受 DRM 保护，SwiftDM 不支持下载")))
    resp = client.post("/api/add", json={"url": "https://c/v/a.m3u8"})
    body = resp.get_json()
    assert resp.status_code == 409 and body["reason"] == "drm_protected"
    assert body["success"] is False


def test_api_add_dropped_task_is_not_left_in_manager(client, monkeypatch):
    monkeypatch.setattr(MediaTask, "preflight",
                        lambda self: (_ for _ in ()).throw(media.NeedsFfmpegError("需要 ffmpeg")))
    before = len(appmod.manager.get_all_tasks())
    client.post("/api/add", json={"url": "https://c/v/a.m3u8"})
    assert len(appmod.manager.get_all_tasks()) == before


def test_api_add_rejects_unknown_kind(client):
    resp = client.post("/api/add", json={"url": "https://c/a.zip", "kind": "flash"})
    assert resp.status_code == 400
    assert resp.get_json()["reason"] == "invalid_kind"


def test_api_add_start_at_defers_start(client, tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(MediaTask, "start", lambda self: calls.append("start"))
    monkeypatch.setattr(MediaTask, "preflight", lambda self: None)
    from scheduler import scheduler
    monkeypatch.setattr(scheduler, "schedule",
                        lambda tid, when: calls.append(("sched", tid, when)))
    resp = client.post("/api/add", json={"url": "https://c/v/a.m3u8",
                                        "save_dir": str(tmp_path), "start_at": 2000000000})
    assert resp.status_code == 200
    assert calls and calls[0][0] == "sched" and calls[0][2] == 2000000000
    assert resp.get_json()["task"]["scheduled_at"] == 2000000000
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_dispatch.py -q`
Expected: FAIL — `create_task() got an unexpected keyword argument 'kind'`

- [ ] **Step 3: 改 `downloader.py` 分流**

`create_task` 整体替换为：

```python
    def create_task(self, url, save_dir, filename=None, segments=8, kind="auto",
                    referer=None, cookies_netscape=None, resolution=None):
        with self._lock:
            self._counter += 1
            task_id = f"dl_{self._counter}"
            # 三种任务类型接口完全兼容：BT/PT → TorrentTask，
            # HLS/DASH/网页视频 → MediaTask，其余可 Range 的普通文件 → 多线程 DownloadTask。
            if self._is_torrent_url(url):
                from torrent import TorrentTask
                task = TorrentTask(task_id, url, save_dir, filename, segments or 0)
            else:
                from media import MediaTask, MEDIA_KINDS, classify_kind, MediaError
                try:
                    resolved = classify_kind(url, kind)
                except MediaError as e:
                    raise ValueError(str(e)) from e
                if resolved in MEDIA_KINDS:
                    task = MediaTask(task_id, url, save_dir, filename, segments, resolved,
                                     referer, cookies_netscape, resolution)
                else:
                    task = DownloadTask(task_id, url, save_dir, filename, segments)
            self._tasks[task_id] = task
            return task
```

`_reconstruct_task` 里在 `filename = ...` 之后插入 kind 分支，并把两条构造都补上 `save_dir`：

```python
        kind = d.get("kind") or ""
        referer = d.get("referer") or ""
        resolution = d.get("resolution") or None
        if self._is_torrent_url(url):
            from torrent import TorrentTask
            task = TorrentTask(d.get("task_id", ""), url, save_dir, filename, segments)
        elif kind in ("hls", "dash", "video_page"):
            from media import MediaTask
            task = MediaTask(d.get("task_id", ""), url, save_dir, filename, segments,
                             kind, referer, None, resolution)
        else:
            task = DownloadTask(d.get("task_id", ""), url, save_dir, filename, segments)
```

`DownloadTask.to_dict()`（`downloader.py:619`）在 `"segments": self.segments,` 之前补两行，让历史记录能定位文件与判断类型：

```python
            "save_dir": self.save_dir,
            "error_reason": getattr(self, "error_reason", ""),
            "kind": "http",
```

同时给 `DownloadTask.__init__` 增加 `self.error_reason = ""`（放在 `self.error = ""` 之后），并给 `DownloadTask._run_segment` 的 `except Exception as e:` 分支在 `self.error = str(e)` 之后加 `self.error_reason = "download_failed"`。

- [ ] **Step 4: 改 `app.py` 的 `add_task`**

```python
@app.route("/api/add", methods=["POST"])
def add_task():
    """添加下载任务（普通直链 / 磁力 / 种子 / HLS / DASH / 网页视频解析）"""
    data = request.get_json(silent=True) or {}
    url = (data.get("url") or "").strip()
    filename = (data.get("filename") or "").strip() or None
    segments = int(data.get("segments") or 8)
    save_dir = data.get("save_dir") or DEFAULT_DOWNLOAD_DIR
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
```

`app.py` 顶部 import 区补：

```python
from scheduler import scheduler
```

并在 `DEFAULT_DOWNLOAD_DIR` 定义之后加统一导出函数（`/api/tasks`、SSE 都改用它，Task 11 依赖）：

```python
def _with_schedule(task):
    """把定时信息并到任务字典里，UI 才能显示「定时 …」而不是干等的等待中。"""
    d = task.to_dict()
    d["scheduled_at"] = scheduler.pending_at(task.task_id)
    return d
```

`/api/tasks` 与 `stream()` 里的 `[t.to_dict() for t in manager.get_all_tasks()]` 全部替换为 `[_with_schedule(t) for t in manager.get_all_tasks()]`（共 2 处）。

- [ ] **Step 5: 建 `scheduler.py` 的最小骨架**

`tests/test_dispatch.py` 的最后一个用例需要提前存在的 `scheduler` 单例。先建 `scheduler.py`（Task 11 补全逻辑）：

```python
"""定时下载与完成后动作。本任务是能让 /api/add 跑通的最小骨架，扫描线程与完成后动作在 Task 11 补全。"""
import threading
import time


class Scheduler:
    def __init__(self):
        self._lock = threading.Lock()
        self._pending = {}          # task_id -> start_at(epoch 秒)
        self._finish_action = "none"

    def schedule(self, task_id, start_at):
        with self._lock:
            self._pending[str(task_id)] = float(start_at)

    def unschedule(self, task_id):
        with self._lock:
            self._pending.pop(str(task_id), None)

    def pending_at(self, task_id):
        with self._lock:
            return self._pending.get(str(task_id))

    def list_pending(self):
        with self._lock:
            return [{"task_id": k, "start_at": v} for k, v in self._pending.items()]

    def get_finish_action(self):
        return self._finish_action

    def set_finish_action(self, action):
        self._finish_action = action if action in ("none", "shutdown", "suspend", "beep") else "none"
        return self._finish_action


scheduler = Scheduler()
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_dispatch.py -q`
Expected: PASS（12 passed）

- [ ] **Step 7: 全量回归**

Run: `python -m pytest tests -q`
Expected: 全部 PASS

- [ ] **Step 8: 提交**

```bash
git add downloader.py app.py scheduler.py tests/test_dispatch.py
git commit -m "feat: 下载管理器按 kind 分流流媒体任务并扩展 /api/add"
```

---

## Task 7: 扩展嗅探规则 `extension/sniff.js`

**Files:**
- Create: `tests/test_sniff.js`
- Create: `extension/sniff.js`

**Interfaces:**
- Consumes: 无（纯函数，service worker / content script / Node 共用）
- Produces（全局 `SwiftDMSniff`，CommonJS 下 `module.exports`）:
  - `classifyResource({url, contentType?, contentDisposition?, status?}) -> {url,kind,quality_hint,bytes,is_mse}|null`，kind ∈ `hls|dash|video`
  - `isMseUrl(url) -> boolean`
  - `noteFragment(tabId, url) -> {url,kind,quality_hint,bytes,is_mse}|null`（同一标签页 10s 窗口内累计 ≥5 个不同 `.ts/.m4s` 片段时，判定「这页在放 HLS」并返回一条 `kind:"hls_segments"` 的摘要条目：`url` = 触发时的片段地址、`quality_hint` = `"N 个分片"`。`hls_segments` 只用于展示，**不可下载**（拿不到清单，popup 必须禁用下载按钮，`/api/add` 也会因不在 `VALID_KINDS` 而 400）；未达阈值或片段重复时返回 `null`）
  - `resetTab(tabId) -> void`
  - `cookiesToNetscape(cookies, pageUrl) -> string`
  - `qualityFromUrl(url) -> string`（`1080p/720p/480p/2160p` 之类提示，识别不到返回 `""`）

- [ ] **Step 1: 写失败的测试**

Create `tests/test_sniff.js`（用 Node 内置断言，无新依赖）:

```js
const assert = require("assert");
const S = require("../extension/sniff.js");

// --- classifyResource ---
assert.strictEqual(S.classifyResource({ url: "https://c/a/index.m3u8" }).kind, "hls");
assert.strictEqual(S.classifyResource({ url: "https://c/a/index.m3u8" }).is_mse, false);
assert.strictEqual(S.classifyResource({ url: "https://c/a/manifest.mpd" }).kind, "dash");
assert.strictEqual(
  S.classifyResource({ url: "https://c/a/x", contentType: "application/vnd.apple.mpegurl" }).kind,
  "hls");
assert.strictEqual(
  S.classifyResource({ url: "https://c/a/x", contentType: "application/dash+xml" }).kind, "dash");
assert.strictEqual(
  S.classifyResource({ url: "https://c/a/x", contentType: "video/mp4" }).kind, "video");
assert.strictEqual(
  S.classifyResource({ url: "https://c/a/seg-1.ts", contentType: "video/mp2t" }), null,
  "单个 ts 分片不该当成资源");
assert.strictEqual(S.classifyResource({ url: "https://c/a/style.css", contentType: "text/css" }), null);
assert.strictEqual(S.classifyResource({ url: "https://c/a/x.mp4", status: 404 }), null);
assert.strictEqual(
  S.classifyResource({ url: "https://c/a/x.mp4", contentDisposition: "attachment; filename=\"a.mp4\"" }).kind,
  null, "attachment 属于文件下载，交给 capture 通道");

// --- quality / size hints ---
assert.strictEqual(S.qualityFromUrl("https://c/1080p/prog_index.m3u8"), "1080P");
assert.strictEqual(S.qualityFromUrl("https://c/n_720p/master.mpd"), "720P");
assert.strictEqual(S.qualityFromUrl("https://c/master.m3u8"), "");
assert.strictEqual(
  S.classifyResource({
    url: "https://c/av/index.m3u8?x-collection=2160", contentType: "application/vnd.apple.mpegurl",
  }).quality_hint, "");

// --- MSE ---
assert.strictEqual(S.isMseUrl("blob:https://site/9c0b"), true);
assert.strictEqual(S.isMseUrl("mediastream:abc"), true);
assert.strictEqual(S.isMseUrl("https://c/a.m3u8"), false);

// --- fragment counting（只用于展示，清单未知所以不可下载） ---
const tab = "42";
for (let i = 0; i < 4; i++) {
  assert.strictEqual(S.noteFragment(tab, `https://c/seg/${i}.ts`), null, "未达阈值");
}
const hit = S.noteFragment(tab, "https://c/seg/4.ts");
assert.ok(hit, "第 5 个分片应给出摘要条目");
assert.strictEqual(hit.kind, "hls_segments");
assert.strictEqual(hit.url, "https://c/seg/4.ts");
assert.strictEqual(hit.quality_hint, "5 个分片");
assert.strictEqual(S.noteFragment(tab, "https://c/seg/4.ts"), null, "重复片段不再触发");
assert.strictEqual(S.noteFragment(tab, "https://c/seg/3.ts"), null, "已计过的片段不再触发");
const grown = S.noteFragment(tab, "https://c/seg/5.ts");
assert.strictEqual(grown.quality_hint, "6 个分片", "新增分片应刷新计数");
S.resetTab(tab);
assert.strictEqual(S.noteFragment(tab, "https://c/seg/0.ts"), null, "reset 后重新计数");

// --- cookies → Netscape ---
const cookies = [
  { name: "sid", value: "abc", domain: ".example.com", path: "/", secure: false, httpOnly: false, expirationDate: 0 },
  { name: "token", value: "xyz", domain: "api.example.com", path: "/v1", secure: true, httpOnly: true, expirationDate: 1893456000 },
  { name: "other", value: "no", domain: ".other.com", path: "/", secure: false, httpOnly: false, expirationDate: 0 },
];
const text = S.cookiesToNetscape(cookies, "https://api.example.com/watch?v=1");
const lines = text.split("\n").filter((l) => l && !l.startsWith("# Netscape") && !l.startsWith("# Generated"));
assert.strictEqual(lines.length, 2, "只保留作用域匹配的 Cookie");
assert.strictEqual(lines[0], ".example.com\tTRUE\t/\tFALSE\t0\tsid\tabc");
assert.strictEqual(lines[1], "#HttpOnly_api.example.com\tFALSE\t/v1\tTRUE\t1893456000\ttoken\txyz");
assert.ok(text.startsWith("# Netscape HTTP Cookie File"));
assert.strictEqual(S.cookiesToNetscape([], "not a url"), "");

console.log("sniff.js OK");
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node tests/test_sniff.js`
Expected: FAIL — `Cannot find module '../extension/sniff.js'`

- [ ] **Step 3: 实现 `extension/sniff.js`**

```js
// SwiftDM 嗅探规则 —— 纯函数集合，service worker / content script / Node 测试共用。
// 修改这里必须同步跑 node tests/test_sniff.js。
(function (root) {
  const MANIFEST_EXT = /\.(m3u8|mpd|f4m|ism)(\?|$)/i;
  const FRAGMENT_RE = /\.(ts|m4s|mp2t)(\?|$)/i;
  const VIDEO_CT_RE = /^(video|audio)\//i;
  const HLS_CT_RE = /^application\/(vnd\.apple\.mpegurl|x-mpegurl|mpegurl)/i;
  const DASH_CT_RE = /^application\/dash\+xml/i;

  const fragState = new Map(); // tabId -> { first: ts, urls: Set, fired: Set }
  const FRAGMENT_WINDOW = 10000;
  const FRAGMENT_THRESHOLD = 5;

  function pathOf(url) {
    try { return new URL(url).pathname.toLowerCase(); } catch (e) { return ""; }
  }

  function isMseUrl(url) {
    return typeof url === "string" && /^(blob:|mediastream:)/i.test(url);
  }

  function qualityFromUrl(url) {
    const m = (url || "").toLowerCase().match(/(2160|1440|1080|720|480|360|240)p?[_\-.\/]/);
    return m ? m[1] + "P" : "";
  }

  function classifyResource(details) {
    const url = (details && details.url) || "";
    if (!url || isMseUrl(url)) return null;
    if (details.status && details.status >= 400) return null;
    const ct = ((details.contentType || "") + "").toLowerCase();
    const cd = ((details.contentDisposition || "") + "").toLowerCase();
    // attachment 属于普通文件下载，走 capture 通道（多段加速），不当媒体嗅探结果
    if (cd.indexOf("attachment") >= 0) return null;
    const path = pathOf(url);
    let kind = null;
    if (HLS_CT_RE.test(ct) || /\.m3u8(\?|$)/i.test(path)) kind = "hls";
    else if (DASH_CT_RE.test(ct) || /\.mpd(\?|$)/i.test(path)) kind = "dash";
    else if (FRAGMENT_RE.test(path) && !/mpegurl|dash/.test(ct)) return null; // 分片交给 noteFragment
    else if (VIDEO_CT_RE.test(ct) && !/mpegurl|dash|mp2t/.test(ct)) kind = "video";
    if (!kind) return null;
    return {
      url: url,
      kind: kind,
      quality_hint: qualityFromUrl(url),
      bytes: Number(details.contentLength || 0) || 0,
      is_mse: false,
    };
  }

  function noteFragment(tabId, url) {
    if (!url || !FRAGMENT_RE.test(pathOf(url))) return null;
    const key = String(tabId);
    const now = Date.now();
    let st = fragState.get(key);
    if (!st || now - st.first > FRAGMENT_WINDOW) {
      st = { first: now, urls: new Set(), fired: new Set() };
      fragState.set(key, st);
    }
    if (st.urls.has(url)) return null;
    st.urls.add(url);
    if (st.urls.size < FRAGMENT_THRESHOLD) return null;
    const signature = Array.from(st.urls).sort().join("|");
    if (st.fired.has(signature)) return null;
    st.fired.add(signature);
    // 清单没被看见（播放器用了加密清单 / Service Worker 内部拉流），只能给出「有多少分片」这一事实。
    // kind 用 hls_segments 明确标成不可下载，避免用户点下去拿到一个孤零零的 .ts。
    return {
      url: url,
      kind: "hls_segments",
      quality_hint: st.urls.size + " 个分片",
      bytes: 0,
      is_mse: false,
    };
  }

  function resetTab(tabId) {
    fragState.delete(String(tabId));
  }

  function cookiesToNetscape(cookies, pageUrl) {
    let host = "";
    try { host = new URL(pageUrl).hostname.toLowerCase(); } catch (e) { return ""; }
    const out = ["# Netscape HTTP Cookie File", "# Generated by SwiftDM", ""];
    for (const c of cookies || []) {
      if (!c || !c.name) continue;
      const bare = String(c.domain || "").replace(/^\./, "").toLowerCase();
      if (!bare) continue;
      if (!(host === bare || host.endsWith("." + bare))) continue;   // 作用域不相关的 Cookie 一律不带
      const domain = String(c.domain).startsWith(".") ? "." + bare : bare;
      const flag = domain.startsWith(".") ? "TRUE" : "FALSE";
      const secure = c.secure ? "TRUE" : "FALSE";
      const expiry = Number(c.expirationDate || 0) | 0;
      const prefix = c.httpOnly ? "#HttpOnly_" : "";
      const value = c.value === undefined || c.value === null ? "" : String(c.value);
      out.push(prefix + [domain, flag, c.path || "/", secure, String(expiry), c.name, value].join("\t"));
    }
    return out.join("\n") + "\n";
  }

  const api = {
    MANIFEST_EXT, classifyResource, noteFragment, resetTab,
    cookiesToNetscape, isMseUrl, qualityFromUrl,
  };
  root.SwiftDMSniff = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
```

- [ ] **Step 4: 跑测试确认通过**

Run: `node tests/test_sniff.js && node --check extension/sniff.js`
Expected: 打印 `sniff.js OK`，`node --check` 无输出（exit 0）

- [ ] **Step 5: 提交**

```bash
git add extension/sniff.js tests/test_sniff.js
git commit -m "feat: 新增扩展嗅探规则 sniff.js（清单/分片/Cookie 转换）"
```

---

## Task 8: background.js —— 加宽嗅探并上报

**Files:**
- Modify: `extension/background.js`（顶部、`SWIFTDM_URLS` 段、webRequest 监听之后、消息监听段）
- Create: `tests/test_background_load.js`

**Interfaces:**
- Consumes: `SwiftDMSniff.classifyResource/noteFragment/resetTab/cookiesToNetscape`、后端 `/api/media/discover`、`/api/media/list`、`/api/add`
- Produces:
  - `SwiftDMSniff` 挂到 globalThis（`importScripts('sniff.js')`），service worker 与 content script 共用同一套规则
  - `postJson(path, data) -> Promise<object|null>`、`getJson(path) -> Promise<object|null>`（依次探测 5000/5002-5005，首个可用端口记住为 `lastGoodBase`，并写进 `chrome.storage.local.swiftBase`）
  - `reportMedia(tabId, items)`（fire-and-forget）
  - `addMediaTask(message) -> Promise<{success, task|error, reason?}>`（`hls_segments` / `is_mse` 在扩展侧直接拒绝，不发请求）
  - 消息分支（全部在既有的那一个 `chrome.runtime.onMessage` 监听器内分流，不得再注册第二个）：
    `{action:'getMedia', tabId}` → `{ok,items,page_url,title}`；
    `{action:'downloadMedia', item, tabId, pageUrl}` → `{success, task|error, reason?}`；
    `{type:'swiftdm-dom-media', items}`（来自 content script）→ `{ok:true}`
  - 既有 `getStatus/toggleEnabled/resetCount` 行为不变

- [ ] **Step 1: 写失败的测试（加载 service worker 脚本并检查接线）**

Create `tests/test_background_load.js`:

```js
// background.js 依赖 chrome.*。这里把 sniff.js + background.js 拼成一段脚本（等同 MV3 的
// importScripts 效果）在 vm 里跑，用最小桩件验证嗅探上报与 popup 消息接线。
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const EXT = (f) => fs.readFileSync(path.join(__dirname, "..", "extension", f), "utf8");
const listeners = { webRequest: [], runtime: [], tabs: [] };
const store = { enabled: true };
const fetchCalls = [];

const chrome = {
  downloads: { onCreated: { addListener() {} }, cancel() {}, erase() {} },
  webRequest: { onHeadersReceived: { addListener((fn) => listeners.webRequest.push(fn)) } },
  runtime: {
    onMessage: { addListener((fn) => listeners.runtime.push(fn)) },
    getURL: (p) => "chrome-extension://x/" + p,
    lastError: null,
  },
  storage: {
    local: {
      get(keys, cb) { cb(Object.assign({}, store)); },
      set(obj) { Object.assign(store, obj); },
    },
  },
  tabs: {
    get(id, cb) { cb({ id, url: "https://site/v", title: "视频页" }); },
    onRemoved: { addListener((fn) => listeners.tabs.push(fn)) },
  },
  cookies: { getAll(filter, cb) { cb([{ name: "sid", value: "abc", domain: ".site", path: "/" }]); } },
  notifications: { create() {} },
};

const sandbox = {
  chrome, console, setTimeout, clearTimeout, setInterval: () => 0, URL, URLSearchParams,
  AbortController: class { constructor() { this.signal = {}; } abort() {} },
  fetch: (url, opt) => {
    fetchCalls.push({ url, opt });
    const body = url.indexOf("/api/media/list") >= 0
      ? { ok: true, items: [{ url: "https://cdn.site/hls/1080p/index.m3u8", kind: "hls" }] }
      : { success: true, ok: true, added: 1, task: { filename: "index.mp4" } };
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  },
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
vm.createContext(sandbox);

const backgroundSource = EXT("background.js");
assert.ok(/importScripts\(['"]sniff\.js['"]\)/.test(backgroundSource),
  "background.js 必须引入 sniff.js");
vm.runInContext(EXT("sniff.js") + "\n" +
  backgroundSource.replace(/importScripts\([^)]*\);/, ""), sandbox,
  { filename: "background.js" });

(async () => {
  assert.ok(sandbox.SwiftDMSniff, "sniff.js 应把规则挂到 globalThis");
  assert.strictEqual(listeners.runtime.length, 1, "只应有一个 onMessage 监听器（消息在同一个 handler 内分流）");
  assert.strictEqual(listeners.webRequest.length, 2, "文件下载检测与媒体嗅探各一个监听器");

  // ① 清单响应 → 上报 discover
  const sniffListener = listeners.webRequest[1];
  await Promise.resolve(sniffListener({
    url: "https://cdn.site/hls/1080p/index.m3u8", tabId: 7, type: "xmlhttprequest",
    status: 200, responseHeaders: [{ name: "Content-Type", value: "application/vnd.apple.mpegurl" }],
  }));
  await new Promise((r) => setTimeout(r, 30));
  const posted = fetchCalls.find((c) => c.url.indexOf("/api/media/discover") >= 0);
  assert.ok(posted, "应向 /api/media/discover 上报");
  const body = JSON.parse(posted.opt.body);
  assert.strictEqual(body.tab_id, "7");
  assert.strictEqual(body.page_url, "https://site/v");
  assert.strictEqual(body.items[0].kind, "hls");
  assert.strictEqual(body.items[0].quality_hint, "1080P");

  // ② popup 拉列表
  const handler = listeners.runtime[0];
  let listResponse = null;
  handler({ action: "getMedia", tabId: 7 }, {}, (r) => { listResponse = r; });
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(listResponse && listResponse.ok === true, "getMedia 应回 ok:true");

  // ③ popup 点下载：带 kind / referer / Cookie
  let addResponse = null;
  handler({
    action: "downloadMedia", tabId: 7, pageUrl: "https://site/v",
    item: { url: "https://cdn.site/hls/1080p/index.m3u8", kind: "hls" },
  }, {}, (r) => { addResponse = r; });
  await new Promise((r) => setTimeout(r, 30));
  const add = fetchCalls.find((c) => c.url.indexOf("/api/add") >= 0);
  assert.ok(add, "downloadMedia 应调用 /api/add");
  const payload = JSON.parse(add.opt.body);
  assert.strictEqual(payload.kind, "hls");
  assert.strictEqual(payload.referer, "https://site/v");
  assert.ok(/Netscape HTTP Cookie File/.test(payload.cookies_netscape), "应随任务上传 Cookie");
  assert.ok(addResponse && addResponse.success === true);

  // ④ 分片摘要条目不可下载：必须在扩展侧就拒绝，不发 /api/add
  const before = fetchCalls.length;
  let segResponse = null;
  handler({
    action: "downloadMedia", tabId: 7, pageUrl: "https://site/v",
    item: { url: "https://cdn.site/hls/seg-9.ts", kind: "hls_segments" },
  }, {}, (r) => { segResponse = r; });
  await new Promise((r) => setTimeout(r, 30));
  assert.strictEqual(fetchCalls.length, before, "hls_segments 不该发请求");
  assert.ok(segResponse && segResponse.success === false && /清单/.test(segResponse.error));

  // ⑤ MSE 条目同样不可下载
  let mseResponse = null;
  handler({
    action: "downloadMedia", tabId: 7, pageUrl: "https://site/v",
    item: { url: "blob:https://site/9c0b", kind: "mse", is_mse: true },
  }, {}, (r) => { mseResponse = r; });
  await new Promise((r) => setTimeout(r, 30));
  assert.ok(mseResponse && mseResponse.success === false && /MSE/.test(mseResponse.error));

  // ⑥ content script 的 DOM 结果也走同一个上报通道
  const domItems = [{ url: "blob:https://site/vid", kind: "mse", is_mse: true }];
  handler({ type: "swiftdm-dom-media", items: domItems }, { tab: { id: 7, url: "https://site/v", title: "T" } }, () => {});
  await new Promise((r) => setTimeout(r, 30));
  const domPost = fetchCalls.filter((c) => c.url.indexOf("/api/media/discover") >= 0).pop();
  assert.deepStrictEqual(JSON.parse(domPost.opt.body).items, domItems);

  console.log("background.js wiring OK");
})().catch((e) => { console.error(e); process.exit(1); });
```

- [ ] **Step 2: 跑测试确认失败**

Run: `node tests/test_background_load.js`
Expected: FAIL — `background.js 必须引入 sniff.js`

- [ ] **Step 3: 改 `extension/background.js`**

第 1 行注释之后插入：

```js
importScripts('sniff.js');
```

把顶部 `SWIFTDM_URLS` 常量整段替换为（保留 5001 `/capture` 只作为文件捕获的回退，媒体端点只存在于 Flask）：

```js
// Flask 后端端口：5000 被占用时主程序会顺延到 5002-5005；5001 是浏览器监控端口
// （只收文件捕获），因此媒体/任务类接口只按 bases 依次尝试。
const SWIFTDM_BASES = [
  'http://127.0.0.1:5000',
  'http://127.0.0.1:5002',
  'http://127.0.0.1:5003',
  'http://127.0.0.1:5004',
  'http://127.0.0.1:5005'
];
const MONITOR_CAPTURE_URL = 'http://127.0.0.1:5001/capture';
let lastGoodBase = '';

async function postJson(path, data) {
  const bases = lastGoodBase ? [lastGoodBase].concat(SWIFTDM_BASES.filter(b => b !== lastGoodBase))
                             : SWIFTDM_BASES.slice();
  for (const base of bases) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 3000);
      const response = await fetch(base + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(data),
        signal: controller.signal
      });
      clearTimeout(timer);
      let result = {};
      try { result = await response.json(); } catch (e) { result = {}; }
      result.__status = response.status;
      lastGoodBase = base;
      chrome.storage.local.set({ swiftBase: base });
      return result;
    } catch (e) {
      console.debug(`[SwiftDM] ${base}${path} 连接失败:`, e.message);
    }
  }
  return null;
}

async function getJson(path) {
  const bases = lastGoodBase ? [lastGoodBase].concat(SWIFTDM_BASES.filter(b => b !== lastGoodBase))
                             : SWIFTDM_BASES.slice();
  for (const base of bases) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 3000);
      const response = await fetch(base + path, { signal: controller.signal });
      clearTimeout(timer);
      lastGoodBase = base;
      try { return await response.json(); } catch (e) { return {}; }
    } catch (e) {
      console.debug(`[SwiftDM] ${base}${path} 连接失败:`, e.message);
    }
  }
  return null;
}
```

（`sendToSwiftDM()` 保留原实现以免动既有捕获逻辑；只需在 `SWIFTDM_URLS` 被引用的地方改用 `CAPTURE_URLS`——在 `MONITOR_CAPTURE_URL` 定义后加：

```js
const CAPTURE_URLS = SWIFTDM_BASES.map(b => b + '/api/browser-capture')
                                  .concat([MONITOR_CAPTURE_URL]);   // 5001 放最后：只收文件捕获
```

并把 `sendToSwiftDM` 内 `for (const url of SWIFTDM_URLS)` 改成 `for (const url of CAPTURE_URLS)`。）

在既有 `chrome.webRequest.onHeadersReceived.addListener(...)`（文件下载检测那个）之后，新增独立的媒体嗅探监听：

```js
// ==================== 流媒体嗅探 ====================
// 与上面的「文件下载」检测互不影响：这里看的是清单/媒体响应本身。
const sniffedSeen = new Map(); // url -> timestamp，60s 内同一 URL 不重复上报

chrome.webRequest.onHeadersReceived.addListener(
  (details) => {
    if (!enabled) return;
    if (typeof details.tabId !== 'number' || details.tabId < 0) return;
    const Sniff = self.SwiftDMSniff;
    if (!Sniff) return;

    const headers = details.responseHeaders || [];
    const header = (name) => {
      const hit = headers.find(h => h.name && h.name.toLowerCase() === name);
      return hit ? hit.value : '';
    };
    const item = Sniff.classifyResource({
      url: details.url,
      contentType: header('content-type'),
      contentDisposition: header('content-disposition'),
      status: details.status,
      contentLength: header('content-length'),
    });
    if (item) {
      const last = sniffedSeen.get(details.url) || 0;
      if (Date.now() - last < 60000) return;
      sniffedSeen.set(details.url, Date.now());
      reportMedia(details.tabId, [item]);
      return;
    }
    // HLS 常见形态：没有清单可见性（DRM 播放器/加密清单），只能靠分片流量推断
    const frag = Sniff.noteFragment(details.tabId, details.url);
    if (frag) reportMedia(details.tabId, [frag]);
  },
  { urls: ['<all_urls>'], types: ['xmlhttprequest', 'media', 'sub_frame', 'object', 'other', 'fetch'] },
  ['responseHeaders']
);

function reportMedia(tabId, items) {
  chrome.tabs.get(tabId, (tab) => {
    const payload = {
      tab_id: String(tabId),
      page_url: (tab && tab.url) || '',
      title: (tab && tab.title) || '',
      items: items
    };
    if (!payload.page_url) return;
    postJson('/api/media/discover', payload).then((res) => {
      if (!res) return;
      if (res.ok) console.log('[SwiftDM] 嗅到 ' + items.length + ' 个媒体资源 (tab ' + tabId + ')');
      else if (res.reason === 'disabled') console.debug('[SwiftDM] 浏览器监控已关闭，忽略嗅探');
    });
  });
}

chrome.tabs.onRemoved.addListener((tabId) => {
  self.SwiftDMSniff && self.SwiftDMSniff.resetTab(tabId);
});
```

接 Step 3 —— 在既有消息监听器里加三个分支。

既有监听器在 `extension/background.js:226-247`，结构是：

```js
chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (message.action === 'getStatus') { ... return true; }
  if (message.action === 'toggleEnabled') { ... return true; }
  if (message.action === 'resetCount') { ... return true; }
});
```

在 `resetCount` 分支之后、同一个函数体内追加（不要新增第二个 `onMessage.addListener`，多个监听器会让 `return true` 的异步应答语义变得不可靠，测试也会断言只有一个）：

```js
  if (message.action === 'getMedia') {
    const tabId = String(message.tabId || '');
    getJson('/api/media/list?tabId=' + encodeURIComponent(tabId)).then((res) => {
      sendResponse(res || { ok: false, error: 'SwiftDM 未运行' });
    });
    return true;
  }
  if (message.action === 'downloadMedia') {
    addMediaTask(message).then((res) => sendResponse(res));
    return true;
  }
  if (message.type === 'swiftdm-dom-media') {
    const tabId = sender.tab && sender.tab.id;
    if (tabId !== undefined && Array.isArray(message.items) && message.items.length) {
      reportMedia(tabId, message.items);
    }
    sendResponse({ ok: true });
    return true;
  }
```

并在文件末尾（`showTakeoverNotification` 之后）加：

```js
// popup 点「下载」：把浏览器 Cookie 交给后端（仅写临时文件，绝不记录内容）
function getCookiesNetscape(pageUrl) {
  return new Promise((resolve) => {
    try {
      const host = new URL(pageUrl).hostname;
      chrome.cookies.getAll({}, (cookies) => {
        const mine = (cookies || []).filter(c => c && c.domain &&
          (host === c.domain.replace(/^\./, '') || host.endsWith('.' + c.domain.replace(/^\./, ''))));
        resolve(self.SwiftDMSniff.cookiesToNetscape(mine, pageUrl));
      });
    } catch (e) {
      resolve('');
    }
  });
}

async function addMediaTask(message) {
  const item = message.item || {};
  if (!item.url) return { success: false, error: '缺少媒体地址' };
  // 分片摘要与 MSE 源只是「嗅到了播放器」，拿单个 .ts / blob: 去下载只会得到一个坏文件
  if (item.kind === 'hls_segments') {
    return { success: false, reason: 'segments_only', error: '只嗅到了 HLS 分片流量，未拿到主清单，无法下载' };
  }
  if (item.is_mse || item.url.indexOf('blob:') === 0) {
    return { success: false, reason: 'mse_source',
             error: 'MSE 播放器接管的视频取不到直连地址，请用「解析本页」按网页解析下载' };
  }
  const kind = item.kind === 'video' ? 'auto' : (item.kind || 'auto');
  const cookies = await getCookiesNetscape(message.pageUrl || item.page_url || '');
  const payload = {
    url: item.url,
    kind: kind,
    filename: item.filename || '',
    referer: message.pageUrl || item.page_url || '',
    cookies_netscape: cookies,
    segments: 8
  };
  const res = await postJson('/api/add', payload);
  if (!res) return { success: false, error: 'SwiftDM 未运行' };
  if (!res.success) return { success: false, error: res.error || '添加失败', reason: res.reason || '' };
  showTakeoverNotification(item.filename || (res.task && res.task.filename) || '流媒体');
  chrome.storage.local.get(['sentCount'], (r) => {
    chrome.storage.local.set({ sentCount: (r.sentCount || 0) + 1 });
  });
  return { success: true, task: res.task };
}
```

`showTakeoverNotification` 的标题写的是「已接管下载」，媒体任务并未经过浏览器原生下载，但文案差异只影响提示语；这里复用它是为了少一个通知模板，popup 里的成功提示会另外给出准确措辞。

- [ ] **Step 4: 跑测试确认通过 + 语法检查**

Run: `node tests/test_background_load.js && node --check extension/background.js && node --check extension/sniff.js`
Expected: `background.js wiring OK`，两次 `--check` 均无输出

- [ ] **Step 5: 提交**

```bash
git add extension/background.js tests/test_background_load.js
git commit -m "feat: 扩展嗅探清单与分片流量并上报 SwiftDM"
```

---

## Task 9: content script 与 manifest 接线

**Files:**
- Create: `extension/content.js`
- Modify: `extension/manifest.json`

**Interfaces:**
- Consumes: `SwiftDMSniff.isMseUrl/qualityFromUrl`（manifest 的 content_scripts 先加载 `sniff.js`）
- Produces: `chrome.runtime.sendMessage({type:'swiftdm-dom-media', items:[...]})`；items 字段与 discover 契约一致，MSE 条目 `url` 以 `blob:` 开头且 `is_mse:true`

- [ ] **Step 1: 写 `extension/content.js`**

```js
// DOM 层嗅探：抓 <video>/<source> 的地址（清单常在网络层已经见过，这里补漏 + 标出 MSE）。
(function () {
  if (window !== window.top) return;          // 只在顶层框架跑，避免同一页面 N 份上报
  const S = window.SwiftDMSniff;
  if (!S) return;

  let timer = null;
  const sent = new Set();

  function collect() {
    const items = [];
    const nodes = document.querySelectorAll('video, audio, source');
    nodes.forEach((el) => {
      const url = el.currentSrc || el.src || '';
      if (!url) return;
      if (S.isMseUrl(url)) {
        items.push({ url: url, kind: 'mse', quality_hint: '', bytes: 0, is_mse: true });
        return;
      }
      if (!/^https?:/i.test(url)) return;
      const guess = S.classifyResource({ url: url });
      items.push(guess || {
        url: url,
        kind: /\.(m3u8)(\?|$)/i.test(url) ? 'hls' : (/\.mpd(\?|$)/i.test(url) ? 'dash' : 'video'),
        quality_hint: S.qualityFromUrl(url),
        bytes: Number(el.duration ? 0 : 0),
        is_mse: false,
      });
    });
    return items.filter((it) => {
      const key = it.url;
      if (sent.has(key)) return false;
      sent.add(key);
      return true;
    });
  }

  function flush(reason) {
    const items = collect();
    if (!items.length) return;
    try { chrome.runtime.sendMessage({ type: 'swiftdm-dom-media', items: items }); } catch (e) {}
  }

  function schedule() {
    if (timer) return;
    timer = setTimeout(() => { timer = null; flush('idle'); }, 1500);
  }

  window.addEventListener('load', schedule, true);
  document.addEventListener('play', schedule, true);
  document.addEventListener('loadedmetadata', schedule, true);
  const observer = new MutationObserver(schedule);
  observer.observe(document.documentElement, { childList: true, subtree: true, attributes: true,
                                               attributeFilter: ['src'] });
  setTimeout(() => observer.disconnect(), 60000);   // 一分钟后再不扫 DOM，省资源
  schedule();
})();
```

- [ ] **Step 2: 改 `extension/manifest.json`**

```json
  "permissions": [
    "downloads",
    "webRequest",
    "storage",
    "notifications",
    "tabs",
    "cookies"
  ],
  "host_permissions": [
    "<all_urls>"
  ],
  "background": {
    "service_worker": "background.js"
  },
  "content_scripts": [
    {
      "matches": ["<all_urls>"],
      "js": ["sniff.js", "content.js"],
      "run_at": "document_idle",
      "all_frames": false
    }
  ],
```

（`tabs` 权限是 `chrome.tabs.get()` 拿 `url/title` 与 `chrome.cookies.getAll` 之外读取活动标签页 URL 的前提。）

- [ ] **Step 3: 校验**

Run: `node --check extension/content.js && node --check extension/background.js && python -c "import json;d=json.load(open('extension/manifest.json',encoding='utf-8'));print(sorted(d['permissions']),d['content_scripts'][0]['js'])"`
Expected: 语法检查无输出；打印 `['cookies', 'downloads', 'notifications', 'storage', 'tabs', 'webRequest'] ['sniff.js', 'content.js']`

- [ ] **Step 4: 提交**

```bash
git add extension/content.js extension/manifest.json
git commit -m "feat: 增加 content script DOM 媒体嗅探与扩展权限"
```

---

## Task 10: popup 媒体列表面板

**Files:**
- Modify: `extension/popup.html`
- Modify: `extension/popup.js`

**Interfaces:**
- Consumes: `chrome.runtime` 消息 `getMedia` / `downloadMedia`（Task 8）
- Produces: 可见 UI —— 两个标签页（捕获 / 媒体），媒体行含 kind 芯片、清晰度提示、体积、下载按钮；MSE 与「只有分片流量」的行不给下载按钮，改给「解析本页」（把 page_url 以 `kind=video_page` 交给后端）

- [ ] **Step 1: 在 `popup.html` 的 `.stats` 区块之后、`.footer` 之前插入面板与样式**

```html
<div class="tabs">
  <button class="tab active" id="tabCapture">捕获</button>
  <button class="tab" id="tabMedia">媒体 <span class="badge" id="mediaCount">0</span></button>
</div>

<div id="panelCapture">
  <div class="status-row">
    <span>
      <span class="status-dot active" id="statusDot"></span>
      <span style="font-size:12px" id="statusText">已启用</span>
    </span>
    <button class="btn btn-toggle" id="toggleBtn">暂停</button>
  </div>
</div>

<div id="panelMedia" class="hidden">
  <div class="media-head">
    <span id="mediaPage" class="ellipsis">当前标签页</span>
  </div>
  <div class="media-list" id="mediaList">
    <div class="media-empty">未嗅探到媒体资源</div>
  </div>
</div>
```

（同时删掉原来单独存在的 `.status-row` 那块 —— 它已被搬进 `#panelCapture`，避免 `statusDot` 重复 id。）

在 `<style>` 末尾追加：

```css
.tabs { display: flex; gap: 6px; margin-bottom: 10px; }
.tab {
  flex: 1; padding: 6px 0; border: none; border-radius: 4px; cursor: pointer;
  background: #22222e; color: #8888a0; font-size: 12px; font-weight: 600;
}
.tab.active { background: #6c5ce7; color: #fff; }
.badge {
  display: inline-block; min-width: 16px; padding: 0 4px; border-radius: 8px;
  background: #00d2a0; color: #06231b; font-size: 10px;
}
.hidden { display: none !important; }
.media-head { font-size: 11px; color: #8888a0; margin-bottom: 6px; }
.ellipsis { display: block; max-width: 232px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.media-list { max-height: 220px; overflow-y: auto; }
.media-item {
  display: flex; align-items: center; gap: 6px; padding: 7px 8px;
  border-radius: 5px; background: #22222e; margin-bottom: 6px;
}
.media-kind {
  font-size: 9px; font-weight: 700; padding: 2px 5px; border-radius: 3px;
  background: #3b2f71; color: #a29bfe; text-transform: uppercase;
}
.media-kind.video { background: #143c33; color: #00d2a0; }
.media-kind.dash { background: #3d2a1a; color: #ffb15e; }
.media-kind.mse { background: #3a1c24; color: #ff5e7a; }
.media-name { flex: 1; font-size: 11px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.media-sub { font-size: 10px; color: #666680; }
.btn-dl { background: #00d2a0; color: #06231b; border: none; border-radius: 3px;
  font-size: 11px; font-weight: 700; padding: 4px 8px; cursor: pointer; }
.btn-dl.secondary { background: #6c5ce7; color: #fff; }
.btn-dl[disabled] { background: #2f2f3d; color: #55556a; cursor: not-allowed; }
.media-empty { font-size: 11px; color: #55556a; text-align: center; padding: 16px 0; }
```

- [ ] **Step 2: 重写 `extension/popup.js`**

```js
// SwiftDM 扩展弹窗逻辑
let enabled = true;
let activeTabId = null;

document.addEventListener('DOMContentLoaded', () => {
  loadStatus();
  document.getElementById('toggleBtn').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'toggleEnabled' }, (response) => {
      if (response) { enabled = response.enabled; updateUI(); }
    });
  });
  document.getElementById('resetBtn').addEventListener('click', () => {
    chrome.runtime.sendMessage({ action: 'resetCount' }, () => {
      document.getElementById('sentCount').textContent = '0';
    });
  });
  document.getElementById('tabCapture').addEventListener('click', () => showPanel('capture'));
  document.getElementById('tabMedia').addEventListener('click', () => showPanel('media'));
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const tab = tabs && tabs[0];
    if (!tab) return;
    activeTabId = tab.id;
    document.getElementById('mediaPage').textContent = tab.title || tab.url || '当前标签页';
    loadMedia();
  });
});

function showPanel(which) {
  document.getElementById('tabCapture').className = 'tab' + (which === 'capture' ? ' active' : '');
  document.getElementById('tabMedia').className = 'tab' + (which === 'media' ? ' active' : '');
  document.getElementById('panelCapture').className = which === 'capture' ? '' : 'hidden';
  document.getElementById('panelMedia').className = which === 'media' ? '' : 'hidden';
  if (which === 'media') loadMedia();
}

function loadStatus() {
  chrome.runtime.sendMessage({ action: 'getStatus' }, (response) => {
    if (!response) return;
    enabled = response.enabled;
    document.getElementById('sentCount').textContent = response.sentCount || 0;
    updateUI();
  });
}

function updateUI() {
  const dot = document.getElementById('statusDot');
  const text = document.getElementById('statusText');
  const btn = document.getElementById('toggleBtn');
  if (enabled) {
    dot.className = 'status-dot active';
    text.textContent = '已启用';
    btn.textContent = '暂停';
  } else {
    dot.className = 'status-dot inactive';
    text.textContent = '已暂停';
    btn.textContent = '启用';
  }
}

function formatSize(bytes) {
  if (!bytes || bytes <= 0) return '';
  const units = ['B', 'KB', 'MB', 'GB'];
  let i = 0, v = bytes;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return v.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
}

function loadMedia() {
  if (activeTabId === null) return;
  chrome.runtime.sendMessage({ action: 'getMedia', tabId: activeTabId }, (res) => {
    const box = document.getElementById('mediaList');
    if (!res || !res.ok) {
      box.innerHTML = '<div class="media-empty">连不上 SwiftDM，确认桌面端已启动</div>';
      document.getElementById('mediaCount').textContent = '0';
      return;
    }
    const items = (res.items || []).slice().sort((a, b) => (b.bytes || 0) - (a.bytes || 0));
    document.getElementById('mediaCount').textContent = String(items.length);
    if (!items.length) {
      box.innerHTML = '<div class="media-empty">未嗅探到媒体资源</div>';
      return;
    }
    box.innerHTML = '';
    items.forEach((item, idx) => box.appendChild(renderItem(item, idx, res.page_url)));
  });
}

function renderItem(item, idx, pageUrl) {
  const row = document.createElement('div');
  row.className = 'media-item';
  const kind = item.is_mse ? 'mse' : (item.kind || 'hls');
  const sub = [item.quality_hint, formatSize(item.bytes)].filter(Boolean).join(' · ') || '流媒体';
  const label = document.createElement('div');
  label.className = 'media-name';
  label.innerHTML = '<div>' + escapeHtml(nameOf(item)) + '</div>' +
                    '<div class="media-sub">' + escapeHtml(sub) + '</div>';
  const chip = document.createElement('span');
  chip.className = 'media-kind ' + kind;
  chip.textContent = kind;
  row.appendChild(chip);
  row.appendChild(label);
  if (item.is_mse || item.kind === 'hls_segments') {
    // blob: 地址离开这个页面就没用了；只有分片流量时手上也没有主清单。
    // 两种情况能做的都只有把页面地址交给解析引擎。
    row.appendChild(pageBtn(pageUrl));
  } else {
    row.appendChild(downloadBtn(item, idx, pageUrl));
  }
  return row;
}

function downloadBtn(item, idx, pageUrl) {
  const btn = document.createElement('button');
  btn.className = 'btn-dl';
  btn.textContent = '下载';
  btn.addEventListener('click', () => {
    btn.disabled = true;
    btn.textContent = '添加中';
    chrome.runtime.sendMessage({ action: 'downloadMedia', item: item, tabId: activeTabId,
      pageUrl: pageUrl || '' }, (res) => {
      if (res && res.success) { btn.textContent = '已添加'; }
      else {
        btn.disabled = false;
        btn.textContent = '重试';
        btn.title = (res && res.error) || '添加失败';
      }
    });
  });
  return btn;
}

function pageBtn(pageUrl) {
  const btn = document.createElement('button');
  btn.className = 'btn-dl secondary';
  btn.textContent = '解析本页';
  btn.title = '把当前页面地址交给网页视频解析（yt-dlp）';
  btn.disabled = !pageUrl;
  btn.addEventListener('click', () => {
    btn.disabled = true;
    btn.textContent = '添加中';
    chrome.runtime.sendMessage({ action: 'downloadMedia', tabId: activeTabId, pageUrl: pageUrl,
      item: { url: pageUrl, kind: 'video_page' } }, (res) => {
      if (res && res.success) { btn.textContent = '已添加'; }
      else {
        btn.disabled = false;
        btn.textContent = '重试';
        btn.title = (res && res.error) || '添加失败';
      }
    });
  });
  return btn;
}

function nameOf(item) {
  try {
    const u = new URL(item.url);
    const last = decodeURIComponent(u.pathname.split('/').filter(Boolean).pop() || '');
    if (last && last.indexOf('.') > 0) return last;
    return u.hostname;
  } catch (e) {
    return item.url.slice(0, 28);
  }
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
```

- [ ] **Step 3: 校验**

Run: `node --check extension/popup.js && node tests/test_background_load.js`
Expected: 无语法错误、接线测试仍通过

- [ ] **Step 4: 手工验收（必须有真实 Chrome，这一步不能跳过）**

1. `chrome://extensions` → 开发者模式 → 加载已解压的扩展程序 → 选 `extension/`。
2. 启动桌面端：`SWIFTDM_PORT=5080 SWIFTDM_MONITOR_PORT=5081 python main.py --web-only`（后台运行）。
3. 临时把 `SWIFTDM_BASES` 首项改成 `http://127.0.0.1:5080`（验收完还原，或用 `chrome.storage.local.set({swiftBase})` 调试）。
4. 打开含 HLS 的测试页（例如 `https://test-streams.mux.dev/x36xhzz/x36xhzz.m3u8` 的播放页），点扩展图标 → 「媒体」标签。
Expected: 至少一条 `hls`，点「下载」→ SwiftDM 任务列表出现该任务并开始下载，桌面出现接管通知。
5. 记录无法自动化的结果（含 DRM 站点应提示「受 DRM 保护」）。

- [ ] **Step 5: 提交**

```bash
git add extension/popup.html extension/popup.js
git commit -m "feat: 扩展弹窗新增嗅探媒体列表与一键下载"
```

---

## Task 11: 定时下载与完成后动作（scheduler.py 完整实现）

**Files:**
- Modify: `scheduler.py`（整体替换 Task 6 的骨架）
- Create: `tests/test_scheduler.py`、`tests/test_scheduler_api.py`
- Modify: `app.py:96-101`（`/api/cancel`）、`app.py:116-119`（`/api/remove`）、`app.py:144-158`（`/api/settings`）、`app.py` 新增 `/api/finish_action/cancel`
- Modify: `main.py:206-207`（Flask 线程之前拉起扫描线程）、`app.py` 末尾 `__main__` 段

**Interfaces:**
- Consumes: Task 6 骨架的 `schedule/unschedule/pending_at/list_pending/get_finish_action/set_finish_action` 签名（**必须保持**，否则 `tests/test_dispatch.py` 与 `_with_schedule()` 会断）、`manager.get_all_tasks()`、`manager.get_task(task_id)`、Task 3 的 `media.ffmpeg_status()` / `media.ytdlp_available()`、Task 4 的 `_cors()`
- Produces:
  - `Scheduler(manager=None, runner=None, beeper=None, suspender=None, clock=time.time)`、模块级单例 `scheduler`
  - `scheduler.scan(now=None) -> {"started": [task_id], "armed": bool, "disarmed": bool, "fired": action|None}` —— **唯一推进状态的入口**；扫描线程和测试都调它
  - `scheduler.start() -> bool`（幂等；只在 `main.py` / `python app.py` 里调用，pytest 永不拉起线程）
  - `scheduler.status(now=None) -> {"finish_action","fire_at","remaining","scheduled"}`
  - `scheduler.countdown_remaining(now=None) -> int`（未武装返回 0）
  - `scheduler.cancel_finish_action() -> {"action": str, "cancelled": bool}`
  - `build_command(action, platform=None) -> list[str] | None`
  - 常量 `FINISH_ACTIONS = ("none","shutdown","suspend","beep")`、`COUNTDOWN_SECONDS = 60`、`SCAN_INTERVAL = 5`
  - HTTP：`GET /api/settings` 增加 `finish_action / finish_countdown / scheduled / capabilities`；`POST /api/settings {"finish_action": ...}`；`POST /api/finish_action/cancel`

设计要点（评审时按这几条判断，不要自由发挥）：

1. 状态只在 `scan()` 里推进，倒计时靠 `fire_at` 与外部时钟比较得出，不睡眠、不猜。
2. `set_finish_action()` 之后必须**先观察到忙过一次**才允许武装倒计时 —— 否则用户在任务全部结束之后才勾选「关机」，会立刻关机。
3. 副作用（`task.start()` / 关机命令 / 提示音）一律在 `self._lock` 之外执行，避免持锁调用外部代码。
4. 一次动作只服务一批任务：触发后 `_finish_action` 复位为 `none`，要再来一次必须重新勾选。
5. `finish_action` 与 `rate_limit` 一样**不落盘**：重启即失效，避免上次会话遗留的关机计划把机器关掉。
6. 定时任务不跨重启：历史加载时 `pending` 会被 `_reconstruct_task()` 改成 `cancelled`（`downloader.py:703` 起的既有行为），UI 侧无需特判。
7. 倒计时已经武装后，只要列表里再出现任何忙任务（重试、新添加、还没到期的定时任务），本轮 `scan()` 立刻撤销倒计时（`disarmed=True`）—— 对应 spec §4「60 秒缓冲期内任何新任务取消动作」。
8. Web-only 下没有系统托盘，完成后的通知退化为 SSE 字段：`_stream_payload()` 里带 `finish: {action, remaining}`，前端据此弹一次提示（Task 13 消费）。

- [ ] **Step 1: 写失败的单元测试**

Create `tests/test_scheduler.py`:

```python
import pytest

from scheduler import COUNTDOWN_SECONDS, Scheduler, build_command


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class FakeTask:
    def __init__(self, task_id, status="pending"):
        self.task_id = task_id
        self.status = status
        self.started = 0

    def start(self):
        self.started += 1
        self.status = "downloading"


class FakeManager:
    def __init__(self, tasks=()):
        self.tasks = {t.task_id: t for t in tasks}

    def get_all_tasks(self):
        return list(self.tasks.values())

    def get_task(self, task_id):
        return self.tasks.get(task_id)


def make(tasks=(), start=1000.0):
    """返回 (scheduler, clock, 命令记录, 提示音记录, 休眠记录)，全部副作用都是假的。"""
    clock = Clock(start)
    cmds, beeps, suspends = [], [], []
    s = Scheduler(manager=FakeManager(tasks), runner=cmds.append,
                  beeper=lambda: beeps.append(1), suspender=lambda: suspends.append(1),
                  clock=clock)
    return s, clock, cmds, beeps, suspends


# ---------------- 定时开始 ----------------
def test_scan_starts_only_due_tasks():
    a, b = FakeTask("t_1"), FakeTask("t_2")
    s, clock, *_ = make([a, b])
    s.schedule("t_1", 1000.0)
    s.schedule("t_2", 2000.0)
    clock.t = 1001.0
    out = s.scan()
    assert out["started"] == ["t_1"]
    assert a.started == 1 and a.status == "downloading"
    assert b.started == 0
    assert s.pending_at("t_1") is None and s.pending_at("t_2") == 2000.0


def test_scan_drops_tasks_that_are_no_longer_pending():
    a = FakeTask("t_1", status="cancelled")
    s, clock, *_ = make([a])
    s.schedule("t_1", 1000.0)
    clock.t = 1005.0
    assert s.scan()["started"] == []
    assert a.started == 0 and s.pending_at("t_1") is None


def test_scan_ignores_unknown_task_id():
    s, clock, *_ = make([])
    s.schedule("ghost", 1000.0)
    clock.t = 1001.0
    assert s.scan()["started"] == []


def test_unschedule_reports_whether_it_removed():
    s, *_ = make([])
    s.schedule("t_1", 10.0)
    assert s.unschedule("t_1") is True
    assert s.unschedule("t_1") is False


def test_list_pending_is_sorted_by_start_at():
    s, *_ = make([])
    s.schedule("t_2", 30.0)
    s.schedule("t_1", 10.0)
    assert s.list_pending() == [{"task_id": "t_1", "start_at": 10.0},
                                {"task_id": "t_2", "start_at": 30.0}]


# ---------------- 完成后动作 ----------------
def test_finish_action_arms_after_all_tasks_leave_busy_state():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, beeps, _ = make([t])
    s.set_finish_action("beep")
    s.scan()
    assert s.countdown_remaining() == 0          # 还在下载，不该倒计时
    t.status = "completed"
    s.scan()
    assert s.countdown_remaining() == COUNTDOWN_SECONDS
    clock.t += COUNTDOWN_SECONDS
    assert s.scan()["fired"] == "beep"
    assert beeps == [1] and cmds == []
    assert s.countdown_remaining() == 0


def test_finish_action_does_not_fire_when_nothing_ever_ran():
    done = FakeTask("t_1", status="completed")
    s, clock, cmds, _, suspends = make([done])
    s.set_finish_action("shutdown")
    s.scan()
    clock.t += COUNTDOWN_SECONDS * 2
    s.scan()
    assert cmds == [] and suspends == []          # 没见过「忙」，就不该突然关机
    assert s.countdown_remaining() == 0


def test_pause_counts_as_busy():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, _, _ = make([t])
    s.set_finish_action("shutdown")
    t.status = "paused"
    s.scan()
    clock.t += COUNTDOWN_SECONDS * 2
    s.scan()
    assert cmds == [] and s.countdown_remaining() == 0


def test_cancel_finish_action_stops_countdown():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, _, _ = make([t])
    s.set_finish_action("shutdown")
    t.status = "completed"
    s.scan()
    assert s.countdown_remaining() == COUNTDOWN_SECONDS
    res = s.cancel_finish_action()
    assert res == {"action": "shutdown", "cancelled": True}
    assert s.get_finish_action() == "none"
    clock.t += COUNTDOWN_SECONDS
    s.scan()
    assert cmds == []


def test_new_task_during_countdown_cancels_the_action():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, _, _ = make([t])
    s.set_finish_action("shutdown")
    t.status = "completed"
    s.scan()
    assert s.countdown_remaining() == COUNTDOWN_SECONDS
    t.status = "downloading"                 # 缓冲期内任务又跑起来了（重试 / 新添加）
    assert s.scan()["disarmed"] is True
    assert s.countdown_remaining() == 0
    clock.t += COUNTDOWN_SECONDS
    s.scan()
    assert cmds == []


def test_watch_stops_after_one_fire():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, _, _ = make([t])
    s.set_finish_action("shutdown")
    t.status = "completed"
    s.scan()
    clock.t += COUNTDOWN_SECONDS
    s.scan()
    assert len(cmds) == 1
    clock.t += COUNTDOWN_SECONDS * 2
    s.scan()
    assert len(cmds) == 1                         # 一次勾选只管一批任务


def test_suspend_uses_injected_suspender():
    t = FakeTask("t_1", status="downloading")
    s, clock, cmds, _, suspends = make([t])
    s.set_finish_action("suspend")
    t.status = "completed"
    s.scan()
    clock.t += COUNTDOWN_SECONDS
    assert s.scan()["fired"] == "suspend"
    assert suspends == [1] and cmds == []


def test_scan_survives_failing_runner():
    t = FakeTask("t_1", status="downloading")
    clock = Clock()

    def boom(cmd):
        raise OSError("系统拒绝访问")

    s = Scheduler(manager=FakeManager([t]), runner=boom, clock=clock)
    s.set_finish_action("shutdown")
    t.status = "completed"
    s.scan()
    clock.t += COUNTDOWN_SECONDS
    assert s.scan()["fired"] == "shutdown"        # 异常被记录，不再向上抛
    assert s.get_finish_action() == "none"


def test_unknown_action_falls_back_to_none():
    s, *_ = make([])
    assert s.set_finish_action("reboot") == "none"
    assert s.get_finish_action() == "none"


def test_start_is_idempotent():
    s, *_ = make([])
    assert s.start() is True
    assert s.start() is False


# ---------------- 平台命令 ----------------
@pytest.mark.parametrize("plat,expected", [
    ("win32", ["shutdown", "/s", "/t", "5"]),
    ("darwin", ["osascript", "-e", 'tell app "System Events" to shut down']),
    ("linux", ["shutdown", "-h", "+1"]),
])
def test_build_command_shutdown(plat, expected):
    assert build_command("shutdown", plat) == expected


def test_build_command_suspend_and_beep():
    assert build_command("suspend", "win32") is None      # Windows 走 SetSuspendState
    assert build_command("suspend", "linux") == ["systemctl", "suspend"]
    assert build_command("beep", "win32") is None
    assert build_command("none", "win32") is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_scheduler.py -q`
Expected: FAIL —— `TypeError: Scheduler.__init__() got an unexpected keyword argument 'manager'`（Task 6 骨架还没有注入点）

- [ ] **Step 3: 用完整实现整体替换 `scheduler.py`**

```python
"""
下载调度 —— 定时开始 + 一批任务全部结束后的动作（关机 / 休眠 / 提示音）。

所有状态推进都集中在 scan()：扫描线程按点调它，测试直接调它。
这样倒计时不依赖真实 sleep，也不会有第二个地方偷偷改 fire_at。
"""
import logging
import os
import sys
import threading
import time

FINISH_ACTIONS = ("none", "shutdown", "suspend", "beep")
COUNTDOWN_SECONDS = 60     # 全部完成后再等 60 秒，给人留出取消时间
SCAN_INTERVAL = 5          # 扫描节拍：定时开始的延迟不会超过它
OS_TAIL_SECONDS = 5        # 命令下发后操作系统自己的宽限（Windows shutdown /t）
BUSY_STATES = ("pending", "downloading", "paused")
DONE_STATES = ("completed", "failed", "cancelled")

logger = logging.getLogger("swiftdm.scheduler")


def build_command(action, platform=None):
    """把动作翻译成外部命令；返回 None 表示不需要命令（走系统 API 或本进程内处理）。"""
    plat = platform or sys.platform
    if action == "shutdown":
        if plat.startswith("win"):
            return ["shutdown", "/s", "/t", str(OS_TAIL_SECONDS)]
        if plat == "darwin":
            return ["osascript", "-e", 'tell app "System Events" to shut down']
        return ["shutdown", "-h", "+1"]
    if action == "suspend":
        if plat.startswith("win"):
            return None
        return ["systemctl", "suspend"]
    return None


def _default_runner(cmd):
    import subprocess
    kwargs = {"creationflags": 0x08000000} if os.name == "nt" else {}   # CREATE_NO_WINDOW
    subprocess.Popen(cmd, **kwargs)


def _default_beeper():
    if os.name == "nt":
        import winsound
        for freq in (880, 660, 880):
            winsound.Beep(freq, 220)
    else:
        sys.stdout.write("\a")
        sys.stdout.flush()


def _default_suspender():
    if os.name == "nt":
        import ctypes
        ctypes.windll.powrprof.SetSuspendState(0, 1, 0)
        return
    cmd = build_command("suspend", sys.platform)
    if cmd:
        _default_runner(cmd)


class Scheduler:
    def __init__(self, manager=None, runner=None, beeper=None, suspender=None,
                 clock=time.time):
        self._lock = threading.Lock()
        self._pending = {}           # task_id -> start_at（epoch 秒）
        self._finish_action = "none"
        self._watching = False       # 是否仍在等待「本批任务结束」
        self._saw_busy = False       # 勾选之后是否真的忙过
        self._fire_at = None         # 倒计时到点时刻
        self._manager = manager      # 惰性取全局 manager，测试可注入假的
        self._runner = runner or _default_runner
        self._beeper = beeper or _default_beeper
        self._suspender = suspender or _default_suspender
        self._clock = clock
        self._started = False

    # ---------------- 任务表 ----------------
    def _mgr(self):
        if self._manager is None:
            from downloader import manager
            self._manager = manager
        return self._manager

    # ---------------- 定时开始 ----------------
    def schedule(self, task_id, start_at):
        with self._lock:
            self._pending[str(task_id)] = float(start_at)

    def unschedule(self, task_id):
        with self._lock:
            return self._pending.pop(str(task_id), None) is not None

    def pending_at(self, task_id):
        with self._lock:
            return self._pending.get(str(task_id))

    def list_pending(self):
        with self._lock:
            return self._pending_list()

    def _pending_list(self):
        return sorted(({"task_id": k, "start_at": v} for k, v in self._pending.items()),
                      key=lambda d: d["start_at"])

    # ---------------- 完成后动作 ----------------
    def get_finish_action(self):
        with self._lock:
            return self._finish_action

    def set_finish_action(self, action):
        value = action if action in FINISH_ACTIONS else "none"
        with self._lock:
            self._finish_action = value
            self._watching = value != "none"
            self._saw_busy = False
            self._fire_at = None
        return value

    def countdown_remaining(self, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            return self._remaining(now)

    def _remaining(self, now):
        if self._fire_at is None:
            return 0
        return max(0, int(round(self._fire_at - now)))

    def cancel_finish_action(self):
        with self._lock:
            action = self._finish_action
            was_armed = self._fire_at is not None
            self._finish_action = "none"
            self._watching = False
            self._saw_busy = False
            self._fire_at = None
        return {"action": action, "cancelled": was_armed}

    def status(self, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            return {
                "finish_action": self._finish_action,
                "fire_at": self._fire_at,
                "remaining": self._remaining(now),
                "scheduled": self._pending_list(),
            }

    # ---------------- 状态推进 ----------------
    def scan(self, now=None):
        now = self._clock() if now is None else now
        due, fire, armed, disarmed = [], None, False, False
        with self._lock:
            for tid, at in list(self._pending.items()):
                if at <= now:
                    self._pending.pop(tid, None)
                    due.append(tid)

            if self._watching:
                busy = done = False
                for t in self._mgr().get_all_tasks():
                    st = getattr(t, "status", "")
                    if st in BUSY_STATES:
                        busy = True
                    elif st in DONE_STATES:
                        done = True
                if busy:
                    self._saw_busy = True
                    if self._fire_at is not None:
                        self._fire_at = None        # 缓冲期内又来了新任务 -> 撤销倒计时
                        disarmed = True
                elif done and self._saw_busy and self._fire_at is None:
                    self._fire_at = now + COUNTDOWN_SECONDS
                    armed = True

            if self._fire_at is not None and now >= self._fire_at:
                fire = self._finish_action
                self._fire_at = None
                self._watching = False
                self._saw_busy = False
                self._finish_action = "none"

        started = self._start_now(due)          # 锁外执行副作用
        if fire:
            self._fire(fire)
        return {"started": started, "armed": armed, "disarmed": disarmed, "fired": fire}

    def _start_now(self, task_ids):
        started = []
        for tid in task_ids:
            task = self._mgr().get_task(tid)
            if task is None or getattr(task, "status", None) != "pending":
                continue                        # 已取消/已删除的到点任务直接丢弃登记
            try:
                task.start()
                started.append(tid)
            except Exception as e:
                logger.warning("定时启动任务 %s 失败: %s", tid, e)
        return started

    def _fire(self, action):
        try:
            if action == "beep":
                self._beeper()
                return
            if action == "suspend":
                self._suspender()
                return
            cmd = build_command(action, sys.platform)
            if cmd:
                self._runner(cmd)
        except Exception as e:
            logger.warning("完成后动作 %s 执行失败: %s", action, e)

    # ---------------- 扫描线程 ----------------
    def start(self):
        with self._lock:
            if self._started:
                return False
            self._started = True
        threading.Thread(target=self._loop, daemon=True, name="swiftdm-scheduler").start()
        return True

    def _loop(self):
        while True:
            time.sleep(SCAN_INTERVAL)
            try:
                self.scan()
            except Exception:
                logger.warning("定时扫描异常", exc_info=True)


scheduler = Scheduler()
```

- [ ] **Step 4: 跑测试确认通过 + 回归 Task 6**

Run: `python -m pytest tests/test_scheduler.py tests/test_dispatch.py -q`
Expected: PASS —— `tests/test_scheduler.py` 19 个用例（`test_build_command_shutdown` 参数化成 3 个）全绿，`test_dispatch.py` 12 个仍绿

- [ ] **Step 5: 写失败的 API 测试**

Create `tests/test_scheduler_api.py`:

```python
import pytest

import app as appmod


class FakeScheduler:
    """只实现 /api/settings 与取消类路由用到的方法。"""

    def __init__(self):
        self.action = "none"
        self.unscheduled = []

    def get_finish_action(self):
        return self.action

    def set_finish_action(self, action):
        self.action = action if action in ("none", "shutdown", "suspend", "beep") else "none"
        return self.action

    def cancel_finish_action(self):
        old, self.action = self.action, "none"
        return {"action": old, "cancelled": True}

    def status(self, now=None):
        return {"finish_action": self.action, "fire_at": None, "remaining": 42,
                "scheduled": [{"task_id": "t_9", "start_at": 1234.0}]}

    def unschedule(self, task_id):
        self.unscheduled.append(task_id)
        return True

    def pending_at(self, task_id):
        return 1234.0 if task_id == "t_9" else None


@pytest.fixture
def fake(monkeypatch):
    f = FakeScheduler()
    monkeypatch.setattr(appmod, "scheduler", f)
    monkeypatch.setattr(appmod, "ffmpeg_status", lambda: {"available": False, "path": None})
    monkeypatch.setattr(appmod, "ytdlp_available", lambda: True)
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client(), f


def test_settings_reports_scheduler_state_and_capabilities(fake):
    client, _ = fake
    got = client.get("/api/settings").get_json()
    assert got["finish_action"] == "none"
    assert got["finish_countdown"] == 42
    assert got["scheduled"] == [{"task_id": "t_9", "start_at": 1234.0}]
    assert got["capabilities"] == {"ffmpeg": {"available": False, "path": None},
                                   "ytdlp": True}


def test_settings_post_finish_action_roundtrip(fake):
    client, f = fake
    posted = client.post("/api/settings", json={"finish_action": "shutdown"}).get_json()
    assert posted["success"] is True and posted["finish_action"] == "shutdown"
    assert f.action == "shutdown"
    client.post("/api/settings", json={"finish_action": "reboot"})
    assert f.action == "none"


def test_cancel_endpoint_clears_action(fake):
    client, f = fake
    f.action = "shutdown"
    res = client.post("/api/finish_action/cancel").get_json()
    assert res["success"] is True and res["cancelled"] is True and res["action"] == "shutdown"
    assert f.action == "none"


def test_cancel_and_remove_routes_unschedule_the_task(fake):
    client, f = fake
    client.post("/api/cancel/t_9")
    client.delete("/api/remove/t_9")
    assert f.unscheduled == ["t_9", "t_9"]
```

- [ ] **Step 6: 跑测试确认失败**

Run: `python -m pytest tests/test_scheduler_api.py -q`
Expected: FAIL —— `AttributeError: module 'app' has no attribute 'ffmpeg_status'`（fixture 还挂不上不存在的名）；即使挂上，`/api/settings` 也没有 `finish_action` 字段

- [ ] **Step 7: 改 `app.py`**

顶部 import 区补一行（Task 6 已经引入了 `scheduler` 单例）：

```python
from media import ffmpeg_status, ytdlp_available
```

`settings()`（`app.py:144-158`，Task 4 已加过 `rate_limit`）整体替换为：

```python
@app.route("/api/settings", methods=["GET", "POST", "OPTIONS"])
def settings():
    if request.method == "OPTIONS":
        return _cors(app.make_default_options_response(), "GET, POST, OPTIONS")
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        # 代理模式: env(系统代理) / direct(直连) / 自定义地址
        if "proxy_mode" in data:
            set_proxy_mode(data["proxy_mode"])
        if "rate_limit" in data:
            set_rate(data["rate_limit"])
        if "finish_action" in data:
            scheduler.set_finish_action(data["finish_action"])
        return jsonify({"success": True, "proxy_mode": get_proxy_mode(),
                        "rate_limit": get_rate(),
                        "finish_action": scheduler.get_finish_action()})
    st = scheduler.status()
    return jsonify({
        "download_dir": DEFAULT_DOWNLOAD_DIR,
        "default_segments": 8,
        "proxy_mode": get_proxy_mode(),
        "proxy_modes": ["env", "direct"],
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
```

`cancel_task()`（`app.py:96-101`）与 `remove_task()`（`app.py:116-119`）各加一行，撤销定时登记，免得取消/删除之后到点又被拉起来：

```python
@app.route("/api/cancel/<task_id>", methods=["POST"])
def cancel_task(task_id):
    scheduler.unschedule(task_id)
    task = manager.get_task(task_id)
    if task:
        task.cancel()
    return jsonify({"success": True})


@app.route("/api/remove/<task_id>", methods=["DELETE"])
def remove_task(task_id):
    scheduler.unschedule(task_id)
    manager.remove_task(task_id)
    return jsonify({"success": True})
```

最后把 SSE 帧的构造抽成纯函数（`stream()` 是死循环生成器，没法直接单测；spec §4 要求 Web-only 下完成后的通知退化为 SSE 事件，所以这一帧必须可测）。用 Task 6 改过的 `stream()`（`app.py:257-274`）替换为：

```python
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
    """Server-Sent Events 实时推送任务状态"""
    def generate():
        last_stats = None
        while True:
            payload_str = json.dumps(_stream_payload())

            # 仅在有变化时推送
            if payload_str != last_stats:
                last_stats = payload_str
                yield f"data: {payload_str}\n\n"

            time.sleep(0.5)
    return Response(generate(), mimetype="text/event-stream")
```

并在 `tests/test_scheduler_api.py` 末尾补一个用例（同一个 `fake` fixture）：

```python
def test_stream_payload_carries_schedule_and_finish_state(fake):
    _client, f = fake
    f.action = "shutdown"
    payload = appmod._stream_payload()
    assert payload["finish"] == {"action": "shutdown", "remaining": 42}
    assert isinstance(payload["tasks"], list) and "stats" in payload
```

- [ ] **Step 8: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_scheduler_api.py -q && python -m pytest tests -q`
Expected: 5 passed；随后全量 PASS

- [ ] **Step 9: 拉起扫描线程**

`main.py` 在 `# 3. 启动 Flask Web 服务器` 之前（`main.py:206` 附近）插入：

```python
    # 2.5 下载调度线程：定时开始 + 完成后的动作（不启动则定时永不生效）
    from scheduler import scheduler as dl_scheduler
    dl_scheduler.start()
```

`app.py` 末尾的 `if __name__ == "__main__":` 段里，`app.run(...)` 之前加：

```python
    scheduler.start()
```

- [ ] **Step 10: 手工验收（不真关机）**

1. `python -m pytest tests -q` 全绿后启动：`SWIFTDM_PORT=5080 SWIFTDM_MONITOR_PORT=5081 python main.py --web-only`。
2. 勾完成后动作：`curl -s -X POST http://127.0.0.1:5080/api/settings -H "Content-Type: application/json" -d "{\"finish_action\":\"beep\"}"`（Windows 下该命令走 `shutdown /s /t 5` 的分支只由单元测试覆盖，这里用 `beep` 验证真实触发链路）。
3. 添加一个本机测试下载（`/api/self-test` 或直链），等它完成 → 60 秒倒计时后应听到提示音；期间 `curl -s http://127.0.0.1:5080/api/settings` 的 `finish_countdown` 应逐秒递减，`POST /api/finish_action/cancel` 后应停在 0。
4. 定时：`curl -s -X POST http://127.0.0.1:5080/api/add -H "Content-Type: application/json" -d "{\"url\":\"http://127.0.0.1:5080/api/local-test-file\",\"start_at\":<当前 epoch+90>}"` → 任务应先停在「等待中」，90 秒后自动开跑。
Expected: 三条全部符合；不符合就回到 Step 3 修 `scan()`，不要绕过测试。

- [ ] **Step 11: 提交**

```bash
git add scheduler.py app.py main.py tests/test_scheduler.py tests/test_scheduler_api.py
git commit -m "feat: 新增定时下载与完成后动作调度器"
```

---

## Task 12: Web UI —— 添加面板与任务卡升级

**Files:**
- Modify: `templates/index.html:445-449`（add-bar）、`templates/index.html:508-525`（`addTask()`）、`templates/index.html:623-700`（`createTaskCard()`）、`<style>` 段末尾（`index.html:422` 之前）
- Create: `tests/test_web_ui.py`

**Interfaces:**
- Consumes: `POST /api/add` 的扩展字段（Task 6）、任务字典里的 `kind / error_reason / resolution / scheduled_at`（Task 5、6）、SSE 推送的 `_with_schedule()` 结果（Task 6）
- Produces: DOM 契约 —— `#advToggle` `#addAdv` `#kindSelect` `#resolutionSelect` `#startAtInput`；`addTask()` 发送 `{url, segments:8, kind, resolution, start_at}`；任务卡渲染 `.kind-badge.<kind>`、`.sched-badge`；全局函数 `escapeHtml(s)` 与 `REASON_HINTS`

- [ ] **Step 1: 写失败的测试**

`index.html` 是纯静态模板，测试就盯住渲染结果里的结构契约（新增 id、请求字段、转义调用），不测像素。

Create `tests/test_web_ui.py`:

```python
import re

import pytest

import app as appmod


@pytest.fixture(scope="module")
def page():
    appmod.app.config["TESTING"] = True
    body = appmod.app.test_client().get("/").get_data(as_text=True)
    assert "SwiftDM" in body
    return body


def test_add_panel_has_kind_resolution_and_schedule(page):
    for needle in ('id="advToggle"', 'id="addAdv"', 'id="kindSelect"',
                   'id="resolutionSelect"', 'id="startAtInput"'):
        assert needle in page, needle


def test_kind_options_match_backend_contract(page):
    options = re.findall(r'<option value="(\w+)"', page)
    # 后端 VALID_KINDS 去掉磁力/种子（按 URL 自动识别，不需要用户选）
    assert set(["auto", "http", "hls", "dash", "video_page"]) <= set(options)


def test_add_task_sends_new_fields(page):
    start = page.index("async function addTask")
    payload = page[start:page.index("async function addFromClipboard")]
    assert re.search(r"JSON\.stringify\(\{[^}]*kind", payload), "kind 必须直接出现在请求体里"
    for key in ("url", "segments", "kind", "resolution", "start_at"):
        assert key in payload, key


def test_start_at_is_converted_to_epoch_seconds(page):
    assert "getTime()" in page and "1000" in page
    # 留空 = 立即开始，不能把 NaN 发给后端
    assert re.search(r"start_at:\s*[^\n]*NaN[^\n]*", page) or "isNaN" in page


def test_task_card_shows_kind_and_schedule(page):
    card = page[page.index("function createTaskCard"):]
    card = card[:card.index("function renderStats")]
    assert "kind-badge" in card and "scheduled_at" in card
    assert "REASON_HINTS" in page and "drm_protected" in page


def test_filename_and_error_are_escaped(page):
    card = page[page.index("function createTaskCard"):]
    card = card[:card.index("function renderStats")]
    assert "escapeHtml(task.filename)" in card
    assert "escapeHtml(task.error)" in card
    # 原始插值必须消失，否则远端文件名可以注入脚本
    assert 'title="${task.filename}"' not in card
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_web_ui.py -q`
Expected: FAIL —— `assert 'id="kindSelect"' in ...`（6 个用例全红）

- [ ] **Step 3: 改 HTML —— 展开式添加面板**

把 `index.html:445-449` 的 add-bar 整段替换为：

```html
  <!-- 添加下载 -->
  <div class="add-wrap">
    <div class="add-bar">
      <input type="text" id="urlInput" placeholder="粘贴下载链接 (HTTP/HTTPS / 磁力 / m3u8 / 视频网页) ..." autocomplete="off">
      <button class="btn btn-secondary btn-icon" onclick="addFromClipboard()" title="粘贴剪贴板">&#128203;</button>
      <button class="btn btn-secondary btn-icon" id="advToggle" onclick="toggleAdvanced()" title="类型 / 清晰度 / 定时">&#8987;</button>
      <button class="btn btn-primary" onclick="addTask()">&#11015; 开始下载</button>
    </div>
    <div class="add-adv" id="addAdv">
      <label>类型
        <select id="kindSelect">
          <option value="auto">自动识别</option>
          <option value="http">普通文件直链</option>
          <option value="hls">HLS 流 (m3u8)</option>
          <option value="dash">DASH 流 (mpd)</option>
          <option value="video_page">网页视频解析</option>
        </select>
      </label>
      <label>清晰度
        <select id="resolutionSelect">
          <option value="0">最佳可用</option>
          <option value="1080">1080p</option>
          <option value="720">720p</option>
          <option value="480">480p</option>
        </select>
      </label>
      <label>定时开始
        <input type="datetime-local" id="startAtInput">
      </label>
      <span class="adv-tip">清晰度仅对「网页视频解析」生效；留空类型即按链接自动判断</span>
    </div>
  </div>
```

在 `<style>` 结束标签之前（`index.html:422` 之前）追加：

```css
/* 添加面板：默认收起，避免把主输入框挤得不像样 */
.add-adv {
  display: none; gap: 12px; align-items: flex-end; flex-wrap: wrap;
  margin-top: 8px; padding: 10px 12px;
  background: var(--card); border: 1px solid var(--border); border-radius: 8px;
}
.add-adv.open { display: flex; }
.add-adv label { display: flex; flex-direction: column; gap: 4px; font-size: 11px; color: var(--text2); }
.add-adv select, .add-adv input[type="datetime-local"] {
  background: var(--bg); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 5px 8px; font-size: 12px; font-family: inherit;
}
.adv-tip { font-size: 11px; color: var(--text2); flex-basis: 100%; }
.kind-badge, .sched-badge {
  display: inline-block; padding: 1px 6px; margin-right: 6px; border-radius: 4px;
  font-size: 10px; font-weight: 700; vertical-align: 2px;
}
.kind-badge { background: #2b2b3d; color: #a9a9d0; }
.kind-badge.hls { background: #123a2e; color: #3ddc97; }
.kind-badge.dash { background: #102f45; color: #4cc2ff; }
.kind-badge.video_page { background: #3a2410; color: #ffb45c; }
.sched-badge { background: #2d2440; color: #b79cff; }
```

（`--card` / `--border` / `--bg` / `--text` / `--text2` 都是 `index.html` 里 `:root` 已有的变量，直接复用。）

- [ ] **Step 4: 改 JS —— 提交扩展字段**

把 `addTask()`（`index.html:508-525`）整体替换，并在它前面插入两个工具函数：

```js
function escapeHtml(s) {
  return String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

// 后端 /api/add 的 reason -> 人话（流媒体失败原因大多来自嗅探/解析，用户需要知道下一步做什么）
const REASON_HINTS = {
  drm_protected: "该资源受 DRM 保护，SwiftDM 不支持下载",
  needs_ffmpeg: "需要系统安装 ffmpeg 才能混流：安装后重试，或在设置面板查看探测结果",
  cookies_required: "站点要求登录：请在浏览器里登录后，通过扩展弹窗重新发送任务",
  needs_ytdlp: "解析器未就绪：pip install yt-dlp 后重启 SwiftDM",
  parse_failed: "页面解析失败，可尝试换「类型」为 HLS/DASH 直链，或改用扩展嗅探",
  download_failed: "直链下载失败，可点击重试继续断点"
};

function addOptions() {
  const startAt = document.getElementById("startAtInput").value;
  let start_at = null;
  if (startAt) {
    const ms = new Date(startAt).getTime();
    if (!isNaN(ms) && ms > Date.now()) start_at = Math.floor(ms / 1000);
  }
  const resolution = parseInt(document.getElementById("resolutionSelect").value, 10) || 0;
  return {
    kind: document.getElementById("kindSelect").value,
    resolution: resolution,
    start_at: start_at
  };
}

function toggleAdvanced() {
  const box = document.getElementById("addAdv");
  box.classList.toggle("open");
  document.getElementById("advToggle").classList.toggle("active");
}

async function addTask() {
  const input = document.getElementById("urlInput");
  const url = input.value.trim();
  if (!url) { showToast("请输入下载链接", "error"); return; }

  const opts = addOptions();
  const res = await api("/api/add", {
    method: "POST",
    body: JSON.stringify({
      url, segments: 8,
      kind: opts.kind, resolution: opts.resolution, start_at: opts.start_at
    }),
  });

  if (res.success) {
    input.value = "";
    document.getElementById("startAtInput").value = "";
    showToast(opts.start_at ? "已加入定时下载" : "已添加下载任务");
  } else {
    showToast(res.error || "添加失败", "error");
  }
}
```

（`start_at` 为 `null` 时后端走立即开始分支；不合法或已过期的时间一律按 `null` 处理，绝不把 `NaN` 发出去。）

- [ ] **Step 5: 改 JS —— 任务卡显示类型 / 定时 / 失败原因**

`createTaskCard()`（`index.html:623-700`）里做四处替换，其余保持不动。

① `let metaHtml = "";` 之后插入类型芯片与定时徽章：

```js
  const kindLabels = { http: "直链", hls: "HLS", dash: "DASH", video_page: "网页解析", torrent: "BT/PT" };
  if (task.kind && kindLabels[task.kind]) {
    metaHtml += `<span class="kind-badge ${escapeHtml(task.kind)}">${kindLabels[task.kind]}</span>`;
  }
  if (task.scheduled_at) {
    const when = new Date(task.scheduled_at * 1000);
    metaHtml += `<span class="sched-badge">&#128337; ${escapeHtml(when.toLocaleString())} 开始</span>`;
  }
```

（顺序：这两段必须在 `if (task.total_size > 0) {` 之前插入，让芯片出现在体积信息左边。）

② `pending` 分支的文案改成能区分「排队」与「定时」：

```js
    case "pending": statusLabel = task.scheduled_at ? "&#128337; 定时等待" : "&#8987; 等待中"; statusClass = "pending"; break;
```

③ 卡片末尾的错误行改为带提示的转义版本（原来的 `${task.error ? ... }` 整行替换）：

```js
        ${task.error ? `<div style="font-size:11px;color:var(--red);margin-top:4px">${escapeHtml(task.error)}${
          task.error_reason && REASON_HINTS[task.error_reason]
            ? `<br><span style="color:var(--text2)">${REASON_HINTS[task.error_reason]}</span>` : ""
        }</div>` : ""}
```

④ 文件名行做转义（远端页面控制得了这个字符串）：

```js
        <div class="task-filename" title="${escapeHtml(task.filename)}">${escapeHtml(task.filename)}</div>
```

- [ ] **Step 6: 跑测试确认通过**

Run: `python -m pytest tests/test_web_ui.py -q && python -m pytest tests -q`
Expected: 6 passed；随后全量 PASS

- [ ] **Step 7: 浏览器实测（UI 改动必须真看过）**

1. 起服务：`SWIFTDM_PORT=5080 SWIFTDM_MONITOR_PORT=5081 python main.py --web-only`（后台）。
2. 打开 `http://127.0.0.1:5080/`，点「⌛」展开面板 → 选 `HLS 流`、清晰度 `720p`、定时 +2 分钟 → 粘贴一个真实 m3u8 → 开始下载。
3. 核对：卡片显示 `HLS` 芯片 + `定时等待` + `… 开始` 时间；到点后自动转为「下载中」；进度、速度、ETA 正常刷新。
4. 失败态：把类型选成 `网页视频解析` 并粘贴一个不存在解析器的链接（如 `https://example.com/nope`）→ 卡片应显示错误文字与 `REASON_HINTS` 建议（未装 yt-dlp 时应为「解析器未就绪」）。
5. 转义：`curl -s -X POST http://127.0.0.1:5080/api/add -H "Content-Type: application/json" -d '{"url":"http://127.0.0.1:5080/api/local-test-file","filename":"<img src=x onerror=alert(1)>.bin"}'` → 页面上文件名应以字面文本显示，且不弹窗（DevTools 里确认没有新增 `<img>` 节点）。
Expected: 5 项全通过；任何一项不符回到 Step 3-5 修正后重跑测试。

- [ ] **Step 8: 提交**

```bash
git add templates/index.html tests/test_web_ui.py
git commit -m "feat: 下载页支持类型/清晰度/定时与流媒体失败原因提示"
```

---

## Task 13: Web UI —— 设置面板（限速 / 完成后动作 / 依赖能力）

**Files:**
- Modify: `templates/index.html`（header 区、Toast 之前、`<style>` 末尾、`<script>` 末尾 `connectSSE()` 之前）
- Create: `tests/test_settings_panel.py`

**Interfaces:**
- Consumes: `GET/POST /api/settings` 的 `rate_limit / finish_action / finish_countdown / scheduled / capabilities`（Task 4、11）、`POST /api/finish_action/cancel`（Task 11）、`REASON_HINTS`/`escapeHtml`（Task 12）
- Produces: DOM 契约 —— `#settingsBtn` `#settingsModal` `#rateInput` `#rateUnlimited` `#applyRate` `#finishAction` `#finishStatus` `#cancelFinish` `#capFfmpeg` `#capYtdlp`；JS 函数 `openSettings() / closeSettings() / refreshSettings() / KB2B(v) / B2KB(v)`

- [ ] **Step 1: 写失败的测试**

Create `tests/test_settings_panel.py`:

```python
import re

import pytest

import app as appmod


@pytest.fixture(scope="module")
def page():
    appmod.app.config["TESTING"] = True
    return appmod.app.test_client().get("/").get_data(as_text=True)


def test_settings_button_and_modal_exist(page):
    for needle in ('id="settingsBtn"', 'id="settingsModal"', 'class="modal"'):
        assert needle in page, needle


def test_rate_control_converts_kb_to_bytes(page):
    assert 'id="rateInput"' in page and 'id="rateUnlimited"' in page
    assert re.search(r"function KB2B\(v\)[\s\S]{0,120}\*\s*1024", page)
    assert re.search(r'rate_limit:\s*KB2B\(', page)
    assert '"rate_limit": 0' in page or "rate_limit: 0" in page      # 勾选不限制时送 0


def test_finish_action_options_match_backend(page):
    block = page[page.index('id="finishAction"'):]
    block = block[:block.index("</select>")]
    values = re.findall(r'<option value="(\w+)"', block)
    assert values == ["none", "shutdown", "suspend", "beep"]


def test_dangerous_actions_ask_for_confirmation(page):
    handler = page[page.index("function onFinishActionChange"):]
    handler = handler[:handler.index("\n}")]
    assert "confirm(" in handler
    assert "/^(shutdown|suspend)$/" in handler


def test_cancel_button_hits_the_cancel_endpoint(page):
    assert 'api("/api/finish_action/cancel", { method: "POST" })' in page


def test_settings_are_refreshed_while_panel_is_open(page):
    opener = page[page.index("function openSettings"):]
    opener = opener[:opener.index("\n}")]
    assert "refreshSettings()" in opener and "setInterval(refreshSettings, 2000)" in opener
    closer = page[page.index("function closeSettings"):]
    closer = closer[:closer.index("\n}")]
    assert "clearInterval" in closer


def test_capabilities_are_rendered_from_the_api(page):
    assert "renderCapabilities(res.capabilities || {})" in page
    body = page[page.index("function renderCapabilities"):]
    body = body[:body.index("\n}")]
    assert "ffmpeg" in body and "ytdlp" in body
    assert "未检测到" in body            # 缺 ffmpeg / yt-dlp 必须明说，不要静默降级


def test_sse_finish_field_drives_a_one_shot_toast(page):
    assert "renderFinishFromStream(data.finish);" in page
    body = page[page.index("function renderFinishFromStream"):]
    body = body[:body.index("\n}")]
    assert "lastFinishRemaining === 0" in body        # 只在倒数刚开始时提示一次
    assert "showToast(" in body
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_settings_panel.py -q`
Expected: FAIL —— `assert 'id="settingsBtn"' in page`

- [ ] **Step 3: 改 HTML —— 入口按钮与模态框**

`index.html:428-434` 的 header 整段替换（右侧多一个齿轮）：

```html
  <!-- 头部 -->
  <div class="header">
    <div class="logo">&#11015;</div>
    <div class="header-text">
      <h1>SwiftDM</h1>
      <div class="sub">高速多线程下载管理器</div>
    </div>
    <button class="btn btn-secondary btn-icon settings-btn" id="settingsBtn"
            onclick="openSettings()" title="限速 / 完成后动作 / 依赖检测">&#9881;</button>
  </div>
```

（`.header` 已有 `display:flex` 时只需 `.settings-btn { margin-left: auto; }`；若原样式里没有 flex，就把这一条改成 `.settings-btn { position:absolute; right:18px; top:18px; }` 并给 `.header` 补 `position:relative`。以实际渲染效果为准，Step 7 必须看到按钮在右上角且不遮挡标题。）

在 `<div class="toast" id="toast"></div>` 之前插入模态框：

```html
<!-- 设置面板 -->
<div class="modal" id="settingsModal">
  <div class="modal-card">
    <div class="modal-head">
      <h2>下载设置</h2>
      <button class="btn btn-secondary btn-sm" onclick="closeSettings()">&#10006; 关闭</button>
    </div>

    <div class="modal-row">
      <div class="modal-label">全局限速</div>
      <div class="modal-controls">
        <input type="number" min="0" step="1" id="rateInput" style="width:110px"> <span class="unit">KB/s</span>
        <label class="inline"><input type="checkbox" id="rateUnlimited" checked> 不限制</label>
        <button class="btn btn-primary btn-sm" id="applyRate" onclick="applyRate()">应用</button>
      </div>
      <div class="modal-note">对多线程分段与流媒体下载同时生效，单位 KB/s。</div>
    </div>

    <div class="modal-row">
      <div class="modal-label">全部下载完成后</div>
      <div class="modal-controls">
        <select id="finishAction" onchange="onFinishActionChange(this.value)">
          <option value="none">无动作</option>
          <option value="shutdown">关机</option>
          <option value="suspend">睡眠</option>
          <option value="beep">提示音</option>
        </select>
        <span class="finish-status" id="finishStatus"></span>
        <button class="btn btn-danger btn-sm" id="cancelFinish" onclick="cancelFinishAction()" style="display:none">取消倒计时</button>
      </div>
      <div class="modal-note">仅在任务列表里再没有下载中 / 等待中 / 已暂停的任务后开始 60 秒倒计时；关闭程序不会自动取消，请用上面的按钮。</div>
    </div>

    <div class="modal-row">
      <div class="modal-label">依赖检测</div>
      <div class="modal-controls">
        <span class="chip" id="capFfmpeg">ffmpeg: …</span>
        <span class="chip" id="capYtdlp">yt-dlp: …</span>
      </div>
      <div class="modal-note">ffmpeg 只用系统安装的那个，SwiftDM 不会自动下载二进制文件。</div>
    </div>

    <div class="modal-row">
      <div class="modal-label">定时任务</div>
      <div class="modal-controls"><span id="scheduledList" class="sched-list">无</span></div>
    </div>
  </div>
</div>
```

`<style>` 末尾追加：

```css
.settings-btn { margin-left: auto; }
.modal {
  display: none; position: fixed; inset: 0; z-index: 60;
  background: rgba(0, 0, 0, .55); backdrop-filter: blur(2px);
}
.modal.open { display: flex; align-items: flex-start; justify-content: center; padding: 48px 16px; }
.modal-card {
  width: 100%; max-width: 560px; background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; padding: 16px 18px; box-shadow: 0 18px 48px rgba(0,0,0,.45);
}
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 12px; }
.modal-head h2 { font-size: 15px; margin: 0; }
.modal-row { padding: 10px 0; border-top: 1px solid var(--border); }
.modal-label { font-size: 12px; color: var(--text2); margin-bottom: 6px; }
.modal-controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.modal-controls input, .modal-controls select {
  background: var(--bg); color: var(--text); border: 1px solid var(--border);
  border-radius: 6px; padding: 5px 8px; font-size: 12px; font-family: inherit;
}
.modal-note { font-size: 11px; color: var(--text2); margin-top: 6px; line-height: 1.5; }
.inline { display: inline-flex; align-items: center; gap: 4px; font-size: 12px; }
.unit { font-size: 12px; color: var(--text2); }
.chip {
  padding: 2px 8px; border-radius: 10px; font-size: 11px;
  background: #2b2b3d; color: #a9a9d0;
}
.chip.ok { background: #123a2e; color: #3ddc97; }
.chip.bad { background: #3a1720; color: #ff7a90; }
.finish-status { font-size: 12px; color: #ffb45c; }
.sched-list { font-size: 12px; color: var(--text2); }
```

- [ ] **Step 4: 改 JS —— 读写设置**

在 `// ===== 渲染 =====` 一节之前（`connectSSE()` 定义之前的任意位置，保持与既有函数同级）插入：

```js
// ===== 设置面板 =====
let settingsTimer = null;

function KB2B(v) { return Math.max(0, Math.round((parseFloat(v) || 0) * 1024)); }
function B2KB(v) { return v > 0 ? Math.round(v / 1024) : 0; }

function openSettings() {
  document.getElementById("settingsModal").classList.add("open");
  refreshSettings();
  clearInterval(settingsTimer);
  settingsTimer = setInterval(refreshSettings, 2000);   // 倒计时要继续走，不必等 SSE
}

function closeSettings() {
  document.getElementById("settingsModal").classList.remove("open");
  clearInterval(settingsTimer);
  settingsTimer = null;
}

async function refreshSettings() {
  const res = await api("/api/settings");
  if (!res || res.error) return;
  const unlimited = !res.rate_limit;
  document.getElementById("rateUnlimited").checked = unlimited;
  document.getElementById("rateInput").disabled = unlimited;
  if (document.activeElement !== document.getElementById("rateInput")) {
    document.getElementById("rateInput").value = B2KB(res.rate_limit);
  }
  document.getElementById("finishAction").value = res.finish_action || "none";
  renderFinishStatus(res);
  renderCapabilities(res.capabilities || {});
  renderScheduled(res.scheduled || []);
}

async function applyRate() {
  const unlimited = document.getElementById("rateUnlimited").checked;
  const body = unlimited ? { rate_limit: 0 } : { rate_limit: KB2B(document.getElementById("rateInput").value) };
  const res = await api("/api/settings", { method: "POST", body: JSON.stringify(body) });
  if (res && res.success) showToast(unlimited ? "已取消限速" : `限速 ${B2KB(res.rate_limit)} KB/s`);
  else showToast((res && res.error) || "设置失败", "error");
  refreshSettings();
}

async function onFinishActionChange(value) {
  if (/^(shutdown|suspend)$/.test(value)) {
    const tip = value === "shutdown" ? "确定在全部下载完成后关机吗？" : "确定在全部下载完成后让电脑睡眠吗？";
    if (!confirm(tip)) { refreshSettings(); return; }      // 用户反悔 -> 回读后端真实值
  }
  const res = await api("/api/settings", { method: "POST", body: JSON.stringify({ finish_action: value }) });
  if (res && res.success) showToast("已更新完成后动作");
  refreshSettings();
}

async function cancelFinishAction() {
  const res = await api("/api/finish_action/cancel", { method: "POST" });
  if (res && res.success) showToast(res.cancelled ? "已取消倒计时" : "当前没有进行中的倒计时", "info");
  refreshSettings();
}

function renderFinishStatus(res) {
  const left = res.finish_countdown || 0;
  const btn = document.getElementById("cancelFinish");
  const box = document.getElementById("finishStatus");
  if (left > 0) {
    box.textContent = `${left} 秒后${res.finish_action === "suspend" ? "睡眠" : "关机"}`;
    btn.style.display = "";
  } else if (res.finish_action && res.finish_action !== "none") {
    const name = { shutdown: "关机", suspend: "睡眠", beep: "提示音" }[res.finish_action];
    box.textContent = `等待下载全部完成后${name}`;
    btn.style.display = "";
  } else {
    box.textContent = "";
    btn.style.display = "none";
  }
}

function renderCapabilities(caps) {
  const ff = document.getElementById("capFfmpeg");
  const ffmpeg = caps.ffmpeg || {};
  ff.textContent = ffmpeg.available ? "ffmpeg: 已就绪" : "ffmpeg: 未检测到";
  ff.className = "chip " + (ffmpeg.available ? "ok" : "bad");
  ff.title = ffmpeg.path || "未找到 ffmpeg：混流/转封装功能不可用";
  const yd = document.getElementById("capYtdlp");
  yd.textContent = caps.ytdlp ? "yt-dlp: 已就绪" : "yt-dlp: 未检测到";
  yd.className = "chip " + (caps.ytdlp ? "ok" : "bad");
  yd.title = caps.ytdlp ? "" : "网页视频解析不可用：pip install yt-dlp 后重启 SwiftDM";
}

function renderScheduled(list) {
  const box = document.getElementById("scheduledList");
  if (!list.length) { box.textContent = "无"; return; }
  box.innerHTML = list.map(s => {
    const when = new Date(s.start_at * 1000).toLocaleString();
    return `<div>${escapeHtml(when)} · ${escapeHtml(s.task_id)}</div>`;
  }).join("");
}
```

（`showToast(msg, type)` 已存在；本任务用到它的 `"info"` 分支 —— 若现有 CSS 里没有 `.toast.info` 配色，就在 `<style>` 的 toast 规则旁补 `.toast.info { border-left-color: var(--accent); }`，与 success/error 同构。）

接 Step 4 —— 接 SSE 里的 `finish` 字段。

`connectSSE()`（`index.html:710` 起）的 `evtSource.onmessage` 回调里，`renderStats(data.stats);` 之后追加：

```js
      renderFinishFromStream(data.finish);
```

并在设置面板那一段函数里补上（放在 `renderFinishStatus()` 之后）：

```js
// Web-only 没有系统托盘，倒计时靠 SSE 通知；只在「刚开始倒数」那一刻提示一次。
let lastFinishRemaining = 0;

function renderFinishFromStream(finish) {
  const f = finish || {};
  const remaining = f.remaining || 0;
  if (remaining > 0 && lastFinishRemaining === 0) {
    showToast(`${remaining} 秒后${f.action === "suspend" ? "进入睡眠" : "关机"}，打开设置面板可取消`, "info");
  }
  lastFinishRemaining = remaining;
  const modal = document.getElementById("settingsModal");
  if (modal && modal.classList.contains("open")) {
    renderFinishStatus({ finish_action: f.action, finish_countdown: remaining });
  }
}
```

（SSE 帧里的字段名是 `finish.action / finish.remaining`，`GET /api/settings` 里是 `finish_action / finish_countdown` —— 两处命名不同源，所以这里显式转换，不要让 `renderFinishStatus()` 去猜。）

- [ ] **Step 5: 跑测试确认通过 + 全量回归**

Run: `python -m pytest tests/test_settings_panel.py -q && python -m pytest tests -q && node tests/test_background_load.js && node tests/test_sniff.js`
Expected: 8 passed；全量 PASS；两个 Node 接线测试仍绿

- [ ] **Step 6: 手工验收（限速与倒计时都是可观察行为）**

1. `SWIFTDM_PORT=5080 SWIFTDM_MONITOR_PORT=5081 python main.py --web-only` → 打开 `http://127.0.0.1:5080/`。
2. 齿轮 → 面板应显示 `ffmpeg: 未检测到`（本机确实没装）与 `yt-dlp:` 的真实状态。
3. 限速：取消「不限制」→ 填 `64` → 应用；`curl -s http://127.0.0.1:5080/api/settings` 应回 `"rate_limit": 65536`。同时添加 `/api/self-test` 直链下载，观察速度稳定压在 64 KB/s 附近（允许 ±20%）。
4. 完成后动作：选「提示音」→ 跑一个短下载 → 完成后 `finishStatus` 应显示 60→0 逐秒递减，点「取消倒计时」应立刻停下；再选「关机」时浏览器必须弹 `confirm`，取消后下拉框要回读到 `无动作`。**不要真的让机器关机** —— 关机分支只由 `tests/test_scheduler.py` 的注入 runner 覆盖。
5. 关闭面板后 `curl -s http://127.0.0.1:5080/api/settings | findstr finish_countdown` 仍应看到倒计时继续（说明状态在后端，不在前端）。
Expected: 5 项全通过。

- [ ] **Step 7: 提交**

```bash
git add templates/index.html tests/test_settings_panel.py
git commit -m "feat: 下载页新增设置面板（限速/完成后动作/依赖检测）"
```

---

## Task 14: 合成 HLS 端到端集成测试

**Files:**
- Create: `tests/test_media_integration.py`

`tests/conftest.py` 在 Task 1 已经把仓库根挂进 `sys.path`，本任务所需的环境隔离（清代理、屏蔽 history 读写、复位限速）全部写在这个测试文件自己的 autouse fixture 里，不动公共 conftest。

**Interfaces:**
- Consumes: `POST /api/add`（Task 6）、`media.MediaTask` 的 yt-dlp 真实路径（Task 5）、`throttle.set_rate`（Task 4）、`media.ffmpeg_status`（Task 3）、`DownloadManager._reconstruct_task`（Task 6）、`app._stream_payload()`（Task 11）
- Produces: 无新接口 —— 纯验证任务，覆盖 spec §6 的「集成」（含「SSE 帧含进度」）与「兼容性」两行

- [ ] **Step 1: 写测试（本机 HTTP + 合成 HLS，不依赖外网）**

Create `tests/test_media_integration.py`:

```python
"""合成 HLS 端到端：本地起服务器 → /api/add → MediaTask 真跑 yt-dlp → 校验字节与限速。

需要已安装 yt-dlp（requirements.txt 已列）；缺依赖时整个模块 skip，而不是 fail，
以免没装解析器的机器上跑全量测试被误判成功。
"""
import hashlib
import http.server
import json
import os
import threading
import time

import pytest

pytest.importorskip("yt_dlp", reason="流媒体集成测试需要已安装 yt-dlp（pip install -r requirements.txt）")

import app as appmod                       # noqa: E402
import downloader                          # noqa: E402
import media                               # noqa: E402
import throttle                            # noqa: E402
from downloader import manager             # noqa: E402

SEG_COUNT = 8
SEG_SIZE = 32 * 1024                        # 每片 32KB，共 256KB：限速用例秒级可辨
TOTAL = SEG_COUNT * SEG_SIZE


def _segment_bytes(i):
    """伪 TS 分片：188 字节包对齐，内容可复现。"""
    packet = bytes([0x47]) + ("seg%02d" % i).encode("utf-8").ljust(187, b"\x00")
    return (packet * (SEG_SIZE // 188 + 1))[:SEG_SIZE]


PLAYLIST = (
    "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n"
    + "".join("#EXTINF:1.0,\nseg%02d.ts\n" % i for i in range(SEG_COUNT))
    + "#EXT-X-ENDLIST\n"
)
EXPECTED_SHA = hashlib.sha256(b"".join(_segment_bytes(i) for i in range(SEG_COUNT))).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _payload(self):
        path = self.path.split("?")[0]
        if path.endswith(".m3u8"):
            return 200, PLAYLIST.encode("utf-8"), "application/vnd.apple.mpegurl"
        name = path.rsplit("/", 1)[-1]
        if name.startswith("seg") and name.endswith(".ts"):
            return 200, _segment_bytes(int(name[3:5])), "video/mp2t"
        return 404, b"", "text/plain"

    def _respond(self, with_body):
        status, data, ctype = self._payload()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        if with_body and data:
            self.wfile.write(data)

    def do_GET(self):
        self._respond(True)

    def do_HEAD(self):
        self._respond(False)


@pytest.fixture(scope="module")
def hls_base():
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:%d" % httpd.server_address[1]
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """本机回环不能被系统代理劫走（这台机器上有 Clash），并且不能污染真实历史文件。"""
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setattr(downloader.DownloadManager, "_load_history", lambda self: None)
    monkeypatch.setattr(downloader.DownloadManager, "_start_saver", lambda self: None)
    monkeypatch.setattr(downloader.DownloadManager, "save_history", lambda self: None)
    media.set_ytdlp_module(None)
    throttle.set_rate(0)
    yield
    throttle.set_rate(0)


def _download(base, save_dir, segments=8, **extra):
    client = appmod.app.test_client()
    os.makedirs(save_dir, exist_ok=True)
    payload = {"url": base + "/index.m3u8", "kind": "hls",
               "save_dir": str(save_dir), "segments": segments}
    payload.update(extra)
    resp = client.post("/api/add", json=payload)
    assert resp.status_code == 200, resp.get_json()
    body = resp.get_json()
    task = manager.get_task(body["task"]["task_id"])
    started = time.time()
    deadline = started + 90
    while time.time() < deadline and task.status not in ("completed", "failed", "cancelled"):
        time.sleep(0.25)
    result = {"status": task.status, "elapsed": time.time() - started,
              "filepath": task.filepath, "error": task.error,
              "downloaded": task.downloaded}
    manager.remove_task(task.task_id)
    return result


needs_native_hls = pytest.mark.skipif(
    media.ffmpeg_status()["available"],
    reason="系统里有 ffmpeg 时 yt-dlp 会把 HLS 转封装成 mp4，字节序列不再等于分片拼接")


def test_local_hls_downloads_end_to_end(hls_base, tmp_path):
    got = _download(hls_base, tmp_path / "a")
    assert got["status"] == "completed", got["error"]
    assert os.path.isfile(got["filepath"])
    assert got["downloaded"] == TOTAL, "进度统计应等于所有分片之和"


@needs_native_hls
def test_concatenated_segments_match_source_bytes(hls_base, tmp_path):
    got = _download(hls_base, tmp_path / "b")
    assert got["status"] == "completed", got["error"]
    with open(got["filepath"], "rb") as f:
        assert hashlib.sha256(f.read()).hexdigest() == EXPECTED_SHA, "分片顺序被搞乱了"


def test_global_rate_limit_applies_to_stream_download(hls_base, tmp_path):
    fast = _download(hls_base, tmp_path / "fast")
    assert fast["status"] == "completed", fast["error"]
    throttle.set_rate(128 * 1024)
    # segments=1 是必需的：yt-dlp 的 ratelimit 按「每条连接」限速，
    # 8 路并发分片会把总速率抬到 8 倍，那样时间断言毫无意义。
    slow = _download(hls_base, tmp_path / "slow", segments=1)     # 256KB / 128KBps ⇒ ≥2 秒
    assert slow["status"] == "completed", slow["error"]
    assert slow["elapsed"] >= 1.5, (fast, slow)
    assert slow["elapsed"] > fast["elapsed"]


def test_stream_payload_reports_media_progress(hls_base, tmp_path):
    """进度要沿 `/api/stream` 那一帧流出去 —— 前端复用同一条流，不新增轮询（spec §2、§6）。"""
    client = appmod.app.test_client()
    throttle.set_rate(64 * 1024)                                  # 256KB 单连接 ⇒ 约 4 秒采样窗口
    resp = client.post("/api/add", json={"url": hls_base + "/index.m3u8", "kind": "hls",
                                        "save_dir": str(tmp_path / "sse"), "segments": 1})
    assert resp.status_code == 200, resp.get_json()
    tid = resp.get_json()["task"]["task_id"]
    frames = []
    deadline = time.time() + 90
    try:
        while time.time() < deadline:
            mine = [t for t in appmod._stream_payload()["tasks"] if t["task_id"] == tid]
            assert mine, "SSE 帧里丢了刚添加的媒体任务"
            frames.append(mine[0])
            if mine[0]["status"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.1)
    finally:
        manager.remove_task(tid)
        throttle.set_rate(0)
    assert frames[-1]["status"] == "completed", frames[-1]["error"]
    assert any(0 < f["progress"] < 100 and f["downloaded"] > 0 for f in frames), frames
    assert frames[-1]["progress"] == 100.0 and frames[-1]["downloaded"] == TOTAL


def test_legacy_history_without_kind_still_loads_and_deletes(tmp_path, monkeypatch):
    """老版本没有 kind 字段，连 m3u8 也是按普通文件下的 —— 必须能加载并删除。"""
    legacy = {"version": 1, "saved_at": time.time(), "tasks": [
        {"task_id": "dl_1", "url": "https://c/a.zip", "filename": "a.zip",
         "status": "completed", "filepath": str(tmp_path / "a.zip"),
         "total_size": 10, "downloaded": 10, "progress": 100.0},
        {"task_id": "dl_2", "url": "https://c/v/index.m3u8", "filename": "index.m3u8",
         "status": "completed", "save_dir": str(tmp_path),
         "total_size": 20, "downloaded": 20, "progress": 100.0},
    ]}
    path = tmp_path / "history.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")

    monkeypatch.setattr(downloader.DownloadManager, "_start_saver", lambda self: None)
    monkeypatch.setattr(downloader.DownloadManager, "_load_history", lambda self: None)
    m = downloader.DownloadManager()
    m._history_path = str(path)
    m._load_history()                       # 直接调实例方法，绕开 fixture 里的类级替换
    assert {t.task_id for t in m.get_all_tasks()} == {"dl_1", "dl_2"}
    m.remove_task("dl_1")
    assert {t.task_id for t in m.get_all_tasks()} == {"dl_2"}
```

- [ ] **Step 2: 跑测试**

Run: `python -m pip install "yt-dlp>=2025.1.15"` 然后 `python -m pytest tests/test_media_integration.py -q -s`
Expected: 5 个用例，`skipped` 只允许出现在 `test_concatenated_segments_match_source_bytes`（本机装了 ffmpeg 时）；其余必须 PASS

- [ ] **Step 3: 若字节校验或进度统计不符，先定因再改实现**

这一步是集成测试的价值所在，不许放宽断言让它变绿。按顺序排查：

1. `downloaded != TOTAL` → `_on_progress()` 里 `downloaded` 的取值口径（yt-dlp 的 `downloaded_bytes` 是「本次累计」还是「当前分片内偏移」），修 `media.py` 的进度换算。**修的时候同时回头补 `tests/test_media_task.py`**：给假模块的 `captured["events"]` 增加一条带 `frag_index/total_frags` 的分片形事件，断言换算后的 `downloaded` 仍是全部分片之和 —— 否则离线单测锁不住这个口径，下次 yt-dlp 改报告方式又会退回去。
2. sha256 不等 → yt-dlp 走了 ffmpeg 转封装分支（`ffmpeg_status()["available"]` 应为 False 却走了 ffmpeg，说明 `hls` 分支的 opts 不对），检查 `_build_opts()` 是否误设了 `merge_output_format` / `remux-video`。
3. 限速用例不慢 → `throttle.get_rate()` 没传成 yt-dlp 的 `ratelimit`，或单位换算错（yt-dlp 用 字节/秒）；再确认用例传了 `segments: 1` —— yt-dlp 是按连接限速的，`concurrent_fragment_downloads=8` 会把总吞吐放大到约 8 倍。
4. 请求根本没到本地服务器（`_Handler.log_message` 临时打开可看到）→ 代理环境变量没清干净，检查 `_clean_env`。
   每次修完都要重跑 Step 2，并把结论写进本文件的注释里。

- [ ] **Step 4: 全量回归**

Run: `python -m pytest tests -q`
Expected: 全部 PASS（`test_media_integration.py` 里最多 1 skip）

- [ ] **Step 5: 提交**

```bash
git add tests/test_media_integration.py media.py
git commit -m "test: 增加合成 HLS 端到端与历史记录兼容性测试"
```

---

## Task 15: 打包、全量验收与交接文档

**Files:**
- Modify: `build_exe.py`（`opts` 列表内补 hidden-import）
- Modify: `HANDOFF.md`
- Create: `docs/superpowers/plans/2026-09-23-media-sniffing-acceptance.md`（验收记录，逐条写结论）

**Interfaces:**
- Consumes: 前 14 个任务的全部产物
- Produces: `dist/SwiftDM.exe`（或对应平台产物）+ 验收记录 + 更新后的 HANDOFF

- [ ] **Step 1: 打包配置补全**

`build_exe.py` 的 `opts` 里，`"--hidden-import", "torrent",` 之后补：

```python
        # 调度器/媒体任务在函数内按平台导入，PyInstaller 静态分析看不到
        "--hidden-import", "winsound",
        "--hidden-import", "yt_dlp.utils",
        "--hidden-import", "mutagen",
```

（`mutagen` 是 yt-dlp 的可选依赖，缺了它打包不报错但运行时读取媒体信息会退化；`--collect-all yt_dlp` 已在 Task 3 加过。）

- [ ] **Step 2: 全量自动化验证（逐条贴输出）**

```bash
python -m pytest tests -q
node tests/test_sniff.js
node tests/test_background_load.js
node --check extension/sniff.js
node --check extension/background.js
node --check extension/content.js
node --check extension/popup.js
python -c "import json;json.load(open('extension/manifest.json',encoding='utf-8'));print('manifest ok')"
python -c "import app, media, media_service, scheduler, throttle;print('imports ok')"
python test_norange.py
```
Expected: pytest 全绿（最多 1 skip）；两个 Node 测试各自打印 OK；`--check` 无输出；`manifest ok`；`imports ok`；`test_norange.py` 报通过。任何一条不达标都必须停下修，不许进入打包。

- [ ] **Step 3: 打包**

Run: `python build_exe.py`
Expected: 结尾打印产物路径与大小，`dist/SwiftDM.exe` 存在。构建前 `build_exe.py` 会 `taskkill /F /IM SwiftDM.exe`，如果预览进程正占着端口会被杀掉 —— 这是预期行为。

- [ ] **Step 4: 二进制端到端**

Run: `python test_binary.py`
Expected: 其内置断言全过（分段下载 / sha256 完整性 / 暂停-继续-取消 / 浏览器捕获端点）。
再手动确认打包版的能力探测：

```bash
start "" dist\SwiftDM.exe --web-only
curl -s http://127.0.0.1:5000/api/settings
```
Expected: JSON 里 `"capabilities":{"ffmpeg":{...},"ytdlp":true}` —— `ytdlp` 必须为 `true`，否则说明 `--collect-all yt_dlp` 没生效，回到 Step 1。看完用 `taskkill //IM SwiftDM.exe //F` 收工。

- [ ] **Step 5: 手工验收清单（写进验收记录文件）**

`docs/superpowers/plans/2026-09-23-media-sniffing-acceptance.md` 逐条记录「命令 / 期望 / 实际 / 结论」，至少覆盖：

1. 扩展嗅探：真实 HLS 页 → popup 媒体列表出现 `hls` 条目 → 下载完成。
2. 登录站点：Cookie 透传后能下载需要 referer/cookie 的清单（只记录站点与结论，**不记录 Cookie 内容**）。
3. MSE 页面：blob 行不给下载按钮，「解析本页」能建任务。
4. DRM：受保护内容被拒绝，原因是 `drm_protected`，无绕过尝试。
5. 网页解析：一个 yt-dlp 支持的公开视频页，选清晰度 720 能下载。
6. 限速：64 KB/s 时速度稳定在容差内，改回 0 立刻恢复。
7. 定时 + 完成后动作：定时到点自启；「提示音」在全部完成后 60 秒触发；期间新增任务能撤销倒计时。**不要真的执行关机** —— 该分支由 `tests/test_scheduler.py` 的注入 runner 覆盖。
8. 兼容性：删除历史里的老任务记录（含无 `kind` 的 m3u8 记录）不报错。
9. 隐私核对：`~/.swiftdm/ck_*.txt` 在任务结束后不存在；`swiftdm.log` 里搜不到 Cookie 值。
10. 无 ffmpeg 降级（本机 PATH 里就没有 ffmpeg，直接验）：纯 TS 的 HLS 直链能下载完成（yt-dlp 内置合并，不该报 `needs_ffmpeg`）；音视频分离的网页解析返回 409 `needs_ffmpeg`，卡片与设置面板都给出安装指引。

- [ ] **Step 6: 更新 HANDOFF.md**

在 HANDOFF.md 增补一节「流媒体嗅探与下载（2026-09-23）」，写清：

- 新模块职责一句话：`media_service.py`（嗅探缓存）/ `media.py`（MediaTask + 依赖探测）/ `throttle.py`（令牌桶）/ `scheduler.py`（定时与完成后动作）/ `extension/sniff.js`（共享嗅探规则）。
- 运行依赖：`yt-dlp` 必装（`pip install -r requirements.txt`）；`ffmpeg` 选装且只探测系统版本，SwiftDM 不会自动下载二进制；`SWIFTDM_FFMPEG` 可指定路径。
- 测试命令：`python -m pytest tests -q` + `node tests/test_sniff.js` + `node tests/test_background_load.js`。
- 已知限制：不做 DRM 绕过；MSE/blob 只能退化为解析页面地址；定时与 `finish_action`、`rate_limit` 都不落盘（重启即失效）；嗅探只在扩展启用且页面有网络请求时才有结果；流媒体限速是 yt-dlp 的**每条连接**限速，实际总速率约为 `限速 × segments`（`DownloadTask` 的全局令牌桶没有这个放大）。
- 端口事实：Flask 5000 被占时顺延 5002-5005，扩展按 `SWIFTDM_BASES` 依次探测并把首个可用端口存进 `chrome.storage.local.swiftBase`。

- [ ] **Step 7: 提交**

```bash
git add build_exe.py HANDOFF.md docs/superpowers/plans/2026-09-23-media-sniffing-acceptance.md
git commit -m "docs: 补充流媒体嗅探打包配置与验收记录"
```

---

## 与 spec 的差异说明

实现时按本计划为准，以下是有意偏离 spec 的地方（都已在评审中确认过取舍）：

1. **里程碑 4 的「i18n」不做。** `templates/index.html` 实际没有任何 i18n 层（全部中文硬编码），要为四个新控件引入字典属于无必要的抽象。文案统一中文，且经由 `escapeHtml()` 输出。
2. **spec §3 的「展开选清晰度」简化为「每个清单一条 + 解析时按分辨率下拉」。** 后端 registry 记录的是嗅到的实际清单地址，多码率 master 清单的变体展开属于后续任务。
3. **`hls_segments`（靠分片流量推断的摘要）明确为只读展示项**，扩展侧与 popup 都不给下载按钮（点也无意义：手上只有一个 `.ts`）。主清单确实嗅不到时，走「解析本页」交 yt-dlp。
4. **完成后动作用自己的 60 秒倒计时**（可取消），OS 命令只带 5 秒尾时（Windows `shutdown /s /t 5`），避免两段倒计时叠加让人误判。
5. **`MediaTask` 落在 `media.py`，`media_service.py` 只放 `MediaRegistry`**（spec §2 组件表把两者都写进 `media_service.py`）。理由：`media_service.py` 只用标准库，没装 yt-dlp 的机器上也能导入并单测；把依赖 yt-dlp/ffmpeg 的代码隔离到 `media.py`，`app.py` 才能对导入失败做降级。
6. **`/api/settings` 字段名用 `rate_limit` 与 `capabilities:{ffmpeg,ytdlp}`**（spec 写 `limit_rate`、`ffmpeg_available`）。理由：现有响应已经是 `download_dir / default_segments / proxy_mode` 这组扁平 snake_case 名，只读的依赖探测归到 `capabilities` 一个对象里，避免每加一种探测能力就多一个顶层键。
7. **调度扫描间隔 5 秒**（spec 写 30 秒）。理由：30 秒意味着「定时 20:00 开始」最坏延后到 20:00:30，用户一定会当成 bug；一次扫描只是内存里遍历 `_tasks`，5 秒的代价可以忽略。
8. **Web UI 不做「媒体嗅探」汇总页签**（spec §2 Web UI 行）。理由：媒体列表的入口在扩展 popup（设计阶段已确认），后端 `MediaRegistry` 是 60 秒 TTL 的易失缓存，Web 页签只在扩展刚上报过的一瞬间有内容；Web UI 用「类型 = HLS/DASH/网页视频解析 + 直链粘贴」覆盖同一诉求，Task 12 已实现。
9. **完成后动作取值固定为 `none / shutdown / suspend / beep`**（spec §1 那句还列了「静音」）。理由：关机/睡眠前把系统静音对下载任务没有任何作用，而 `beep`（`winsound`）加上 Web-only 下的 SSE 提示已经覆盖了「通知」这一项。
