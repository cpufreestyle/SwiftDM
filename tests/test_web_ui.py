import json
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
    # 线程数不能再写死 8：设置面板里改了得生效
    assert "segments: defaultSegments()" in payload, payload[:200]


def test_new_task_thread_count_comes_from_settings(page):
    """默认线程数由 /api/settings 下发，/拖入、剪贴板复制都要走同一个来源。"""
    assert "defaultSegmentsCache" in page
    assert "res.default_segments" in page
    assert "function syncSegmentsInput" in page
    assert "async function applySegments" in page
    # 两个入口都不能再写死 8
    assert "segments: 8" not in page, "还有地方写死了 segments: 8"
    assert 'id="segmentsInput"' in page and 'id="applySegments"' in page
    # 空值不能静默通过
    body = page[page.index("async function applySegments"):][:220]
    assert 'showToast(' in body and '"error"' in body, body   # 非法输入要报错，不能静默通过


def test_settings_segments_row_in_download_group(page):
    dl = page.index('<div class="modal-section">下载</div>')
    ui = page.index('<div class="modal-section">界面与诊断</div>')
    for needle in ('id="segmentsInput"', 'id="dirInput"', 'id="proxySelect"'):
        assert dl < page.index(needle) < ui, needle


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


def test_status_filter_and_overall(page):
    assert 'id="filterBar"' in page
    for fid in ("fc-all", "fc-active", "fc-completed", "fc-failed"):
        assert fid in page, fid
    assert "function setFilter" in page and "matchFilter" in page
    assert 'id="overallProgress"' in page


def test_drop_supports_magnet_and_multiple(page):
    assert "extractDropUrls" in page
    assert "magnet:" in page and "enqueueUrl" in page


def test_copy_link_and_open_folder(page):
    assert "async function copyLink" in page and "navigator.clipboard.writeText" in page
    assert "/api/open_folder/" in page and 'onclick="openFolder(' in page
    assert 'onclick="copyLink(' in page


def test_open_folder_endpoint_exists():
    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()
    r = client.post("/api/open_folder/dl_does_not_exist")
    assert r.status_code == 404
    assert r.get_json().get("success") is False


def test_document_title_reflects_activity(page):
    assert "document.title" in page and "个下载中" in page
    assert "SwiftDM - 高速下载管理器" in page


def test_filter_selection_persisted(page):
    assert "localStorage.getItem(\"swiftdm.filter\")" in page
    assert "localStorage.setItem(\"swiftdm.filter\"" in page
    assert "function syncFilterButtons" in page


def test_clear_completed_asks_confirmation_with_count(page):
    start = page.index("async function clearCompleted")
    body = page[start:start + 700]
    assert "confirm(" in body
    assert "不可恢复" in body
    assert "_lastTasks" in body


class _FakeTask:
    def __init__(self, status, ok=True):
        self.status = status
        self._ok = ok
        self.calls = 0

    def retry(self):
        self.calls += 1
        return self._ok


def test_retry_all_route_retries_only_failed_and_cancelled(monkeypatch):
    class FakeManager:
        def __init__(self):
            self.tasks = [_FakeTask("failed"), _FakeTask("cancelled"),
                          _FakeTask("completed"), _FakeTask("downloading")]

        def get_all_tasks(self):
            return self.tasks

    fm = FakeManager()
    monkeypatch.setattr(appmod, "manager", fm)
    appmod.app.config["TESTING"] = True
    resp = appmod.app.test_client().post("/api/retry_all")
    data = resp.get_json()
    assert data["success"] is True
    assert data["retried"] == 2
    assert fm.tasks[2].calls == 0 and fm.tasks[3].calls == 0


def test_web_has_retry_all_failed_action(page):
    assert 'onclick="retryAllFailed()"' in page
    body = page[page.index("async function retryAllFailed"):][:700]
    assert "/api/retry_all" in body
    assert "confirm(" in body


def test_web_has_task_search_box(page):
    assert 'id="searchInput"' in page
    assert "function setSearch" in page
    assert "function matchSearch" in page
    assert "matchFilter(t) && matchSearch(t)" in page


def test_default_thread_count_is_a_real_setting(monkeypatch):
    """设置面板的「下载线程数」必须真的生效，不能只是个附带作用的字段。

    之前 /api/add 硬编码 `or 8`，桌面端 _create_and_start 也写死 8，
    结果 config 里的 segments 被读了一眼就没人用。
    """
    import app as appmod
    import config

    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()

    saved = config.get("segments")

    def _set(value):
        config.set("segments", value)

    created = []

    class _Mgr:
        def create_task(self, *args, **kwargs):
            created.append(args[3])
            raise _Stop()

    class _Stop(Exception):
        pass

    monkeypatch.setattr(appmod, "manager", _Mgr())
    try:
        for value, expected in ((3, 3), (32, 32), (1, 1), (99, 32), (0, 8)):
            _set(value)
            created.clear()
            try:
                client.post("/api/add", json={"url": "http://127.0.0.1:1/x.bin"})
            except _Stop:
                pass
            assert created == [expected], (value, created)

        # 显式给出线程数时仍然优先，且同样要钳制
        _set(4)
        created.clear()
        try:
            client.post("/api/add", json={"url": "http://127.0.0.1:1/x.bin", "segments": 100})
        except _Stop:
            pass
        assert created == [32], created
    finally:
        _set(saved)


def test_web_has_compact_mode_toggle(page):
    assert 'onclick="toggleCompact()"' in page
    assert "localStorage.getItem(\"swiftdm.compact\")" in page
    assert "localStorage.setItem(\"swiftdm.compact\"" in page
    assert ".task-list.compact .task-meta { display: none; }" in page
    body = page[page.index("function applyCompact"):]
    body = body[:body.index("\n}")]
    assert 'classList.toggle("compact"' in body


def test_web_settings_cover_directory_and_proxy(page):
    """桌面端能改的下载目录/代理，Web 端也得能改。

    后端 /api/settings 一直接受这两个键，之前只有桌面端有入口，
    HEADLESS 部署时想改目录或换代理只能手改配置文件。
    """
    for needle in ('id="dirInput"', 'id="applyDir"', 'id="proxySelect"', 'id="proxyInput"',
                   'id="applyProxy"'):
        assert needle in page, needle
    for fn in ("function applyDir", "function applyProxy", "function applyProxyMode",
               "function syncDirInput", "function syncProxyInputs",
               "function onProxyModeChange"):
        assert fn in page, fn
    # 代理选项和桌面端一致：系统 / 直连 / 自定义
    block = page[page.index('id="proxySelect"'):page.index('id="proxyInput"')]
    assert 'value="env"' in block and 'value="direct"' in block and 'value="custom"' in block
    # 自定义代理没填地址时不能静默通过
    body = page[page.index("async function applyProxy()"):][:400]
    assert "请填写代理地址" in body
    body = page[page.index("async function applyDir()"):][:260]
    assert "请填写下载目录" in body


def test_web_settings_rows_stay_in_download_group(page):
    """新增的两行必须留在「下载」分组，否则添到界面分组会砸块。"""
    dl = page.index('<div class="modal-section">下载</div>')
    ui = page.index('<div class="modal-section">界面与诊断</div>')
    for needle in ('id="dirInput"', 'id="proxySelect"', 'id="finishAction"'):
        assert dl < page.index(needle) < ui, needle
    # 共享配置的说明要写清楚，免得用户以为只影响 Web
    assert "与桌面端、浏览器捕获共用同一份配置" in page


def test_web_settings_modal_is_grouped(page):
    assert '<div class="modal-section">下载</div>' in page
    assert '<div class="modal-section">界面与诊断</div>' in page
    dl = page.index('<div class="modal-section">下载</div>')
    ui = page.index('<div class="modal-section">界面与诊断</div>')
    assert dl < page.index('id="finishAction"') < ui      # 完成后动作归到「下载」组
    assert dl < page.index('id="rateInput"') < ui
    assert dl < page.index('id="scheduledList"') < ui
    assert ui < page.index('id="themeSelect"')
    assert ui < page.index('id="capFfmpeg"')


def test_web_has_batch_copy_and_export(page):
    assert 'onclick="copyAllLinks()"' in page
    assert 'onclick="exportTasksCsv()"' in page
    assert "function visibleTasks" in page
    assert "matchFilter(t) && matchSearch(t)" in page
    body = page[page.index("function copyAllLinks"):]
    body = body[:body.index("\n}")]
    assert "navigator.clipboard.writeText(urls.join" in body
    body = page[page.index("function exportTasksCsv"):]
    body = body[:body.index("\n}")]
    assert "swiftdm-tasks.csv" in body
    assert "text/csv;charset=utf-8" in body
    assert "\\uFEFF" in page        # BOM：Excel 直接打开中文不乱码


def test_web_has_multi_select_batch(page):
    # 选择执行条默认隐藢，不干扰日常界面
    assert 'id="selectBar" hidden' in page
    assert 'id="selectCount"' in page
    for bid in ("selPause", "selResume", "selRetry", "selRemove"):
        assert f'id="{bid}"' in page, bid
    # 每张卡片带复选框，状态跨刷新保持
    card = page[page.index("function createTaskCard"):]
    card = card[:card.index("function renderStats")]
    assert 'class="task-pick-box"' in card
    assert "_selected.has(task.task_id)" in card
    assert "onchange=\"togglePick(this)\"" in card
    # 批量逻辑：按状态过滤 + 清理失效选择 + 删除要确认
    body = page[page.index("const BATCH_STATUS"):]
    body = body[:body.index("async function copyAllLinks")]
    assert 'pause: ["downloading"]' in body
    assert 'resume: ["paused"]' in body
    assert 'retry: ["failed", "cancelled"]' in body
    assert "pruneSelection" in body and "updateSelectBar" in body
    assert "confirm(" in body and "/api/remove/" in body
    # 每次渲染后都要剪枝 + 刷新执行条（否则空列表时选择不会清理）
    rt = page[page.index("function renderTasks"):]
    rt = rt[:rt.index("function createEmptyState")]
    assert "pruneSelection();" in rt and "updateSelectBar();" in rt


def test_web_card_shows_auto_retry_countdown(page):
    card = page[page.index("function createTaskCard"):]
    card = card[:card.index("function renderStats")]
    assert "task.auto_retry_at" in card
    assert "retry-badge" in card
    assert "\u81ea\u52a8\u91cd\u8bd5" in card


def test_web_has_task_sorting(page):
    assert 'onchange="setSort(this.value)"' in page
    assert "function sortTasks" in page
    assert "localStorage.getItem(\"swiftdm.sort\")" in page
    assert "localStorage.setItem(\"swiftdm.sort\"" in page
    body = page[page.index("function renderTasks"):]
    body = body[:body.index("function createEmptyState")]
    assert "sortTasks(view.filter(t => t.status === \"downloading\"))" in body


def test_open_download_dir_button(page):
    assert 'onclick="openDownloadDir()"' in page
    assert '"/api/open_download_dir"' in page


def test_open_download_dir_endpoint_creates_and_reveals(monkeypatch, tmp_path):
    import os as _os
    target = str(tmp_path / "downloads")
    revealed = []
    monkeypatch.setattr(appmod.config, "get_download_dir", lambda: target)
    monkeypatch.setattr(appmod, "_reveal_in_file_manager", lambda p: revealed.append(p))
    appmod.app.config["TESTING"] = True
    resp = appmod.app.test_client().post("/api/open_download_dir")
    data = resp.get_json()
    assert data["success"] is True
    assert data["path"] == target
    assert _os.path.isdir(target)          # 目录不存在时先创建，不能直接报错
    assert revealed == [target]


def _css_block(page, selector):
    start = page.index(selector)
    start = page.index("{", start)
    end = page.index("}", start)
    return page[start + 1:end]


def test_light_theme_defines_every_token(page):
    dark = _css_block(page, ":root {")
    light = _css_block(page, ':root[data-theme="light"]')
    dark_tokens = set(re.findall(r"--([a-zA-Z0-9-]+)\s*:", dark))
    light_tokens = set(re.findall(r"--([a-zA-Z0-9-]+)\s*:", light))
    assert dark_tokens, "dark theme block lost its tokens"
    assert dark_tokens == light_tokens, sorted(dark_tokens ^ light_tokens)


def test_theme_controls_are_wired(page):
    assert 'id="themeSelect"' in page and 'id="themeBtn"' in page
    assert 'onclick="toggleTheme()"' in page
    assert "function applyTheme" in page and "function setTheme" in page
    assert 'localStorage.getItem("swiftdm.theme")' in page
    assert 'localStorage.setItem("swiftdm.theme"' in page
    assert '(prefers-color-scheme: light)' in page


def test_no_stray_hardcoded_colors_in_css(page):
    # CSS 不应再出现硬编码颜色：颜色全部由 :root 下发的变量提供（含带色背景按钮的 var(--accent-ink)）
    css = page[:page.index("</style>")]
    css = css[css.index("<style>"):]
    css = re.sub(r':root(\[data-theme="light"\])? \{[\s\S]*?\}', '', css)
    css = re.sub(r"/\*[\s\S]*?\*/", "", css)
    leftovers = re.findall(r"#[0-9a-fA-F]{3,6}\b", css)
    assert not leftovers, leftovers


def test_web_notifies_new_failures_via_toast(page):
    assert "function notifyNewFailures" in page
    assert "function failSummaryText" in page
    assert "notifyNewFailures(data.tasks);" in page
    # 首帧只记录不提示，避免刷新页面补弹旧账
    assert "_knownFailedIds = null" in page
    assert "_knownFailedIds === null" in page
    # 聚合文案与桌面端 _fail_summary_text 一致
    assert "下载失败 [" in page
    assert "个任务下载失败（" in page


def test_web_notifies_new_completions_via_toast(page):
    assert "function notifyNewCompletions" in page
    assert "notifyNewCompletions(data.tasks);" in page
    # 首帧只记录不提示，避免刷新页面补弹旧账
    assert "_knownDoneIds === null" in page
    # 完成文案与桌面端 _notify_complete 对齐
    assert "下载完成: $" in page
    assert "个任务下载完成" in page


def test_task_card_renders_segment_strip(page):
    seg_src = page[page.index("function segmentProgress"):
                   page.index("function createTaskCard")]
    assert "segments_offsets" in seg_src and "segments_progress" in seg_src
    # 只在下载中/暂停展示：完成、失败、单段都不该出现进度条噪音
    assert 'task.status !== "downloading"' in seg_src and "paused" in seg_src
    card = page[page.index("function createTaskCard"):]
    card = card[:card.index("function renderStats")]
    assert "${segmentStripHtml(task)}" in card
    for needle in ("seg-strip", "seg-cell", "seg-fill", "seg-count"):
        assert needle in page, needle


def test_stream_payload_includes_segment_fields(monkeypatch):
    class _Task:
        task_id = "t_seg"

        def to_dict(self):
            return {
                "task_id": self.task_id, "filename": "multi.bin",
                "status": "downloading", "progress": 10.0,
                "total_size": 20, "downloaded": 2, "speed": 5, "eta": "4s",
                "segments": 2,
                "segments_progress": [1, 1],
                "segments_offsets": [[0, 9], [10, 19]],
                "segments_total": 2,
            }

    class _Mgr:
        def get_all_tasks(self):
            return [_Task()]

        def get_stats(self):
            return {"total": 1, "active": 1, "completed": 0, "failed": 0,
                    "paused": 0, "total_speed": 5}

    monkeypatch.setattr(appmod, "manager", _Mgr())
    payload = appmod._stream_payload()
    task = payload["tasks"][0]
    assert task["segments_progress"] == [1, 1]
    assert task["segments_offsets"] == [[0, 9], [10, 19]]
    assert task["segments_total"] == 2
    # 字段必须可 JSON 序列化，否则 SSE 推送会整帧失败
    json.loads(json.dumps(payload))


def test_task_card_shows_bt_seeds_and_peers(page):
    card = page[page.index("function createTaskCard"):]
    card = card[:card.index("function renderStats")]
    assert 'task.kind === "torrent"' in card
    assert "task.seeds" in card and "task.peers" in card
    # HTML 实体图标与桌面卡片一致（🌱 / 👥）
    assert "&#127793;" in card and "&#128101;" in card


def test_web_detail_modal_wired_to_cards(page):
    # 模态框骨架
    for needle in ('id="detailModal"', 'id="detailTitle"', 'id="detailBody"',
                   'id="detailActions"'):
        assert needle in page, needle
    seg_src = page[page.index("// ===== 任务详情 ====="):
                   page.index("function createTaskCard")]
    for fn in ("function openDetail", "function closeDetail",
               "function renderDetail", "function detailRows", "function detailText",
               "function syncDetailModal"):
        assert fn in seg_src, fn
    # 卡片操作区有「详情」入口
    card = page[page.index("function createTaskCard"):]
    card = card[:card.index("function renderStats")]
    assert 'onclick="openDetail(' in card
    # 刷新时同步（含任务被删后自动关闭）
    assert "syncDetailModal();" in page
    # 详情值一律转义，防文件名/链接注入
    assert "escapeHtml(String(v))" in seg_src


def test_web_keyboard_shortcuts_match_desktop(page):
    assert "function shortcutAction" in page
    assert 'document.addEventListener("keydown", runShortcut);' in page
    # Esc 关弹窗要详情优先于设置；焦点类快捷键必须 preventDefault
    body = page[page.index("function shortcutAction"):
                page.index("function extractDropUrls")]
    assert body.index("detailModal") < body.index("settingsModal")
    assert "preventDefault" in page
    assert '"focus_url"' in page and '"focus_search"' in page


def test_ui_preferences_are_shared_with_the_desktop(page):
    """theme / filter / sort must go through /api/settings, not just localStorage.

    The desktop dialog reads and writes the same keys in the shared config; the
    Web used to keep private localStorage copies, so switching between the two
    UIs lost the theme, filter and sort choices.
    """
    for needle in ("function syncThemeInput(res)", "function syncFilterInput(res)",
                   "function syncSortInput(res)"):
        assert needle in page, needle
    body = page[page.index("async function refreshSettings"):]
    body = body[:body.index("\n}")]
    for call in ("syncThemeInput(res)", "syncFilterInput(res)", "syncSortInput(res)"):
        assert call in body, call
    assert "JSON.stringify({ theme: pref })" in page
    assert "JSON.stringify({ filter: f })" in page
    assert "JSON.stringify({ sort: k })" in page


def test_settings_api_round_trips_ui_preferences(monkeypatch):
    """POST /api/settings persists theme/filter/sort and GET echoes them back."""
    import config

    appmod.app.config["TESTING"] = True
    client = appmod.app.test_client()
    saved = {key: config.get(key) for key in ("theme", "filter", "sort")}
    try:
        resp = client.post("/api/settings", json={"theme": "light",
                                                  "filter": "failed",
                                                  "sort": "speed"})
        assert resp.status_code == 200 and resp.get_json()["success"]
        assert config.get("theme") == "light"
        assert config.get("filter") == "failed"
        assert config.get("sort") == "speed"
        got = client.get("/api/settings").get_json()
        assert (got["theme"], got["filter"], got["sort"]) == ("light", "failed", "speed")
        # unknown values fall back instead of poisoning the shared config
        client.post("/api/settings", json={"theme": "neon", "filter": "nope",
                                           "sort": "chaos"})
        assert config.get("theme") == "auto"
        assert config.get("filter") == "all"
        assert config.get("sort") == "default"
    finally:
        for key, value in saved.items():
            config.set(key, value)


def test_keyboard_focus_is_visible_and_motion_can_be_turned_off(page):
    # 纯键盘用户此前只能靠 border-color 变化判断焦点，弱光下等于没有焦点
    assert ":focus-visible" in page and "outline-offset: 2px" in page
    # 焦点环要压过 .add-bar input / .search-input 的 outline:none，靠同名类兜一手
    for cls in (".btn:focus-visible", ".filter-btn:focus-visible",
                ".search-input:focus-visible", ".sort-select:focus-visible"):
        assert cls in page, cls
    # 系统开启「减弱动态效果」时呼吸动画必须停
    reduced = page[page.index("prefers-reduced-motion"):]
    assert "animation: none" in reduced


def test_icon_only_buttons_have_accessible_names(page):
    # 图标按钮没有文字，不写 aria-label 时读屏软件只能读出空；
    # title 必须留着，鼠标悬浮提示还靠它
    for marker in ('id="themeBtn"', 'onclick="openDownloadDir()"',
                   'onclick="copyAllLinks()"', 'onclick="exportTasksCsv()"',
                   'onclick="addFromClipboard()"', 'id="advToggle"'):
        seg = page[page.index(marker):]
        assert 'aria-label="' in seg[:seg.index(">")], marker
        assert 'title="' in seg[:seg.index(">")], marker


def test_toast_and_modals_are_announced(page):
    # toast 是全部操作反馈的唯一通道，必须进无障碍树
    toast = page[page.index('id="toast"'):]
    tag = toast[:toast.index(">")]
    assert 'role="status"' in tag and 'aria-live="polite"' in tag
    for mid in ("settingsModal", "detailModal"):
        seg = page[page.index('id="%s"' % mid):]
        seg = seg[:seg.index(">")]
        assert 'role="dialog"' in seg and 'aria-modal="true"' in seg, mid
    assert 'aria-labelledby="settingsTitle"' in page and 'id="settingsTitle"' in page


def test_toast_css_rules_survive_inline_bom_damage(page):
    """三种 toast 皮肤都得是真规则，选择器前面不能混进 BOM。

    文件中间掉进的 U+FEFF 会被 CSS 当成选择器的一部分，规则整条静默失效：
    `.toast.info` 曾因此变成死规则，showToast(msg, "info") 渲染出来
    没有任何视觉变化，而肉眼和 git diff 都看不出那一行有问题。
    """
    for kind in ("success", "error", "info"):
        rule = ".toast.%s {" % kind
        assert rule in page, "%s 规则缺失" % rule
        line = [l for l in page.splitlines() if l.startswith(rule)]
        assert len(line) == 1, "%s 规则应当独立成行" % rule
        assert not line[0].startswith("\ufeff"), "%s 选择器前有游离 BOM" % rule
    # showToast 必须真的会把 info 这个类名拼上去，否则规则对了也没人用
    assert 'toast.className = `toast ${type} show`' in page


def test_filters_and_compact_report_pressed_state(page):
    # 筛选/紧凑是开关语义，没有 aria-pressed 时读屏用户听不出当前是开是关
    assert 'role="group"' in page and 'aria-label="按状态筛选"' in page
    for f in ("all", "active", "completed", "failed"):
        assert 'data-filter="%s" aria-pressed=' % f in page, f
    sync = page[page.index("function syncFilterButtons"):page.index("let _lastTasks")]
    assert 'setAttribute("aria-pressed"' in sync
    # 紧凑按钮复用 .filter-btn 类名，选择器不收窄就会被 setFilter 一起抹掉 active
    assert 'querySelectorAll(".filter-btn[data-filter]")' in sync
    assert 'querySelectorAll(".filter-btn")' not in sync
    assert 'setAttribute("aria-pressed"' in page[page.index("function applyCompact"):]


def test_card_progress_and_pick_box_are_labelled(page):
    card = page[page.index("function createTaskCard"):]
    card = card[:card.index("function renderStats")]
    assert 'role="progressbar"' in card and "aria-valuenow" in card
    assert 'class="task-pick-box" aria-label=' in card


def test_escape_html_is_declared_once(page):
    # 同名函数声明两次，后一份静默覆盖前一份，XSS 注释也跟着失效
    assert page.count("function escapeHtml(") == 1
