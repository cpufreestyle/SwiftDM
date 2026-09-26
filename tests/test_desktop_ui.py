"""桌面 UI 新增交互的单元测试（拖入链接解析 / 状态分段过滤 / 窗口几何持久化 / 清除确认）。

仅覆盖纯逻辑，不依赖显示设备：通过 QT_QPA_PLATFORM=offscreen + QMimeData 完成；
若运行环境缺少 PyQt6 或离屏平台不可用则自动跳过，避免影响无界面 CI。
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest

pytest.importorskip("PyQt6.QtWidgets")
from PyQt6.QtCore import QMimeData, QUrl
from PyQt6.QtWidgets import QLabel


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

        _close_task_detail = mw.MainWindow._close_task_detail

        def _add_download(self):  # pragma: no cover - just a slot target
            pass

    host = _Host()
    mw.MainWindow._setup_shortcuts(host)
    seqs = sorted(seq for seq, _ in host._shortcuts)
    assert "Ctrl+N" in seqs and "Ctrl+F" in seqs, seqs
    # Esc 关详情面板，和 Web 端对齐
    assert "Escape" in seqs, seqs


def test_escape_closes_task_detail(qt_app):
    """Esc 只在详情面板开着时才生效，不能吃掉其它场景的 Esc。"""
    import main_window as mw
    from PyQt6.QtWidgets import QDialog

    class _Dlg(QDialog):
        def __init__(self):
            super().__init__()
            self.rejected_count = 0

        def reject(self):
            self.rejected_count += 1
            super().reject()

    class _Host:
        def __init__(self):
            self._detail_dialog = None

    host = _Host()
    # 没开面板：不报错、不做事
    mw.MainWindow._close_task_detail(host)
    assert host._detail_dialog is None

    dlg = _Dlg()
    host._detail_dialog = dlg
    mw.MainWindow._close_task_detail(host)
    assert dlg.rejected_count == 1
    # finished 信号同步触发 _detail_dialog_closed；这里手动模拟一遍
    mw.MainWindow._detail_dialog_closed(host)
    assert host._detail_dialog is None
    # 二次 Esc 不要再拋错
    mw.MainWindow._close_task_detail(host)
    assert dlg.rejected_count == 1


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


def test_tray_icon_follows_theme_palette(qt_app):
    """托盘底色必须取自主题令牌，不能再写死一套深色。"""
    import main_window as mw

    for theme in ("dark", "light"):
        tokens = mw.THEMES[theme]
        for state, key in mw.TRAY_ICON_BG_KEY.items():
            assert key in tokens, (theme, state, key)
            # 各主题下能正常绘制，且不同主题肯定不并逐底色相同
            assert (
                tokens[mw.TRAY_ICON_BG_KEY["idle"]]
                != tokens[mw.TRAY_ICON_BG_KEY["downloading"]]
            ) or theme == "light"


def test_pick_readable_fg_beats_fixed_ink_on_every_state(qt_app):
    """前景色按对比度挑选：任意主题、任意状态都要趾过图标可读阈值。"""
    import main_window as mw

    def contrast(a, b):
        la, lb = mw._relative_luminance(a), mw._relative_luminance(b)
        hi, lo = max(la, lb), min(la, lb)
        return (hi + 0.05) / (lo + 0.05)

    for theme in ("dark", "light"):
        tokens = mw.THEMES[theme]
        for state in mw.TRAY_ICON_STATES:
            bg = tokens[mw.TRAY_ICON_BG_KEY[state]]
            fg = mw._pick_readable_fg(bg)
            assert fg in mw.TRAY_ICON_INK.values(), (theme, state, fg)
            assert contrast(bg, fg) >= 3.0, (
                theme, state, bg, fg, round(contrast(bg, fg), 2))


def test_pick_readable_fg_flips_with_background():
    import main_window as mw

    assert mw._pick_readable_fg("#ffffff") == mw.TRAY_ICON_INK["dark"]
    assert mw._pick_readable_fg("#000000") == mw.TRAY_ICON_INK["light"]


def test_apply_theme_repaints_tray_icon(qt_app):
    """切主题时托盘图标要重画，否则状态缓存会把颜色卡在旧主题上。"""
    import main_window as mw

    class _Tray:
        def __init__(self):
            self.icons = []

        def setIcon(self, icon):
            self.icons.append(icon)

    class _Host:
        _refresh_tray_icon = mw.MainWindow._refresh_tray_icon

        def __init__(self):
            self._cards = {}
            self.sheet = None
            self._tray_icon_state = "downloading"   # 状态不变，即使重绘也不该跳过

        def setStyleSheet(self, sheet):
            self.sheet = sheet

    host = _Host()
    host.tray = _Tray()
    mw.MainWindow._apply_theme(host, "light")
    assert len(host.tray.icons) == 1
    mw.MainWindow._apply_theme(host, "dark")
    assert len(host.tray.icons) == 2
    # 没有托盘的容器（正在搭建窗体）不能因此报错
    host2 = _Host()
    mw.MainWindow._apply_theme(host2, "light")


def _opaque_extent(img, threshold=128):
    """返回几乎完整覆盖的像素范围：抗锯齿边缘只会产生低覆盖度，不让它们干扰判断。"""
    xs, ys = [], []
    for y in range(img.height()):
        for x in range(img.width()):
            if img.pixelColor(x, y).alpha() >= threshold:
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys))


# 和 _tray_icon 里的 drawRoundedRect(2, 4, 28, 24, 6, 6) 对应：色块占满 x 2~29、y 4~27
BADGE_BOUNDS = (2, 4, 29, 27)


def test_tray_icon_stays_inside_the_badge(qt_app):
    """箭头和角标必须完整落在四弯方块里。

    角标更容易失质：画块底边在 y=27，而圆点一旦画到 y>=28 就会被裁掉一块。
    """
    import main_window as mw

    for theme in ("dark", "light"):
        for state in mw.TRAY_ICON_STATES:
            img = mw._tray_icon(state, mw.THEMES[theme]).pixmap(32, 32).toImage()
            extent = _opaque_extent(img)
            assert extent[0] >= BADGE_BOUNDS[0], (theme, state, "left", extent)
            assert extent[1] >= BADGE_BOUNDS[1], (theme, state, "top", extent)
            assert extent[2] <= BADGE_BOUNDS[2], (theme, state, "right", extent)
            assert extent[3] <= BADGE_BOUNDS[3], (theme, state, "bottom", extent)
            # 色块本身要填满，不能因为参数改动缓成一个小点
            assert extent == BADGE_BOUNDS, (theme, state, extent)


def test_desktop_uses_configured_thread_count(qt_app, monkeypatch):
    """桌面端新任务也得跟设置面板的线程数走；之前函数签名写死 8。"""
    import main_window as mw
    import config

    saved = config.get("segments")

    class _Mgr:
        def __init__(self):
            self.seen = []

        def create_task(self, *args, **kwargs):
            self.seen.append(args[3])
            raise RuntimeError("stop")

    class _Host:
        _threads_default = mw.MainWindow._threads_default

    host = _Host()
    host.download_dir = "C:/does-not-matter"
    mgr = _Mgr()
    monkeypatch.setattr("downloader.manager", mgr)
    try:
        for value, expected in ((3, 3), (32, 32), (None, 8)):
            config.set("segments", value)
            mgr.seen.clear()
            try:
                mw.MainWindow._create_and_start(host, "http://127.0.0.1:1/x.bin")
            except RuntimeError:
                pass
            assert mgr.seen == [expected], (value, mgr.seen)
        # 显式传参仍然最优先
        config.set("segments", 4)
        mgr.seen.clear()
        try:
            mw.MainWindow._create_and_start(host, "http://127.0.0.1:1/x.bin", segments=16)
        except RuntimeError:
            pass
        assert mgr.seen == [16], mgr.seen
    finally:
        config.set("segments", saved)


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
    assert [g.title() for g in groups] == ["下载", "网络", "完成后", "外观",
                                         "诊断"]

    def row_labels(group):
        return {lbl.text() for lbl in group.findChildren(QLabel)
                if lbl.text().endswith(":")}

    by_title = {g.title(): row_labels(g) for g in groups}
    assert by_title["下载"] == {"下载目录:", "下载线程数:", "下载限速:", "失败自动重试:"}
    assert by_title["网络"] == {"浏览器监控:", "下载代理:", "自定义代理:", "剪贴板监听:"}
    assert by_title["完成后"] == {"全部下载完成后:", "完成提示音:"}
    assert by_title["外观"] == {"界面主题:"}
    assert by_title["诊断"] == {"ffmpeg:", "yt-dlp:", "链路自检:"}


def test_settings_dialog_get_settings_still_complete(qt_app):
    import main_window as mw

    dlg = mw.SettingsDialog()
    settings = dlg.get_settings()
    for key in ("dir", "segments", "monitor", "proxy_mode",
                "rate_limit", "auto_retry", "clipboard_watch",
                "finish_action", "notify_sound", "theme"):
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


def test_qss_template_has_no_color_literals_at_all(qt_app):
    import main_window as mw
    import re as _re

    # 比跨端版本（test_theme_tokens）更严：QSS 里一个十六进色都不能有，白色也得走令牌；
    # 换主题时会把配色整花（与 Web 端 test_no_stray_hardcoded_colors_in_css 同一条准线）
    template = mw.QSS_TEMPLATE.template
    assert _re.findall(r"#[0-9a-fA-F]{3,8}\b", template) == []
    assert "$onAccent" in template
    for theme, tokens in mw.THEMES.items():
        qss = mw._qss_for(theme)
        assert tokens["onAccent"] in qss, theme


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


def test_overflow_plan_hides_from_tail_until_fits():
    import main_window as mw

    assert mw._overflow_plan([60, 60, 60], 300, 14) == (3, [])
    assert mw._overflow_plan([60, 60, 60], 200, 14) == (2, [2])     # 收最后一个就够
    assert mw._overflow_plan([60, 60, 60], 100, 14) == (1, [1, 2])
    assert mw._overflow_plan([60, 60, 60], 10, 14) == (0, [0, 1, 2])  # 极窄：全部溢出
    assert mw._overflow_plan([], 100, 14) == (0, [])
    # 零宽度不能循环或报错
    assert mw._overflow_plan([60, 60], 0, 14) == (0, [0, 1])


def test_action_buttons_overflow_into_more_menu(qt_app, monkeypatch):
    """容器变窄时，放不下的操作按钮进“···”菜单，功能不丢。"""
    import main_window as mw
    from PyQt6.QtWidgets import QMenu

    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin", "url": "https://a/x.bin",
                        "status": "downloading", "total_size": 100, "downloaded": 0})
    n_btns = len(card._overflow_btns)
    assert n_btns == 3  # 暂停 / 取消 / 打开文件夹
    card.show()   # 隐藢窗口不会收到 resizeEvent，测试要 show

    # 足够宽：全部展示，无“···”
    card.resize(900, 140)
    assert card._hidden_actions == []
    assert not card._more_btn.isVisible()

    # 容器变窄：resizeEvent 自动重算，尾部按钮被收起
    card.resize(230, 140)
    hidden = card._hidden_actions
    assert hidden, "窄卡片上应该有按钮被收起"
    assert card._more_btn.isVisible()
    for i, (btn, _) in enumerate(card._overflow_btns):
        assert btn.isVisible() == (i < n_btns - len(hidden))

    # 溢出菜单项与按钮一一对应，触发后照常发 action
    monkeypatch.setattr(QMenu, "exec", lambda self, *a, **k: None)
    got = []
    card.action_triggered.connect(lambda a, t: got.append((a, t)))
    menu = card._show_overflow_menu()
    assert [a.text() for a in menu.actions()] == [btn.text() for btn, _ in hidden]
    menu.actions()[0].trigger()
    assert got == [(hidden[0][1], "t1")]

    # 重新变宽一切重整
    card.resize(900, 140)
    assert card._hidden_actions == []
    assert not card._more_btn.isVisible()


def test_clipboard_download_url_only_accepts_standalone_links():
    import main_window as mw

    assert mw._clipboard_download_url("https://a.com/x.zip") == "https://a.com/x.zip"
    assert mw._clipboard_download_url("  http://a.com/y  ") == "http://a.com/y"
    assert mw._clipboard_download_url("magnet:?xt=urn:btih:ABC") == "magnet:?xt=urn:btih:ABC"
    # 非下载链接/多行/整段文字：不触发，避免把用户复制的普通文本当任务
    assert mw._clipboard_download_url("ftp://a.com/x") is None
    assert mw._clipboard_download_url("www.a.com") is None
    assert mw._clipboard_download_url("https://a.com/a https://a.com/b") is None
    assert mw._clipboard_download_url("来自 https://a.com/x.zip 的分享") is None
    assert mw._clipboard_download_url("") is None
    assert mw._clipboard_download_url(None) is None


def test_check_clipboard_prompts_once_per_link(qt_app, monkeypatch):
    """复制链接 → 拖盘提示一次 → 点击才新建；重复链接与已存在任务不扔提示。"""
    import main_window as mw
    from PyQt6.QtWidgets import QApplication

    class _Tray:
        def __init__(self):
            self.messages = []

        def showMessage(self, title, text, icon, ms):
            self.messages.append(text)

    class _Mgr:
        def __init__(self, tasks):
            self._tasks = tasks

        def get_all_tasks(self):
            return self._tasks

    class _Host:
        def __init__(self, tasks=()):
            self._clip_last = None
            self._clip_pending = None
            self.tray = _Tray()
            self._mgr = _Mgr(list(tasks))
            self.created = []

        def _get_manager(self):
            return self._mgr

        def _create_and_start(self, url):
            self.created.append(url)
            return None

    import config as _cfg
    monkeypatch.setattr(_cfg, "get",
                        lambda key: True if key == "clipboard_watch" else None)

    host = _Host()
    QApplication.clipboard().setText("https://a.com/x.zip")
    mw.MainWindow._check_clipboard(host)
    assert host._clip_pending == "https://a.com/x.zip"
    assert len(host.tray.messages) == 1
    assert "https://a.com/x.zip" in host.tray.messages[0]

    # 同一链接不重复提示
    mw.MainWindow._check_clipboard(host)
    assert len(host.tray.messages) == 1

    # 点击拖盘气泡 → 新建下载（不自动下载，由用户确认）
    mw.MainWindow._tray_message_clicked(host)
    assert host.created == ["https://a.com/x.zip"]
    assert host._clip_pending is None

    # 任务榜已有相同链接：不再提示
    dup = type("T", (), {"url": "https://a.com/x.zip"})()
    host2 = _Host([dup])
    mw.MainWindow._check_clipboard(host2)
    assert host2._clip_pending is None
    assert host2.tray.messages == []

    # 普通文本不触发
    host3 = _Host()
    QApplication.clipboard().setText("今天天气不错")
    mw.MainWindow._check_clipboard(host3)
    assert host3._clip_pending is None
    assert host3.tray.messages == []


def test_check_clipboard_noop_when_disabled(qt_app, monkeypatch):
    import main_window as mw
    from PyQt6.QtWidgets import QApplication

    class _Host:
        _clip_last = None
        _clip_pending = None

        class tray:
            @staticmethod
            def showMessage(*a, **k):
                raise AssertionError("关闭时不应弹提示")

        def _get_manager(self):
            raise AssertionError("关闭时不应查任务榜")

    import config as _cfg
    monkeypatch.setattr(_cfg, "get",
                        lambda key: False if key == "clipboard_watch" else None)
    QApplication.clipboard().setText("https://a.com/x.zip")
    mw.MainWindow._check_clipboard(_Host())
    assert _Host._clip_pending is None


def test_detail_rows_cover_shared_and_kind_specific_fields():
    import main_window as mw

    http_rows = dict(mw._detail_rows({
        "task_id": "t1", "filename": "movie.bin",
        "url": "https://site/movie.bin", "filepath": "C:/dl/movie.bin",
        "save_dir": "C:/dl", "status": "downloading", "progress": 42.55,
        "total_size": 10 * 1024 * 1024, "downloaded": 4 * 1024 * 1024,
        "speed": 1048576, "eta": "6s", "kind": "http",
    }))
    assert http_rows["文件名"] == "movie.bin"
    assert http_rows["状态"] == "● 下载中"
    assert http_rows["进度"] == "42.5%"
    assert http_rows["已下载 / 总大小"] == "4.0 MB / 10.0 MB"
    assert http_rows["下载速度"] == "1.0 MB/s"
    assert http_rows["预计剩余"] == "6s"
    assert http_rows["保存目录"] == "C:/dl"
    assert http_rows["下载链接"] == "https://site/movie.bin"
    # 直链任务不出现 BT / 媒体专属行
    assert "传输协议" not in http_rows
    assert "种子 / 同伴" not in http_rows
    assert "分辨率" not in http_rows

    bt_rows = dict(mw._detail_rows({
        "task_id": "t2", "filename": "linux.iso",
        "url": "magnet:?xt=urn:btih:0123456789abcdef", "status": "downloading",
        "kind": "torrent", "protocol": "BitTorrent", "seeds": 3, "peers": 7,
        "total_size": 1000, "downloaded": 50,
    }))
    assert bt_rows["传输协议"] == "BitTorrent"
    assert bt_rows["种子 / 同伴"] == "3 / 7"
    assert bt_rows["已下载 / 总大小"] == "50 B / 1000 B"

    failed_rows = dict(mw._detail_rows({
        "task_id": "t3", "filename": "v.mp4", "status": "failed",
        "error": "boom", "error_reason": "needs_ffmpeg",
    }))
    assert failed_rows["失败原因"] == "boom"
    assert "ffmpeg" in failed_rows["处理建议"]
    assert "下载链接" not in failed_rows  # 没有链接时不占一行


def test_detail_size_text_unknown_total_falls_back():
    import main_window as mw
    assert mw._detail_size_text(5, 10) == "5 B / 10 B"
    assert mw._detail_size_text(2048, 0) == "2.0 KB（总大小未知）"
    assert mw._detail_size_text(0, 0) == "-"


def test_segment_rows_reconstructs_byte_ranges_and_skips_junk():
    import main_window as mw
    rows = mw._segment_rows({
        "segments_offsets": [[0, 9], [10, 19], [20, 24]],
        "segments_progress": [4, 10, "bad"],
    })
    assert rows == [{"done": 4, "total": 10}, {"done": 10, "total": 10},
                    {"done": 0, "total": 5}]
    # 单段 / 无分段数据 / 结构异常都不展示分段区
    assert mw._segment_rows({"segments_offsets": [[0, 9]], "segments_progress": [3]}) == []
    assert mw._segment_rows({"segments_progress": [1, 2]}) == []
    assert mw._segment_rows({"segments_offsets": "oops", "segments_progress": [1]}) == []
    assert mw._segment_rows(None) == []


def test_download_task_to_dict_reports_segments_even_when_cached():
    import downloader
    t = downloader.DownloadTask("t1", "https://x/y.bin", "C:/tmp", "y.bin", 2)
    first = t.to_dict()
    assert first["segments_progress"] == []
    assert first["segments_offsets"] == []
    assert first["segments_total"] == 0
    # 缓存命中路径也必须拿到最新分段进度（否则详情面板进度条会卡住）
    t._segment_offsets = [(0, 9), (10, 19)]
    t._segment_progress = [3, 7]
    cached = t.to_dict()
    assert cached["segments_progress"] == [3, 7]
    assert cached["segments_total"] == 2
    assert cached["segments_offsets"] == [[0, 9], [10, 19]]


def test_media_and_torrent_to_dict_expose_segment_placeholders():
    import main_window
    from media import MediaTask
    from torrent import TorrentTask
    tasks = [MediaTask("m1", "https://x/v.m3u8", "C:/tmp", "v.mp4", 4),
             TorrentTask("b1", "magnet:?xt=urn:btih:0123456789abcdef", "C:/tmp")]
    for task in tasks:
        d = task.to_dict()
        assert d["segments_progress"] == []
        assert d["segments_offsets"] == []
        assert d["segments_total"] == 0
        detail = dict(main_window._detail_rows(d))
        assert detail["文件名"] == task.filename
        assert detail["保存目录"] == task.save_dir


def test_task_card_double_click_emits_activated(qt_app):
    import main_window as mw
    from PyQt6.QtGui import QMouseEvent
    from PyQt6.QtCore import QPointF, Qt

    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin",
                        "status": "downloading", "total_size": 10,
                        "downloaded": 1})
    got = []
    card.activated.connect(lambda tid: got.append(tid))
    ev = QMouseEvent(QMouseEvent.Type.MouseButtonDblClick, QPointF(2, 2),
                     Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                     Qt.KeyboardModifier.NoModifier)
    card.mouseDoubleClickEvent(ev)
    assert got == ["t1"]


def test_context_menu_offers_details_entry(qt_app, monkeypatch):
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

    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin",
                        "url": "https://site/x.bin", "status": "downloading"})
    emitted = []
    card.action_triggered.connect(lambda a, t: emitted.append((a, t)))
    card.contextMenuEvent(_Ev())
    texts = [a.text for a in created[-1].actions]
    details = [a for a in created[-1].actions if a.text == "查看详情"]
    assert len(details) == 1, texts
    details[0].trigger()
    assert emitted == [("details", "t1")]


def test_handle_action_details_opens_task_detail(qt_app):
    import main_window as mw

    class _Task:
        task_id = "t1"

    class _Mgr:
        def get_task(self, tid):
            return _Task()

        def save_history(self):
            pass

    class _Bar:
        def showMessage(self, text, timeout=0):
            pass

    class _Host:
        def __init__(self):
            self._mgr = _Mgr()
            self.status_bar = _Bar()
            self.shown = []

        def _get_manager(self):
            return self._mgr

        def _show_task_detail(self, task_id):
            self.shown.append(task_id)

    host = _Host()
    mw.MainWindow._handle_action(host, "details", "t1")
    assert host.shown == ["t1"]


def test_task_detail_dialog_renders_rows_and_auto_closes(qt_app):
    import main_window as mw
    from PyQt6.QtWidgets import QFormLayout, QDialog

    data = {
        "task_id": "t1", "filename": "movie.bin", "url": "https://site/movie.bin",
        "save_dir": "C:/dl", "status": "downloading", "progress": 50.0,
        "total_size": 20, "downloaded": 10, "speed": 5, "eta": "2s",
        "kind": "http", "segments": 2,
        "segments_progress": [5, 5],
        "segments_offsets": [[0, 9], [10, 19]],
        "segments_total": 2,
    }
    polled = []
    dlg = mw.TaskDetailDialog("t1", data, fetch=lambda: polled.append(1) or None)
    dlg._timer.stop()  # 手动驱动轮询，避免测试间相互干扰
    assert dlg.title_label.text() == "movie.bin"
    labels = [dlg.grid.itemAt(i, QFormLayout.ItemRole.LabelRole).widget().text()
              for i in range(dlg.grid.rowCount())]
    assert "下载链接:" in labels
    assert not dlg.seg_wrap.isHidden()
    assert dlg.seg_rows.count() == 2

    # 单段任务：分段区整体隐藏，不浪费高度
    single = dict(data, segments=1, segments_progress=[10],
                  segments_offsets=[[0, 19]], segments_total=1)
    dlg.update_data(single)
    assert dlg.seg_wrap.isHidden()

    dlg.update_data(data)
    dlg._poll()
    assert polled
    assert dlg.result() == QDialog.DialogCode.Rejected
    assert not dlg._timer.isActive()


def test_task_detail_dialog_follows_theme(qt_app):
    import main_window as mw
    data = {"task_id": "t1", "filename": "x.bin", "status": "downloading"}
    dlg = mw.TaskDetailDialog("t1", data, fetch=None, theme="light")
    dlg._timer.stop()
    assert mw.THEMES["light"]["surface"] in dlg.styleSheet()
    dlg.apply_theme("dark")
    assert mw.THEMES["dark"]["surface"] in dlg.styleSheet()
    dlg.apply_theme("nonsense")
    assert dlg._theme == "dark"


def test_tray_message_click_only_adds_for_clip_prompt(qt_app):
    """完成/失败等提示气泡被误点不应凭空新建下载，只有剪贴板待确认气泡可以。"""
    import main_window as mw

    class _Host:
        def __init__(self, kind):
            self._tray_msg_kind = kind
            self._clip_pending = "https://a.com/x.zip"
            self.created = []

        def _create_and_start(self, url):
            self.created.append(url)

    info = _Host("info")
    mw.MainWindow._tray_message_clicked(info)
    assert info.created == []
    assert info._clip_pending == "https://a.com/x.zip"  # 不消费待确认链接

    prompt = _Host("clip_prompt")
    mw.MainWindow._tray_message_clicked(prompt)
    assert prompt.created == ["https://a.com/x.zip"]
    assert prompt._clip_pending is None
    # 重复点击不重复添加
    mw.MainWindow._tray_message_clicked(prompt)
    assert prompt.created == ["https://a.com/x.zip"]


def test_notify_capture_surfaces_source_of_new_task(qt_app):
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
    mw.MainWindow._notify_capture(host, "movie.mkv")
    assert any("浏览器捕获" in m for m in host.status_bar.messages)
    title, body, icon, _ms = host.tray.messages[0]
    assert title == "SwiftDM" and "movie.mkv" in body
    assert icon == QSystemTrayIcon.MessageIcon.Information
    assert host._tray_msg_kind == "info"   # 点击不触发任何动作


def test_task_detail_dialog_copy_details_button(qt_app):
    import main_window as mw
    from PyQt6.QtWidgets import QApplication, QPushButton

    data = {"task_id": "t1", "filename": "movie.bin",
            "url": "https://site/movie.bin", "save_dir": "C:/dl",
            "status": "downloading", "progress": 10.0, "total_size": 100,
            "downloaded": 10, "speed": 0, "eta": "18s", "kind": "http"}
    dlg = mw.TaskDetailDialog("t1", data, fetch=None)
    dlg._timer.stop()
    copy_btn = [b for b in dlg.findChildren(QPushButton)
                if b.text() == "复制详情"]
    assert len(copy_btn) == 1
    copy_btn[0].click()
    text = QApplication.clipboard().text()
    assert "文件名: movie.bin" in text
    assert "保存目录: C:/dl" in text
    assert "下载链接: https://site/movie.bin" in text
    # 复制的就是当前刷新出来的内容
    dlg.update_data(dict(data, filename="other.bin"))
    copy_btn[0].click()
    assert "文件名: other.bin" in QApplication.clipboard().text()


def test_reason_hint_covers_download_failed():
    """HTTP 分段失败是最高频的失败类型，桌面端必须给得出可操作提示。"""
    import main_window as mw
    hint = mw._reason_hint("download_failed")
    assert "重试" in hint
    # 与 Web 端同源的其它键仍然可用
    assert "ffmpeg" in mw._reason_hint("needs_ffmpeg")
    assert mw._reason_hint("") == ""
    assert mw._reason_hint("unknown_reason") == ""


def test_settings_dialog_has_a_diagnostics_group(qt_app):
    """桌面端之前没有任何诊断信息；缺 ffmpeg/yt-dlp 时用户完全没感知。"""
    import main_window as mw
    dlg = mw.SettingsDialog(http_port=5000)
    try:
        assert dlg.selftest_btn is not None
        assert dlg.selftest_btn.isEnabled() is True
        assert dlg.selftest_label.text() != ""
        # ffmpeg / yt-dlp 两行都要有内容，不能空着
        labels = [lbl.text() for lbl in dlg.findChildren(QLabel)
                  if "已就绪" in lbl.text() or "未检测到" in lbl.text()
                  or "检测失败" in lbl.text()]
        assert len(labels) >= 2, labels
    finally:
        dlg.deleteLater()


def test_self_test_worker_reports_backend_result(monkeypatch, qt_app):
    import main_window as mw

    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    seen = []
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda url, timeout=None: (seen.append(url), _Resp(
                            {"success": True}))[1])
    worker = mw.SelfTestWorker(5123)
    got = []
    worker.finished.connect(lambda ok, detail: got.append((ok, detail)))
    worker.run()
    assert seen == ["http://127.0.0.1:5123/api/self-test"], seen
    assert got == [(True, "")], got

    # 连不上服务器时也得给出失败原因，不能安静失败
    def _boom(url, timeout=None):
        raise OSError("connection refused")
    monkeypatch.setattr("urllib.request.urlopen", _boom)
    got.clear()
    worker.run()
    assert len(got) == 1 and got[0][0] is False and "connection refused" in got[0][1], got


def test_zero_speed_reads_as_zero_not_unknown():
    """format_size(0) is "unknown size"; a speed of 0 is simply idle.

    format_speed used to defer to format_size, so an idle window showed
    "unknown/s" in the status bar and the tray tooltip could too.
    """
    import main_window as mw
    assert mw.format_size(0) == "未知"
    assert mw.format_speed(0) == "0 B/s"
    assert mw.format_speed(None) == "0 B/s"
    assert mw.format_speed(1536) == "1.5 KB/s"
    assert mw.format_speed(1048576) == "1.0 MB/s"
