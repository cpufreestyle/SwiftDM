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


def test_web_has_compact_mode_toggle(page):
    assert 'onclick="toggleCompact()"' in page
    assert "localStorage.getItem(\"swiftdm.compact\")" in page
    assert "localStorage.setItem(\"swiftdm.compact\"" in page
    assert ".task-list.compact .task-meta { display: none; }" in page
    body = page[page.index("function applyCompact"):]
    body = body[:body.index("\n}")]
    assert 'classList.toggle("compact"' in body


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
    body = page[page.index("function renderTasks"):][:1600]
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
    # 除白色（用于带色背景的按钮/标频）外，SS 不应再出现硬编码颜色
    css = page[:page.index("</style>")]
    css = css[css.index("<style>"):]
    css = re.sub(r':root(\[data-theme="light"\])? \{[\s\S]*?\}', '', css)
    css = re.sub(r"/\*[\s\S]*?\*/", "", css)
    leftovers = [c for c in re.findall(r"#[0-9a-fA-F]{3,6}\b", css) if c != "#fff"]
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
