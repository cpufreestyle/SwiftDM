# SwiftDM 流媒体嗅探与下载增强 — 设计规格

日期：2026-09-23 ｜ 状态：待审阅 ｜ 基线：主副本 `D:\ai share\repo\SwiftDM`（commit `6459a97`）

## 1. 目标与范围

为主流下载软件的收费功能做一轮对齐：

1. **流媒体嗅探**：浏览器扩展检测页面中的 HLS(.m3u8)/DASH(.mpd)/直链视频资源，popup 媒体面板按标签页展示，用户选择清晰度后下载。
2. **HLS/DASH 下载**：清单解析、分片并发下载、无损合并为 mp4；网页视频 URL 交 yt-dlp 解析（登录态站点靠浏览器 cookies 透传）。
3. **ffmpeg 混流**：DASH 音视频分离流合流、转封装；使用系统 ffmpeg，检测缺失并引导安装。
4. **下载调度**：全局限速、定时开始、完成后动作（关机/休眠/静音/通知）。

**明确不做**：DRM 内容（Widevine/FairPlay/SAMPLE-AES）绕过与解密；嗅探到即标记 🔒 拒绝。

## 2. 架构（方案 A：双引擎按 kind 分流，已经逐节评审）

```
扩展(network+DOM 嗅探) ─POST /api/media/discover─▶ MediaRegistry(按tab缓存)
        popup/Web UI 展示、用户点下载
                │ POST /api/add {url, kind, referer, cookies, resolution}
                ▼
app.add_task (app.py:53) ──kind=http/直链──▶ DownloadTask 8段引擎 (downloader.py:142)
        └─kind=hls/dash/video_page──▶ MediaTask (media_service.py, yt-dlp)
                两类任务同注册 DownloadManager._tasks (downloader.py:636)
                SSE /api/stream (app.py:257) → 前端零改动获得进度
```

先例：`create_task` 已按 URL 分发 `TorrentTask`（downloader.py:661），`kind` 分流沿用该模式。

### 组件

| 单元 | 文件 | 职责 | 依赖 |
|---|---|---|---|
| MediaTask | 新增 `media_service.py` | yt-dlp Python API 适配：进度回调、清晰度、referer/cookies、限速、伪续传；对外接口与 DownloadTask 对齐（start/pause/cancel/to_dict） | yt-dlp |
| MediaRegistry | `media_service.py` | 按 tabId 聚合嗅探结果，URL 规范化去重，tab 关闭/超时 60s 清除；不是任务库 | — |
| kind 分流 | `downloader.py:655` `create_task` | 按 `kind`/URL 特征创建 MediaTask 或原任务 | 上两者 |
| 网络层嗅探 | `extension/background.js:75` webRequest 监听 | 从仅 main/sub_frame 扩到全 resourceType：识别 m3u8/mpd URL、`application/vnd.apple.mpegurl`、`application/dash+xml`、`video/*`，ts 分片同前缀≥5 判 HLS | — |
| DOM 层嗅探 | 新增 `extension/content.js` | `<video>/<source>` 的 src/currentSrc、blob:(MSE) 标记"需解析页面" | — |
| 上报与面板 | `background.js` 聚合 + `popup.html/js` 媒体标签页 | `POST /api/media/discover`；popup 打开时 `GET /api/media/list?tabId` 轮询；下载时 `chrome.cookies.getAll` 随带 | 新端点 |
| 限速 | 新增 `throttle.py` | 全局令牌桶；分段引擎读循环 acquire，yt-dlp 用 ratelimit 参数 | — |
| 调度 | 新增 `scheduler.py` | 30s 扫描 `start_at` 到点启动；全部完成后执行 finish_action（Windows：`shutdown /s /t 60` 可撤销、SetSuspendState、提示音/托盘通知） | — |
| 设置扩展 | `app.py:144` `/api/settings` | 新增 `limit_rate`、`finish_action`、`ffmpeg_available`（只读）字段 | — |
| Web UI | `templates/index.html` | 媒体图标+清晰度徽章；drm_protected 灰色锁标；"媒体嗅探"汇总页签；设置弹窗扩展；i18n zh/en（英文文案按发布级撰写） | SSE 已有 |

### 关键接口

`POST /api/add` 请求体向后兼容扩展：
`{url, save_dir?, filename?, segments?, kind?: "auto"|"http"|"hls"|"dash"|"video_page", referer?, cookies_netscape?, resolution?, start_at?}`；`kind=auto`（默认）仅按 URL 特征判定（.m3u8/.mpd→对应类型，magnet/.torrent→torrent，其余→http）；`video_page` 只接受显式传入（popup"解析页面地址"按钮），不对普通 URL 猜测，避免把普通直链误交给 yt-dlp。错误响应码：`409 {reason:"drm_protected"|"needs_ffmpeg"}`、`400 invalid_url`。

`POST /api/media/discover`：`{tab_id, page_url, title, items:[{url,kind,quality_hint?,bytes?,is_mse?}]}` → `{ok, deduped:n}`。
`GET /api/media/list?tabId=`（省略=全部 tab）→ MediaRegistry 快照。

### MediaTask 行为

- `yt_dlp.YoutubeDL`：`format` 映射清晰度（`bv*[height<=H]+ba/b`，默认 best）；`progress_hooks` 更新 downloaded/total/speed；`concurrent_fragment_downloads` 走 yt-dlp HLS 分段并发；`continue_dl=True` + 任务专属 `.parts` 目录实现恢复。
- cookies：扩展 Netscape 串写任务临时文件，`cookiefile` 传入，finally 删除（不落历史、不进日志）。
- DRM 预检：`extract_info(download=False)` 中 format 含 `drm` 字段 → 拒绝；AES-128 的 m3u8 允许（yt-dlp 原生支持 `#EXT-X-KEY` 解密）。
- ffmpeg：启动时 `shutil.which()` 探测并缓存；纯 TS 合并可由 yt-dlp 内置完成，仅合流/转封装任务在缺失时 409 needs_ffmpeg。不自动下载二进制。

## 3. 数据流（嗅探→下载全链路）

1. 用户播放视频 → 扩展 network/DOM 层发现媒体 → background 规范化去重 → `/api/media/discover`。
2. popup 打开 → 拉 `/api/media/list?tabId=当前tab` → 渲染列表（类型图标/清晰度/大小/🔒）。
3. 点击"下载"（或展开选清晰度）→ `POST /api/add`（带 referer+cookies）→ kind 分流 → MediaTask → SSE 推进度卡片。
4. 失败路径：409/网络错 → 任务标 failed，error 原因经 SSE 展示（复用失败原因可视化）。

## 4. 错误处理与边界

- 扩展端点发现沿用 `SWIFTDM_URLS` 端口回退（background.js:4）；后端不可达时静默重试不打扰。
- MSE/blob: 不直接下载，提示用"解析页面地址"（把 page_url 以 `kind=video_page` 交 yt-dlp）。
- MediaRegistry 上限每 tab 50 条、总量 2000 条，LRU 逐出；history.json 持久化对媒体任务记录 kind/resolution（`to_dict` 扩展，旧记录缺字段按 http 处理，`_reconstruct_task` 已容错）。
- 限速热更新：令牌桶 `set_rate()` 原子替换，0=解除。
- 完成后动作仅在"无未完成任务且无未到期 start_at 任务"时触发；关机 60s 缓冲期内任何新任务取消动作。
- Web-only 与桌面模式功能一致；finish_action 的托盘通知在 Web-only 退化为 SSE 事件。

## 5. 依赖与打包

- `requirements.txt` += `yt-dlp>=2025.1.1`（成熟 API 稳定，无需钉死）；纯 Python，PyInstaller 直接收集（spec 加 `hiddenimports` 如需要）。
- ffmpeg/ffprobe 仅系统探测，不打包（体积 +70MB 与杀软误报考量）。
- 构建回归：改 downloader/新增模块后 `python build_exe.py` + `python test_norange.py` 必跑。

## 6. 测试

- 单测（pytest，dev 依赖与 requirements 分离）：清单识别与 kind 判定、URL 规范化去重、令牌桶速率与热更新、`/api/add` 分流决策、ffmpeg 检测 mock、DRM 预检拒绝路径。
- 集成：本地起合成 HLS 服务器（生成 m3u8+8 个 ts 分片），`POST /api/add` → 断言任务完成、顺序合并文件 sha256 与拼接一致、SSE 帧含进度、限速下速率在容差内。
- 手工验收（真实 Chrome）：示例 HLS 页嗅探→popup 列表→下载完成；登录站点 cookies 透传 1 例；blob 页面提示解析；🔒 内容拒绝。
- 兼容性：无 yt-dlp 支持的历史任务记录可正常加载/删除。

## 7. 里程碑切分（供实现计划排序）

1. 后端：media_service(MediaTask+Registry) + kind 分流 + /api/media/* + settings（可测：合成 HLS 集成）
2. 扩展：网络层嗅探 + content.js + popup 媒体面板 + cookies 上报
3. 调度：throttle + scheduler + finish_action + 设置 UI
4. 前端收尾：徽章/媒体页签/i18n + 手工验收清单

每里程碑独立可回归；1、2 有接口依赖，3、4 可与 2 并行。
