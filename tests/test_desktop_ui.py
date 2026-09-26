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
    assert [g.title() for g in groups] == ["下载", "网络", "完成后"]

    def row_labels(group):
        return {lbl.text() for lbl in group.findChildren(QLabel)
                if lbl.text().endswith(":")}

    by_title = {g.title(): row_labels(g) for g in groups}
    assert by_title["下载"] == {"下载目录:", "下载线程数:", "下载限速:"}
    assert by_title["网络"] == {"浏览器监控:", "下载代理:", "自定义代理:"}
    assert by_title["完成后"] == {"全部下载完成后:", "完成提示音:"}


def test_settings_dialog_get_settings_still_complete(qt_app):
    import main_window as mw

    dlg = mw.SettingsDialog()
    settings = dlg.get_settings()
    for key in ("dir", "segments", "monitor", "proxy_mode",
                "rate_limit", "finish_action", "notify_sound"):
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
