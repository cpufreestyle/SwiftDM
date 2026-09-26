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
