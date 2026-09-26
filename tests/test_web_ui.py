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

