# SwiftDM 交接文档（Handoff）

> 本文档供接手 SwiftDM 项目的下一个 agent 阅读。最后更新：2026-09-26。
> 项目根目录：`D:\ai share\repo\SwiftDM\`（唯一主副本）
> （git 仓库，远程 `cpufreestyle/SwiftDM`，GitHub）
> 交付物/构建产物位于 `dist/SwiftDM.exe`。

---

## 1. 项目是什么

IDM 风格的多线程下载管理器：

- **桌面 UI**：PyQt6（深色 / 浅色双主题、IDM 风格任务卡片）
- **Web UI**：Flask（端口默认 5000，备用访问方式）
- **浏览器扩展**：Chrome MV3（`extension/` 目录，需用户手动加载）
- **BT / PT 下载**：基于 libtorrent（磁力链接 + `.torrent` 种子，含私有 Tracker / PT 合规做种）
- 多线程分段下载、断点续传、完整性校验、剪贴板监听、系统托盘。

当前版本：`0.1.0`（见 `version_info.txt`）。

---

## 2. ✅ 当前状态：改动已提交，重复副本已归档

- 上表的未提交改动已于 commit `869b289` 提交（提交时顺带修复了 `background.js` 一处 HEAD 里就存在的非法语法——可选链作赋值左值，曾导致 MV3 service worker 整体加载失败，浏览器接管功能实际不可用；修复后需重新加载扩展验证）。
- 曾存在同仓库的旧工作副本 `D:\ai sheare\repo\download_manager\download_manager\`（HEAD 落后 7 个提交，其未提交内容经逐项函数比对为本仓库的严格子集），已改名归档为 `download_manager_old_backup`，确认无误后可删除。
- 注意：**未经用户明确要求不要主动 commit / push / 发布**——但用户已对动作确认并说「继续」即视为授权。

---

## 3. 环境特性与反复踩的坑（必读）

### 3.1 本机没有直连外网出口
- 所有外网下载必须走 **Windows 系统代理 `127.0.0.1:7897`**（Clash）。
- 已修复 `downloader.py`：env 模式在环境变量缺失时自动读取 Windows 系统代理（winreg 读 HKCU/HKLM Internet Settings）。**这个修复必须 `python build_exe.py` 重建 EXE 才进入交付物**。
- 本地请求（`127.0.0.1`/`localhost`）需设 `NO_PROXY=127.0.0.1,localhost`，否则会被送进代理导致 502。

### 3.2 端口冲突会"探错进程"，表现为路由 404（假象）
- Web 端口默认 5000、监控端口默认 5001。
- 若 5000 被**无关进程**占用（本机曾有另一个 python.exe 占 5000），`SO_REUSEADDR` 让 SwiftDM 仍能绑定 `0.0.0.0:5000`，但 `127.0.0.1:5000` 的 GET 会被 OS 路由到那个无关进程 → `/api/tasks` 等全 404。**这是假象，EXE 本身正常**。
- 已修复 `main.py` 的 `_port_bindable()`：现在先用真实 TCP 连接探测，能连上即视为占用，自动跳到下一端口（实测曾自动改用 5002）。
- 排查：`Get-NetTCPConnection -LocalPort <端口>` 看 OwningProcess → `Get-Process -Id` 确认路径。
- 干净启动可强制端口：`SWIFTDM_PORT=5100 SWIFTDM_MONITOR_PORT=5101 dist\SwiftDM.exe --web-only`。

### 3.3 旧路径残留进程
- 项目曾从 `D:\Michael\...` 迁到 `C:\Users\michael\...`。旧路径的 SwiftDM 残留进程可能仍在跑（占 5001 等），用 `Get-Process -Name SwiftDM` 清理。

### 3.4 二进制会过期
- `dist/` 里的 EXE 改完源码必须 `python build_exe.py` 重建后再测；`test_norange.py` 报 sha256 不一致时先怀疑 stale binary 而非源码。

### 3.5 Shell 是 PowerShell
- 不支持 heredoc（`<<EOF` 报错）；多行 git 提交信息需写临时文件后 `git commit -F <file>`。
- 读文件/命令未指定 encoding 可能被拦截，注意 `-Encoding UTF8`。

---

## 4. 常用命令

```bash
# 构建 Windows 单文件 EXE（约 44 MB）
python build_exe.py

# 发布到 GitHub Release（token 读 ~/.git-credentials；幂等，复用/创建 tag v0.1.0）
python release.py
python release.py --body-only        # 只更新 Release 文案、不重传二进制

# 关键回归测试：暂停→继续后文件 sha256 与源一致
python test_norange.py
# 失败路径冒烟
python _smoketest.py

# 本地干净验证（指定端口避免冲突）
SWIFTDM_PORT=5100 SWIFTDM_MONITOR_PORT=5101 dist\SwiftDM.exe --web-only
```

---

## 5. 最近一轮已完成的工作（2026-09-26，已推送）

主题：桌面浅色主题落地（QSS 全面 token 化）。每项改动均跑过
全量 pytest + `python build_exe.py` + `python test_binary.py`（全绿）后提交。

1. **桌面深/浅双主题**：`main_window.py` 新增 `THEMES = {"dark": {...}, "light": {...}}`
   （键集完全一致，色值与 Web 端 `:root[data-theme=...]` token 对齐）；全局 QSS 改为
   `string.Template` 模板 + `_qss_for(theme)` 渲染，`MainWindow._apply_theme()` 切换时
   重刷全局样式表、任务卡片与日志面板。
2. **任务卡片主题化**：`TaskCard.apply_theme(theme)` 把外壳、状态徽标、进度条、失败原因条、
   操作按钮按 token 整体重刷；操作按钮改走语义色键（orange/red/green/blue/accent2），
   状态语义色由 `_status_colors(tokens)` 统一派生（与 Web 端同色）。
3. **设置 → 外观 → 界面主题**：深色/浅色下拉，改动即时预览；保存后写 `config.py` 新键
   `theme`，重启后仍是该主题；新建下载对话框跟随当前主题。
4. **监控状态标签改用动态属性**（`on="true"/"false"`）驱动颜色，切主题自动跟随；
   头部信息栏与空状态提示改走 objectName + 全局样式表。
5. **修掉一个只在浅色主题才暴露的渲染问题**：原全局 `QWidget{background-color}` 会给每个
   标签/按钮盒单独铺底色，浅色下变成灰块；现在页面底色只铺 `QWidget#appCentral`，
   通用 `QWidget` 规则只留 color/font（`tests/test_desktop_ui.py` 有断言守着防回归）。
6. **文档同步**：README 主题条目改为「桌面端支持深色 / 浅色」。

剩余候选：扩展 popup 显示失败任务/重试（需用户重载 Chrome 扩展验证）。

---

## 6. 关键文件速查

| 文件 | 职责 |
|------|------|
| `main.py` | 入口：启动监控 + UI；端口选择（`_port_bindable`/`_pick_port`）、防 502 代理绕过、`open_browser` |
| `main_window.py` | PyQt6 桌面主窗口；设置（含「浏览器监控」开关）、托盘、工具栏 |
| `downloader.py` | 下载引擎：多线程分段、暂停/恢复、代理模式（env/direct/显式）；per-task 生命周期转换锁 `_xlock` + 启动令牌 `_start_token`，回归见 `tests/test_resume_race.py` |
| `browser_monitor.py` | 浏览器监控本地捕获服务（端口 5001）+ 剪贴板监听 |
| `app.py` | Flask 后端 API（含 `/api/browser-capture`、SSE 流、任务管理） |
| `torrent.py` | libtorrent 封装（BT/PT）；`retry()` 锁内完成「重置 + 启动」，`start()` guard 收回锁内，`_tick()` 整段持 `_lock` 刷新 |
| `media_service.py` | 嗅探缓存 `MediaRegistry`（60s TTL，按 tab 聚合媒体项） |
| `media.py` | `MediaTask`：HLS/DASH/站点解析下载编排 + yt-dlp/ffmpeg 依赖探测；与 `DownloadTask` 同构的 `_xlock` 转换锁 + `_drain_worker`（worker 先 start 再发布） |
| `throttle.py` | 限速令牌桶（写入粒度切片，修瞬时速度读数虚高） |
| `scheduler.py` | 定时到点自启 + 完成动作（none/shutdown/suspend/beep）+ 可撤销倒计时 |
| `extension/content.js` | DOM 侧嗅探（MSE/blob、video 元素），`runtime.sendMessage` 上报 |
| `extension/sniff.js` | content/background 共用的嗅探规则 + Cookie→Netscape |
| `templates/index.html` | Web UI（内嵌打包） |
| `extension/` | Chrome 扩展（manifest/background/popup/icons） |
| `build_exe.py` / `SwiftDM.spec` | PyInstaller 打包 |
| `release.py` | GitHub Release 上传 |
| `requirements.txt` | 依赖（Python 3.13，PyInstaller 6.22 / PyQt6 / libtorrent 已装） |

---

## 7. 流媒体嗅探与下载（2026-09-23，已实现并验收）

**新模块职责（一句话）：**
- `media_service.py`（嗅探缓存）：`MediaRegistry`，60s TTL 的易失缓存，按 tab 聚合“嗅探/解析到的媒体项”，扩展 popup 与页面解析都从这里读。
- `media.py`（MediaTask + 依赖探测）：`MediaTask` 统一编排 HLS/DASH/站点解析的下载；运行期探测 `yt-dlp`（必选）与 `ffmpeg`（可选，仅检测系统版本，不自动下载）；`SWIFTDM_FFMPEG` 可显式指定可执行路径。
- `throttle.py`（限速令牌）：写入粒度的令牌桶，`write_granularity = rate × 0.5s` 突发额度，避免 256KB 整块写入把瞬时速度读数放大。
- `scheduler.py`（定时与完成动作）：计划任务到点自启、完成后动作（`none / shutdown / suspend / beep`）与 60s 可撤销倒计时；关机的验证由 `tests/test_scheduler.py` 的注册 runner 覆盖（不真关机）。
- `extension/sniff.js`（共享嗅探规则）：content.js 与 service worker 共用的 `classifyResource` / `noteFragment` / `cookiesToNetscape`。
- `extension/content.js`（DOM 侧嗅探）：兜底 MediaSource(`blob:`) / `<video>` 元素，经 `chrome.runtime.sendMessage({type:'swiftdm-dom-media'})` 上报。

**运行依赖：**
- `yt-dlp` **必装**（`pip install -r requirements.txt`；`build_exe.py` 已 `--collect-all yt_dlp` + `--hidden-import yt_dlp.utils / mutagen`）。
- `ffmpeg` **选装**：SwiftDM **只检测系统 PATH 里的 ffmpeg 版本**，不自动下载二进制；缺失时纯 TS 的 HLS 仍可下载（yt-dlp 内置合流），唯有“视听分离”解析才返回 409 `needs_ffmpeg`（卡单与设置面板都给安装指引）；`SWIFTDM_FFMPEG` 指定路径可覆盖。

**测试命令：**
```bash
python -m pytest tests -q          # 218 passed, 1 skipped
node tests/test_sniff.js
node tests/test_background_load.js
node --check extension/sniff.js extension/background.js extension/content.js extension/popup.js
python -c "import json;json.load(open('extension/manifest.json',encoding='utf-8'))"
python test_binary.py              # 打包产物端到端（分段/完整性/暂停续传取消/浏览器单端点）
python test_norange.py             # 服务器不支持 Range 时，暂停/续传后 sha256 仍一致
```

**已知限制：**
- **不做 DRM 绕过**：受保护清单在 preflight / 提取期即判 `drm_protected`，返回 409，无绕过尝试、无任务残留。
- **MSE / blob 只能降级为“解析本页”**：拿不到直链，改交 yt-dlp 解析页面地址。
- **定时、`finish_action`、`rate_limit` 均不落盘**，重启即失效。
- **嗅探仅在扩展启用且页面确有网络请求时才有结果**；纯 TS 的 HLS 也能嗅到（靠分片流量摘要归为 `hls_segments`，只读展示项，不可直接下载）。
- **流媒体限速是 yt-dlp 的“每条连接”限速**：`concurrent_fragment_downloads` 取 `segments`（默认 8），实际总速率上限约为 `limit × segments`（`DownloadTask` 的全局令牌桶没有这种放大）。

**端口事实：** Flask 5000 被占时依次回落 5002–5005；扩展按 `SWIFTDM_BASES` 依次探测，把首个可用端口持久化到 `chrome.storage.local.swiftBase`。

**关键坑（2026-09-23 修复，务必记住）：** MV3 service worker 顶层脚本里 `chrome.webRequest.onHeadersReceived.addListener` 的过滤 `types` 若含非法值（例如 `'fetch'`——webRequest 合法 ResourceType 里没有 `fetch`，`fetch()` 请求实际报为 `xmlhttprequest` 同族），`addListener` 会同步抛 schema 校验异常，导致其后所有注册（含 `chrome.runtime.onMessage`）全部死亡并进入 crash-loop，表现为“嗅探上报链路断了、popup 收不到 DOM 媒体”。node 桩测试因忽略 `filter` 参数**无法捕获**，必须真机 / Playwright 核验。已从 `extension/background.js` 删除该 `'fetch'`。

---


## 8. 给下一个 agent 的建议 / 待办

- **下一轮迭代已在设计**：流媒体嗅探 + HLS/DASH 下载（扩展 popup 媒体面板）、yt-dlp 网页视频解析、ffmpeg 混流、下载调度（定时/限速/完成动作）。已定方案：双引擎按 `kind` 分流——http 直链走现有分段引擎，流媒体走 yt-dlp Python API，任务统一注册进 `DownloadManager._tasks`。spec 将基于本副本核对挂点后撰写。
- 浏览器接管功能需要**真实 Chrome + 手动加载扩展**才能端到端验证（自动化测试覆盖不到扩展侧）。
- 若用户要继续迭代：常见方向——扩展打包成 CRX 免手动加载、GUI 增加「浏览器捕获」实时面板、接管时支持更多浏览器（目前仅 Chrome MV3）。
- 保持 `test_norange.py` 绿（暂停/继续完整性），改 `downloader.py` 后务必 `build_exe.py` 再测。

---

## 9. 临时文件清理提示

根目录下的 `_build.err` / `_run.err` / `_run.out` / `_src.err` / `_src.out` 是调试日志（以 `_` 开头，多数已被 `.gitignore` 忽略）。交接后可手动删除，不影响构建。
