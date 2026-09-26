"""桌面 UI 新增交互的单元测试（拖入链接解析 / 状态分段过滤）。

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
