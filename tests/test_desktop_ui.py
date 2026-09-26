"""桌面 UI 新增交互的单元测试（拖入链接解析 / 状态分段过滤 / 窗口几何持久化 / 清除确认）。

仅覆盖纯逻辑，不依赖显示设备：通过 QT_QPA_PLATFORM=offscreen + QMimeData 完成；
若运行环境缺少 PyQt6 或离屏平台不可用则自动跳过，避免影响无界面 CI。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import QMimeData, QUrl


@pytest.fixture(scope="module")
def qt_app():
    try:
        from PyQt6.QtWidgets import QApplication
        return QApplication.instance() or QApplication([])
    except Exception as exc:  # 离屏平台缺失等情况
        pytest.skip(f"Qt 应用无法初始化: {exc}")


def _mime(urls=(), text=None):
    m = QMimeData()
    if urls:
        m.setUrls(list(urls))
    if text is not None:
        m.setText(text)
    return m


def test_urls_from_mime_extracts_http_and_magnet(qt_app):
    import main_window as mw
    m = _mime(
        urls=[QUrl("https://a.com/x.zip"), QUrl.fromLocalFile("C:/local.torrent"),
              QUrl("magnet:?xt=urn:btih:ABC")],
        text="http://c.com/y.bin\n magnet:?xt=urn:btih:ZZZ",
    )
    assert mw.MainWindow._urls_from_mime(m) == [
        "https://a.com/x.zip", "magnet:?xt=urn:btih:ABC",
        "http://c.com/y.bin", "magnet:?xt=urn:btih:ZZZ",
    ]


def test_urls_from_mime_ignores_local_only(qt_app):
    import main_window as mw
    m = _mime(urls=[QUrl.fromLocalFile("C:/only.torrent")])
    assert mw.MainWindow._urls_from_mime(m) == []


def test_urls_from_mime_dedupes(qt_app):
    import main_window as mw
    m = _mime(text="https://a.com/x https://a.com/x")
    assert mw.MainWindow._urls_from_mime(m) == ["https://a.com/x"]


@pytest.mark.parametrize("active_filter,status,expected", [
    ("all", "downloading", True),
    ("all", "completed", True),
    ("active", "downloading", True),
    ("active", "pending", True),
    ("active", "paused", True),
    ("active", "completed", False),
    ("completed", "completed", True),
    ("completed", "downloading", False),
    ("failed", "failed", True),
    ("failed", "cancelled", True),
    ("failed", "completed", False),
])
def test_match_filter(qt_app, active_filter, status, expected):
    import main_window as mw
    shim = type("Shim", (), {"_filter": active_filter})()
    assert mw.MainWindow._match_filter(shim, {"status": status}) is expected


def test_keyboard_shortcuts_registered(qt_app):
    import main_window as mw
    from PyQt6.QtWidgets import QWidget, QLineEdit

    class _Host(QWidget):
        def __init__(self):
            super().__init__()
            self._shortcuts = []
            self.url_input = QLineEdit()

        def _add_download(self):  # pragma: no cover - just a slot target
            pass

    host = _Host()
    mw.MainWindow._setup_shortcuts(host)
    seqs = sorted(seq for seq, _ in host._shortcuts)
    assert "Ctrl+N" in seqs and "Ctrl+F" in seqs, seqs


def test_title_and_tray_helpers_surface_activity():
    import main_window as mw
    assert mw._status_title(0, 0, 5) == "SwiftDM - 高速下载管理器"
    active = mw._status_title(2, 1536, 8)
    assert "下载中" in active and "2" in active and "↓" in active
    assert mw._tray_tip(0, 0, 5) == "SwiftDM - 下载管理器"
    assert "下载中 2/8" in mw._tray_tip(2, 1536, 8)



def test_clear_confirm_text_mentions_count_and_consequence():
    import main_window as mw
    text = mw._clear_confirm_text(3)
    assert "3" in text
    assert "不可恢复" in text


def test_task_card_top_row_has_copy_link_button(qt_app):
    """复制链接从右键菜单提升到卡片顶栏（只对有 URL 的任务显示）。"""
    import main_window as mw
    from PyQt6.QtWidgets import QPushButton

    def link_btns(card):
        return [b for b in card.findChildren(QPushButton)
                if b.toolTip() == "复制下载链接"]

    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin", "url": "https://a/x.bin",
                        "status": "downloading", "total_size": 0, "downloaded": 0})
    btns = link_btns(card)
    assert len(btns) == 1
    got = []
    card.action_triggered.connect(lambda a, t: got.append((a, t)))
    btns[0].click()
    assert got == [("copy_link", "t1")]
    # 注册进语义色通道；切主题时回和其他按钮一起重刷
    assert any(key == "textMuted" and btn.toolTip() == "复制下载链接"
               for btn, key in card._semantic_btns)
    card.apply_theme("light")
    assert mw.THEMES["light"]["textMuted"] in btns[0].styleSheet()

    # 没有链接的任务不显示（历史脏数据也不会弹出空按钮）
    assert link_btns(mw.TaskCard({"task_id": "t2", "filename": "y.bin",
                                  "status": "pending"})) == []


def test_auto_retry_hint_and_status_text(qt_app):
    import time

    import main_window as mw
    import re as _re

    assert mw._auto_retry_hint(0) is None
    assert mw._auto_retry_hint(None) is None
    assert mw._auto_retry_hint(time.time() - 5) == "↻ 即将自动重试"
    hint = mw._auto_retry_hint(time.time() + 12)
    assert _re.fullmatch(r"↻ \d+s 后自动重试", hint), hint
    # 失败卡片状态行带倒计时；无计划或其他状态不受影响
    soon = time.time() + 30
    text = mw._card_status_text("failed", None, soon)
    assert text.startswith("✗ 失败  ·  ↻ ")
    assert mw._card_status_text("failed") == "✗ 失败"
    assert mw._card_status_text("failed", None, 0) == "✗ 失败"
    assert mw._card_status_text("downloading", None, soon) == "● 下载中"
    assert mw._card_status_text("pending", 1770000000) == "⏰ 定时等待"


def test_window_geometry_saved_and_restored(qt_app, tmp_path):
    import main_window as mw
    from PyQt6.QtCore import QSettings
    from PyQt6.QtWidgets import QWidget
    s = QSettings(str(tmp_path / "geo_test.ini"), QSettings.Format.IniFormat)
    try:
        # 无记录：不动窗口几何，返回 False
        assert mw._restore_window_geometry(QWidget(), s) is False
        # 脏数据不能炸，也不能污染窗口
        s.setValue("window/geometry", b"not-a-real-geometry")
        assert mw._restore_window_geometry(QWidget(), s) is False
        # 正常往返：保存后能被 Qt 接受并恢复
        w = QWidget()
        w.resize(1024, 768)
        mw._save_window_geometry(w, s)
        assert s.contains("window/geometry")
        assert mw._restore_window_geometry(QWidget(), s) is True
    finally:
        for k in s.allKeys():
            s.remove(k)
        s.sync()


def test_match_search_matches_filename_and_url(qt_app):
    import main_window as mw
    m = mw.MainWindow._match_search
    assert m({"filename": "Ubuntu-24.04.iso", "url": "https://x/y.bin"}, "ubuntu") is True
    assert m({"filename": "a.bin", "url": "https://example.com/patch.zip"}, "patch") is True
    assert m({"filename": "a.bin", "url": "https://example.com"}, "zzz") is False
    assert m({"filename": "a.bin", "url": "https://example.com"}, "") is True
    assert m({"filename": "a.bin", "url": "https://example.com"}, "  ") is True
    assert m({}, "any") is False


def test_parse_and_format_rate_limit_kbps():
    import main_window as mw
    assert mw._parse_rate_kbps("") == 0
    assert mw._parse_rate_kbps("  ") == 0
    assert mw._parse_rate_kbps("0") == 0
    assert mw._parse_rate_kbps("512") == 512 * 1024
    assert mw._parse_rate_kbps("1.5") == 1536
    assert mw._parse_rate_kbps("abc") is None
    assert mw._parse_rate_kbps("-1") is None
    assert mw._format_rate_kbps(0) == ""
    assert mw._format_rate_kbps(None) == ""
    assert mw._format_rate_kbps(512 * 1024) == "512"
    assert mw._format_rate_kbps(1536) == "1"


def test_sorted_task_ids_orders_and_stays_stable():
    import main_window as mw
    tasks = {
        "a": {"filename": "b.iso", "total_size": 10, "downloaded": 5, "speed": 1},
        "b": {"filename": "A.iso", "total_size": 100, "downloaded": 10, "speed": 9},
        "c": {"filename": "c.iso", "total_size": 100, "downloaded": 90, "speed": 0},
    }
    assert mw._sorted_task_ids(tasks, "default") == ["a", "b", "c"]
    assert mw._sorted_task_ids(tasks, "name") == ["b", "a", "c"]      # 大小写不敏感
    assert mw._sorted_task_ids(tasks, "size") == ["b", "c", "a"]     # 倒序，同值按 id
    assert mw._sorted_task_ids(tasks, "progress") == ["c", "a", "b"]
    assert mw._sorted_task_ids(tasks, "speed") == ["b", "a", "c"]


def test_fail_summary_text_single_and_aggregated():
    import main_window as mw

    assert mw._fail_summary_text([]) is None
    one = [{"filename": "a.iso", "error": "连接超时"}]
    assert mw._fail_summary_text(one) == "✗ 下载失败 [a.iso]: 连接超时"
    no_err = [{"filename": "a.iso", "error": ""}]
    assert mw._fail_summary_text(no_err) == "✗ 下载失败: a.iso"

    same = [{"filename": "a", "error": "连接超时"},
            {"filename": "b", "error": "连接超时"}]
    text = mw._fail_summary_text(same)
    assert text == "✗ 2 个任务下载失败（连接超时）"

    mixed = [{"filename": "a", "error": "x"},
             {"filename": "b", "error": "y"},
             {"filename": "c", "error": "z"}]
    text = mw._fail_summary_text(mixed)
    assert text.startswith("✗ 3 个任务下载失败")
    assert "x、y" in text and text.endswith("等）")

    assert mw._fail_summary_text([{"filename": "a"}, {"filename": "b"}]) == \
        "✗ 2 个任务下载失败"


def test_finish_countdown_text_hidden_and_labeled():
    import main_window as mw

    assert mw._finish_countdown_text("none", 30) is None
    assert mw._finish_countdown_text(None, 30) is None
    assert mw._finish_countdown_text("", 30) is None
    assert mw._finish_countdown_text("shutdown", 0) is None
    assert mw._finish_countdown_text("shutdown", -5) is None
    assert mw._finish_countdown_text("shutdown", 42) == "42s 后关机"
    assert mw._finish_countdown_text("suspend", 7) == "7s 后睡眠"
    assert mw._finish_countdown_text("beep", 1) == "1s 后提示音"
    assert mw._finish_countdown_text("mystery", 3) == "3s 后mystery"


def test_finish_countdown_text_tolerates_junk_remaining():
    import main_window as mw

    assert mw._finish_countdown_text("shutdown", None) is None
    assert mw._finish_countdown_text("shutdown", "abc") is None


def test_links_text_dedupes_and_skips_blank():
    import main_window as mw

    assert mw._links_text(["a", " b ", "a", "", None]) == "a\nb"
    assert mw._links_text([]) == ""
    assert mw._links_text(["only"]) == "only"


def test_tasks_export_text_has_header_and_rows_in_order():
    import main_window as mw

    tasks = {
        "t1": {"filename": "a.iso", "url": "http://x/a", "status": "completed",
               "total_size": 10, "downloaded": 10, "speed": 0},
        "t2": {"filename": "b.iso", "url": "http://x/b", "status": "downloading",
               "total_size": 20, "downloaded": 5, "speed": 1024},
    }
    lines = mw._tasks_export_text(tasks, ["t2", "t1"]).strip().splitlines()
    assert lines[0] == "文件名,链接,状态,总大小,已下载,速度"
    assert "b.iso" in lines[1] and "downloading" in lines[1]
    assert "a.iso" in lines[2] and "completed" in lines[2]
    assert mw._tasks_export_text({}, []).strip() == lines[0]


def test_tray_icon_state_priority():
    import main_window as mw

    assert mw._tray_icon_state(0, 0) == "idle"
    assert mw._tray_icon_state(3, 0) == "downloading"
    assert mw._tray_icon_state(3, 5) == "downloading"   # 下载中优先于有失败
    assert mw._tray_icon_state(0, 2) == "attention"
    assert mw._tray_icon_state(0, 0) == "idle"


def test_tray_icon_renders_every_state(qt_app):
    import main_window as mw

    for state in mw.TRAY_ICON_STATES:
        icon = mw._tray_icon(state)
        assert not icon.isNull()


def test_card_height_bounds_for_compact_and_default():
    import main_window as mw

    assert mw._card_height_bounds(False) == (120, 140)
    assert mw._card_height_bounds(True) == (64, 64)


def test_task_card_compact_hides_secondary_info(qt_app):
    import main_window as mw

    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin",
                        "status": "downloading", "total_size": 100,
                        "downloaded": 50, "speed": 1024, "eta": "00:10"})
    assert card.minimumHeight() == 120 and card.maximumHeight() == 140
    card.set_compact(True)
    assert card.minimumHeight() == 64 and card.maximumHeight() == 64
    assert card.size_label.isHidden() and card.eta_label.isHidden()
    card.set_compact(False)
    assert card.maximumHeight() == 140
    assert not card.size_label.isHidden() and not card.eta_label.isHidden()


def test_task_card_compact_keeps_error_in_tooltip(qt_app):
    import main_window as mw

    card = mw.TaskCard({"task_id": "t2", "filename": "y.bin",
                        "status": "failed", "error": "连接超时",
                        "total_size": 10, "downloaded": 0, "speed": 0})
    card.set_compact(True)
    assert card.error_label.isHidden()          # 不占高度
    assert "连接超时" in card.toolTip()   # 原因放工具提示
    card.set_compact(False)
    assert not card.error_label.isHidden()
    assert card.toolTip() == ""


def test_settings_dialog_groups_cover_every_row(qt_app):
    import main_window as mw
    from PyQt6.QtWidgets import QGroupBox, QLabel

    dlg = mw.SettingsDialog()
    groups = dlg.findChildren(QGroupBox)
    assert [g.title() for g in groups] == ["下载", "网络", "完成后", "外观"]

    def row_labels(group):
        return {lbl.text() for lbl in group.findChildren(QLabel)
                if lbl.text().endswith(":")}

    by_title = {g.title(): row_labels(g) for g in groups}
    assert by_title["下载"] == {"下载目录:", "下载线程数:", "下载限速:", "失败自动重试:"}
    assert by_title["网络"] == {"浏览器监控:", "下载代理:", "自定义代理:"}
    assert by_title["完成后"] == {"全部下载完成后:", "完成提示音:"}
    assert by_title["外观"] == {"界面主题:"}


def test_settings_dialog_get_settings_still_complete(qt_app):
    import main_window as mw

    dlg = mw.SettingsDialog()
    settings = dlg.get_settings()
    for key in ("dir", "segments", "monitor", "proxy_mode",
                "rate_limit", "auto_retry", "finish_action", "notify_sound", "theme"):
        assert key in settings, key


def test_step_selection_moves_clamps_and_handles_unknown():
    import main_window as mw

    ids = ["a", "b", "c"]
    assert mw._step_selection(ids, None, 1) == "a"
    assert mw._step_selection(ids, None, -1) == "c"
    assert mw._step_selection(ids, "a", 1) == "b"
    assert mw._step_selection(ids, "b", -1) == "a"
    assert mw._step_selection(ids, "a", -1) == "a"      # 到头钳制，不循环
    assert mw._step_selection(ids, "c", 1) == "c"
    assert mw._step_selection(ids, "gone", 1) == "a"    # 当前项不在列表
    assert mw._step_selection([], None, 1) is None


def test_keyboard_nav_defers_to_input_widgets(qt_app):
    import main_window as mw
    from PyQt6.QtWidgets import QLineEdit, QComboBox, QSpinBox, QLabel

    assert mw._keyboard_nav_allowed(None) is True
    assert mw._keyboard_nav_allowed(QLabel()) is True
    assert mw._keyboard_nav_allowed(QLineEdit()) is False
    assert mw._keyboard_nav_allowed(QComboBox()) is False
    assert mw._keyboard_nav_allowed(QSpinBox()) is False


def test_task_card_selection_property_and_click_signal(qt_app):
    import main_window as mw
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import QPointF, Qt

    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin",
                        "status": "completed", "total_size": 1,
                        "downloaded": 1})
    assert card.property("selected") in (None, False)
    card.set_selected(True)
    assert card.property("selected") is True
    card.set_selected(False)
    assert card.property("selected") is False

    got = []
    card.selected.connect(lambda tid: got.append(tid))
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(2, 2),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    card.mousePressEvent(ev)
    assert got == ["t1"]


def test_settings_dialog_exposes_finish_action(qt_app):
    import main_window as mw
    import notify_sound

    dlg = mw.SettingsDialog()
    labels = [dlg.finish_combo.itemText(i) for i in range(dlg.finish_combo.count())]
    data = [dlg.finish_combo.itemData(i) for i in range(dlg.finish_combo.count())]
    assert labels == ["无动作", "关机", "睡眠", "提示音"]
    assert data == ["none", "shutdown", "suspend", "beep"]
    settings = dlg.get_settings()
    assert settings["finish_action"] in mw.FINISH_ACTIONS
    sound_labels = [dlg.sound_combo.itemText(i)
                    for i in range(dlg.sound_combo.count())]
    sound_data = [dlg.sound_combo.itemData(i)
                   for i in range(dlg.sound_combo.count())]
    assert sound_labels == [notify_sound.NOTIFY_SOUND_LABELS[k]
                           for k in notify_sound.NOTIFY_SOUNDS]
    assert sound_data == list(notify_sound.NOTIFY_SOUNDS)
    assert settings["notify_sound"] in notify_sound.NOTIFY_SOUNDS
    notify_sound.set_sound("none")


def test_pause_all_and_resume_all_only_touch_active_tasks(qt_app):
    import main_window as mw

    class _Task:
        def __init__(self, status):
            self.status = status
            self.paused = False
            self.resumed = False

        def pause(self):
            self.paused = True
            self.status = "paused"

        def resume(self):
            self.resumed = True
            self.status = "downloading"

    class _Mgr:
        def __init__(self, tasks):
            self.tasks = tasks
            self.saved = 0

        def get_all_tasks(self):
            return self.tasks

        def save_history(self):
            self.saved += 1

    class _Bar:
        def __init__(self):
            self.messages = []

        def showMessage(self, text, timeout=0):
            self.messages.append(text)

    class _Host:
        def __init__(self, mgr):
            self._mgr = mgr
            self.status_bar = _Bar()

        def _get_manager(self):
            return self._mgr

    tasks = [_Task("downloading"), _Task("paused"), _Task("completed")]
    mgr = _Mgr(tasks)
    host = _Host(mgr)
    mw.MainWindow._pause_all(host)
    assert tasks[0].paused and tasks[0].status == "paused"
    assert not tasks[1].paused and not tasks[2].paused
    assert mgr.saved == 1
    assert any("暂停" in m for m in host.status_bar.messages)

    host.status_bar.messages.clear()
    mw.MainWindow._resume_all(host)
    assert tasks[0].resumed and tasks[1].resumed
    assert not tasks[2].resumed
    assert mgr.saved == 2
    assert any("恢复" in m for m in host.status_bar.messages)


def test_notify_failures_aggregates_status_and_tray(qt_app):
    import main_window as mw
    from PyQt6.QtWidgets import QSystemTrayIcon

    class _Bar:
        def __init__(self):
            self.messages = []

        def showMessage(self, text, timeout=0):
            self.messages.append(text)

    class _Tray:
        def __init__(self):
            self.messages = []

        def showMessage(self, title, body, icon, timeout):
            self.messages.append((title, body, icon, timeout))

    class _Host:
        def __init__(self):
            self.status_bar = _Bar()
            self.tray = _Tray()

    host = _Host()
    items = [
        {"task_id": "a", "filename": "a.bin", "status": "failed",
         "error": "连接超时"},
        {"task_id": "b", "filename": "b.bin", "status": "failed",
         "error": "连接超时"},
    ]
    mw.MainWindow._notify_failures(host, items)
    expected = "✗ 2 个任务下载失败（连接超时）"
    assert host.status_bar.messages == [expected]
    title, body, icon, timeout = host.tray.messages[0]
    assert title == "SwiftDM"
    assert body == expected
    assert icon == QSystemTrayIcon.MessageIcon.Warning
    assert timeout >= 4000


def test_tray_tip_surfaces_failures_and_countdown():
    import main_window as mw
    # 无附加信息时与旧行为完全一致（不破捯其他断言）
    assert mw._tray_tip(0, 0, 5) == "SwiftDM - 下载管理器"
    assert "下载中 2/8" in mw._tray_tip(2, 1536, 8)
    # 空闲 + 失败：tooltip 必须能看到失败数（图标只是红点）
    tip = mw._tray_tip(0, 0, 5, failed=3)
    assert "✗ 3 个失败" in tip
    # 下载中 + 失败 + 倒计时：三段并列
    tip2 = mw._tray_tip(2, 1536, 8, failed=1, countdown="12s 后关机")
    assert "下载中 2/8" in tip2 and "✗ 1 个失败" in tip2
    assert tip2.endswith("· 12s 后关机")
    # countdown 为 None/空时不会留導助分隔符
    assert mw._tray_tip(0, 0, 5, countdown=None) == "SwiftDM - 下载管理器"


def test_failed_card_shows_actionable_reason_hint(qt_app):
    import main_window as mw
    card = mw.TaskCard({"task_id": "t1", "filename": "x.mp4",
                        "status": "failed", "total_size": 0,
                        "downloaded": 0, "error": "ffmpeg not found",
                        "error_reason": "needs_ffmpeg"})
    # 原始错误 + 可操作提示（与 Web 端同源）都要出现
    assert "ffmpeg not found" in card.error_label.text()
    assert "PATH" in card.error_label.text()
    # 紧凑模式：提示进 tooltip，不占卡片高度
    card.set_compact(True)
    assert not card.error_label.isVisible()
    assert "ffmpeg not found" in card.toolTip()
    assert "PATH" in card.toolTip()
    # 无原因码时不应出现提示行
    card2 = mw.TaskCard({"task_id": "t2", "filename": "y.bin",
                         "status": "failed", "total_size": 0,
                         "downloaded": 0, "error": "HTTP 404"})
    assert card2.error_label.text() == "⚠ 失败原因: HTTP 404"


def test_card_status_text_marks_scheduled_pending():
    import main_window as mw
    assert mw._card_status_text("pending") == "⏳ 等待中"
    assert mw._card_status_text("pending", 1770000000) == "⏰ 定时等待"
    assert mw._card_status_text("downloading", 1770000000) == "● 下载中"
    assert mw._card_status_text("completed", None) == "✓ 完成"
    assert mw._card_status_text("weird") == "weird"


def test_scheduled_suffix_formats_local_time():
    import main_window as mw
    assert mw._scheduled_suffix(None) == ""
    assert mw._scheduled_suffix(0) == ""
    text = mw._scheduled_suffix(1770000000)
    assert text.startswith("  ⏰ ") and text.endswith(" 开始"), text


def test_task_card_marks_scheduled_pending(qt_app):
    import main_window as mw
    card = mw.TaskCard({"task_id": "s1", "filename": "later.bin",
                        "status": "pending", "total_size": 0, "downloaded": 0,
                        "scheduled_at": 1770000000})
    assert card.status_label.text() == "⏰ 定时等待"
    assert "⏰" in card.size_label.text()
    # 非定时的 pending 不加标记
    plain = mw.TaskCard({"task_id": "s2", "filename": "now.bin",
                         "status": "pending", "total_size": 0, "downloaded": 0})
    assert plain.status_label.text() == "⏳ 等待中"
    assert "⏰" not in plain.size_label.text()
    # 更新路径同样生效
    card.update_data({"task_id": "s1", "filename": "later.bin",
                      "status": "pending", "total_size": 0, "downloaded": 0,
                      "scheduled_at": None})
    assert card.status_label.text() == "⏳ 等待中"
    assert "⏰" not in card.size_label.text()


def test_add_dialog_exposes_schedule_option(qt_app):
    import main_window as mw
    from PyQt6.QtCore import QDateTime
    dlg = mw.AddDialog()
    # 默认不定时：get_data 不带 start_at（立即下载）
    dlg.url_edit.setText("https://example.com/x.bin")
    assert dlg.get_data()["start_at"] is None
    assert not dlg.sched_time.isEnabled()
    # 勾选后可编辑，默认十分钟后
    dlg.sched_check.setChecked(True)
    assert dlg.sched_time.isEnabled()
    when = dlg.sched_time.dateTime()
    assert when.toSecsSinceEpoch() > int(__import__("time").time()) + 60
    data = dlg.get_data()
    assert data["start_at"] == float(when.toSecsSinceEpoch())
    # 未来时间才算定时；过去时间视为立即下载
    dlg.sched_time.setDateTime(QDateTime.currentDateTime().addSecs(-60))
    assert dlg.get_data()["start_at"] is None


def test_theme_tokens_cover_same_keys(qt_app):
    import main_window as mw
    assert set(mw.THEMES) == {"dark", "light"}
    assert set(mw.THEMES["dark"]) == set(mw.THEMES["light"])
    # 两套主题不能完全同色，否则切换没有意义
    assert mw.THEMES["dark"] != mw.THEMES["light"]


def test_qss_for_theme_renders_every_token(qt_app):
    import main_window as mw
    for theme, tokens in mw.THEMES.items():
        qss = mw._qss_for(theme)
        assert "$" not in qss, f"{theme} 有未解析占位符"
        for key in ("bg", "surface", "border", "text", "accent"):
            assert tokens[key] in qss, (theme, key)
    # 页面底色只铺窗口/中央区：通用 QWidget 规则不能带背景色，
    # 否则浅色主题下每个标签/按钮盒都会被刷成灰块
    for theme, tokens in mw.THEMES.items():
        qss = mw._qss_for(theme)
        assert "QWidget#appCentral" in qss
        widget_rule = qss.split("QWidget {", 1)[1].split("}", 1)[0]
        assert "background" not in widget_rule, theme
    # 未知主题回退暗色，不会渲染出空样式表
    assert mw._qss_for("neon") == mw._qss_for("dark")


def test_apply_theme_switches_stylesheet_and_cards(qt_app):
    import main_window as mw

    class _Card:
        def __init__(self):
            self.seen = []

        def apply_theme(self, theme):
            self.seen.append(theme)

    class _Host:
        def __init__(self):
            self._cards = {"t1": _Card()}
            self.sheet = None

        def setStyleSheet(self, sheet):
            self.sheet = sheet

    host = _Host()
    mw.MainWindow._apply_theme(host, "light")
    assert host._theme == "light"
    assert host._cards["t1"].seen == ["light"]
    assert mw.THEMES["light"]["bg"] in host.sheet
    # 非法主题安全回退暗色
    mw.MainWindow._apply_theme(host, "bogus")
    assert host._theme == "dark"
    assert host._cards["t1"].seen == ["light", "dark"]
    assert mw.THEMES["dark"]["bg"] in host.sheet


def test_task_card_restyles_on_theme_switch(qt_app):
    import main_window as mw
    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin", "status": "downloading",
                        "total_size": 1024, "downloaded": 256, "speed": 2048},
                       theme="dark")
    assert mw.THEMES["dark"]["surface"] in card.styleSheet()
    assert mw.THEMES["dark"]["textMuted"] in card.size_label.styleSheet()

    card.apply_theme("light")
    light = mw.THEMES["light"]
    assert light["surface"] in card.styleSheet()
    assert light["textStrong"] in card.name_label.styleSheet()
    assert light["textMuted"] in card.size_label.styleSheet()
    assert light["accent2"] in card.speed_label.styleSheet()
    assert light["accent"] in card.progress_bar.styleSheet()
    assert light["accent2"] in card.status_label.styleSheet()
    # 操作按钮（下载中=暂停/取消）也按键重刷
    assert card._semantic_btns
    assert all(light[key] in btn.styleSheet() for btn, key in card._semantic_btns)
    # 非法主题被忽略，不影响当前外观
    card.apply_theme("neon")
    assert card.styleSheet() == card._card_qss()

    # 失败卡片：原因条用浅色主题的「深字浅底」
    failed = mw.TaskCard({"task_id": "t2", "filename": "y.bin", "status": "failed",
                          "total_size": 0, "downloaded": 0, "error": "boom"},
                         theme="light")
    assert light["redText"] in failed.error_label.styleSheet()
    assert light["redSoft"] in failed.error_label.styleSheet()
    failed.apply_theme("dark")
    assert mw.THEMES["dark"]["redText"] in failed.error_label.styleSheet()


def test_settings_dialog_theme_option_and_preview(qt_app):
    import main_window as mw

    dlg = mw.SettingsDialog()
    assert dlg.theme_combo.count() == 2
    assert [dlg.theme_combo.itemData(i) for i in range(2)] == ["dark", "light"]
    assert dlg.get_settings()["theme"] in ("dark", "light")
    dlg.theme_combo.setCurrentIndex(1)  # 触发即时预览
    assert dlg._theme == "light"
    assert mw.THEMES["light"]["surface"] in dlg.styleSheet()
    dlg.apply_theme("bogus")  # 忽略非法主题
    assert dlg._theme == "light"


def test_add_dialog_follows_theme(qt_app):
    import main_window as mw
    dlg = mw.AddDialog(theme="light")
    assert mw.THEMES["light"]["surface"] in dlg.styleSheet()
    assert dlg.get_data()["start_at"] is None


def test_torrent_paths_from_mime_collects_local_seeds(qt_app):
    import main_window as mw
    m = _mime(urls=[QUrl.fromLocalFile("D:/dl/a.torrent"),
                    QUrl.fromLocalFile("D:/dl/notes.txt"),
                    QUrl("https://site/b.torrent")],
              text="https://site/c.zip")
    assert mw.MainWindow._torrent_paths_from_mime(m) == ["D:/dl/a.torrent"]
    # 大小写不敏感（Windows 上常见 .TORRENT）
    m2 = _mime(urls=[QUrl.fromLocalFile("D:/dl/D.TORRENT")])
    assert mw.MainWindow._torrent_paths_from_mime(m2) == ["D:/dl/D.TORRENT"]
    # 没有本地种子时为空
    assert mw.MainWindow._torrent_paths_from_mime(_mime(text="magnet:?xt=urn:btih:ABC")) == []
    # http 的 .torrent 仍走链接通道，不会重复计一次
    m3 = _mime(urls=[QUrl("https://site/b.torrent")])
    assert mw.MainWindow._torrent_paths_from_mime(m3) == []
    assert mw.MainWindow._urls_from_mime(m3) == ["https://site/b.torrent"]


def test_drop_event_adds_local_torrent_files(qt_app):
    """拖入「链接 + 本地种子」两种内容都要建成任务。"""
    import main_window as mw

    class _Host:
        # 复用真实静态方法：_urls_from_mime / _torrent_paths_from_mime
        _urls_from_mime = staticmethod(mw.MainWindow._urls_from_mime)
        _torrent_paths_from_mime = staticmethod(mw.MainWindow._torrent_paths_from_mime)

        def __init__(self):
            self.created = []

        def _create_and_start(self, url, **kw):
            self.created.append(url)

        class logger:
            @staticmethod
            def exception(msg):
                pass

        class status_bar:
            @staticmethod
            def showMessage(msg, timeout=0):
                pass

    class _Event:
        def __init__(self, mime):
            self._mime = mime
            self.accepted = False

        def mimeData(self):
            return self._mime

        def acceptProposedAction(self):
            self.accepted = True

    host = _Host()
    mime = _mime(urls=[QUrl("https://site/a.zip"),
                       QUrl.fromLocalFile("D:/dl/seed.torrent")])
    mw.MainWindow.dropEvent(host, _Event(mime))
    assert host.created == ["https://site/a.zip", "D:/dl/seed.torrent"]

    # 只有种子文件时也能用
    host2 = _Host()
    mw.MainWindow.dropEvent(host2, _Event(_mime(urls=[QUrl.fromLocalFile("D:/dl/only.torrent")])))
    assert host2.created == ["D:/dl/only.torrent"]
    # 「什么都没有」的分支会走 super().dropEvent()，需要真实 QWidget，
    # 这里只保证拖入普通文本/非种子文件时两个提取函数都返回空（交给默认行为）
    plain = _mime(urls=[QUrl.fromLocalFile("D:/dl/x.txt")], text="hello")
    assert mw.MainWindow._urls_from_mime(plain) == []
    assert mw.MainWindow._torrent_paths_from_mime(plain) == []
def _open_folder_button(card):
    from PyQt6.QtWidgets import QPushButton
    for btn in card.findChildren(QPushButton):
        if btn.text().endswith("\u6253\u5f00\u6587\u4ef6\u5939"):
            return btn
    return None


@pytest.mark.parametrize("status", ["downloading", "paused", "failed", "cancelled"])
def test_task_card_has_open_folder_button_in_active_states(qt_app, status):
    """\u4e0b\u8f7d\u4e2d/\u6682\u505c/\u5931\u8d25/\u53d6\u6d88\u90fd\u53ef\u4e00\u952e\u5b9a\u4f4d\u76ee\u5f55\uff0c\u65b9\u4fbf\u67e5\u770b\u5206\u7247\u6b8f\u7559\u3002"""
    import main_window as mw

    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin", "status": status,
                        "total_size": 100, "downloaded": 3, "speed": 0})
    btn = _open_folder_button(card)
    assert btn is not None, status
    got = []
    card.action_triggered.connect(lambda action, tid: got.append((action, tid)))
    btn.click()
    assert got == [("open_folder", "t1")]


def test_pending_card_has_no_open_folder_button(qt_app):
    import main_window as mw
    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin", "status": "pending"})
    assert _open_folder_button(card) is None


def test_completed_card_offers_exactly_one_open_folder(qt_app):
    """completed \u5206\u652f\u81ea\u5e26\u201c\u6253\u5f00\u6587\u4ef6\u5939\u201d\uff0c\u4e0d\u80fd\u88ab\u901a\u7528\u5206\u652f\u91cd\u590d\u6e32\u67d3\u3002"""
    import main_window as mw
    from PyQt6.QtWidgets import QPushButton

    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin",
                        "status": "completed", "total_size": 10, "downloaded": 10})
    texts = [b.text() for b in card.findChildren(QPushButton)
             if b.text().endswith("\u6253\u5f00\u6587\u4ef6\u5939")]
    assert texts == ["\U0001f5c1 \u6253\u5f00\u6587\u4ef6\u5939"]


def test_context_menu_open_folder_entry_and_action(qt_app, monkeypatch):
    import main_window as mw

    created = []

    class _FakeAction:
        def __init__(self, text, slot=None):
            self.text = text
            self._slot = slot

        def trigger(self):
            if self._slot:
                self._slot()

    class FakeMenu:
        def __init__(self, parent=None):
            self.actions = []
            created.append(self)

        def addAction(self, text, slot=None):
            act = _FakeAction(text, slot)
            self.actions.append(act)
            return act

        def addSeparator(self):
            self.actions.append(_FakeAction("---sep---"))

        def exec(self, pos=None):
            pass

    monkeypatch.setattr(mw, "QMenu", FakeMenu)

    class _Ev:
        def globalPos(self):
            return None

    def menu_for(status):
        card = mw.TaskCard({"task_id": "t1", "filename": "x.bin",
                            "url": "https://site/x.bin", "status": status})
        emitted = []
        card.action_triggered.connect(lambda a, t: emitted.append((a, t)))
        card.contextMenuEvent(_Ev())
        texts = [a.text for a in created[-1].actions]
        folder_acts = [a for a in created[-1].actions if a.text ==
                       "\u6253\u5f00\u6587\u4ef6\u5939"]
        return texts, folder_acts, emitted

    for status in ("downloading", "paused", "failed", "cancelled"):
        texts, folder_acts, emitted = menu_for(status)
        assert "\u6253\u5f00\u6587\u4ef6\u5939" in texts, (status, texts)
        assert len(folder_acts) == 1, (status, texts)
        folder_acts[0].trigger()
        assert emitted == [("open_folder", "t1")]

    texts, folder_acts, _ = menu_for("pending")
    assert "\u6253\u5f00\u6587\u4ef6\u5939" not in texts, texts


def test_handle_action_open_folder_reports_missing_dir(qt_app, tmp_path, monkeypatch):
    import main_window as mw

    opened = []
    monkeypatch.setattr(mw, "open_in_system", lambda p: opened.append(p))

    class _Task:
        def __init__(self, filepath):
            self.filepath = filepath

    class _Mgr:
        def __init__(self, task):
            self._task = task
            self.saved = 0

        def get_task(self, tid):
            return self._task

        def save_history(self):
            self.saved += 1

    class _Bar:
        def __init__(self):
            self.messages = []

        def showMessage(self, text, timeout=0):
            self.messages.append(text)

    class _Host:
        def __init__(self, mgr):
            self._mgr = mgr
            self.status_bar = _Bar()

        def _get_manager(self):
            return self._mgr

    good = tmp_path / "a.bin"
    good.write_bytes(b"x")

    host = _Host(_Mgr(_Task(str(good))))
    mw.MainWindow._handle_action(host, "open_folder", "t1")
    assert opened == [str(tmp_path)]
    assert any(str(tmp_path) in m for m in host.status_bar.messages)

    missing = str(tmp_path / "gone" / "a.bin")
    host2 = _Host(_Mgr(_Task(missing)))
    mw.MainWindow._handle_action(host2, "open_folder", "t1")
    assert any("\u4e0d\u5b58\u5728" in m for m in host2.status_bar.messages)
    assert opened == [str(tmp_path)]

    host3 = _Host(_Mgr(_Task("")))
    mw.MainWindow._handle_action(host3, "open_folder", "t1")
    assert any("\u672a\u77e5\u6587\u4ef6\u8def\u5f84" in m for m in host3.status_bar.messages)
    assert host3._mgr.saved == 1
