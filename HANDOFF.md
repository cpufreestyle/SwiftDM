# SwiftDM 交接文档（Handoff）

> 本文档供接手 SwiftDM 项目的下一个 agent 阅读。最后更新：2026-09-27。
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

- 状态（截至 commit `9dded5c`）：工作区干净，与 `origin/main` 完全同步（0/0）；本轮 15 个 commit 的验证状态见第 5 节。
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

## 5. 最近一轮已完成的工作（2026-09-27，已推送）

主题：设置项的“最后一段路”——灭重写死的线程数、把 Web 端三个私有偏好接进共享配置、删掉死接口；后续追加扩展 popup 主题化改造与 Web 端键盘焦点环、无障碍属性；末尾再把同一套落到桌面端、把筛选芯片全部接进 Tab 顺序，最后补齐扩展 popup 的焦点环与 aria 语义、给动态列表按钮补上可访问名，并修掉长文件名撑破弹窗行高的布局缺陷。

本轮共 15 个 commit（HEAD = `9dded5c`，`git status -sb` 与 origin/main 0/0）：

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
8. **扩展 popup 完成主题化改造，三端调色板完全统一**（`ca64fd7`）：`extension/popup.html` 有 51 处硬编码 hex 色值，主界面切换主题时 popup 不会跟着变，浅色系统下看起来很不协调。
   - `extension/popup.html`：51 处色值全部换成 `var(--x)` 令牌，变量名与 Web 端 `:root` 同名（19 个）；文件顶部新增两套调色板（`:root` 深色、`:root[data-theme="light"]` 浅色），与桌面端两种主题及 Web 端 `:root` 变量逐个对应；
   - 配套修正：`.badge` / `.badge.warn` 由「实色底 + 深色字」改为「柔色底 + 饱和字」；`.media-sub` / `.media-empty` 由 `--faint` 提到 `--text2`（可读性）；`.btn-reset:hover` 改用 `var(--red-soft)`；`.footer` / `.btn-dl[disabled]` 改用 `--faint`；
   - 浅色主题的 `--on-green` 定为 `#06231b` 而非白色：白字在 `#0a9d7c` 上对比度仅 3.43，深色字为 4.85，超过 4.5:1 的门槛；
   - `extension/popup.js`：新增 `systemPrefersLight()` / `resolveTheme(pref)` / `applyTheme(pref)` / `loadTheme()`，`DOMContentLoaded` 里 `loadTheme()` 排在 `loadStatus()` 之前，避免首帧主题错误；优先级为显式设置 > 跟随系统；
   - `extension/background.js`：消息桥新增 `getSettings` 分支，转发 `GET /api/settings`，供 popup 读取 theme 设置，分支排在 `getTasks` 之前；
   - 测试：`tests/test_theme_tokens.py` 以 POPUP_TOKENS（popup ↔ Web ↔ 桌面端三端映射，None 表示该端暂无对应令牌）、POPUP_ONLY、POPUP_TEXT_PAIRS、POPUP_CHIP_TINTS 取代原 POPUP_MAP，新增 5 组断言：调色板与共享主题一致、只声明已登记变量、无字面色值（先去掉 HTML 实体，否则 `&#11015;` 误报）、芯片共用柔色、文字对比度 ≥ 4.5、popup 跟随应用设置；
   - `tests/test_extension_panel.js`：sandbox 补 `matchMedia` 与 `documentElement.dataset` 桩，消息顺序断言改为 `['getSettings','getStatus']` 并用注释说明主题必须排在最前。
9. **Web 端补齐键盘焦点环与无障碍属性**（`1a15e83`）：之前网页端所有交互控件没有可见焦点（只有 border-color 变化），图标按钮也没有可读的名字，纯键盘和读屏用户基本上没法用。
   - `templates/index.html`：新增 `:focus-visible` 焦点环，并用 .btn / .filter-btn / .search-input / .sort-select 同名类把特异度抬过 `.add-bar input` 等写死的 `outline:none`；新增 `@media (prefers-reduced-motion: reduce)`，停掉 `.pulse` 呼吸动画与按钮过渡；
   - 7 个图标按钮全部补 `aria-label`（值与现有 `title` 同文，title 保留给鼠标悬浮）；两个模态框声明 `role="dialog"` / `aria-modal` / `aria-labelledby`；toast 声明 `role="status"` + `aria-live="polite"`；
   - 任务卡片的进度条暴露为 `role="progressbar"`（带 aria-valuenow / min / max），多选框补 `aria-label`；筛选区加 `role="group"`，筛选与紧凑芯片由 `syncFilterButtons` / `applyCompact` 维护 `aria-pressed`；`advToggle` / `settingsBtn` 维护 `aria-expanded`；
   - 顺手修两个真 bug：`setFilter` / `syncFilterButtons` 用的 `.filter-btn` 选择器连紧凑按钮一起匹配，切换筛选会把紧凑按钮的 active 状态抹掉（现收窄为 `.filter-btn[data-filter]`）；`escapeHtml` 被声明了两次，后一份静默覆盖前一份，带 XSS 说明的那份根本没生效；
   - 测试：`tests/test_web_ui.py` 新增 6 组断言（焦点环与减弱动态、图标按钮可访问名、模态框与 toast 宣读、芯片按下态、进度条与多选框、`escapeHtml` 只声明一次）；变异测试 7/7 全红。
10. **桌面端补上可见焦点环与可访问名**（`cded294`）：之前 Qt 端所有 QPushButton 都由 QSS 重画，原生焦点框被吃掉——离屏渲染对比过，聚焦前后像素完全一致，纯键盘用户在桌面端也看不见焦点落在哪。
   - `main_window.py` QSS：给 QToolBar 按钮、`#btnAdd`、`#btnFinishCountdown`、筛选芯片分别补强调色焦点环；已选中的芯片底色就是强调色，描边改用正文色才看得见；仅键盘聚焦时显形，鼠标点击不出现。
   - 卡片按钮（`_btn_style`）：描边本来就是彩色，焦点环改用淡填充，否则改描边颜色也看不出来；带 `tooltip` 的按钮把 tooltip 同步成 `setAccessibleName`（顶部那个「📋」图标按钮之前没有任何可读名字）。
   - 其余无名控件：「···」溢出按钮、倒计时按钮（文字是动态的）、卡片进度条、搜索框、排序下拉、紧凑开关全部补 `setAccessibleName`。
   - 测试：`tests/test_desktop_ui.py` 新增三组——离屏渲染对比（两套主题 × 五类按钮，要求焦点环落在最外一圈）、名字源码级守卫、卡片行为级断言；变异测试 8/8 全红。
验证：全量 pytest 355 passed / 1 skipped；12 个 node 测试全绿；变异测试 Web 7/7、桌面 8/8、扩展 popup 15/15 均能把新增守卫打红。
`build_exe.py` 重建（exit 0）+ `test_binary.py` 全过（`=== 全部测试通过 ===`）：本轮改的 `main_window.py` 参与打包，桌面 QSS 与控件属性的改动以二进制端到端复测为准；打包后的 EXE 起 --web-only 服务，已确认返回页面含 outline-offset / prefers-reduced-motion、role="dialog"、aria-live、aria-valuenow、aria-pressed、filter-btn[data-filter] 与 setPanelExpanded。
离屏渲染实测：修复前同一按钮聚焦前后像素完全一致（原生焦点框被 QSS 重画吃掉），修复后最外一圈出现强调色描边；两套主题下都成立。

11. **筛选芯片全部变成真正的 Tab 停留点**（`c3906ea`）：四颗芯片的互斥之前交给 exclusive QButtonGroup，
    Qt 为此会把除第一颗以外的芯片摘掉 TabFocus（只剩点击/滚轮），纯键盘用户根本到不了
    「进行中 / 已完成 / 失败」；方向键也不管用，QPushButton 只在 autoExclusive 时才在组内移动焦点。
   - `main_window.py`：互斥改为由 `_set_filter` 手工维护（每次切换把四颗芯片的 checked 全部刷一遍，点已选中的那颗也会按回去）；
     芯片显式 `setFocusPolicy(StrongFocus)`，四颗都进 Tab 顺序；删掉已经没用的 `QButtonGroup` 导入。
   - 顺带结论（上一轮的假设被推翻）：「工具栏后半段被 Tab 跳过」其实是窗口过窄时 QToolBar 把按钮收起来了；
     容器默认顺序就等于控件创建顺序，已经就是视觉阅读顺序（日志面板默认排在最后），所以不需要 `setTabOrder` 显式链。
   - `tests/test_desktop_ui.py`：新增两组行为级护卫——真实主窗口下从 url_input 一站站 Tab 到任务区（隔离本机 `history.json`）、
     芯片互斥 + 每颗都 Tab 得到；变异测试 4/4 均变红。
12. **扩展 popup 补上焦点环、页签语义与按下态**（`555d2f6`）：popup 是三端里唯一还没对齐的一块——Web 端上一轮已加
    `:focus-visible`，桌面端已补 QSS 焦点环，而 popup 里所有按钮都是 `border:none`，键盘聚焦时画面毫无变化。
   - `extension/popup.html`：新增 `:focus-visible` 焦点环（强调色描边 + 2px 偏移），颜色只走令牌；选中页签底色本就是强调色，
     同色描边会糊在一起，另给 `.tab.active:focus-visible` 换亮一号的 `--accent2`；
   - 页签容器声明 `role="tablist"` + `aria-label="面板切换"`，三个 button 补 `role="tab"` / `aria-selected` / `aria-controls`，
     三个面板补 `role="tabpanel"` / `aria-labelledby`，读屏用户能听到「任务页签，已选中」；
   - 暂停按钮补 `aria-pressed`（初始 `true`，与文案「暂停」对应）；
   - `extension/popup.js`：`showPanel()` 在刷 `className` 后同步写 `aria-selected`（这是上一轮 `PANELS` 集中遍历的延伸，
     新增面板只要进 PANELS 就自动带上）；`updateUI()` 末尾写 `aria-pressed`，与按钮文案同源；
   - 测试：`tests/test_popup_panels.js` 的 `makeEl` 桩补 `setAttribute`/`getAttribute`，新增两组——源码级断言 CSS 形状
     （焦点环走令牌、有偏移、选中页签用 accent2、tablist 与 tabpanel 双向挂名）与两个行为级断言（切换页签时 aria-selected
     三个值同时翻转；`loadStatus()` 回来后 `aria-pressed` 与按钮文案同步，暂停/启用两个方向都测）；变异测试 8/8 全红。
   - 踩坑记录：桩里 `document.addEventListener` 是空函数，`loadStatus()` 不会自动跑，直接调 `updateUI()` 只会看到初始值
     `enabled = true`，所以行为级用例要先 `sandbox.loadStatus()`；该测试文件是 UTF-8 带 BOM，读写要 `utf-8-sig`，
     写回时补回 BOM，否则 diff 多一行无关改动。

13. **扩展 popup 列表按钮补上可访问名**（`072c60f`）：媒体/任务列表的行内按钮是 `createElement` 动态拼的，
    文案只写了「下载」「解析本页」「重试」，一屏几十行下来读屏用户 Tab 过去只听到一个裸动词，
    完全不知道焦点落在哪一条；而且按钮文案随后台结果会变成「已添加」/「已重试」，名字不同步就更对不上。
   - `extension/popup.js`：新增 `relabel(btn, text, name)` 助手，同时写 `textContent` 与 `aria-label`，
    并把对象名从 `renderItem`（`nameOf(item)` / `task.filename`）一路传进 `downloadBtn` / `pageBtn` / `retryBtn`；
     名字取不到时（blob: 地址）不拼尾随空格，`aria-label` 就只剩动作词；
   - 三次状态迁移（添加中→已添加 / 重试中、重试中→已重试 / 重试）全部走 `relabel`，可见文字与可访问名永远一致；
   - 测试：`tests/test_popup_panels.js` 新增 ⑦⑧（媒体行/分片行/blob 行的 aria-label、点击后两条分支都跟着变、
     任务行带文件名）；`tests/test_extension_panel.js` 的同名 stub 也补 `setAttribute`/`getAttribute` 并在 ②③ 里加了断言；
     变异测试 9/9 全红，且是跨「全 node 套件」验证的（两个测试文件各自都能打红）。
   - 踩坑记录：`tests/test_extension_panel.js` 与 `test_popup_panels.js` 各有一份重复的 `makeEl` 桩，
     只改一个会漏——这次就是改了 popup_panels 的桩、extension_panel 那份还是老的，当场 `setAttribute is not a function`；
     另外点击回调在桩里是同步回调的，handler 返回 `undefined` 时走的是失败分支（「重试」），别误写成「添加中」。
14. **修掉扩展 popup 长文件名撑破行高**（`9dded5c`）：媒体/任务行是 260px 宽的 flex 行，
    名字列 `flex: 1` 但没写 `min-width: 0`——flex 子项默认 `min-width:auto`，长文件名不肯收缩，
    把「下载」按钮整个顶出弹窗；更隐蔽的是 `text-overflow: ellipsis` 写在 `.media-name` 上，
    而文字其实在它的子 `div` 里，省略号一个字符都没生效。
   - `extension/popup.html`：`.media-name` 补 `min-width: 0`，省略号下沉到真正包着文字的子元素
     （新增 `.media-name > div { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }`）；
   - 截断会丢信息，所以 `renderItem` / `taskRow` 给整行 `label` 加 `title` 悬浮提示，
     带上「文件名 · 清晰度 · 大小」或「文件名 · 失败原因」；
   - 测试：`tests/test_popup_panels.js` 新增 ⑨——源码级断言 CSS 三段形状（min-width 归零、
     省略号在子元素且不在父级、子元素不换行）+ 行为级断言两条 `title` 的完整内容；变异测试 6/6 全红。
   - 踩坑记录：这是「既有缺陷被旧写法掩盖」的典型——`.media-name` 上有 `overflow:hidden`，
     视觉上像被裁掉了，实际是整行超宽后被 body 裁的；动手前先在浏览器里量一行真实宽度。
剩余候选：
- （已关账）托盘失败数角标：16px 白点 + tooltip 「✗ N 个失败」随每次刷新更新，可读性由 tooltip 解决，角标改数字不可行。
- 扩展打包成 CRX（现代 Chrome 已禁止拖拽安装，收益存疑）。
- 桌面端“剪贴板监听”在 Web 无对应物，属合理不迁移（浏览器无法后台监听系统剪贴板）。
- （已关账）Web、桌面端、扩展 popup 三端均已补上焦点环与 aria-属性，popup 动态列表按钮带上了对象名、长文件名也收进了 260px 弹窗；筛选芯片也已全部 Tab 得到（`setTabOrder` 经实测是多余的，默认顺序已经对的）。
- （已关账）扩展 popup 的 CSS 已全部纳入令牌守卫（POPUP_TOKENS），旧 POPUP_MAP 只覆盖 10 条选择器，已取代。

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
