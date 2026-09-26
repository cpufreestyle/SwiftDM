import re

import pytest

import app as appmod
import config
import notify_sound


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


def test_settings_post_persists_finish_action(monkeypatch):
    recorded = []
    monkeypatch.setattr(appmod.config, "set", lambda k, v: recorded.append((k, v)))
    appmod.app.config["TESTING"] = True
    resp = appmod.app.test_client().post("/api/settings",
                                        json={"finish_action": "shutdown"})
    assert resp.status_code == 200
    assert resp.get_json()["finish_action"] == "shutdown"
    assert ("finish_action", "shutdown") in recorded
    appmod.scheduler.cancel_finish_action()  # 复位，避免影响其他用例


def test_settings_post_coerces_bogus_finish_action(monkeypatch):
    recorded = []
    monkeypatch.setattr(appmod.config, "set", lambda k, v: recorded.append((k, v)))
    appmod.app.config["TESTING"] = True
    resp = appmod.app.test_client().post("/api/settings",
                                        json={"finish_action": "does-not-exist"})
    assert resp.get_json()["finish_action"] == "none"
    assert ("finish_action", "none") in recorded
    appmod.scheduler.cancel_finish_action()


def test_settings_get_reports_current_rate(page):
    assert "rate_limit" in appmod.app.test_client().get("/api/settings").get_json()


def test_settings_get_reports_retry_sound_and_capture(page):
    """桌面端早就支持的三个设置，GET 也得下发可选范围。"""
    from downloader import AUTO_RETRY_MAX
    body = appmod.app.test_client().get("/api/settings").get_json()
    assert body["auto_retry_max"] == AUTO_RETRY_MAX
    assert body["notify_sounds"] == notify_sound.NOTIFY_SOUND_LABELS
    for key in ("auto_retry", "notify_sound", "browser_capture"):
        assert key in body, key


def test_web_exposes_the_three_desktop_only_settings(page):
    """后端接受的键 Web 端必须有入口，否则 HEADLESS 时只能手改配置文件。"""
    for needle in ('id="autoRetrySelect"', 'id="soundSelect"', 'id="captureSelect"'):
        assert needle in page, needle
    for fn in ("applyAutoRetry", "applySound", "applyCapture"):
        assert "async function %s()" % fn in page, fn
    for key in ("auto_retry:", "notify_sound:", "browser_capture:"):
        assert key in page, key
    # 档位由后端上限生成，不写死范围
    assert "res.auto_retry_max" in page and "res.notify_sounds" in page


def test_retry_options_come_from_the_backend_limit(page):
    panel = page[page.index('id="autoRetrySelect"'):]
    panel = panel[:panel.index("</select>")]
    assert "<option" not in panel, "失败重试档位应由 syncAutoRetryOptions 生成"


def test_auto_retry_post_persists_and_clamps():
    """超出上限/乱码要夹住，而 0 是合法值（关闭）不能被当成“没填”。"""
    appmod.app.config["TESTING"] = True
    saved = config.get("auto_retry")
    try:
        for value, expected in ((0, 0), (3, 3), (99, appmod.AUTO_RETRY_MAX), ("x", 0)):
            resp = appmod.app.test_client().post("/api/settings",
                                                json={"auto_retry": value})
            assert resp.status_code == 200
            assert resp.get_json()["auto_retry"] == expected, value
            assert config.get("auto_retry") == expected, value
    finally:
        config.set("auto_retry", saved)


def test_notify_sound_post_persists_and_coerces(monkeypatch):
    recorded = []
    monkeypatch.setattr(appmod.config, "set", lambda k, v: recorded.append((k, v)))
    appmod.app.config["TESTING"] = True
    saved = notify_sound.get_sound()
    try:
        resp = appmod.app.test_client().post("/api/settings",
                                            json={"notify_sound": "beep"})
        assert resp.get_json()["notify_sound"] == "beep"
        assert ("notify_sound", "beep") in recorded
        resp = appmod.app.test_client().post("/api/settings",
                                            json={"notify_sound": "loud"})
        assert resp.get_json()["notify_sound"] == "none"
        assert ("notify_sound", "none") in recorded
    finally:
        notify_sound.set_sound(saved)


def test_browser_capture_post_toggles_the_shared_flag(monkeypatch):
    saved = (appmod.BROWSER_CAPTURE_ENABLED, config.get("monitor_enabled"))
    try:
        resp = appmod.app.test_client().post("/api/settings",
                                            json={"browser_capture": False})
        assert resp.status_code == 200
        assert resp.get_json()["browser_capture"] is False
        assert appmod.BROWSER_CAPTURE_ENABLED is False
        assert config.get("monitor_enabled") is False
        resp = appmod.app.test_client().post("/api/settings",
                                            json={"browser_capture": True})
        assert resp.get_json()["browser_capture"] is True
        assert appmod.BROWSER_CAPTURE_ENABLED is True
        assert config.get("monitor_enabled") is True
    finally:
        appmod.BROWSER_CAPTURE_ENABLED = saved[0]
        config.set("monitor_enabled", saved[1])


def test_browser_capture_route_uses_the_configured_thread_count(monkeypatch):
    """扩展推送任务也得跟设置面板走；之前这里写死 8。"""
    saved = (appmod.BROWSER_CAPTURE_ENABLED, config.get("segments"))
    created = []

    class _Stop(Exception):
        pass

    class _Mgr:
        def get_all_tasks(self):
            return []

        def create_task(self, *args, **kwargs):
            created.append(args[3])
            raise _Stop()

    monkeypatch.setattr(appmod, "manager", _Mgr())
    try:
        for value, expected in ((6, 6), (99, appmod.config.SEGMENTS_MAX), (None, 8)):
            config.set("segments", value)
            created.clear()
            try:
                appmod.app.test_client().post(
                    "/api/browser-capture", json={"url": "http://127.0.0.1:1/x.bin"})
            except _Stop:
                pass
            assert created == [expected], (value, created)
    finally:
        appmod.BROWSER_CAPTURE_ENABLED = saved[0]
        config.set("segments", saved[1])


def test_compact_mode_is_shared_with_the_desktop(page):
    """紧凑模式之前只存 localStorage，桌面端读写共享配置，两边各记一份。"""
    assert 'JSON.stringify({ compact: compactMode })' in page
    assert "function syncCompactInput" in page
    assert "syncCompactInput(res);" in page
    refresh = page[page.index("function syncCompactInput"):]
    refresh = refresh[:refresh.index("\n}")]
    assert "localStorage" in refresh, "本地缓存要同步，否则刷新前会回退"


def test_compact_post_persists_and_round_trips():
    appmod.app.config["TESTING"] = True
    saved = config.get("compact")
    try:
        for value in (True, False):
            resp = appmod.app.test_client().post("/api/settings",
                                                json={"compact": value})
            assert resp.status_code == 200
            assert resp.get_json()["compact"] is value
            assert config.get("compact") is value
        assert appmod.app.test_client().get("/api/settings").get_json()["compact"] is False
    finally:
        config.set("compact", saved)
