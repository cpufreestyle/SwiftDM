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


def test_settings_post_persists_rate_limit(monkeypatch):
    from throttle import get_rate, set_rate
    recorded = []
    monkeypatch.setattr(appmod.config, "set", lambda k, v: recorded.append((k, v)))
    appmod.app.config["TESTING"] = True
    resp = appmod.app.test_client().post("/api/settings", json={"rate_limit": 2048})
    assert resp.status_code == 200
    assert resp.get_json()["rate_limit"] == 2048
    assert get_rate() == 2048
    assert ("rate_limit", 2048) in recorded
    set_rate(0)  # 不把限速泄漏到其它测试


def test_settings_get_reports_current_rate(page):
    assert "rate_limit" in appmod.app.test_client().get("/api/settings").get_json()
