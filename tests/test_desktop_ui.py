"""桌面 UI 新增交互的单元测试（拖入链接解析 / 状态分段过滤 / 窗口几何持久化 / 清除确认）。

仅覆盖纯逻辑，不依赖显示设备：通过 QT_QPA_PLATFORM=offscreen + QMimeData 完成；
若运行环境缺少 PyQt6 或离屏平台不可用则自动跳过，避免影响无界面 CI。
"""
import contextlib
import io
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
        _on_escape = mw.MainWindow._on_escape
        _select_all_visible = mw.MainWindow._select_all_visible
        _focus_search = mw.MainWindow._focus_search

        def _add_download(self):  # pragma: no cover - just a slot target
            pass

    host = _Host()
    mw.MainWindow._setup_shortcuts(host)
    seqs = sorted(seq for seq, _ in host._shortcuts)
    assert "Ctrl+N" in seqs and "Ctrl+F" in seqs, seqs
    # Esc 关详情面板，和 Web 端对齐
    assert "Escape" in seqs, seqs
    # Ctrl+A 全选可见任务：桌面端多选批量的键盘入口
    assert "Ctrl+A" in seqs, seqs

    # Ctrl+F 聚焦搜索框（与 Web 端、以及「Ctrl+F = 查找」的通用约定一致；
    # 链接输入框常驻工具栏，Ctrl+N 已覆盖新建入口）
    host2 = _Host()
    host2.search_input = QLineEdit()
    focused = []
    host2.search_input.setFocus = lambda: focused.append("search")
    mw.MainWindow._focus_search(host2)
    assert focused == ["search"]


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


def test_task_detail_dialog_offers_state_actions(qt_app):
    """详情面板要能直接暂停/继续/重试/打开。

    用户往往是看了失败原因才想重试、看了进度才想暂停，从前详情面板只有
    「打开文件夹/复制链接/复制详情」几个静态按钮，只能退回列表找卡片操作。
    动作集合与卡片行、右键菜单保持同一套状态取舍，且与 Web 端详情面板一致。
    """
    import main_window as mw
    from PyQt6.QtWidgets import QPushButton

    base = {"task_id": "t1", "filename": "a.bin", "url": "https://s/a.bin",
            "save_dir": "C:/dl"}

    def state_buttons(dlg):
        return [b.text() for b in dlg._state_btns_widgets]

    dlg = mw.TaskDetailDialog("t1", dict(base, status="downloading"), fetch=None)
    dlg._timer.stop()
    assert state_buttons(dlg) == ["⏸ 暂停", "✕ 取消"]

    dlg.update_data(dict(base, status="paused"))
    assert state_buttons(dlg) == ["▶ 继续", "✕ 取消"]

    dlg.update_data(dict(base, status="failed", error="HTTP 404"))
    assert state_buttons(dlg) == ["↻ 重试"]

    dlg.update_data(dict(base, status="cancelled"))
    assert state_buttons(dlg) == ["↻ 重试"]

    dlg.update_data(dict(base, status="completed", total_size=10))
    assert state_buttons(dlg) == ["📂 打开文件"]

    # 等待中的定时任务没有可用状态动作，不该摆一排禁用的按钮
    dlg.update_data(dict(base, status="pending"))
    assert state_buttons(dlg) == []

    # 点击要真的把动作抛回主窗口（信号名与 task_id 都对）
    seen = []
    dlg.action_requested.connect(lambda action, task_id: seen.append((action, task_id)))
    dlg.update_data(dict(base, status="failed"))
    retry = [b for b in dlg.findChildren(QPushButton) if b.text() == "↻ 重试"][0]
    retry.click()
    assert seen == [("retry", "t1")], seen

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


def _focus_pixel_diff(app, qss, objname, toolbar=False, checkable=False, checked=False):
    """在离屏环境里渲染两个相同的按钮，返回只聚焦其中一个前后像素变化的坐标。

    空列表 = 焦点前后一模一样，也就是说键盘用户完全看不见焦点落在哪。
    """
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QPushButton, QToolBar, QVBoxLayout, QWidget

    holder = QWidget()
    outer = QVBoxLayout(holder)
    tb = QToolBar() if toolbar else None
    if tb is not None:
        outer.addWidget(tb)

    def mk():
        b = QPushButton("全部暂停")
        if objname:
            b.setObjectName(objname)
        if checkable:
            b.setCheckable(True)
            b.setChecked(checked)
        (tb.addWidget(b) if tb is not None else outer.addWidget(b))
        return b

    a, b = mk(), mk()
    holder.setStyleSheet(qss)
    holder.resize(460, 240)
    holder.show()
    app.processEvents()
    b.setFocus(Qt.FocusReason.TabFocusReason)
    app.processEvents()
    ia, ib = a.grab().toImage(), b.grab().toImage()
    holder.hide()
    return [(x, y) for y in range(min(ia.height(), ib.height()))
            for x in range(min(ia.width(), ib.width()))
            if QColor(ia.pixel(x, y)) != QColor(ib.pixel(x, y))]


def test_desktop_buttons_show_a_keyboard_focus_ring(qt_app):
    """桌面端原生焦点框被 QSS 吃掉，之前聚焦前后像素完全一致。

    与 Web 端 :focus-visible 同一条准线：强调色描边、仅键盘聚焦才显形。
    """
    import main_window as mw

    cases = (("toolbar", dict(objname="", toolbar=True)),
             ("toolbar-add", dict(objname="btnAdd", toolbar=True)),
             ("toolbar-countdown", dict(objname="btnFinishCountdown", toolbar=True)),
             ("filter-chip", dict(objname="filterBtn")),
             ("filter-chip-checked", dict(objname="filterBtn",
                                          checkable=True, checked=True)))
    for theme in mw.THEMES:
        qss = mw._qss_for(theme)
        for label, kwargs in cases:
            diff = _focus_pixel_diff(qt_app, qss, **kwargs)
            assert diff, f"{theme} {label}: 聚焦前后像素无变化，焦点环没生效"
            # 焦点环必须落在最外一圈，否则只是底色变化，弱光下仍然看不出
            assert min(y for _, y in diff) == 0, f"{theme} {label}"
            assert max(y for _, y in diff) == max(p[1] for p in diff), f"{theme} {label}"


def test_nameless_desktop_widgets_expose_accessible_names(qt_app):
    """没有文字的工具按钮和输入框，对读屏软件原本是空的。"""
    import main_window as mw

    src = io.open(mw.__file__, encoding="utf-8").read()
    for needle in (
        'self._more_btn.setAccessibleName("更多操作")',
        'self.finish_btn.setAccessibleName("取消下载完成后的倒计时")',
        'self.progress_bar.setAccessibleName("下载进度")',
        'self.search_input.setAccessibleName("搜索任务")',
        'self.sort_combo.setAccessibleName("任务列表排序方式")',
        'self.compact_btn.setAccessibleName("紧凑模式")',
    ):
        assert needle in src, needle


def test_card_action_buttons_use_tooltip_as_accessible_name(qt_app):
    """卡片顶部的图标按钮（“📋”）没有文字，
    只能把 tooltip 同步成可访问名。"""
    import main_window as mw
    from PyQt6.QtWidgets import QPushButton

    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin", "url": "https://a/x.bin",
                        "status": "downloading", "total_size": 0, "downloaded": 0})
    tipped = [b for b in card.findChildren(QPushButton) if b.toolTip()]
    assert tipped, "卡片里至少要有一个带工具提示的按钮"
    for btn in tipped:
        assert btn.accessibleName() == btn.toolTip(), btn.text()
    assert "QPushButton:focus" in tipped[0].styleSheet()
    assert card.progress_bar.accessibleName() == "下载进度"


def test_desktop_card_focus_ring_is_visible(qt_app):
    """卡片按钮自带彩色描边，焦点环改用淡填充（否则描边变色也看不出来）。"""
    import main_window as mw
    from PyQt6.QtCore import Qt
    from PyQt6.QtGui import QColor
    from PyQt6.QtWidgets import QPushButton, QVBoxLayout, QWidget

    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin", "url": "https://a/x.bin",
                        "status": "completed", "total_size": 10, "downloaded": 10})
    host = QWidget()
    QVBoxLayout(host).addWidget(card)
    host.resize(600, 400)
    host.show()
    qt_app.processEvents()
    # 必须拍同一个按钮：卡片按钮各自用不同的语义色，换按钮比就比出配色差异而不是焦点环。
    # “⋯⋯”溢出按钮默认是隐藏的，拍它只会得到一张空白图。
    target = next(b for b in card.findChildren(QPushButton) if b.isVisible())
    # 窗口展示时第一个按钮已经自动拿到焦点，先撤掉才能拍到未聚焦的底片
    target.clearFocus()
    qt_app.processEvents()
    ia = target.grab().toImage()
    target.setFocus(Qt.FocusReason.TabFocusReason)
    qt_app.processEvents()
    ib = target.grab().toImage()
    assert target.hasFocus(), "聚焦没生效，这次对比没意义"
    host.hide()
    diff = [(x, y) for y in range(min(ia.height(), ib.height()))
            for x in range(min(ia.width(), ib.width()))
            if QColor(ia.pixel(x, y)) != QColor(ib.pixel(x, y))]
    assert diff, "卡片按钮聚焦前后像素无变化"


def _widget_name(widget):
    text = getattr(widget, "text", None)
    return text() if callable(text) else type(widget).__name__


@contextlib.contextmanager
def _shown_main_window(qt_app, monkeypatch):
    """真实主窗口（离屏），并把全局 manager 换成没有历史的桩。

    窗口构造会跑一遍 _refresh()，真的会去读 ~/.swiftdm/history.json；
    换成空桩之后窗口里没有卡片，测出来的 Tab 顺序才不受本机历史影响。
    """
    import downloader
    import main_window as mw

    class _Manager:
        def get_all_tasks(self):
            return []

        def get_stats(self):
            return {"total_speed": 0.0, "active": 0, "completed": 0,
                    "failed": 0, "paused": 0, "total": 0}

    monkeypatch.setattr(downloader, "manager", _Manager())
    was_quit = qt_app.quitOnLastWindowClosed()
    # 最后一个窗口关掉不能顺带退出 QApplication，否则后面的 grab/断言全废
    qt_app.setQuitOnLastWindowClosed(False)
    win = mw.MainWindow()
    win.resize(2200, 900)  # 工具栏够宽，后半段按钮才不会被 QToolBar 藏起来
    win.show()
    qt_app.processEvents()
    try:
        yield win
    finally:
        win.close()
        # 全局 logger 上还挂着这个窗口的 handler：窗口一删，后台写日志就会
        # 往已销毁的 QObject 发信号，直接 access violation
        win.logger.removeHandler(win._log_handler)
        win.deleteLater()
        qt_app.processEvents()
        qt_app.setQuitOnLastWindowClosed(was_quit)


def _tab_to(win, qt_app, prev, nxt):
    """从 prev 按一次 Tab，断言焦点落在 nxt。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    prev.setFocus(Qt.FocusReason.OtherFocusReason)
    qt_app.processEvents()
    assert win.focusWidget() is prev, f"没能在 {_widget_name(prev)} 上起手"
    QTest.keyClick(win, Qt.Key.Key_Tab)
    qt_app.processEvents()
    assert win.focusWidget() is nxt, (
        f"焦点从 {_widget_name(prev)} 走到了 {_widget_name(win.focusWidget())}，"
        f"应该落在 {_widget_name(nxt)}")


def test_desktop_tab_order_walks_the_window_in_reading_order(qt_app, monkeypatch):
    """Tab 要按视觉阅读顺序走：工具栏从左到右，再过筛选栏、搜索，进任务区。

    Qt 默认顺序就是控件创建顺序，真正的屏障在别处：筛选芯片一旦进
    Qt 的互斥按钮组，除第一颗外其余三颗会被摘掉 TabFocus，键盘到不了。这里把整条
    阅读顺序走一遍，哪一站被摘掉会立即变红。
    """
    with _shown_main_window(qt_app, monkeypatch) as win:
        chain = [
            win.url_input, win.btn_add, win.btn_pause_all, win.btn_resume_all,
            win.btn_retry_failed, win.btn_clear, win.btn_open_dir,
            win.btn_copy_links, win.btn_export, win.btn_log, win.btn_settings,
            win._filter_btns["all"], win._filter_btns["active"],
            win._filter_btns["completed"], win._filter_btns["failed"],
            win.sort_combo, win.compact_btn, win.search_input, win.scroll,
        ]
        for prev, nxt in zip(chain, chain[1:]):
            # 隐藏/停用的控件（倒计时按钮、日志面板（默认收起）、被工具栏藏起来的
            # 按钮）本来就不在 Tab 顺序里，跳过这一段
            if not all(w.isVisible() and w.isEnabled() for w in (prev, nxt)):
                continue
            _tab_to(win, qt_app, prev, nxt)


def test_filter_bar_hints_at_the_task_keyboard_shortcuts(qt_app, monkeypatch):
    """↑↓/Enter  shortcuts 早就接好了，但桌面上从前没有任何地方提到。

    Web 端筛选栏里一直挂着一行 .kbd-hint「↑↓ 选择任务 · Enter 打开」，
    桌面端能力相同却只字未提，纯键盘用户不会去试。这里补上同一行提示，
    并要求它贴着一排筛选芯片（而不是漂到右上角跟排序/搜索混在一起）。
    """
    import io as _io

    import main_window as mw

    with _shown_main_window(qt_app, monkeypatch) as win:
        hint = win.kbd_hint
        assert hint.objectName() == "kbdHint"
        assert hint.text() == "↑↓ 选择任务 · Enter 打开 · Ctrl+A 全选"
        # 多选三件套（Ctrl+点击 / Ctrl+A / Esc）也要在悬浮说明里说清
        assert "↑↓" in hint.toolTip() and "回车" in hint.toolTip()
        assert "Ctrl" in hint.toolTip() and "Esc" in hint.toolTip()
        assert "Ctrl+N" in hint.toolTip() and "Ctrl+F" in hint.toolTip()
        assert "Ctrl" in hint.accessibleName() and "Esc" in hint.accessibleName()
        assert hint.accessibleName() == (
            "键盘导航提示：↑↓ 在可见任务间移动，回车打开文件；"
            "Ctrl+点击卡片多选，Ctrl+A 全选可见任务，Esc 取消选择；"
            "Ctrl+N 新建下载，Ctrl+F 搜索任务")

        # 位置：伸缩位是「左组 / 右组」的分界，addStretch 之前的都跟着芯片在左边
        layout = hint.parentWidget().layout()
        chips = [layout.indexOf(win._filter_btns[k])
                 for k in ("all", "active", "completed", "failed")]
        stretch_slots = [i for i in range(layout.count()) if layout.stretch(i) > 0]
        assert stretch_slots, "筛选栏要留一段伸缩，把右侧控件推到边上"
        assert max(chips) < layout.indexOf(hint), "提示要贴着筛选芯片放"
        assert layout.indexOf(hint) < min(stretch_slots),             "提示该和芯片同组，不该漂到排序/紧凑那一侧"

    # 样式走令牌：和 Web 端 .kbd-hint 同为 11px + textMuted（#8888a0）
    qss = _io.open(mw.__file__, encoding="utf-8").read()
    assert "QLabel#kbdHint { font-size: 11px; color: $textMuted; }" in qss
    web = _io.open(os.path.join(os.path.dirname(mw.__file__), "templates", "index.html"),
                   encoding="utf-8").read()
    assert web.count("kbd-hint") >= 2, "Web 端的提示样式/节点都还在，别只改一边"

    # 真的离屏渲染一次，确认提示文字可见且不是零尺寸
    from PyQt6.QtWidgets import QLabel

    with _shown_main_window(qt_app, monkeypatch) as win:
        hint = win.kbd_hint
        hint.show()
        qt_app.processEvents()
        assert hint.size().width() > 0 and hint.size().height() > 0, hint.text()


def test_filter_chips_stay_exclusive_and_each_reachable_by_tab(qt_app, monkeypatch):
    """四颗筛选芯片每颗都能 Tab 到，同时保持互斥。

    互斥原先交给 exclusive QButtonGroup：Qt 会把除第一颗以外的芯片从 Tab 顺序里
    摘掉（focusPolicy 只剩点击/滚轮），纯键盘用户够不到「进行中/已完成/失败」。
    改成在 _set_filter 里手工刷 checked 之后，四颗都是正常 Tab 停留点。
    """
    from PyQt6.QtCore import Qt

    with _shown_main_window(qt_app, monkeypatch) as win:
        chips = win._filter_btns
        for key in ("all", "active", "completed", "failed"):
            assert chips[key].focusPolicy() == Qt.FocusPolicy.StrongFocus, key
        # 四颗得真的 Tab 得到：一站一站往后走
        _tab_to(win, qt_app, chips["all"], chips["active"])
        _tab_to(win, qt_app, chips["active"], chips["completed"])
        _tab_to(win, qt_app, chips["completed"], chips["failed"])

        # 点已选中的那颗：不能把自己放倒（互斥改成手工维护后这是唯一防线）
        checked_key = next(k for k, b in chips.items() if b.isChecked())
        chips[checked_key].click()
        qt_app.processEvents()
        assert [b.isChecked() for b in chips.values()] == [
            k == checked_key for k in chips
        ], "点已选中的芯片后选中态掉了"

        # 点另一颗：只有它亮，self._filter 跟着走
        other = next(k for k in chips if k != checked_key)
        chips[other].click()
        qt_app.processEvents()
        assert [b.isChecked() for b in chips.values()] == [
            k == other for k in chips
        ], "点其他芯片后选中态没跟着走"
        assert win._filter == other

        # 程序化切换（_set_compact/_set_sort 之外的入口）也要把视觉刷对
        win._set_filter("completed")
        qt_app.processEvents()
        assert [b.isChecked() for b in chips.values()] == [
            k == "completed" for k in chips
        ]
        assert win._filter == "completed"

# ===== 多选批量操作（与 Web 端 #selectBar 对齐） =====

def _fake_batch_task(tid, status):
    """批量测试用的假任务：只实现 _refresh/_handle_action 会碰到的面。"""

    class _Task:
        def __init__(self):
            self.task_id = tid
            self.status = status
            self.filename = tid + ".bin"
            self.url = "http://example.com/" + tid + ".bin"
            self.filepath = ""
            self.paused = self.resumed = self.retried = False

        def to_dict(self):
            return {"task_id": tid, "filename": self.filename, "status": status,
                    "total_size": 10, "downloaded": 1, "progress": 10, "speed": 0,
                    "eta": "", "url": self.url, "filepath": "", "protocol": "http"}

        def pause(self):
            self.paused = True

        def resume(self):
            self.resumed = True

        def retry(self):
            self.retried = True
            return True

        def cancel(self):
            pass

    return _Task()


class _FakeBatchManager:
    """只回答主窗口问到的几个问题，并记录删除/落盘。"""

    def __init__(self, tasks):
        self.tasks = list(tasks)
        self.removed = []
        self.saved = 0

    def get_all_tasks(self):
        return list(self.tasks)

    def get_task(self, tid):
        return next((t for t in self.tasks if t.task_id == tid), None)

    def remove_task(self, tid):
        self.removed.append(tid)
        self.tasks = [t for t in self.tasks if t.task_id != tid]

    def save_history(self):
        self.saved += 1

    def get_stats(self):
        return {"total_speed": 0.0, "active": 1, "completed": 1, "failed": 1,
                "paused": 0, "total": len(self.tasks)}


@contextlib.contextmanager
def _batch_window(qt_app, monkeypatch, tasks):
    """带假任务的主窗口：卡片是真的，manager 是桩。"""
    import downloader

    mgr = _FakeBatchManager(tasks)
    with _shown_main_window(qt_app, monkeypatch) as win:
        monkeypatch.setattr(downloader, "manager", mgr)
        # 刷新手会触发完成/失败通知（托盘气泡），批量测试不关心，掐掉
        monkeypatch.setattr(win, "_notify_complete", lambda d: None)
        monkeypatch.setattr(win, "_notify_failures", lambda items: None)
        win._refresh()
        # 本机配置里可能存着其它分段（上一轮用「已完成」关的窗口），
        # 批量测试要看得见全部卡片：直接改内存态，不动用户的配置文件
        win._filter = "all"
        win._refresh()
        qt_app.processEvents()
        yield win, mgr


def test_batch_targets_matches_the_web_status_rules():
    """桌面端多选与 Web 端 batchTargets 同一套状态取舍。"""
    import main_window as mw

    task_dict = {
        "dl": {"status": "downloading"},
        "pz": {"status": "paused"},
        "fl": {"status": "failed"},
        "cc": {"status": "cancelled"},
        "ok": {"status": "completed"},
        "pd": {"status": "pending"},
    }
    picked = set(task_dict)
    assert mw.batch_targets(picked, task_dict, "pause") == ["dl"]
    assert mw.batch_targets(picked, task_dict, "resume") == ["pz"]
    assert mw.batch_targets(picked, task_dict, "retry") == ["fl", "cc"]
    assert mw.batch_targets(picked, task_dict, "remove") == list(task_dict)
    # 勾选中途任务被删：不对空气下手；没有勾选时全军覆没
    assert mw.batch_targets(picked | {"ghost"}, task_dict, "remove") == list(task_dict)
    assert mw.batch_targets(set(), task_dict, "pause") == []


def test_task_card_ctrl_click_toggles_multi_select(qt_app):
    """Ctrl+点击卡片 = 切换多选（等效于点左上角勾选框），普通点击仍是单选。"""
    import main_window as mw
    from PyQt6.QtCore import QPointF, Qt
    from PyQt6.QtGui import QMouseEvent

    def press(mods):
        return QMouseEvent(QMouseEvent.Type.MouseButtonPress, QPointF(2, 2),
                           Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton, mods)

    card = mw.TaskCard({"task_id": "t1", "filename": "x.bin",
                        "status": "downloading", "total_size": 10, "downloaded": 1})
    single, toggles = [], []
    card.selected.connect(lambda tid: single.append(tid))
    card.toggled.connect(lambda tid, on: toggles.append((tid, on)))

    card.mousePressEvent(press(Qt.KeyboardModifier.NoModifier))
    assert single == ["t1"]
    assert toggles == []

    card.mousePressEvent(press(Qt.KeyboardModifier.ControlModifier))
    assert single == ["t1"], "Ctrl+点击不该顺带触发单选"
    assert card.property("checked") is True
    card.mousePressEvent(press(Qt.KeyboardModifier.ControlModifier))
    assert card.property("checked") is False
    assert toggles == [("t1", True), ("t1", False)]

    # 主窗口回放勾选态（刷新/换主题后重放）不能再发信号，否则会来回翻烧饼
    card.set_checked(True)
    assert toggles == [("t1", True), ("t1", False)]
    assert card._checked is True


def test_batch_shortcuts_registered_and_escape_prefers_selection(qt_app):
    """Ctrl+A 全选、Esc 先收起多选：快捷键注册与优先级都要在。"""
    import main_window as mw
    from PyQt6.QtWidgets import QLineEdit, QWidget

    class _Host(QWidget):
        def __init__(self):
            super().__init__()
            self._shortcuts = []
            self.url_input = QLineEdit()

        _close_task_detail = mw.MainWindow._close_task_detail
        _on_escape = mw.MainWindow._on_escape
        _select_all_visible = mw.MainWindow._select_all_visible
        _focus_search = mw.MainWindow._focus_search

        def _add_download(self):  # pragma: no cover - 只是槽位
            pass

    host = _Host()
    mw.MainWindow._setup_shortcuts(host)
    seqs = sorted(seq for seq, _ in host._shortcuts)
    assert "Ctrl+A" in seqs and "Escape" in seqs, seqs

    class _Host2:
        def __init__(self, selected):
            self._selected_ids = set(selected)
            self._detail_dialog = None
            self.cleared = 0

        def _clear_selection(self):
            self.cleared += 1
            self._selected_ids = set()

        _close_task_detail = mw.MainWindow._close_task_detail

    host2 = _Host2({"t1"})
    mw.MainWindow._on_escape(host2)
    assert host2.cleared == 1 and host2._selected_ids == set()

    host3 = _Host2(set())
    mw.MainWindow._on_escape(host3)
    assert host3.cleared == 0, "没有多选时 Esc 应该留给详情面板"


def test_batch_bar_drives_checked_tasks_like_the_web_panel(qt_app, monkeypatch):
    """桌面端补齐 Web 端早有的多选批量：勾选 -> 操作条 -> 按状态批量执行。"""
    import main_window as mw

    tasks = [_fake_batch_task("dl", "downloading"), _fake_batch_task("pz", "paused"),
             _fake_batch_task("fl", "failed"), _fake_batch_task("ok", "completed")]
    with _batch_window(qt_app, monkeypatch, tasks) as (win, mgr):
        assert set(win._cards) == {"dl", "pz", "fl", "ok"}
        assert not win.select_bar.isVisible(), "没有勾选时操作条要藏起来"

        # 勾两张卡（走卡片勾选框，与 Ctrl+点击同一条信号）
        win._cards["dl"].pick_box.click()
        win._cards["pz"].pick_box.click()
        qt_app.processEvents()
        assert win._selected_ids == {"dl", "pz"}
        assert win.select_count.text() == "已选 2 项"
        assert win.select_bar.isVisible()
        # 状态过滤与 Web 端一致：下载中的能暂停、暂停了的能继续、没有失败就禁重试
        assert win._batch_btns["pause"].isEnabled()
        assert win._batch_btns["resume"].isEnabled()
        assert not win._batch_btns["retry"].isEnabled()
        assert win._batch_btns["remove"].isEnabled()

        win._batch_btns["pause"].click()
        qt_app.processEvents()
        assert tasks[0].paused and not tasks[1].paused, "只该暂停下载中的那张"
        assert win._selected_ids == set(), "批量完成后要清空多选"
        assert not win.select_bar.isVisible()

        # 删除走确认框；勾失败 + 完成两项，确认后只删勾了的
        monkeypatch.setattr(
            mw.QMessageBox, "question",
            staticmethod(lambda *a, **k: mw.QMessageBox.StandardButton.Yes))
        win._toggle_selection("fl", True)
        win._toggle_selection("ok", True)
        win._batch_action("remove")
        assert mgr.removed == ["fl", "ok"]

        # 任务被外部清掉后，勾选集要跟着收刈，操作条不能再出現
        mgr.tasks.clear()
        win._refresh()
        assert win._selected_ids == set()
        assert not win.select_bar.isVisible()


def test_ctrl_a_and_escape_drive_the_batch_selection(qt_app, monkeypatch):
    """Ctrl+A 全选当前可见任务；Esc 收起多选。焦点在输入框里时都要让位。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    tasks = [_fake_batch_task("dl", "downloading"), _fake_batch_task("pz", "paused")]
    with _batch_window(qt_app, monkeypatch, tasks) as (win, mgr):
        win.search_input.setFocus()
        QTest.keyClick(win, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        qt_app.processEvents()
        assert win._selected_ids == set(), "焦点在搜索框里时 Ctrl+A 是全选文字，不能被抢"

        win.scroll.setFocus()
        QTest.keyClick(win, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        qt_app.processEvents()
        assert win._selected_ids == {"dl", "pz"}
        assert win.select_count.text() == "已选 2 项"

        QTest.keyClick(win, Qt.Key.Key_Escape)
        qt_app.processEvents()
        assert win._selected_ids == set(), "Esc 要先收起多选"
        assert not win.select_bar.isVisible()

def test_ctrl_f_focuses_search_box_and_ctrl_a_selects_visible(qt_app, monkeypatch):
    """Ctrl+F 聚焦搜索框（不是链接框，那与通用「查找」约定相反）；
    Ctrl+A 全选当前可见任务。两条都走真实按键，验证快捷键真的接对了线。"""
    from PyQt6.QtCore import Qt
    from PyQt6.QtTest import QTest

    tasks = [_fake_batch_task("dl", "downloading"), _fake_batch_task("pz", "paused")]
    with _batch_window(qt_app, monkeypatch, tasks) as (win, mgr):
        win.scroll.setFocus()
        qt_app.processEvents()
        QTest.keyClick(win, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier)
        qt_app.processEvents()
        assert win._selected_ids == {"dl", "pz"}
        assert win.select_count.text() == "已选 2 项"

        # 焦点在链接框时按 Ctrl+F：焦点要挪到搜索框（Ctrl+F 不再是「跳到链接框」）
        win.url_input.setFocus()
        qt_app.processEvents()
        QTest.keyClick(win, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
        qt_app.processEvents()
        assert win.focusWidget() is win.search_input, (
            f"Ctrl+F 后焦点在 {_widget_name(win.focusWidget())}，应该落在搜索框")

# ===== 定时任务的取消入口（此前 pending 卡片一个按钮都没有） =====

def test_pending_card_offers_cancel_schedule(qt_app):
    """定时等待中的任务要能「取消定时」：取消登记 + 任务转已取消（比重试轻、比删除轻）。

    此前 pending 在 _build_ui 的三个分支里全部落空，一张按钮都不摆，
    想叫停还没开始的定时任务只能整条删除——而删除在过去甚至不会清调度表。
    """
    import main_window as mw
    from PyQt6.QtWidgets import QPushButton

    scheduled = mw.TaskCard({"task_id": "t1", "filename": "late.iso",
                             "status": "pending", "scheduled_at": 1770000000})
    # findChildren 连隐藏的「···」溢出按钮一起捞，按业务按钮过滤
    action_texts = [b.text() for b in scheduled.findChildren(QPushButton)
                    if b.text() not in ("☐", "···")]
    assert action_texts == ["✕ 取消定时"], action_texts

    # 没有定时信息的 pending（理论上很短命）文案退化成「取消」
    plain = mw.TaskCard({"task_id": "t2", "filename": "x.iso", "status": "pending"})
    plain_texts = [b.text() for b in plain.findChildren(QPushButton)
                   if b.text() not in ("☐", "···")]
    assert plain_texts == ["✕ 取消"], plain_texts


def test_pending_card_context_menu_offers_cancel_schedule(qt_app, monkeypatch):
    """右键菜单与卡片按钮同一套动作：pending -> 取消定时。"""
    import main_window as mw
    from PyQt6.QtCore import QPointF

    card = mw.TaskCard({"task_id": "t1", "filename": "late.iso", "status": "pending",
                        "scheduled_at": 1770000000})
    seen = []
    monkeypatch.setattr(mw.QMenu, "addAction",
                        lambda self, text, slot=None: seen.append(text))
    monkeypatch.setattr(mw.QMenu, "exec", lambda self, pos=None: None)
    card.contextMenuEvent(type("E", (), {"globalPos": staticmethod(lambda: QPointF(0, 0))})())
    assert "取消定时" in seen and "查看详情" in seen, seen


def test_cancel_action_unschedules_before_cancelling(qt_app):
    """取消/删除定时任务必须先取消登记，否则调度表里留着幽灵条目。"""
    import main_window as mw
    import scheduler

    order = []

    class _Task:
        task_id = "t1"
        status = "pending"
        filename = "late.iso"
        url = "http://x/late.iso"
        filepath = ""

        def cancel(self):
            order.append("cancel")

    class _Mgr:
        def __init__(self):
            self.removed = []
            self.saved = 0

        def get_task(self, tid):
            return _Task()

        def remove_task(self, tid):
            self.removed.append(tid)

        def save_history(self):
            self.saved += 1

    class _Bar:
        def __init__(self):
            self.messages = []

        def showMessage(self, text, timeout=0):
            self.messages.append(text)

    class _Host:
        def __init__(self):
            self._mgr = _Mgr()
            self.status_bar = _Bar()

        def _get_manager(self):
            return self._mgr

    class _Sched:
        def unschedule(self, tid):
            order.append(("unschedule", tid))

    orig = scheduler.scheduler
    scheduler.scheduler = _Sched()
    try:
        mw.MainWindow._handle_action(_Host(), "cancel", "t1")
    finally:
        scheduler.scheduler = orig
    assert order == [("unschedule", "t1"), "cancel"], order


def test_remove_task_unschedules_the_task(monkeypatch):
    """删除任务 = 取消登记：桌面端删除定时任务后不能再有幽灵条目。"""
    import threading

    import downloader
    import scheduler

    unscheduled = []
    monkeypatch.setattr(scheduler, "scheduler",
                        type("S", (), {"unschedule": lambda self, t: unscheduled.append(t)})())
    mgr = object.__new__(downloader.DownloadManager)
    mgr._tasks = {}
    mgr._lock = threading.Lock()
    mgr._auto_retry_lock = threading.Lock()
    mgr._auto_retry_state = {}
    mgr.save_history = lambda: None
    mgr.remove_task("ghost-1")
    assert unscheduled == ["ghost-1"], "删除不存在的任务也要清登记（幂等）"
