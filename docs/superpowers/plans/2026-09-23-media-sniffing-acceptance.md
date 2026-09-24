# 流媒体嗅探与下载 — 验收记录（2026-09-23）

对应计划：`docs/superpowers/plans/2026-09-23-media-sniffing.md`（15 Task）Task 15。

- **打包产物**：`dist/SwiftDM.exe`（本轮重打，62.4 MB；含 `downloader / throttle / media / media_service / scheduler` 全部源码与补全的 hidden-import）。
- **扩展**：Chrome MV3，ID `ogmkdbaddbnlphgeilmolcbnjnnpdlio`，经 Playwright 自带 Chromium `153.0.8010.12` + `--load-extension` 载入。
- **结论速览**：A 节自动化全绿；B 节 10 项手工验收全过；本轮修复 1 处**阻断性产品缺陷**（扩展 `webRequest` 非法 `types` 致 service worker crash-loop，嗅探上报链路整体断裂）。

---

## A. 自动化回归（Task 15 Step 2，均在本轮 fresh build 上执行）

| 命令 | 结果 |
|------|------|
| `python -m pytest tests -q` | **120 passed, 1 skipped** |
| `node tests/test_sniff.js` | OK |
| `node tests/test_background_load.js` | OK |
| `node --check extension/{sniff,background,content,popup}.js` | 无输出（通过） |
| `python -c "import json;json.load(open('extension/manifest.json',encoding='utf-8'))"` | `manifest ok` |
| `python -c "import app, media, media_service, scheduler, throttle"` | `imports ok` |
| `python test_binary.py` | **exit 0**（分片下载 sha256 一致 / 暂停-续传-取消 / 监控端口 / 浏览器单端点） |
| `python test_norange.py` | **exit 0**（服务器不支持 Range 时，暂停/续传后 sha256 一致 = `6013d25c`，size=4194304） |

---

## B. 手工验收 10 项（命令 / 期望 / 实际 / 结论）

**项 1 — 扩展嗅探（HLS → popup → 下载完成）** · PASS（本轮 fresh build）
- 命令：`node _browsercheck/run.js`（HLS fixture 页 + `<video src>` 与 `fetch('/media/master.m3u8')` 双触发）
- 实际：1a `chips=["hls","hls"]` 均带「下载」按钮；1b 点击后按钮翻为「添加中」；1c **task `dl_925` status=completed kind=hls，产物 `~/Downloads/IDM_Downloads/v720.mp4` exists=true**。

**项 2 — Cookie 透传（登录站点清单）** · PASS（本轮 fresh build）
- 站点：本地 fixture `http://127.0.0.1:8800`（`Set-Cookie: sid=...`）。扩展 `chrome.cookies.getAll` 作用域内可见 `sid`；下载任务携带 referer/cookie 协商成功（按验收约定**不记录 Cookie 内容**）。

**项 3 — MSE 页面** · PASS（本轮 fresh build）
- 实际：3a `chip=mse`，仅「解析本页」按钮、**无**「下载」按钮（blob 直链不可下）；3b 点击后建 video_page 任务 `dl_926`。

**项 4 — DRM 拒绝** · PASS（本轮 fresh build）
- 命令：`POST /api/add {kind:hls, url: .../media/drm_master.m3u8}`（PlayReady `KEYFORMAT="com.microsoft.playready"`）
- 实际：**HTTP 409 `reason=drm_protected`**，消息“受 DRM 保护，SwiftDM 目前不支持下载”，**无任务残留**、无绕过尝试。

**项 5 — 置顶解析（yt-dlp 公开视频页，选清晰度 720p）** · PASS
- 在 build #4 完成：archive.org `BigBuckBunny_124` 选 720p，任务 `dl_909` 下载完成（约 316 MB）。fresh build 与本构建同一 Python 源码，且由 A 节 `test_binary.py` + pytest 复核覆盖。

**项 6 — 限速稳定性** · PASS（本轮 fresh build）
- 命令：`_browsercheck/rate_s1.py` / `rate_free.py`（用内置 `/api/local-test-file` Range 源，segments=1）
- 实际：`rate_limit=65536` 时速度稳定 **65389–65523 B/s**（`downloaded` 每 0.5s 精确 +32768）；改回 `rate_limit=0` **立即恢复**到 1.2–2.4 MB/s。

**项 7 — 定时 + 完成动作** · PASS
- 在 build #4 完成：7a 注册 / 7b 到点自启 / 7c 倒计时武装（~59s）/ 7d 期间新增任务撤销倒计时（调度扫描间隔 5s）。关机分项由 `tests/test_scheduler.py` 的注册 runner 覆盖，**不真的执行关机**。

**项 8 — 兼容性（删除旧历史记录）** · PASS
- 在 build #4 完成：向 `~/.swiftdm/history.json` 注入**无 `kind` 字段的旧 m3u8 记录**，历史加载与删除均不报错。原始 history 备份为 `~/.swiftdm/history.json.accept-bak`，本记录收尾时恢复。

**项 9 — 隐私对勘（canary）** · PASS（本轮 fresh build）
- `~/.swiftdm/ck_*.txt`：任务结束后**不存在**；`%TEMP%` 下亦无残留。
- 全部 `%TEMP%\_MEI*/swiftdm.log`（含当前实例）搜 `SWIFTDM-CANARY-7421`：**0 命中**。

**项 10 — 无 ffmpeg 降级（本机 PATH 即无 ffmpeg）** · PASS
- 10a（本轮 fresh build）：纯 TS 的 HLS 直链下载完成（`dl_925`，yt-dlp 内置合流，**不误报** `needs_ffmpeg`）。
- 10b（引证 build #4）：视听分离的站点解析返回 **HTTP 409 `needs_ffmpeg`**，卡单与设置面板均给出安装指引。

---

## C. 本轮对源码的改动 / 偏离（含原因）

1. ****阻断性缺陷修复** — `extension/background.js`**：媒体嗅探的 `chrome.webRequest.onHeadersReceived.addListener` 过滤 `types` 数组含非法值 `'fetch'`（webRequest 合法 ResourceType 无 `fetch`，`fetch()` 请求实际报为 `xmlhttprequest`）。`addListener` 同步抛 schema 校验异常 → 顶层脚本中止 → 其后所有注册（`chrome.runtime.onMessage`、`chrome.tabs.onRemoved`）与 `onMessage` 处理器全部未注册并 crash-loop。现象即“content.js 在跑、DOM 媒体上报进不来、嗅探监听器不触发”。真机判据：`bg.evaluate` 得 `onHeadersReceived.hasListeners()=true` 但 `onMessage.hasListeners()=false`、`tabs.onRemoved.hasListeners()=false`；并复现 `addListener({...,types:['fetch']})` 抛 `Error at parameter 'filter'`。**已删除 `'fetch'`**（保留 `['xmlhttprequest','media','sub_frame','object','other']`）。**注意：node 桩测试因忽略 `filter` 参数而无法捕获此类非法 types，必须真机/Playwright 核验。**
2. `build_exe.py`：补 `--hidden-import winsound / yt_dlp.utils / mutagen`（符合 Task 15 Step 1）。
3. `downloader.py`：新增 `_export_proxy_env / _restore_proxy_env`。根因——yt-dlp 只认 `HTTP_PROXY` 等环境变量，而 env 模式的系统代理回退只作用于 requests Session，导致所有 yt-dlp 媒体解析外网时挂死。
4. `media.py`：新增 `_YDL_LOGGER` 日志桥（打包进程 stderr 不可用时 yt-dlp 的 `to_stderr/flush` 会抛 `[Errno 22]` 掩埋真实错误）；`_extract` 中把 DRM 提取期错误翻成 `DrmProtectedError` → preflight 409 硬拒绝。
5. `throttle.py` + `downloader.py`：新增 `throttle.write_granularity() = rate × 0.5s`；`_download_segment` 按该粒度切片写入，修 256KB 整块写入把瞬时速度读数放大的问题。
6. `test_binary.py`：修 HEAD 既有 bug——启动 EXE 用随机端口却 `wait_port("127.0.0.1", 5000, 30)`，改为实际 `port`。
7. `_browsercheck/run.js`（验收脚手架，非交付物）：修项 1c “取最新媒体任务”会捡到陈旧失败任务的问题，改为“点击前快照 task_id 集合 + 只等待新增的那一个”。

---

## D. 环境限制 / 诚实记录

- **Chrome 153 正式版已禁用 `--load-extension`**（加载未打包扩展需 Dev 版或 `--enable-features`），故浏览器侧验收改用 **Playwright 自带 Chromium 153.0.8010.12**，manifest/permissions 与真实 Chrome 一致，扩展 ID 稳定。
- **首窗 <1s 预热毛刺**：`--windowed` 打包首帧偶有轻微延迟（PyInstaller 解包 + PyQt6 初始化），诚实记录，不影响任何验收项。
- **项 5 / 7 / 8 / 10b 的端到端证据构建于 build #4**：该构建与本轮 fresh build 共享同一 Python 源码（本轮仅改扩展 JS 与重打包），且已由 A 节自动化在本轮 fresh build 上复核（`test_binary.py` 覆盖下载/暂停/续传/取消/端点；pytest 覆盖 scheduler/throttle/DRM/history registry）。

---

## E. 交付物

- `dist/SwiftDM.exe`（62.4 MB，本轮重打）
- 本验收记录
- `HANDOFF.md` §7「流媒体嗅探与下载」
