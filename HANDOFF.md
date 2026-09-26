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

- 状态（截至 commit `1d9502d`）：工作区干净，与 `origin/main` 完全同步（0/0）；本轮 8 个 commit 的验证状态见第 5 节。
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
- 核对中文不要用 `Get-Content`（按 GBK 解码显示乱码）：先 `[Console]::OutputEncoding=[Text.Encoding]::UTF8` 再用 `Select-String -Path <f> -Pattern <关键词>`；python `print` 中文到管道可能触发 `UnicodeEncodeError`，用码点校验最稳。

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

主题：设置项的“最后一段路”——灭重写死的线程数、把 Web 端三个私有偏好接进共享配置、删掉死接口。

本轮共 8 个 commit（HEAD = `1d9502d`，`git status -sb` 与 origin/main 0/0）：

1. **所有入口都读 segments 设置**（`d93c66f`）：`browser_monitor.py` 两处（HTTP 捕获、监控线程自动添加）与 `main.py` 的捕获回调原先写死 `create_task(..., 8)`，
   改为 `config.clamp_segments(config.get("segments"))`，与 `app.py`/`main_window.py` 一致。
   - 新增 `tests/test_dispatch.py::test_no_entry_point_hard_codes_the_thread_count`：源码级守卫，扫描全部受版本控制的 `.py/.js/.html`，任何 `create_task(` 调用点（downloader.py 的签名默认值除外）出现裸整数字面量即失败；
     已用“把 8 塞回去”的方式验证过它真的会红。
   - 两个行为级测试覆盖 `browser_monitor` 的两个入口（3/16/99/0 四组值，含 1-32 钳制与未配置回退），
     跑完用真 `config.set` + finally 还原。
2. **Web 端 theme/filter/sort 接入共享配置**（`5dd2619`）：此前只存 localStorage，桌面端读写同一份 `~/.swiftdm/config.json`，两边各记一份、切换即丢。
   - `app.py`：POST 接受三个键（按桌面端同套键名校验，非法值回退默认），GET 下发；
   - `templates/index.html`：`setTheme/setFilter/setSort` 改为 POST，`refreshSettings()` 里新增 `syncThemeInput/syncFilterInput/syncSortInput`，
     服务端值优先、localStorage 只做首屏缓存（沿用 compact 的模式）；
   - `config.py` `_DEFAULTS` 补 `filter: all` / `sort: default`；
   - 主题说明文案改为“与桌面端共用”；`test_api_contract.js` 的设置契约自动覆盖新键。
3. **删除死接口 `/api/clipboard`**（`240a4d7`）：写入的 `_clipboard_url` 全仓库无人读取，三个端也都没有调用方；
   剪贴板功能实际由 `browser_monitor.py` 的系统剪贴板监听实现。

4. **`create_task` 默认值随设置：不传参即读共享设置，与传 None 语义统一；传 0 与显式值不变。**

5. **空闲速率显示 0 B/s 而非「未知/s」**（`b1309a1`）：`format_speed` 原先复用 `format_size`，而 `format_size(0)` 的语义是「大小未知」，空闲时桌面状态栏与 Web 顶栏都显示「未知/s」；扩展 popup 的 `formatSpeed(0)` 本就是 `0 B/s`，本案让三端语义统一：
   - `main_window.py`：`format_speed` 对 falsy/非正值直接返回 `"0 B/s"`；
   - `templates/index.html`：`formatSpeed` 加同样的零值守卫；
   - `extension/popup.js`：`formatSize` 单位表补 TB，与 Web/桌面单位表一致；
   - 测试：桌面零速用例、新增 `tests/test_web_format.js`（vm 提取 index.html 的 `formatSize`/`formatSpeed` 做行为断言，并校验 popup 单位表含 TB）、`tests/test_extension_panel.js` 补 TB 断言。

6. **强调按钮文字色改为主题令牌 `onAccent` / `--accent-ink`**（`3b7d9fc`）：此前桌面 QSS 两处与 Web 六处规则把「强调色背景上的文字」写死 `#fff`，换主题时这块颜色不随主题走。两端各加一个令牌（桌面 `THEMES.*.onAccent`、Web `:root --accent-ink`，值均为 `#ffffff`），并登记进跨端映射表 `PAIRS`：
   - `main_window.py`：`#btnAdd` 与 `#filterBtn:checked` 改用 `$onAccent`；
   - `templates/index.html`：6 条 `color: #fff` 规则改用 `var(--accent-ink)`；
   - `tests/test_desktop_ui.py`：新增更严的桌面守卫——QSS_TEMPLATE 里一个十六进制色值都不允许有（白色也得走令牌），已用「塞回 #fff / #abcdef」验证会红；
   - `tests/test_web_ui.py`：Web 端守卫去掉 `#fff` 白名单，与桌面同标准；`tests/test_theme_tokens.py` 登记 `accent-ink ↔ onAccent`，改任一端值即红。

7. **Web 端补齐键盘导航：↑↓/Enter 三端一致**（`1d9502d`）：桌面端快捷键除 Ctrl+N / Ctrl+F / Esc 外还有 ↑↓ 移动选中、回车打开选中任务，Web 端 shortcutAction 只有前三个，纯键盘用户只能用鼠标。
   - `templates/index.html`：新增 focusableCardIds() / stepFocus(delta) / setFocusTask(id, scroll)，语义对齐桌面 _step_selection——到头钳制不循环、空序列清空焦点、焦点 id 不在可见卡片里时落到首/尾；
   - shortcutAction 在不带修饰键时认 arrowup / arrowdown / enter；没有焦点任务时 Enter 不拦截，交给输入框和按钮自己处理；
   - runShortcut 新增输入控件让位守卫（input/select/textarea/contentEditable），对齐桌面 _keyboard_nav_allowed；回车走卡片「打开」按钮同一条 openFile() 路径；
   - 轮询每 500ms 重建卡片，renderTasks 末尾用 scroll=false 重贴焦点类名，避免把页面拽走；
   - 视觉：.task-card.focused 用强调色描边加 1px 光环，筛选栏新增 .kbd-hint 提示「↑↓ 选择任务 · Enter 打开」；
   - 测试：tests/test_web_shortcuts.js 的 sandbox 补卡片桩（id/classList/scrollIntoView）与 openFile、_focusTaskId，覆盖新动作、钳制、焦点失效、空列表、轮询重贴、输入控件让位；跨 realm 数组改用 deepEqual 断言；
   - tests/test_web_ui.py：renderTasks 的切片窗口从写死 1600 字符改为「切到下一个函数定义」，排序守护不再随函数行数漂移（本轮新增焦点簿记行数后原窗口已失效）。

验证：全量 pytest 336 passed / 1 skipped；12 个 node 测试全绿；
`build_exe.py` 重建 + `test_binary.py` 全过（`main_window.py`/`templates/` 参与打包）。
打包后的 EXE 起 --web-only 服务，已确认返回的页面含 stepFocus/setFocusTask/focusableCardIds 与 kbd-hint 提示。

剩余候选：
- （已关账）托盘失败数角标：16px 白点 + tooltip 「✗ N 个失败」随每次刷新更新，可读性由 tooltip 解决，角标改数字不可行。
- 扩展打包成 CRX（现代 Chrome 已禁止拖拽安装，收益存疑）。
- 桌面端“剪贴板监听”在 Web 无对应物，属合理不迁移（浏览器无法后台监听系统剪贴板）。
- 扩展 popup 的 CSS 只有 10 条选择器纳入令牌守护（POPUP_MAP），其余规则可逐条补齐。

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
node tests/test_extension_panel.js   # popup「任务」页：失败筛选/渲染/重试消息
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
