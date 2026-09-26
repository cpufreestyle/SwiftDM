"""
SwiftDM 主窗口 —— PyQt6 原生桌面 UI（IDM 风格）
"""
import os
import sys
import io
import csv
import time
import logging
import re
import string
import subprocess
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLineEdit, QLabel, QProgressBar, QScrollArea, QFrame,
    QToolBar, QStatusBar, QSystemTrayIcon, QMenu, QApplication,
    QMessageBox, QFileDialog, QDialog, QDialogButtonBox,
    QFormLayout, QSpinBox, QComboBox, QListWidget, QListWidgetItem,
    QGroupBox,
    QSizePolicy, QSplitter, QHeaderView, QDockWidget, QPlainTextEdit,
    QCheckBox, QDateTimeEdit, QLayout
)
from PyQt6.QtCore import Qt, QTimer, QSize, QSettings, pyqtSignal, QThread, QMimeData, QUrl, QPoint, QDateTime
from PyQt6.QtGui import (QAction, QIcon, QFont, QColor, QPalette, QPixmap,
                     QPainter, QBrush, QDrag, QShortcut, QKeySequence,
                     QPolygon)
from log_helper import setup_logging, QtLogHandler


def format_size(bytes_val):
    if bytes_val <= 0:
        return "未知"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if bytes_val < 1024:
            return f"{bytes_val:.1f} {unit}" if unit != "B" else f"{int(bytes_val)} {unit}"
        bytes_val /= 1024
    return f"{bytes_val:.1f} TB"


def format_speed(bps):
    # a rate is zero when idle, never "unknown" (format_size(0) means that);
    # the extension popup already reports 0 B/s, so match it here too
    if not bps or bps <= 0:
        return "0 B/s"
    return format_size(bps) + "/s"


def _status_title(active, total_speed, total):
    """When downloads are active, surface speed + count in the title bar."""
    if active > 0:
        return f"↓ {format_speed(total_speed)} · {active} 个下载中 - SwiftDM"
    return "SwiftDM - 高速下载管理器"


def _tray_tip(active, total_speed, total, failed=0, countdown=None):
    """Tray tooltip: live speed and running count.

    空闲/最小化时用户只能看到 tooltip，因此附带失败数
    与「全部完成后动作」倒计时（关机/休眠即将触发时必须可见）。
    """
    if active > 0:
        text = f"SwiftDM · ↓{format_speed(total_speed)} · 下载中 {active}/{total}"
    else:
        text = "SwiftDM - 下载管理器"
    if failed > 0:
        text += f" · ✗ {failed} 个失败"
    if countdown:
        text += f" · {countdown}"
    return text


def _clear_confirm_text(n):
    """Destructive clear-finished confirmation copy: state count + consequence."""
    return f"将清除 {n} 个任务（已完成/已失败/已取消），此操作不可恢复。确定继续吗？"


FINISH_ACTIONS = ("none", "shutdown", "suspend", "beep")
FINISH_ACTION_LABELS = {"none": "无动作", "shutdown": "关机",
                        "suspend": "睡眠", "beep": "提示音"}


NAV_INPUT_TYPES = (QLineEdit, QSpinBox, QComboBox, QPlainTextEdit)


def _step_selection(ordered_ids, current_id, delta):
    """在可见任务序列里移动选中项；到头钳制（不循环），空序列返回 None。"""
    if not ordered_ids:
        return None
    if current_id not in ordered_ids:
        return ordered_ids[0] if delta > 0 else ordered_ids[-1]
    idx = ordered_ids.index(current_id)
    nxt = idx + (1 if delta > 0 else -1)
    return ordered_ids[max(0, min(len(ordered_ids) - 1, nxt))]


BATCH_LABELS = {"pause": "暂停", "resume": "继续", "retry": "重试", "remove": "删除"}
BATCH_STATUS = {
    "pause": ("downloading",),        # 与 Web 端 BATCH_STATUS 逐字对齐
    "resume": ("paused",),
    "retry": ("failed", "cancelled"),
    "remove": None,                   # 删除不限状态
}


def batch_targets(selected_ids, task_dict, action):
    """多选集 + 任务快照 -> 该动作真正可执行的 task_id 序列。

    和 Web 端 batchTargets 同一套状态取舍：暂停只点下载中的、继续只点暂停的、
    重试只点失败/取消的，删除不限；已消失（清理/删除）的勾选项顺带滤掉。
    """
    allowed = BATCH_STATUS.get(action)
    picked = set(selected_ids or ())
    ids = [tid for tid in task_dict if tid in picked]
    if allowed is None:
        return ids
    return [tid for tid in ids if task_dict.get(tid, {}).get("status") in allowed]


def _keyboard_nav_allowed(focus_widget):
    """焦点在输入类控件（输入框/下拉/数字框）上时，让位给控件自身的按键处理。"""
    if focus_widget is None:
        return True
    return not isinstance(focus_widget, NAV_INPUT_TYPES)


CARD_HEIGHTS = {"default": (120, 140), "compact": (64, 64)}


def _card_height_bounds(compact):
    """任务卡片高度区间（最小,最大）；紧凑模式压到一行摘要的高度。"""
    return CARD_HEIGHTS["compact"] if compact else CARD_HEIGHTS["default"]


TRAY_ICON_STATES = ("idle", "downloading", "attention")
# 托盘底色取主题令牌：托盘常驻系统托盘，切浅色主题时不能还留一套深色配色
TRAY_ICON_BG_KEY = {"idle": "accent", "downloading": "green", "attention": "red"}
# 箭头的两种前景候选：一深一浅，按对比度选择，就不用每个主题手动配一套
TRAY_ICON_INK = {"light": "#ffffff", "dark": "#0d1117"}


def _relative_luminance(hexcolor):
    """WCAG 相对亮度（0~1），用来判断某个底色上配深色还是白色更清楚。"""

    def _lin(channel):
        channel /= 255.0
        return channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4

    r, g, b = (int(hexcolor[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _pick_readable_fg(bg):
    """在两种前景色里挑对比度更高的那个，因此不必每个主题单独配色。"""
    lum = _relative_luminance(bg)
    dark = (lum + 0.05) / (_relative_luminance(TRAY_ICON_INK["dark"]) + 0.05)
    light = (1.0 + 0.05) / (lum + 0.05)
    return TRAY_ICON_INK["dark"] if dark >= light else TRAY_ICON_INK["light"]


def _tray_icon_state(active, failed):
    """按活跃/失败任务数推导托盘图标状态：下载中 > 有失败 > 空闲。"""
    if active > 0:
        return "downloading"
    if failed > 0:
        return "attention"
    return "idle"


def _tray_icon(icon_state, tokens=None):
    """按状态绘制托盘图标（圆角方块 + 向下箭头；attention 加白色角标）。

    tokens 传当前主题色板，缺省退回 dark。
    """
    tokens = tokens or THEMES["dark"]
    bg = tokens[TRAY_ICON_BG_KEY.get(icon_state, "accent")]
    fg = _pick_readable_fg(bg)
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(bg)))
    painter.drawRoundedRect(2, 4, 28, 24, 6, 6)
    painter.setBrush(QBrush(QColor(fg)))
    painter.drawRoundedRect(14, 8, 4, 12, 2, 2)          # 箭头杆
    painter.drawPolygon(QPolygon([QPoint(11, 17), QPoint(21, 17), QPoint(16, 25)]))
    if icon_state == "attention":
        painter.setBrush(QBrush(QColor(TRAY_ICON_INK["light"])))
        painter.drawEllipse(20, 18, 8, 8)               # 右下角提示点
    painter.end()
    return QIcon(pixmap)


def _tray_bubble(owner, title, text, icon, msecs, kind="info"):
    """弹托盘气泡并记录类型。完成/失败/捕获等提示点了不该有副作用，
    只有「剪贴板链接待确认」气泡被点击后才添加下载。

    收 owner（而非方法）是为了让测试能用鸭子宿主直接驱动这些通知路径。
    """
    owner._tray_msg_kind = kind
    try:
        owner.tray.showMessage(title, text, icon, msecs)
    except Exception:
        pass


def _links_text(urls):
    """把任务链接拼成剪贴板文本（每行一个，去空去重保序）。"""
    seen, out = set(), []
    for u in urls:
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return "\n".join(out)


def _tasks_export_text(task_dict, ordered_ids):
    """把任务导出为 CSV 文本（按给定顺序）；无任务时仍有表头。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["文件名", "链接", "状态", "总大小", "已下载", "速度"])
    for tid in ordered_ids:
        d = task_dict.get(tid) or {}
        writer.writerow([d.get("filename", ""), d.get("url", ""),
                         d.get("status", ""), d.get("total_size", 0),
                         d.get("downloaded", 0), d.get("speed", 0)])
    return buf.getvalue()


def _reason_hint(reason):
    """已知失败原因码 -> 可操作提示（与 Web 端 REASON_HINTS 同源，取 media._MEDIA_HINTS）。"""
    if not reason:
        return ""
    try:
        from media import _MEDIA_HINTS
    except Exception:
        return ""
    return _MEDIA_HINTS.get(reason, "")


def _fail_summary_text(items):
    """把一批新失败任务汇总成一条状态栏文案；空列表返回 None。

    单个失败保留文件名 + 原因；多个失败统计数量并去重后列出前两个不同原因。
    """
    if not items:
        return None
    if len(items) == 1:
        err = (items[0].get("error") or "").strip()
        name = items[0].get("filename") or "文件"
        return f"✗ 下载失败 [{name}]: {err}" if err else f"✗ 下载失败: {name}"
    reasons = []
    for it in items:
        reason = (it.get("error") or "").strip()
        if reason and reason not in reasons:
            reasons.append(reason)
    if not reasons:
        return f"✗ {len(items)} 个任务下载失败"
    shown = "、".join(reasons[:2]) + ("等" if len(reasons) > 2 else "")
    return f"✗ {len(items)} 个任务下载失败（{shown}）"


def _auto_retry_hint(auto_retry_at):
    """失败任务的自动重试倒计时文案（无计划返回 None）。"""
    if not auto_retry_at:
        return None
    remain = int(auto_retry_at - time.time())
    if remain <= 0:
        return "↻ 即将自动重试"
    return f"↻ {remain}s 后自动重试"


def _card_status_text(status, scheduled_at=None, auto_retry_at=None):
    """任务卡片状态文案；定时等待中的 pending 显示「⏰ 定时等待」而非「等待中」；
    失败且排了自动重试时附带倒计时，避免用户误以为任务已终止。"""
    if status == "pending" and scheduled_at:
        return "⏰ 定时等待"
    text = {
        "downloading": "● 下载中", "paused": "⏸ 已暂停",
        "completed": "✓ 完成", "failed": "✗ 失败",
        "pending": "⏳ 等待中", "cancelled": "✗ 已取消"
    }.get(status, status)
    if status == "failed":
        hint = _auto_retry_hint(auto_retry_at)
        if hint:
            text += f"  ·  {hint}"
    return text


def _clipboard_download_url(text):
    """剪贴板里“整段就是一个下载链接”时返回它，否则 None。

    只认 http/https/magnet 且不含空格/换行：用户把链接贴进聊天窗口
    时不会误触发；一段文字里只包含链接时安静忽略（宁放过）。
    """
    t = (text or "").strip()
    if not t or any(c.isspace() for c in t):
        return None
    if t.lower().startswith(("http://", "https://", "magnet:?")):
        return t
    return None


def _overflow_plan(widths, avail, spacing):
    """窄宽度收纳方案：从尾部开始隐藏按钮，直到剩余按钮能并排放下。

    widths: 各按钮宽度（显示顺序）；avail: 可用像素；spacing: 按钮间距。
    返回 (可见按钮个数, 溢出按钮下标列表)，溢出项保持原顺序。
    """
    n = len(widths)
    visible = n
    while visible > 0:
        need = sum(widths[:visible]) + spacing * (visible - 1)
        if need <= avail:
            break
        visible -= 1
    return visible, list(range(visible, n))


def _scheduled_suffix(scheduled_at):
    """定时任务的「⏰ MM-DD HH:MM 开始」后缀；未定时返回空串。"""
    if not scheduled_at:
        return ""
    return "  ⏰ " + time.strftime("%m-%d %H:%M", time.localtime(scheduled_at)) + " 开始"


def _detail_size_text(downloaded, total):
    """详情行的「已下载 / 总大小」文案；总大小未知时只报已下载量。"""
    if total > 0:
        return f"{format_size(downloaded)} / {format_size(total)}"
    if downloaded > 0:
        return f"{format_size(downloaded)}（总大小未知）"
    return "-"


def _detail_rows(task_data):
    """任务详情面板的信息行 [(标签, 值), ...]。

    直链 / 媒体 / 磁力三类任务共有字段各一行，再按类型追加专属行；
    失败任务附失败原因与可操作建议（与卡片上的提示同源）。
    """
    d = task_data or {}
    rows = [
        ("文件名", d.get("filename") or "-"),
        ("任务 ID", d.get("task_id") or "-"),
        ("状态", _card_status_text(d.get("status") or "", d.get("scheduled_at"),
                                  d.get("auto_retry_at") or 0)),
        ("进度", f"{float(d.get('progress') or 0):.1f}%"),
        ("已下载 / 总大小", _detail_size_text(d.get("downloaded") or 0,
                                            d.get("total_size") or 0)),
        ("下载速度", format_speed(d.get("speed") or 0)
         if (d.get("speed") or 0) > 0 and d.get("status") == "downloading" else "-"),
        ("预计剩余", d.get("eta") or "-"),
        ("保存目录", d.get("save_dir") or d.get("filepath") or "-"),
    ]
    if (d.get("kind") or "http") == "torrent":
        rows += [
            ("传输协议", d.get("protocol") or "-"),
            ("种子 / 同伴", f"{d.get('seeds') or 0} / {d.get('peers') or 0}"),
        ]
    if d.get("resolution"):
        rows.append(("分辨率", f"{d['resolution']}p"))
    err = (d.get("error") or "").strip()
    if err:
        rows.append(("失败原因", err))
        hint = _reason_hint(d.get("error_reason"))
        if hint:
            rows.append(("处理建议", hint))
    if d.get("url"):
        rows.append(("下载链接", d["url"]))
    return rows


def _segment_rows(task_data):
    """分段进度明细 [{"done": int, "total": int}, ...]；不足两段返回 []。

    每段的实际字节区间由 to_dict 的 segments_offsets 还原（最后一段吃掉余数），
    与 downloader._calc_segments 的切分一致；单段任务的整体进度条已等价，
    无需逐段展示。
    """
    d = task_data or {}
    offsets = d.get("segments_offsets") or []
    if not isinstance(offsets, (list, tuple)) or len(offsets) < 2:
        return []
    progress = d.get("segments_progress") or []
    rows = []
    for i, span in enumerate(offsets):
        try:
            start, end = int(span[0]), int(span[1])
        except (TypeError, ValueError, IndexError):
            continue
        total = max(end - start + 1, 0)
        try:
            done = int(progress[i])
        except (TypeError, ValueError, IndexError):
            done = 0
        rows.append({"done": min(done, total) if total else done, "total": total})
    return rows


def _scheduled_at(task_id):
    """任务定时启动时间（epoch 秒）；未定时返回 None。与 Web 端 _with_schedule 同源。"""
    try:
        from scheduler import scheduler as _dl_scheduler
        return _dl_scheduler.pending_at(task_id)
    except Exception:
        return None


def _finish_countdown_text(action, remaining):
    """「全部下载完成后动作」工具栏文案；无动作/已到点/非法值返回 None 表示隐藏按钮。"""
    if not action or action == "none":
        return None
    try:
        left = int(remaining or 0)
    except (TypeError, ValueError):
        return None
    if left <= 0:
        return None
    label = FINISH_ACTION_LABELS.get(action, action)
    return f"{left}s 后{label}"


def _window_settings():
    """窗口几何信息（大小/位置）持久化，重启后恢复上次布局。"""
    return QSettings("SwiftDM", "SwiftDM")


def _restore_window_geometry(win, settings):
    """按上次保存的几何信息恢复窗口；无记录时返回 False。"""
    data = settings.value("window/geometry")
    if not data:
        return False
    try:
        return bool(win.restoreGeometry(data))
    except Exception:
        return False


def _save_window_geometry(win, settings):
    """保存窗口几何信息（隐藏到托盘/退出前调用）。"""
    try:
        settings.setValue("window/geometry", win.saveGeometry())
    except Exception:
        pass


def _parse_rate_kbps(text):
    """解析设置面板的限速输入（KB/s，空或 0 = 不限速）；非法输入返回 None。"""
    s = str(text or "").strip()
    if not s:
        return 0
    try:
        value = float(s)
    except ValueError:
        return None
    if value < 0:
        return None
    return int(value * 1024)


def _format_rate_kbps(bytes_per_sec):
    """字节/秒 格式化为 KB/s 文本（不限速时返回空串）。"""
    try:
        value = int(bytes_per_sec or 0)
    except (TypeError, ValueError):
        return ""
    return "" if value <= 0 else str(value // 1024)


SORT_KEYS = ("default", "name", "size", "progress", "speed")
SORT_LABELS = {"default": "默认", "name": "文件名", "size": "大小",
               "progress": "进度", "speed": "速度"}


def _sort_key_for(data, key):
    """返回任务在指定排序键下的值；倒序指标（大小/进度/速度）取负。"""
    if key == "name":
        return str(data.get("filename", "")).lower()
    if key == "size":
        return -int(data.get("total_size") or 0)
    if key == "progress":
        total = int(data.get("total_size") or 0)
        done = int(data.get("downloaded") or 0)
        return -(done / total) if total > 0 else 0.0
    if key == "speed":
        return -int(data.get("speed") or 0)
    return 0


def _sorted_task_ids(task_dict, sort_key):
    """按排序键返回任务 id 顺序；default 保持添加顺序（同值时用 id 破平，结果稳定）。"""
    if sort_key == "default":
        return list(task_dict)
    return sorted(task_dict, key=lambda tid: (_sort_key_for(task_dict[tid], sort_key), tid))


def open_in_system(path):
    """跨平台用系统默认程序打开文件/目录（os.startfile 仅 Windows 可用）。"""
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa: P201  (Windows only)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# ==================== 样式表 ====================
THEMES = {
    "dark": {
        "bg": "#0f0f14",
        "surface": "#1a1a23",
        "surface2": "#22222e",
        "surface3": "#2b2b3d",
        "input": "#1a1a26",
        "toolbar": "#16161f",
        "hover": "#2e2e3e",
        "selected": "#20202e",
        "border": "#2a2a3a",
        "borderHover": "#3a3a52",
        "text": "#e0e0e8",
        "textMuted": "#8888a0",
        "textStrong": "#ffffff",
        "faint": "#555555",
        "scroll": "#0f0f14",
        "scrollHandle": "#2a2a3a",
        "scrollHover": "#3a3a4a",
        "logBg": "#0a0a0f",
        "logFg": "#cfcfe0",
        "accent": "#6c5ce7",
        "accentHover": "#7d6ff0",
        "accent2": "#a29bfe",
        "onAccent": "#ffffff",
        "green": "#00d2a0",
        "orange": "#ffa502",
        "orangeSoft": "#3d3320",
        "orangeHover": "#4d3f28",
        "red": "#ff5e7a",
        "redSoft": "#3a1720",
        "redText": "#ffb3c0",
        "blue": "#4da6ff",
    },
    "light": {
        "bg": "#f4f5fa",
        "surface": "#ffffff",
        "surface2": "#eceef6",
        "surface3": "#dfe3ee",
        "input": "#ffffff",
        "toolbar": "#ffffff",
        "hover": "#e4e7f0",
        "selected": "#ece9ff",
        "border": "#d8dce8",
        "borderHover": "#c0c6d8",
        "text": "#1b1e28",
        "textMuted": "#5c6478",
        "textStrong": "#11132a",
        "faint": "#9aa0b4",
        "scroll": "#f4f5fa",
        "scrollHandle": "#c9cddb",
        "scrollHover": "#aab0c4",
        "logBg": "#f7f8fc",
        "logFg": "#232838",
        "accent": "#6c5ce7",
        "accentHover": "#5b4bd6",
        "accent2": "#8f80ff",
        "onAccent": "#ffffff",
        "green": "#0a9d7c",
        "orange": "#d97a00",
        "orangeSoft": "#fbeed8",
        "orangeHover": "#f6dfb4",
        "red": "#df4660",
        "redSoft": "#fbe4e8",
        "redText": "#c22947",
        "blue": "#2b7fd4",
    },
}

QSS_TEMPLATE = string.Template("""
QMainWindow {
    background-color: $bg;
}
QWidget {
    color: $text;
    font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
    font-size: 13px;
}
QToolBar {
    background-color: $toolbar;
    border-bottom: 1px solid $border;
    padding: 6px 10px;
    spacing: 8px;
}
QToolBar QPushButton {
    background-color: $surface2;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 7px 16px;
    color: $text;
    font-weight: 600;
    font-size: 12px;
}
QToolBar QPushButton:hover {
    background-color: $hover;
    border-color: $accent;
}
QToolBar QPushButton#btnAdd {
    background-color: $accent;
    color: $onAccent;
    border: none;
}
QToolBar QPushButton#btnAdd:hover {
    background-color: $accentHover;
}
QToolBar QPushButton#btnFinishCountdown {
    background-color: $orangeSoft;
    color: $orange;
    border: 1px solid $orange;
}
QToolBar QPushButton#btnFinishCountdown:hover {
    background-color: $orangeHover;
}
QLineEdit {
    background-color: $input;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 8px 14px;
    color: $text;
    font-size: 13px;
    selection-background-color: $accent;
}
QLineEdit:focus {
    border-color: $accent;
}
QScrollArea {
    border: none;
    background-color: transparent;
}
QScrollBar:vertical {
    background: $scroll;
    width: 8px;
    border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: $scrollHandle;
    border-radius: 4px;
    min-height: 40px;
}
QScrollBar::handle:vertical:hover {
    background: $scrollHover;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QStatusBar {
    background-color: $toolbar;
    border-top: 1px solid $border;
    color: $textMuted;
    font-size: 12px;
}
QProgressBar {
    background-color: $input;
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    font-size: 0px;
}
QProgressBar::chunk {
    background-color: $accent;
    border-radius: 3px;
}
QMenu {
    background-color: $surface;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 4px;
}
QMenu::item {
    padding: 8px 30px;
    border-radius: 4px;
    color: $text;
}
QMenu::item:selected {
    background-color: $hover;
}
QMenu::separator { height:1px; background:$border; margin:4px 10px; }
QLabel#titleLabel {
    font-size: 16px;
    font-weight: 700;
    color: $textStrong;
}
QLabel#speedLabel {
    color: $accent2;
    font-weight: 700;
    font-size: 13px;
}
QLabel#overallLabel {
    font-size: 12px;
    color: $textMuted;
    margin-left: 14px;
}
QLabel#statsLabel {
    font-size: 12px;
    color: $textMuted;
}
QLabel#emptyLabel {
    font-size: 15px;
    color: $faint;
    padding: 60px;
}
QLabel#monitorLabel {
    font-size: 11px;
    font-weight: 600;
}
QLabel#monitorLabel[on="true"] { color: $green; }
QLabel#monitorLabel[on="false"] { color: $red; }
QWidget#appHeader {
    background-color: $toolbar;
    border-bottom: 1px solid $border;
}
QWidget#appCentral {
    background-color: $bg;
}
QDialog {
    background-color: $surface;
}
QSpinBox, QComboBox, QDateTimeEdit {
    background-color: $surface2;
    border: 1px solid $border;
    border-radius: 4px;
    padding: 5px 8px;
    color: $text;
}
QSpinBox:focus, QComboBox:focus, QDateTimeEdit:focus {
    border-color: $accent;
}
QComboBox QAbstractItemView {
    background-color: $surface;
    border: 1px solid $border;
    selection-background-color: $hover;
    color: $text;
}
QHeaderView::section {
    background-color: $toolbar;
    border: none;
    border-bottom: 1px solid $border;
    padding: 6px;
    color: $textMuted;
    font-weight: 600;
}
QPushButton#filterBtn {
    background:$input; border:1px solid $border; border-radius:13px;
    padding:4px 14px; color:$textMuted; font-size:12px; font-weight:600;
}
QPushButton#filterBtn:hover { color:$text; border-color:$borderHover; }
QPushButton#filterBtn:checked { background:$accent; border-color:$accent; color:$onAccent; }

/* 键盘导航提示：与 Web 端 .kbd-hint 同文案、同色阶（textMuted 与 Web 端 --text2 同值）。
   ↑↓/Enter 的快捷键早就接好了（见 _setup_shortcuts），但桌面端此前没有任何地方提到它，
   纯键盘用户不会知道可以这么用；样式与 Web 端同为 11px 弱化色，不抢筛选芯片的视觉。 */
QLabel#kbdHint { font-size: 11px; color: $textMuted; }

/* ===== 多选批量操作（与 Web 端 .select-bar 对齐） ===== */
QWidget#selectBar {
    background-color: $surface;
    border-top: 1px solid $border;
}
QLabel#selectCount { font-size: 12px; color: $textMuted; }
QPushButton#selBtn {
    background-color: $surface2;
    border: 1px solid $border;
    border-radius: 6px;
    padding: 4px 12px;
    color: $text;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#selBtn:hover { background-color: $hover; border-color: $borderHover; }
QPushButton#selBtn:disabled { color: $faint; border-color: $border; }
QPushButton#selBtn:focus { border: 1px solid $accent; background-color: $hover; }
QPushButton#selBtnDanger {
    background-color: $surface2;
    border: 1px solid $red;
    border-radius: 6px;
    padding: 4px 12px;
    color: $red;
    font-size: 12px;
    font-weight: 600;
}
QPushButton#selBtnDanger:hover { background-color: $redSoft; border-color: $red; }
QPushButton#selBtnDanger:disabled { color: $faint; border-color: $border; }
QPushButton#selBtnDanger:focus { border: 1px solid $red; background-color: $redSoft; }

/* ===== 键盘焦点环 =====
   QPushButton 全部由 QSS 重画，原生焦点框被吃掉，纯键盘用户完全看不到焦点落在哪。
   这里补一条与 Web 端 :focus-visible 同语义的强调色描边；仅键盘聚焦时显形，鼠标点击不出现。 */
QToolBar QPushButton:focus {
    border: 1px solid $accent;
    background-color: $hover;
}
QToolBar QPushButton#btnAdd:focus {
    border: 1px solid $onAccent;
    background-color: $accentHover;
}
QToolBar QPushButton#btnFinishCountdown:focus {
    border: 1px solid $orange;
    background-color: $orangeHover;
}
QPushButton#filterBtn:focus {
    border: 1px solid $accent;
    background-color: $hover;
}
/* 已选中的芯片底色就是强调色，描边改用正文色才看得见 */
QPushButton#filterBtn:checked:focus {
    border: 1px solid $text;
    background-color: $accent;
}
QToolTip {
    background-color: $surface2;
    color: $text;
    border: 1px solid $border;
    padding: 4px;
}
""")


def _qss_for(theme):
    """按主题渲染全局样式表；未知主题名回退暗色。"""
    return QSS_TEMPLATE.substitute(THEMES.get(theme, THEMES["dark"]))


def _status_colors(tokens):
    """任务状态 -> 语义色（与 Web 端同名 token 同色，亮/暗两套）。"""
    return {
        "downloading": tokens["accent2"],
        "paused": tokens["orange"],
        "completed": tokens["green"],
        "failed": tokens["red"],
        "pending": tokens["textMuted"],
        "cancelled": tokens["textMuted"],
    }


class TaskCard(QFrame):
    """单个下载任务卡片"""
    action_triggered = pyqtSignal(str, str)  # action, task_id
    selected = pyqtSignal(str)              # task_id：点击卡片即选中（键盘导航配合）
    activated = pyqtSignal(str)             # task_id：双击卡片（打开任务详情）
    toggled = pyqtSignal(str, bool)         # task_id, checked：勾选/取消多选

    def __init__(self, task_data, parent=None, theme=None):
        super().__init__(parent)
        self.task_id = task_data["task_id"]
        self._theme = theme if theme in THEMES else "dark"
        self._tokens = THEMES[self._theme]
        self._semantic_btns = []  # [(按钮, 语义色键)]：切换主题时按键重刷
        self._checked = False     # 多选勾选态（批量操作条的作用域）
        self._overflow_btns = []  # [(按钮, action)]：底部操作按钮，窄窗口时收进“···”菜单
        self._hidden_actions = []
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.filepath = task_data.get("filepath", "")
        self._drag_start_pos = None
        self._built_status = task_data.get("status", "pending")  # 卡片按钮按此状态生成
        self.url = task_data.get("url", "")
        self.setObjectName("taskCard")
        self.setStyleSheet(self._card_qss())
        self._compact = False
        self._error_text = ""
        self._error_reason = ""
        self.setMinimumHeight(CARD_HEIGHTS["default"][0])
        self._base_max_height = CARD_HEIGHTS["default"][1]
        self.setMaximumHeight(self._base_max_height)
        self._build_ui(task_data)

    def _card_qss(self):
        """卡片外壳样式（背景/边框/选中态全部取自主题 token）。"""
        t = self._tokens
        return f"""
            TaskCard {{
                background-color: {t['surface']};
                border: 1px solid {t['border']};
                border-radius: 8px;
                padding: 2px;
            }}
            TaskCard:hover {{
                border-color: {t['borderHover']};
            }}
            TaskCard[selected="true"] {{
                border: 1px solid {t['accent']};
                background-color: {t['selected']};
            }}
            TaskCard[checked="true"] {{
                border: 1px solid {t['accent2']};
                background-color: {t['selected']};
            }}
        """

    def _status_qss(self, status):
        """状态徽标样式：文字 + 22 alpha 同色底，保证亮/暗都协调。"""
        color = _status_colors(self._tokens).get(status, self._tokens["textMuted"])
        return (f"font-size: 11px; font-weight: 600; color: {color}; "
                f"background-color: {color}22; border-radius: 10px; padding: 2px 10px;")

    def _progress_qss(self, status):
        """进度条样式：完成=绿、暂停=橙、其余=主题强调色。"""
        chunk = {"completed": self._tokens["green"],
                 "paused": self._tokens["orange"]}.get(status, self._tokens["accent"])
        return (f"QProgressBar{{background:{self._tokens['input']};border:none;border-radius:3px;height:6px;}}"
                f"QProgressBar::chunk{{background:{chunk};border-radius:3px;}}")

    def _pick_box_qss(self, tokens):
        """多选勾选框：小方块描边，选中填强调色；焦点环与其它卡片按钮同规格。"""
        t = tokens
        return (
            f"QPushButton{{background:transparent;border:1px solid {t['border']};"
            f"border-radius:4px;color:{t['textMuted']};font-size:13px;padding:0px;"
            f"min-width:18px;max-width:18px;min-height:18px;max-height:18px;}}"
            f"QPushButton:hover{{border-color:{t['borderHover']};color:{t['text']};}}"
            f"QPushButton:checked{{color:{t['accent']};border-color:{t['accent']};}}"
            f"QPushButton:focus{{border:1px solid {t['accent']};background-color:{t['hover']};}}"
        )

    def _error_qss(self):
        """失败原因条样式（浅色主题用深红字 + 浅红底，保持可读）。"""
        t = self._tokens
        return (f"font-size: 11px; color: {t['redText']}; background-color: {t['redSoft']}; "
                f"border: 1px solid {t['red']}55; border-radius: 6px; padding: 6px 8px; margin-top: 2px;")

    def _restyle_labels(self):
        """按当前主题重刷卡片内所有内联样式（建卡与切换主题共用）。"""
        t = self._tokens
        self.name_label.setStyleSheet(
            f"font-size: 13px; font-weight: 600; color: {t['textStrong']};")
        self.prog_label.setStyleSheet(
            f"font-size: 12px; font-weight: 700; color: {t['textStrong']}; min-width: 42px;")
        self.size_label.setStyleSheet(f"font-size: 11px; color: {t['textMuted']};")
        self.eta_label.setStyleSheet(f"font-size: 11px; color: {t['textMuted']};")
        self.speed_label.setStyleSheet(
            f"font-size: 11px; color: {t['accent2']}; font-weight: 600;")
        self.error_label.setStyleSheet(self._error_qss())
        self.pick_box.setStyleSheet(self._pick_box_qss(self._tokens))
        self.status_label.setStyleSheet(self._status_qss(self._built_status))
        self.progress_bar.setStyleSheet(self._progress_qss(self._built_status))
        for btn, key in self._semantic_btns:
            btn.setStyleSheet(self._btn_style(t[key]))

    def _add_action_btn(self, layout, text, color_key, action, tooltip=None,
                        overflow=True):
        """新增卡片操作按钮；color_key 指向主题 token，切主题时按键重刷。

        overflow=False 表示该按钮不参与窄窗口收纽（如顶栏图标）。
        """
        btn = QPushButton(text)
        if tooltip:
            btn.setToolTip(tooltip)
            # 图标按钮的文字部分唯一能拿当可访问名，把工具提示同步过去
            btn.setAccessibleName(tooltip)
        btn.setStyleSheet(self._btn_style(self._tokens[color_key]))
        btn.clicked.connect(lambda: self.action_triggered.emit(action, self.task_id))
        layout.addWidget(btn)
        self._semantic_btns.append((btn, color_key))
        if overflow:
            self._overflow_btns.append((btn, action))

    def apply_theme(self, theme):
        """切换主题：外壳 + 内部标签/按钮一起刷新，状态语义色不变。"""
        if theme not in THEMES:
            return
        self._theme = theme
        self._tokens = THEMES[theme]
        self.setStyleSheet(self._card_qss())
        self._restyle_labels()

    def set_selected(self, on):
        """选中态：高亮边框；配合 dynamic property 重算样式。"""
        self.setProperty("selected", bool(on))
        self.style().unpolish(self)
        self.style().polish(self)

    def set_checked(self, on):
        """多选态：勾选框 + 强调色边框；由主窗口统一回放，不回发 toggled 信号。"""
        on = bool(on)
        if on == self._checked:
            return
        # 回放时勾选框状态可能还是旧的（卡片重建过），blockSignals 防止回灌
        self.pick_box.blockSignals(True)
        self.pick_box.setChecked(on)
        self.pick_box.blockSignals(False)
        self._apply_checked_state(on)

    def _apply_checked_state(self, on):
        """把勾选态画到自己身上：☐/☑ 字形 + 卡片描边。"""
        self._checked = bool(on)
        self.pick_box.setText("☑" if on else "☐")
        self.setProperty("checked", self._checked)
        self.style().unpolish(self)
        self.style().polish(self)

    def _on_pick_toggled(self, on):
        """用户勾选/取消勾选：先刷自己的视觉态，再转告主窗口维护多选集合。"""
        self._apply_checked_state(on)
        self.toggled.emit(self.task_id, bool(on))

    def _build_ui(self, task_data):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        # 第一行：文件名 + 状态
        top = QHBoxLayout()
        top.setSpacing(10)

        # 多选勾选框：与 Web 端卡片左上角的 .task-pick-box 对齐；
        # Ctrl+点击卡片空白处等效于点它（见 mousePressEvent）
        self.pick_box = QPushButton("☐")
        self.pick_box.setObjectName("pickBox")
        self.pick_box.setCheckable(True)
        self.pick_box.setChecked(False)
        # 与其它卡片按钮同一套约定：tooltip == accessibleName（读屏用户也不能少），
        # 「Ctrl+点击卡片」这类富提示走 accessibleDescription
        self.pick_box.setToolTip("选择任务")
        self.pick_box.setAccessibleName("选择任务")
        self.pick_box.setAccessibleDescription("Ctrl+点击任务卡片也可以勾选；Esc 清空多选")
        self.pick_box.setCursor(Qt.CursorShape.PointingHandCursor)
        self.pick_box.setStyleSheet(self._pick_box_qss(self._tokens))
        self.pick_box.toggled.connect(self._on_pick_toggled)
        top.addWidget(self.pick_box)

        name = task_data.get("filename", "unknown")
        self.name_label = QLabel(name)
        self.name_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.name_label.setToolTip(name)
        top.addWidget(self.name_label, 1)

        status = task_data.get("status", "pending")
        status_text = {
            "downloading": "● 下载中", "paused": "⏸ 已暂停",
            "completed": "✓ 完成", "failed": "✗ 失败",
            "pending": "⏳ 等待中", "cancelled": "✗ 已取消"
        }
        self.status_label = QLabel(_card_status_text(
            status, task_data.get("scheduled_at"), task_data.get("auto_retry_at", 0)))
        top.addWidget(self.status_label)
        if self.url:
            # 复制链接从右键菜单提升到卡片顶栏：单任务复制不该藏两级菜单
            self._add_action_btn(top, "📋", "textMuted", "copy_link",
                                 "复制下载链接", overflow=False)
        layout.addLayout(top)

        # 进度条
        prog = task_data.get("progress", 0)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(int(prog))
        self.progress_bar.setTextVisible(False)
        # 文字不可见，不给可访问名的话读屏软件对这条什么都不会说
        self.progress_bar.setAccessibleName("下载进度")

        prog_layout = QHBoxLayout()
        prog_layout.setSpacing(8)
        prog_layout.addWidget(self.progress_bar, 1)
        self.prog_label = QLabel(f"{prog:.1f}%")
        self.prog_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        prog_layout.addWidget(self.prog_label)
        layout.addLayout(prog_layout)

        # 第三行：大小、速度、ETA + 操作按钮
        bottom = QHBoxLayout()
        bottom.setSpacing(14)

        total_size = task_data.get("total_size", 0)
        downloaded = task_data.get("downloaded", 0)
        speed = task_data.get("speed", 0)
        eta = task_data.get("eta", "")

        info_text = f"📦 {format_size(downloaded)}"
        if total_size > 0:
            info_text += f" / {format_size(total_size)}"
        if task_data.get("protocol") == "torrent":
            seeds = task_data.get("seeds", 0)
            peers = task_data.get("peers", 0)
            info_text += f"  🌱 {seeds}  👥 {peers}"
        info_text += _scheduled_suffix(task_data.get("scheduled_at"))
        self.size_label = QLabel(info_text)
        bottom.addWidget(self.size_label)

        self.speed_label = QLabel(f"⚡ {format_speed(speed)}" if speed > 0 else "")
        bottom.addWidget(self.speed_label)

        self.eta_label = QLabel(f"⏱ {eta}" if eta else "")
        bottom.addWidget(self.eta_label)

        bottom.addStretch(1)

        # 操作按钮
        if status == "downloading":
            self._add_action_btn(bottom, "⏸ 暂停", "orange", "pause")
            self._add_action_btn(bottom, "✕ 取消", "red", "cancel")

        elif status == "paused":
            self._add_action_btn(bottom, "▶ 继续", "green", "resume")
            self._add_action_btn(bottom, "✕ 取消", "red", "cancel")

        elif status in ("failed", "cancelled", "completed"):
            if status in ("failed", "cancelled"):
                self._add_action_btn(bottom, "↻ 重试", "blue", "retry")
            self._add_action_btn(bottom, "🗑 删除", "red", "remove")

        if status == "completed" and task_data.get("total_size", 0) > 0:
            self._add_action_btn(bottom, "📂 打开文件", "blue", "open")
            self._add_action_btn(bottom, "🗁 打开文件夹", "accent2", "open_folder")
        elif status in ("downloading", "paused", "failed", "cancelled"):
            # 非 pending 状态都可能已有分片残留：一键定位目录，方便排查/手动续传
            self._add_action_btn(bottom, "🗁 打开文件夹", "accent2", "open_folder")

        layout.addLayout(bottom)

        # “···”溢出按钮：窄窗口时承载放不下的操作（默认隐藢）
        self._more_btn = QPushButton("···")
        self._more_btn.setToolTip("更多操作")
        self._more_btn.setAccessibleName("更多操作")
        self._more_btn.setStyleSheet(self._btn_style(self._tokens["textMuted"]))
        self._more_btn.clicked.connect(self._show_overflow_menu)
        self._more_btn.hide()
        bottom.addWidget(self._more_btn)
        self._semantic_btns.append((self._more_btn, "textMuted"))

        # 允许卡片比内容窄：否则布局最小宽度会把父容器拉出水平滚动条，
        # 按钮放不下时收进“···”菜单才能生效
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        self.setMinimumWidth(200)
        self._apply_overflow()

        # 失败原因（默认隐藏，失败且有错误信息时显示）
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        layout.addWidget(self.error_label)
        # 所有内联样式按当前主题统一刷一遍（切主题时也走这里）
        self._restyle_labels()
        self._apply_error_visibility(status, task_data.get("error", ""),
                                     task_data.get("error_reason", ""))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_overflow()

    def _buttons_avail_width(self):
        """底部按钮区可用宽度：卡片宽度减去边距和左侧信息标签。"""
        avail = self.width() - 28
        for lbl in (self.size_label, self.speed_label, self.eta_label):
            if not lbl.isHidden():
                avail -= lbl.sizeHint().width() + 14
        return max(avail, 0)

    def _apply_overflow(self):
        """窗口宽度变化时重算：放不下的操作按钮进“···”菜单。"""
        if not self._overflow_btns:
            return
        widths = [btn.sizeHint().width() for btn, _ in self._overflow_btns]
        visible_n, hidden_idx = _overflow_plan(widths, self._buttons_avail_width(), 14)
        for i, (btn, _action) in enumerate(self._overflow_btns):
            btn.setVisible(i < visible_n)
        self._hidden_actions = [self._overflow_btns[i] for i in hidden_idx]
        self._more_btn.setVisible(bool(hidden_idx))

    def _show_overflow_menu(self):
        """“···”菜单：执行被收起来的操作（与点按钮等效）。"""
        if not self._hidden_actions:
            return
        menu = QMenu(self)
        for btn, action in self._hidden_actions:
            menu.addAction(btn.text(),
                           lambda a=action: self.action_triggered.emit(a, self.task_id))
        menu.exec(self._more_btn.mapToGlobal(
            self._more_btn.rect().bottomLeft()))
        return menu   # 返回菜单便于测试断言；产线上点击处理器忽略返回值

    # 允许从已完成卡片的文件区域拖出文件（如拖到资源管理器、聊天窗口等）
    def mousePressEvent(self, event):
        if (event.button() == Qt.MouseButton.LeftButton
                and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
            # Ctrl+点击只切换多选，不触发拖拽（拖文件出门是无修饰键的手势）
            self.pick_box.click()
            self._drag_start_pos = None
        else:
            self.selected.emit(self.task_id)
            if (event.button() == Qt.MouseButton.LeftButton
                    and self.filepath and os.path.exists(self.filepath)):
                self._drag_start_pos = event.pos()
            else:
                self._drag_start_pos = None
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        """双击卡片打开任务详情面板（Enter 打开文件之外的“先看状态”入口）。"""
        self.activated.emit(self.task_id)
        super().mouseDoubleClickEvent(event)

    def mouseMoveEvent(self, event):
        if self._drag_start_pos is None or not self.filepath:
            super().mouseMoveEvent(event)
            return
        if event.buttons() != Qt.MouseButton.LeftButton:
            super().mouseMoveEvent(event)
            return
        if (event.pos() - self._drag_start_pos).manhattanLength() < QApplication.startDragDistance():
            super().mouseMoveEvent(event)
            return
        drag = QDrag(self)
        mime = QMimeData()
        url = QUrl.fromLocalFile(self.filepath)
        mime.setUrls([url])
        mime.setText(self.filepath)
        drag.setMimeData(mime)
        # 拖拽时跟随光标显示一个半透明缩略图
        pixmap = self.grab()
        if not pixmap.isNull():
            drag.setPixmap(pixmap)
        drag.exec(Qt.DropAction.CopyAction | Qt.DropAction.MoveAction)
        self._drag_start_pos = None

    def _btn_style(self, color):
        return (
            f"QPushButton{{background:transparent;border:1px solid {color};"
            f"border-radius:4px;padding:3px 10px;color:{color};font-size:11px;font-weight:600;}}"
            f"QPushButton:hover{{background:{color}22;}}"
            # 卡片按钮自带彩色描边，焦点环改用淡填充，否则描边变色也看不出来
            f"QPushButton:focus{{border:1px solid {color};background:{color}33;}}"
        )

    def _apply_error_visibility(self, status, err, reason=""):
        """失败且有错误信息时显示原因，并解除高度限制以保证完整可见。

        reason 为已知原因码（如 needs_ffmpeg）时附加一行可操作提示，
        与 Web 端任务卡片保持一致。
        """
        self._error_text = err or ""
        self._error_reason = reason or ""
        hint = _reason_hint(self._error_reason)
        detail = f"⚠ 失败原因: {self._error_text}"
        if hint:
            detail += f"\n💡 {hint}"
        if status == "failed" and err and not self._compact:
            self.error_label.setText(detail)
            self.error_label.show()
            self.setMaximumHeight(16777215)  # 解除上限，完整显示多行错误
        else:
            self.error_label.hide()
            self.setMaximumHeight(self._base_max_height)
        # 紧凑模式下把失败原因放进工具提示，不占高度
        self.setToolTip(detail if self._compact and self._error_text else "")

    def set_compact(self, on):
        """紧凑模式：压低高度、隐藏次要信息（大小/ETA/失败详情）。"""
        self._compact = bool(on)
        lo, hi = _card_height_bounds(self._compact)
        self._base_max_height = hi
        self.setMinimumHeight(lo)
        self.size_label.setVisible(not self._compact)
        self.eta_label.setVisible(not self._compact)
        self._apply_error_visibility(self._built_status, self._error_text,
                                     self._error_reason)

    def contextMenuEvent(self, event):
        """右键菜单：按状态提供 暂停/继续/重试/打开/复制链接/删除。"""
        status = getattr(self, "_built_status", "pending")
        menu = QMenu(self)

        def add(text, action):
            menu.addAction(text, lambda: self.action_triggered.emit(action, self.task_id))

        if status == "downloading":
            add("暂停", "pause")
        elif status == "paused":
            add("继续", "resume")
        elif status == "completed":
            add("打开文件", "open")
        elif status in ("failed", "cancelled"):
            add("重试", "retry")
        add("查看详情", "details")
        if status != "pending":
            # 下载中/暂停/失败/取消都能定位目录；失败时可查看分片残留决定是否手动续传
            add("打开文件夹", "open_folder")
        if self.url:
            add("复制链接", "copy_link")
        menu.addSeparator()
        add("删除", "remove")
        menu.exec(event.globalPos())

    def update_data(self, task_data):
        """更新卡片显示"""
        self.filepath = task_data.get("filepath", "")
        status = task_data.get("status", "pending")
        prog = task_data.get("progress", 0)
        total_size = task_data.get("total_size", 0)
        downloaded = task_data.get("downloaded", 0)
        speed = task_data.get("speed", 0)
        eta = task_data.get("eta", "")

        status_text = {
            "downloading": "● 下载中", "paused": "⏸ 已暂停",
            "completed": "✓ 完成", "failed": "✗ 失败",
            "pending": "⏳ 等待中", "cancelled": "✗ 已取消"
        }
        self.status_label.setText(_card_status_text(
            status, task_data.get("scheduled_at"), task_data.get("auto_retry_at", 0)))
        self.status_label.setStyleSheet(self._status_qss(status))

        self.progress_bar.setValue(int(prog))
        self.progress_bar.setStyleSheet(self._progress_qss(status))
        self.prog_label.setText(f"{prog:.1f}%")

        info = f"📦 {format_size(downloaded)}"
        if total_size > 0:
            info += f" / {format_size(total_size)}"
        if task_data.get("protocol") == "torrent":
            seeds = task_data.get("seeds", 0)
            peers = task_data.get("peers", 0)
            info += f"  🌱 {seeds}  👥 {peers}"
        info += _scheduled_suffix(task_data.get("scheduled_at"))
        self.size_label.setText(info)

        self.speed_label.setText(f"⚡ {format_speed(speed)}" if speed > 0 and status == "downloading" else "")
        self.eta_label.setText(f"⏱ {eta}" if eta and status == "downloading" else "")

        # 失败原因显示
        self._apply_error_visibility(status, task_data.get("error", ""),
                                     task_data.get("error_reason", ""))


class SelfTestWorker(QThread):
    """链路自检：请求本机 /api/self-test，让下载引擎真跑一遍。

    放在后台线程里；最长要等 30 秒，不能卡住设置对话框。
    """
    finished = pyqtSignal(bool, str)      # ok, detail

    def __init__(self, http_port, parent=None):
        super().__init__(parent)
        self._port = int(http_port)

    def run(self):
        import json
        import urllib.error
        import urllib.request
        url = f"http://127.0.0.1:{self._port}/api/self-test"
        try:
            with urllib.request.urlopen(url, timeout=45) as resp:
                body = json.loads(resp.read().decode("utf-8", "replace"))
        except Exception as exc:              # 连不上本机服务本身就是一个结果
            self.finished.emit(False, str(exc))
            return
        ok = bool(body.get("success"))
        detail = "" if ok else str(body.get("error") or body.get("status")
                                   or "未知原因")
        self.finished.emit(ok, detail)


class SettingsDialog(QDialog):
    """设置对话框"""
    def __init__(self, parent=None, theme=None, http_port=5000):
        super().__init__(parent)
        self._theme = theme if theme in THEMES else "dark"
        self._http_port = int(http_port)
        self._selftest_worker = None
        self.setWindowTitle("⚙ 设置")
        self.setMinimumWidth(460)
        self.setStyleSheet(self._qss())

        layout = QVBoxLayout(self)
        layout.setSpacing(6)
        layout.setContentsMargins(20, 16, 20, 16)
        self._add_groups(layout)

    def _add_groups(self, layout):
        """分三组搭建设置项：下载 / 网络 / 完成后（选项变多后仍可快速定位）。"""
        import config

        # —— 下载 ——
        dl_box = QGroupBox("下载")
        form = QFormLayout(dl_box)
        form.setSpacing(12)
        form.setContentsMargins(10, 6, 10, 6)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        # 下载目录：预填当前生效目录（共享配置），而非空白占位
        self.dir_edit = QLineEdit(config.get_download_dir())
        btn_browse = QPushButton("浏览...")
        btn_browse.clicked.connect(self._browse_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(btn_browse)
        form.addRow("下载目录:", dir_row)

        self.segments_spin = QSpinBox()
        self.segments_spin.setRange(1, config.SEGMENTS_MAX)
        self.segments_spin.setValue(config.clamp_segments(config.get("segments")))
        self.segments_spin.setToolTip("多线程分段数，越大速度越快但占用更多资源")
        form.addRow("下载线程数:", self.segments_spin)

        import throttle
        self.rate_edit = QLineEdit(_format_rate_kbps(throttle.get_rate()))
        self.rate_edit.setPlaceholderText("0 = 不限速")
        self.rate_edit.setToolTip("全局下载限速（KB/s），0 或留空表示不限速；所有任务与分段共享")
        rate_row = QHBoxLayout()
        rate_row.addWidget(self.rate_edit, 1)
        rate_row.addWidget(QLabel("KB/s"))
        form.addRow("下载限速:", rate_row)

        self.auto_retry_spin = QSpinBox()
        self.auto_retry_spin.setRange(0, 5)
        self.auto_retry_spin.setValue(int(config.get("auto_retry") or 0))
        self.auto_retry_spin.setSpecialValueText("关闭")
        self.auto_retry_spin.setSuffix(" 次")
        self.auto_retry_spin.setToolTip(
            "任务失败后自动重试的次数；间隔 30秒/分钟/2分钟/5分钟递增。"
            "任务会从分片残留处继续，0 = 关闭")
        form.addRow("失败自动重试:", self.auto_retry_spin)
        layout.addWidget(dl_box)

        # —— 网络 ——
        net_box = QGroupBox("网络")
        form = QFormLayout(net_box)
        form.setSpacing(12)
        form.setContentsMargins(10, 6, 10, 6)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.monitor_check = QComboBox()
        self.monitor_check.addItems(["启用", "禁用"])
        self.monitor_check.setCurrentIndex(0 if config.get("monitor_enabled") else 1)
        form.addRow("浏览器监控:", self.monitor_check)

        self.clip_check = QCheckBox("复制下载链接后拖盘提示一键添加")
        self.clip_check.setChecked(bool(config.get("clipboard_watch")))
        self.clip_check.setToolTip(
            "开启后：复制 http(s)/magnet 链接时，托盘弹出提示，点击即可新建下载。"
            "同一链接只提示一次，不会重复扔任务到任务榜")
        form.addRow("剪贴板监听:", self.clip_check)

        # 代理模式：系统代理 / 直连 / 自定义（三态与 downloader 实际支持一致）
        from downloader import get_proxy_mode
        self.proxy_combo = QComboBox()
        self.proxy_combo.addItems(["系统代理 (env)", "直连 (direct)", "自定义代理"])
        self.proxy_combo.setToolTip(
            "系统代理: 继承 HTTP_PROXY/HTTPS_PROXY，GitHub 等资源必须走代理\n"
            "直连: 忽略代理，仅适合代理宕机时\n"
            "自定义代理: 在下方填写代理地址（如 http://127.0.0.1:7890 或 socks5://...）")
        cur = get_proxy_mode()
        if cur in ("env", "system", ""):
            self.proxy_combo.setCurrentIndex(0)
        elif cur == "direct":
            self.proxy_combo.setCurrentIndex(1)
        else:
            self.proxy_combo.setCurrentIndex(2)
        form.addRow("下载代理:", self.proxy_combo)

        self.proxy_custom = QLineEdit()
        self.proxy_custom.setPlaceholderText("如 http://127.0.0.1:7890 或 socks5://...")
        if cur not in ("env", "system", "direct", ""):
            self.proxy_custom.setText(cur)
        form.addRow("自定义代理:", self.proxy_custom)
        layout.addWidget(net_box)

        # —— 完成后 ——
        done_box = QGroupBox("完成后")
        form = QFormLayout(done_box)
        form.setSpacing(12)
        form.setContentsMargins(10, 6, 10, 6)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        # 「全部下载完成后」动作：与 Web 端设置面板共用 scheduler 单例
        from scheduler import scheduler as _dl_scheduler
        self.finish_combo = QComboBox()
        for _action in FINISH_ACTIONS:
            self.finish_combo.addItem(FINISH_ACTION_LABELS[_action], _action)
        _current_action = _dl_scheduler.get_finish_action()
        _current_idx = FINISH_ACTIONS.index(_current_action) if _current_action in FINISH_ACTIONS else 0
        self.finish_combo.setCurrentIndex(_current_idx)
        self.finish_combo.setToolTip(
            "任务列表里再没有下载中/等待中/已暂停的任务后，开始 60 秒倒计时\n"
            "关机: 倒计时结束执行系统关机\n"
            "睡眠: 倒计时结束让系统睡眠\n"
            "提示音: 倒计时结束播放提示音\n"
            "倒计时期间工具栏显示剩余时间，点击可取消")
        form.addRow("全部下载完成后:", self.finish_combo)

        # 完成提示音：任务下载完成时的声音提醒（默认关闭）
        import notify_sound
        self.sound_combo = QComboBox()
        for _key in notify_sound.NOTIFY_SOUNDS:
            self.sound_combo.addItem(notify_sound.NOTIFY_SOUND_LABELS[_key], _key)
        _cur_sound = notify_sound.get_sound()
        self.sound_combo.setCurrentIndex(
            notify_sound.NOTIFY_SOUNDS.index(_cur_sound)
            if _cur_sound in notify_sound.NOTIFY_SOUNDS else 0)
        self.sound_combo.setToolTip(
            "任务下载完成时播放声音\n"
            "关闭: 仅托盘气泡 + 状态栏\n"
            "提示音: 三声短中\n"
            "系统音: 播放操作系统提示音")
        form.addRow("完成提示音:", self.sound_combo)
        layout.addWidget(done_box)

        # —— 外观 ——
        look_box = QGroupBox("外观")
        form = QFormLayout(look_box)
        form.setSpacing(12)
        form.setContentsMargins(10, 6, 10, 6)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        self.theme_combo = QComboBox()
        self.theme_combo.addItem("深色", "dark")
        self.theme_combo.addItem("浅色", "light")
        self.theme_combo.setCurrentIndex(
            1 if config.get("theme") == "light" else 0)
        self.theme_combo.setToolTip("切换桌面端界面主题（浅色更适合明亮环境）")
        self.theme_combo.currentIndexChanged.connect(self._preview_theme)
        form.addRow("界面主题:", self.theme_combo)
        layout.addWidget(look_box)

        # —— 诊断 ——
        diag_box = QGroupBox("诊断")
        form = QFormLayout(diag_box)
        form.setSpacing(12)
        form.setContentsMargins(10, 6, 10, 6)
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)

        try:
            from media import (ffmpeg_status as _ff_status,
                               ytdlp_available as _yt_ok)
            _ff = _ff_status()
            _ff_text = ("已就绪: " + (_ff.get("path") or "ffmpeg")
                        if _ff.get("available")
                        else "未检测到（混流/转封装不可用）")
            _yt_text = ("已就绪" if _yt_ok()
                        else "未检测到（网页视频解析不可用）")
        except Exception:
            _ff_text = _yt_text = "检测失败"
        form.addRow("ffmpeg:", QLabel(_ff_text))
        form.addRow("yt-dlp:", QLabel(_yt_text))

        st_row = QHBoxLayout()
        self.selftest_btn = QPushButton("开始自检")
        self.selftest_btn.clicked.connect(self._run_self_test)
        self.selftest_label = QLabel("未运行")
        self.selftest_label.setObjectName("selfTestLabel")
        st_row.addWidget(self.selftest_btn)
        st_row.addWidget(self.selftest_label)
        st_row.addStretch(1)
        form.addRow("链路自检:", st_row)
        layout.addWidget(diag_box)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet("QPushButton{padding:6px 18px;border-radius:4px;}")
        layout.addWidget(btns)

    def _qss(self):
        """对话框样式按主题渲染（组框/标签颜色都走 token）。"""
        t = THEMES.get(getattr(self, "_theme", "dark"), THEMES["dark"])
        return f"""
            QDialog {{ background-color: {t['surface']}; border: 1px solid {t['border']}; border-radius: 10px; }}
            QLabel {{ font-size: 13px; color: {t['text']}; }}
            QGroupBox {{
                border: 1px solid {t['border']}; border-radius: 8px;
                margin-top: 12px; padding: 10px 10px 8px 10px;
                font-size: 12px; font-weight: 700; color: {t['textMuted']};
            }}
            QGroupBox::title {{
                subcontrol-origin: margin; subcontrol-position: top left;
                left: 10px; padding: 0 4px;
            }}
        """

    def apply_theme(self, theme):
        """切换主题（设置里改下拉可立即预览，不用重开对话框）。"""
        if theme not in THEMES:
            return
        self._theme = theme
        self.setStyleSheet(self._qss())

    def _preview_theme(self):
        self.apply_theme(self.theme_combo.currentData() or "dark")

    def _set_selftest(self, text, token):
        """自检结果着色；状态色走主题 token，与其他提示一致。"""
        _t = THEMES.get(getattr(self, "_theme", "dark"), THEMES["dark"])
        color = {"ok": _t["green"], "bad": _t["red"]}.get(token, _t.get("textMuted", ""))
        self.selftest_label.setText(text)
        self.selftest_label.setStyleSheet(f"color: {color};")

    def _run_self_test(self):
        """启动自检；运行中禁用按钮，避免多次跟开。"""
        if getattr(self, "_selftest_worker", None) is not None:
            return
        self.selftest_btn.setEnabled(False)
        self._set_selftest("检测中...", "muted")
        self._selftest_worker = SelfTestWorker(self._http_port, self)
        self._selftest_worker.finished.connect(self._on_self_test_done)
        self._selftest_worker.start()

    def _on_self_test_done(self, ok, detail):
        self.selftest_btn.setEnabled(True)
        if ok:
            self._set_selftest("通过", "ok")
        else:
            self._set_selftest("失败: " + (detail or "未知原因"), "bad")
        self._selftest_worker = None

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择下载目录")
        if d:
            self.dir_edit.setText(d)

    def get_settings(self):
        idx = self.proxy_combo.currentIndex()
        if idx == 0:
            proxy_mode = "env"
        elif idx == 1:
            proxy_mode = "direct"
        else:
            proxy_mode = self.proxy_custom.text().strip() or "direct"
        return {
            "dir": self.dir_edit.text().strip(),
            "segments": self.segments_spin.value(),
            "monitor": self.monitor_check.currentIndex() == 0,
            "clipboard_watch": self.clip_check.isChecked(),
            "proxy_mode": proxy_mode,
            "rate_limit": _parse_rate_kbps(self.rate_edit.text()),
            "auto_retry": self.auto_retry_spin.value(),
            "finish_action": self.finish_combo.currentData() or "none",
            "notify_sound": self.sound_combo.currentData() or "none",
            "theme": self.theme_combo.currentData() or "dark",
        }


class AddDialog(QDialog):
    """添加下载对话框"""
    def __init__(self, parent=None, url="", theme=None):
        super().__init__(parent)
        self._theme = theme if theme in THEMES else "dark"
        self.setWindowTitle("📥 新建下载任务")
        self.setMinimumWidth(520)
        self.setStyleSheet(self._qss())

        layout = QFormLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 24)

        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("https://example.com/file.zip 或 magnet:?xt=urn:btih:...")
        layout.addRow("下载链接:", self.url_edit)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("留空自动识别")
        layout.addRow("文件名:", self.name_edit)

        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("默认下载目录")
        btn_browse = QPushButton("浏览...")
        btn_browse.clicked.connect(self._browse_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(btn_browse)
        layout.addRow("保存到:", dir_row)

        self.seg_spin = QSpinBox()
        self.seg_spin.setRange(1, 32)
        self.seg_spin.setValue(8)
        layout.addRow("线程数:", self.seg_spin)

        # 定时开始（与 Web 端「定时下载」对齐）：默认十分钟后，未勾选则立即下载
        self.sched_check = QCheckBox("定时开始")
        self.sched_time = QDateTimeEdit()
        self.sched_time.setDisplayFormat("yyyy-MM-dd HH:mm")
        self.sched_time.setDateTime(QDateTime.currentDateTime().addSecs(600))
        self.sched_time.setEnabled(False)
        self.sched_check.toggled.connect(self.sched_time.setEnabled)
        sched_row = QHBoxLayout()
        sched_row.addWidget(self.sched_check)
        sched_row.addWidget(self.sched_time, 1)
        layout.addRow("", sched_row)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def _qss(self):
        """对话框样式按主题渲染。"""
        t = THEMES.get(getattr(self, "_theme", "dark"), THEMES["dark"])
        return f"""
            QDialog {{ background-color: {t['surface']}; border: 1px solid {t['border']}; border-radius: 10px; }}
            QLabel {{ font-size: 13px; color: {t['text']}; }}
        """

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择下载目录")
        if d:
            self.dir_edit.setText(d)

    def get_data(self):
        start_at = None
        if self.sched_check.isChecked():
            when = self.sched_time.dateTime()
            if when.toSecsSinceEpoch() > int(time.time()):
                start_at = float(when.toSecsSinceEpoch())
        return {
            "url": self.url_edit.text().strip(),
            "filename": self.name_edit.text().strip() or None,
            "dir": self.dir_edit.text().strip() or None,
            "segments": self.seg_spin.value(),
            "start_at": start_at,
        }


class TaskDetailDialog(QDialog):
    """任务详情面板：双击卡片或右键「查看详情」打开。

    非模态 + 500ms 自轮询刷新；任务被删除后数据源返回 None，面板自动关闭。
    按钮动作经 action_requested 抛回主窗口，与卡片按钮共用同一条处理链。
    """

    action_requested = pyqtSignal(str, str)  # action, task_id

    def __init__(self, task_id, task_data, fetch=None, parent=None, theme=None):
        super().__init__(parent)
        self._task_id = task_id
        self._fetch = fetch  # callable -> dict | None：任务已不存在时返回 None
        self._theme = theme if theme in THEMES else "dark"
        self._tokens = THEMES[self._theme]
        self.setWindowTitle("任务详情")
        self.setMinimumWidth(520)
        self.setStyleSheet(self._qss())

        layout = QVBoxLayout(self)
        layout.setSpacing(10)
        layout.setContentsMargins(18, 16, 18, 16)
        self.title_label = QLabel()
        self.title_label.setObjectName("detailTitle")
        layout.addWidget(self.title_label)

        self.grid = QFormLayout()
        self.grid.setSpacing(6)
        self.grid.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        layout.addLayout(self.grid)

        # 分段进度区：只有真正多段的任务才显示
        self.seg_wrap = QWidget()
        seg_box = QVBoxLayout(self.seg_wrap)
        seg_box.setContentsMargins(0, 0, 0, 0)
        seg_box.setSpacing(4)
        seg_title = QLabel("分段进度")
        seg_title.setObjectName("detailSection")
        seg_box.addWidget(seg_title)
        self.seg_rows = QVBoxLayout()
        self.seg_rows.setSpacing(4)
        seg_box.addLayout(self.seg_rows)
        layout.addWidget(self.seg_wrap)

        # 状态动作：和卡片/右键菜单同一套语义，但按当前状态只留有意义的那几个。
        # 放在详情面板里是因为用户往往是看了失败原因才想重试、看了进度才想暂停，
        # 从前得退回列表找到那张卡才能操作。
        self._state_btns = QHBoxLayout()
        self._state_btns.setSpacing(8)
        self._state_btns.setContentsMargins(0, 0, 0, 0)
        self._state_btns_widgets = []
        self._state_actions = []
        layout.addLayout(self._state_btns)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(self.reject)
        btn_folder = btns.addButton("打开文件夹",
                                    QDialogButtonBox.ButtonRole.ActionRole)
        btn_folder.clicked.connect(
            lambda: self.action_requested.emit("open_folder", self._task_id))
        btn_link = btns.addButton("复制链接",
                                  QDialogButtonBox.ButtonRole.ActionRole)
        btn_link.clicked.connect(
            lambda: self.action_requested.emit("copy_link", self._task_id))
        btn_copy = btns.addButton("复制详情",
                                  QDialogButtonBox.ButtonRole.ActionRole)
        btn_copy.clicked.connect(self._copy_details)
        layout.addWidget(btns)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self._last_data = {}
        self._timer.start(500)
        self.update_data(task_data)

    def _qss(self):
        """详情面板样式（全部取主题 token，跟随亮/暗主题）。"""
        t = self._tokens
        return f"""
            QDialog {{ background-color: {t['surface']};
                       border: 1px solid {t['border']}; border-radius: 10px; }}
            QLabel {{ font-size: 13px; color: {t['text']}; }}
            QLabel#detailTitle {{ font-size: 15px; font-weight: 700; color: {t['textStrong']}; }}
            QLabel#detailSection {{ font-size: 12px; font-weight: 700; color: {t['textMuted']}; }}
            QLabel#detailSegHead {{ font-size: 11px; color: {t['textMuted']}; }}
            QLabel#detailSegText {{ font-size: 11px; color: {t['text']}; }}
        """

    def apply_theme(self, theme):
        """切换主题（主窗口切换时同步刷新本面板）。"""
        if theme not in THEMES:
            return
        self._theme = theme
        self._tokens = THEMES[theme]
        self.setStyleSheet(self._qss())
        self._rebuild_segments(_segment_rows(self._last_data or {}))

    @staticmethod
    def _clear_layout(layout):
        """清空布局并销毁子控件（信息行/分段条每次全量重建）。"""
        while layout.count():
            item = layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _rebuild_rows(self, rows):
        self._clear_layout(self.grid)
        for label, value in rows:
            value_label = QLabel(str(value))
            value_label.setWordWrap(True)
            value_label.setTextInteractionFlags(
                Qt.TextInteractionFlag.TextSelectableByMouse)
            self.grid.addRow(f"{label}:", value_label)

    def _rebuild_segments(self, segments):
        """按最新分段进度重绘进度条；单段任务隐藏整个区域。"""
        self._clear_layout(self.seg_rows)
        if len(segments) < 2:
            self.seg_wrap.setVisible(False)
            return
        self.seg_wrap.setVisible(True)
        t = self._tokens
        for i, seg in enumerate(segments):
            done = int(seg.get("done") or 0)
            total = int(seg.get("total") or 0)
            pct = int(done * 100 / total) if total > 0 else 0
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            head = QLabel(f"段 {i + 1}")
            head.setObjectName("detailSegHead")
            bar = QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(min(max(pct, 0), 100))
            bar.setTextVisible(False)
            bar.setFixedHeight(6)
            bar.setStyleSheet(
                f"QProgressBar{{background:{t['input']};border:none;border-radius:3px;}}"
                f"QProgressBar::chunk{{background:{t['accent']};border-radius:3px;}}")
            text = QLabel(f"{format_size(done)} / {format_size(total)} · {pct}%")
            text.setObjectName("detailSegText")
            row_layout.addWidget(head)
            row_layout.addWidget(bar, 1)
            row_layout.addWidget(text)
            self.seg_rows.addWidget(row)

    def update_data(self, task_data):
        """刷新标题、信息行与分段进度（初始化与轮询共用）。"""
        d = task_data or {}
        self._last_data = d
        filename = d.get("filename") or "任务详情"
        self.title_label.setText(filename)
        self.setWindowTitle(f"{filename} - 任务详情")
        self._rebuild_rows(_detail_rows(d))
        self._rebuild_segments(_segment_rows(d))
        self._rebuild_state_buttons(d.get("status"))

    def _rebuild_state_buttons(self, status):
        """按状态重排详情面板的状态动作（与卡片行同一套动作、同一套取舍）。

        自己存一份按钮引用：deleteLater() 只是延后销毁，findChildren 在
        本轮事件循环里仍能捞出上一状态的按钮，测试要能断言「只剩当前那几个」。
        """
        self._clear_layout(self._state_btns)
        self._state_actions = []
        self._state_btns_widgets = []
        if status == "downloading":
            self._state_actions.append(("⏸ 暂停", "pause"))
            self._state_actions.append(("✕ 取消", "cancel"))
        elif status == "paused":
            self._state_actions.append(("▶ 继续", "resume"))
            self._state_actions.append(("✕ 取消", "cancel"))
        elif status in ("failed", "cancelled"):
            self._state_actions.append(("↻ 重试", "retry"))
        elif status == "completed":
            self._state_actions.append(("📂 打开文件", "open"))
        for text, action in self._state_actions:
            btn = QPushButton(text)
            btn.clicked.connect(
                lambda _=False, a=action: self.action_requested.emit(a, self._task_id))
            self._state_btns.addWidget(btn)
            self._state_btns_widgets.append(btn)

    def _copy_details(self):
        """把当前详情行复制成纯文本（反馈问题时可直接粘贴，省去手抄）。"""
        rows = _detail_rows(getattr(self, "_last_data", None) or {})
        if not rows:
            return
        QApplication.clipboard().setText(
            "\n".join(f"{label}: {value}" for label, value in rows))
        parent = self.parent()
        status_bar = getattr(parent, "status_bar", None)
        if status_bar is not None:
            status_bar.showMessage("已复制任务详情", 3000)

    def _poll(self):
        """每 500ms 拉一次最新状态；任务已被删除则自动关闭面板。"""
        if self._fetch is None:
            return
        try:
            data = self._fetch()
        except Exception:
            return
        if data is None:
            self._timer.stop()
            self.reject()
            return
        self.update_data(data)


class MainWindow(QMainWindow):
    """SwiftDM 主窗口"""
    log_signal = pyqtSignal(str)
    capture_notified = pyqtSignal(str)  # 浏览器捕获到下载（monitor 线程 -> UI 线程）

    def __init__(self, http_port=5000, monitor=None):
        super().__init__()
        self.http_port = http_port
        self.monitor = monitor  # 浏览器监控器（由 main 注入），设置可启停它
        import config
        self.download_dir = config.get_download_dir()
        # 日志：文件 + 控制台 + UI 面板
        self.logger = setup_logging()
        self._log_handler = QtLogHandler(self.log_signal)
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        self.logger.addHandler(self._log_handler)
        self.log_signal.connect(self._append_log)
        self.capture_notified.connect(self._notify_capture)
        self._cards = {}  # task_id -> TaskCard
        self._compact = False  # 任务列表紧凑模式
        self._selected_task_id = None  # 键盘/鼠标选中的任务（↑/↓ 导航）
        self._selected_ids = set()     # 多选勾选的 task_id 集合（批量操作的作用域）
        self._detail_dialog = None    # 打开中的任务详情面板（持引用防被 GC）
        self._search = ""  # 任务搜索关键字（文件名/链接，大小写不敏感）
        self._shortcuts = []  # [(seq, QShortcut)] for tests/extensibility
        self._completed_tasks = set()  # 追踪新完成的任务用于通知
        self._prev_statuses = {}  # task_id -> status
        self._first_refresh = True  # 首刷：从历史加载的任务作为已知状态，不再弹完成/失败通知

        self.setWindowTitle("SwiftDM - 高速下载管理器")
        self.setMinimumSize(780, 560)
        if not _restore_window_geometry(self, _window_settings()):
            self.resize(860, 640)  # 无历史记录时的默认尺寸

        # 主题：样式表需在建控件前生效，控件级主题在搭建完成后统一刷一遍
        import config as _cfg_theme
        _saved_theme = _cfg_theme.get("theme")
        self._theme = _saved_theme if _saved_theme in THEMES else "dark"
        self.setStyleSheet(_qss_for(self._theme))
        self.setAcceptDrops(True)  # 支持把链接/磁力拖入窗口即新建下载

        self._setup_log_panel()
        self._setup_toolbar()
        self._setup_central()
        self._setup_statusbar()
        self._setup_tray()
        self._setup_shortcuts()

        # 头部/日志面板等控件级样式跟随主题
        self._apply_theme(self._theme)

        # 定时刷新
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(500)

        # 剪贴板监听（默认关闭，设置里开启）：复制下载链接后拖盘提示
        self._clip_last = None      # 上一次处理过的剪贴板文本（去重）
        self._clip_pending = None   # 待用户点击拖盘确认的 URL
        self._tray_msg_kind = None  # 最近一条托盘气泡类型：只有 clip_prompt 可点击添加
        self._clip_timer = QTimer(self)
        self._clip_timer.timeout.connect(self._check_clipboard)
        self._clip_timer.start(1000)

        # 初始加载
        self._refresh()

    def _setup_shortcuts(self):
        """Global shortcuts: Ctrl+N new download, Ctrl+F focus the search box."""
        for _seq, _slot in (
            ("Ctrl+N", self._add_download),
            ("Ctrl+F", self._focus_search),
            ("Up", lambda: self._move_selection(-1)),
            ("Down", lambda: self._move_selection(1)),
            ("Return", lambda: self._open_selected_task()),
            # Esc 关详情面板：和 Web 端保持一致（Web 端 Esc 关详情，设置是模态对话框，
            # Qt 自带默认行为）。没开面板时不做任何事，不和其它功能抢键。
            # Esc 先清多选，没有再关详情面板；Ctrl+A 全选当前可见任务
            ("Escape", self._on_escape),
            ("Ctrl+A", self._select_all_visible),
        ):
            _sc = QShortcut(QKeySequence(_seq), self)
            _sc.activated.connect(_slot)
            self._shortcuts.append((_seq, _sc))

    def _focus_search(self):
        """Ctrl+F 聚焦任务搜索框（与 Web 端、以及「Ctrl+F = 查找」的通用约定一致）。

        链接输入框常驻工具栏最左侧且回车即新建，Ctrl+N 已覆盖新建入口，
        这里再占一个 Ctrl+F 反而是把最常用的查找键让给了次要操作。
        """
        self.search_input.setFocus()

    def _close_task_detail(self):
        """Esc 关闭当前托盘/任务详情面板；未打开时空转。"""
        dlg = getattr(self, "_detail_dialog", None)
        if dlg is None:
            return
        # 走 reject() 而不是 hide()：finished 信号释放引用，避免再次打开时挂到旧面板
        dlg.reject()
        dlg.deleteLater()

    def _on_escape(self):
        """Esc 优先收起多选；没有多选时才关详情面板。"""
        if self._selected_ids:
            self._clear_selection()
            return
        self._close_task_detail()

    def _setup_toolbar(self):
        toolbar = QToolBar("主工具栏")
        toolbar.setMovable(False)
        toolbar.setIconSize(QSize(18, 18))
        self.addToolBar(toolbar)

        title = QLabel("  SwiftDM")
        title.setObjectName("titleLabel")
        toolbar.addWidget(title)
        toolbar.addSeparator()

        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("粘贴下载链接或磁力链接(magnet:)...")
        self.url_input.setMinimumWidth(300)
        self.url_input.setMaximumWidth(500)
        self.url_input.returnPressed.connect(self._add_download)
        toolbar.addWidget(self.url_input)

        self.btn_add = QPushButton("＋ 下载")
        self.btn_add.setObjectName("btnAdd")
        self.btn_add.clicked.connect(self._add_download)
        toolbar.addWidget(self.btn_add)

        toolbar.addSeparator()

        self.btn_pause_all = QPushButton("⏸ 全部暂停")
        self.btn_pause_all.clicked.connect(self._pause_all)
        toolbar.addWidget(self.btn_pause_all)

        self.btn_resume_all = QPushButton("▶ 全部恢复")
        self.btn_resume_all.clicked.connect(self._resume_all)
        toolbar.addWidget(self.btn_resume_all)

        self.btn_retry_failed = QPushButton("↻ 重试失败")
        self.btn_retry_failed.clicked.connect(self._retry_all_failed)
        toolbar.addWidget(self.btn_retry_failed)

        self.btn_clear = QPushButton("🗑 清除已完成")
        self.btn_clear.clicked.connect(self._clear_completed)
        toolbar.addWidget(self.btn_clear)

        self.btn_open_dir = QPushButton("📂 打开目录")
        self.btn_open_dir.clicked.connect(self._open_download_dir)
        toolbar.addWidget(self.btn_open_dir)

        self.btn_copy_links = QPushButton("🔗 复制链接")
        self.btn_copy_links.setToolTip("复制当前可见任务（含过滤/搜索）的下载链接")
        self.btn_copy_links.clicked.connect(self._copy_task_links)
        toolbar.addWidget(self.btn_copy_links)

        self.btn_export = QPushButton("💾 导出列表")
        self.btn_export.setToolTip("将当前可见任务导出为 CSV（文件名/链接/状态/大小）")
        self.btn_export.clicked.connect(self._export_task_list)
        toolbar.addWidget(self.btn_export)

        toolbar.addSeparator()

        self.btn_log = QPushButton("📜 日志")
        self.btn_log.setCheckable(True)
        self.btn_log.toggled.connect(self.log_dock.setVisible)
        toolbar.addWidget(self.btn_log)

        self.btn_settings = QPushButton("⚙ 设置")
        self.btn_settings.clicked.connect(self._show_settings)
        toolbar.addWidget(self.btn_settings)

        # 浏览器监控状态
        self.monitor_label = QLabel("  🌐 监控已启用")
        self.monitor_label.setObjectName("monitorLabel")
        try:
            import config as _cfg_mon
            self._set_monitor_state(bool(_cfg_mon.get("monitor_enabled")))
        except Exception:
            self._set_monitor_state(True)
        toolbar.addWidget(self.monitor_label)

        # 「全部下载完成后」倒计时：仅倒计时进行中显示，点击取消
        self.finish_btn = QPushButton("")
        self.finish_btn.setObjectName("btnFinishCountdown")
        self.finish_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.finish_btn.setVisible(False)
        self.finish_btn.setToolTip("全部下载完成后的倒计时进行中，点击取消")
        # 文字是动态倒计时，不写死可访问名就什么都报不出来
        self.finish_btn.setAccessibleName("取消下载完成后的倒计时")
        self.finish_btn.clicked.connect(self._cancel_finish_action)
        toolbar.addWidget(self.finish_btn)

    def _setup_central(self):
        central = QWidget()
        central.setObjectName("appCentral")  # 页面底色只铺中央区，标签/按钮盒保持透明
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 头部信息栏
        header = QWidget()
        header.setObjectName("appHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 8, 16, 8)

        self.total_speed_label = QLabel("总速度: 0 B/s")
        self.total_speed_label.setObjectName("speedLabel")
        header_layout.addWidget(self.total_speed_label)

        self.overall_label = QLabel("")
        self.overall_label.setObjectName("overallLabel")
        header_layout.addWidget(self.overall_label)

        header_layout.addStretch()

        self.stats_label = QLabel("下载中: 0  |  已完成: 0  |  失败: 0  |  总计: 0")
        self.stats_label.setObjectName("statsLabel")
        header_layout.addWidget(self.stats_label)

        layout.addWidget(header)

        # 分类过滤：学习主流下载器的状态分段，pill 样式切换
        filter_bar = QWidget()
        filter_bar.setStyleSheet("background: transparent;")
        fb = QHBoxLayout(filter_bar)
        fb.setContentsMargins(16, 8, 16, 4)
        fb.setSpacing(8)
        try:
            import config as _cfg
            _saved = _cfg.get("filter")
            self._filter = _saved if _saved in ("all", "active", "completed", "failed") else "all"
        except Exception:
            self._filter = "all"
        try:
            import config as _cfg
            _saved_sort = _cfg.get("sort")
            self._sort = _saved_sort if _saved_sort in SORT_KEYS else "default"
            self._compact = bool(_cfg.get("compact"))
        except Exception:
            self._sort = "default"
        # 芯片互斥由 _set_filter 手工维护，不用 QButtonGroup：exclusive 组会把
        # 除第一个以外的芯片从 Tab 顺序里摘掉（其余三个只剩点击/滚轮聚焦），
        # 纯键盘用户够不到「进行中/已完成/失败」，和 Web 端不一致。
        self._filter_btns = {}
        for _key, _label in [("all", "全部"), ("active", "进行中"),
                             ("completed", "已完成"), ("failed", "失败")]:
            _b = QPushButton(_label)
            _b.setObjectName("filterBtn")
            _b.setCheckable(True)
            _b.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            _b.setCursor(Qt.CursorShape.PointingHandCursor)
            _b.clicked.connect(lambda _=False, k=_key: self._set_filter(k))
            self._filter_btns[_key] = _b
            fb.addWidget(_b)
        # 键盘导航提示贴着芯片放（Web 端 .kbd-hint 就在筛选按钮后面），
        # 让「↑↓/Enter 能操作任务」这件事在三个界面里都能被发现。
        self.kbd_hint = QLabel("↑↓ 选择任务 · Enter 打开 · Ctrl+A 全选")
        self.kbd_hint.setObjectName("kbdHint")
        self.kbd_hint.setToolTip(
            "键盘导航：↑↓ 在可见任务间移动，回车打开文件；"
            "Ctrl+点击卡片多选，Ctrl+A 全选可见任务，Esc 取消选择；"
            "Ctrl+N 新建下载，Ctrl+F 搜索任务")
        self.kbd_hint.setAccessibleName(
            "键盘导航提示：↑↓ 在可见任务间移动，回车打开文件；"
            "Ctrl+点击卡片多选，Ctrl+A 全选可见任务，Esc 取消选择；"
            "Ctrl+N 新建下载，Ctrl+F 搜索任务")
        fb.addWidget(self.kbd_hint)
        fb.addStretch(1)
        self.sort_combo = QComboBox()
        for _key in SORT_KEYS:
            self.sort_combo.addItem(SORT_LABELS[_key], _key)
        self.sort_combo.setCurrentIndex(max(0, SORT_KEYS.index(self._sort)))
        self.sort_combo.setToolTip("任务列表排序方式")
        self.sort_combo.setAccessibleName("任务列表排序方式")
        self.sort_combo.currentIndexChanged.connect(self._set_sort)
        fb.addWidget(self.sort_combo)
        self.compact_btn = QPushButton("≡ 紧凑")
        self.compact_btn.setObjectName("filterBtn")
        self.compact_btn.setCheckable(True)
        self.compact_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.compact_btn.setChecked(self._compact)
        self.compact_btn.setToolTip("紧凑模式：隐藏次要信息，一屏看更多任务（可选择会被记住）")
        self.compact_btn.setAccessibleName("紧凑模式")
        self.compact_btn.clicked.connect(self._set_compact)
        fb.addWidget(self.compact_btn)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索任务…")
        self.search_input.setAccessibleName("搜索任务")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedWidth(200)
        self.search_input.textChanged.connect(self._set_search)
        fb.addWidget(self.search_input)
        if self._filter in self._filter_btns:
            self._filter_btns[self._filter].setChecked(True)
        layout.addWidget(filter_bar)

        # 多选操作条：与 Web 端 #selectBar 对齐，有勾选时才出现
        self.select_bar = QWidget()
        self.select_bar.setObjectName("selectBar")
        sb = QHBoxLayout(self.select_bar)
        sb.setContentsMargins(16, 6, 16, 6)
        sb.setSpacing(8)
        self.select_count = QLabel("已选 0 项")
        self.select_count.setObjectName("selectCount")
        sb.addWidget(self.select_count)
        sb.addStretch(1)
        self._batch_btns = {}
        for _act, _text, _obj in (("pause", "⏸ 暂停", "selBtn"),
                                  ("resume", "▶ 继续", "selBtn"),
                                  ("retry", "↻ 重试", "selBtn"),
                                  ("remove", "🗑 删除", "selBtnDanger")):
            _btn = QPushButton(_text)
            _btn.setObjectName(_obj)
            _btn.setToolTip(f"对勾选的任务批量{BATCH_LABELS[_act]}")
            _btn.setAccessibleName(f"批量{BATCH_LABELS[_act]}")
            _btn.setCursor(Qt.CursorShape.PointingHandCursor)
            _btn.clicked.connect(lambda _=False, a=_act: self._batch_action(a))
            self._batch_btns[_act] = _btn
            sb.addWidget(_btn)
        self.sel_cancel_btn = QPushButton("取消选择")
        self.sel_cancel_btn.setObjectName("selBtn")
        self.sel_cancel_btn.setToolTip("清空多选（Esc 同效）")
        self.sel_cancel_btn.clicked.connect(self._clear_selection)
        sb.addWidget(self.sel_cancel_btn)
        self.select_bar.hide()
        layout.addWidget(self.select_bar)

        # 滚动区域 — 任务列表
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self.task_container = QWidget()
        self.task_container.setStyleSheet("background: transparent;")
        self.task_layout = QVBoxLayout(self.task_container)
        self.task_layout.setContentsMargins(14, 12, 14, 12)
        self.task_layout.setSpacing(8)
        self.task_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        self.scroll.setWidget(self.task_container)
        layout.addWidget(self.scroll, 1)

        # 空状态
        self.empty_label = QLabel("📥\n\n还没有下载任务\n粘贴链接或从浏览器捕获下载")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setObjectName("emptyLabel")
        self.task_layout.addWidget(self.empty_label)

    def _setup_statusbar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪  |  下载目录: ~/Downloads/IDM_Downloads")

    def _apply_theme(self, theme):
        """切换主题：全局样式表 + 任务卡片 + 日志面板等控件级样式。"""
        if theme not in THEMES:
            theme = "dark"
        self._theme = theme
        self.setStyleSheet(_qss_for(theme))
        t = THEMES[theme]
        for card in getattr(self, "_cards", {}).values():
            card.apply_theme(theme)
        dock = getattr(self, "log_dock", None)
        if dock is not None:
            dock.setStyleSheet(
                f"QDockWidget::title{{background:{t['toolbar']};"
                f"color:{t['textMuted']};padding:4px 10px;}}")
        detail = getattr(self, "_detail_dialog", None)
        if detail is not None:
            detail.apply_theme(theme)
        log_edit = getattr(self, "log_edit", None)
        if log_edit is not None:
            log_edit.setStyleSheet(
                f"QPlainTextEdit{{background:{t['logBg']};color:{t['logFg']};"
                f"font-family:'Consolas','Menlo','Courier New',monospace;"
                f"font-size:12px;border:none;}}")

        if getattr(self, "tray", None) is not None:
            self._refresh_tray_icon()

    def _set_monitor_state(self, on):
        """监控启停：颜色由主题样式表按动态属性 on 决定，切主题自动跟随。"""
        self.monitor_label.setProperty("on", bool(on))
        self.monitor_label.style().unpolish(self.monitor_label)
        self.monitor_label.style().polish(self.monitor_label)

    def _setup_log_panel(self):
        """底部可展开的运行日志面板"""
        t = THEMES[self._theme]
        self.log_dock = QDockWidget("运行日志", self)
        self.log_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea |
                                      Qt.DockWidgetArea.RightDockWidgetArea)
        self.log_dock.setStyleSheet(
            f"QDockWidget::title{{background:{t['toolbar']};"
            f"color:{t['textMuted']};padding:4px 10px;}}")
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setStyleSheet(
            f"QPlainTextEdit{{background:{t['logBg']};color:{t['logFg']};"
            f"font-family:'Consolas','Menlo','Courier New',monospace;"
            f"font-size:12px;border:none;}}"
        )
        self.log_dock.setWidget(self.log_edit)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.log_dock.hide()

    def _append_log(self, msg):
        """把日志追加到 UI 面板并自动滚动；错误日志自动弹出面板"""
        self.log_edit.appendPlainText(msg)
        sb = self.log_edit.verticalScrollBar()
        sb.setValue(sb.maximum())
        if "[ERROR]" in msg or "[CRITICAL]" in msg:
            if not self.log_dock.isVisible():
                self.log_dock.show()
                self.btn_log.setChecked(True)

    def _setup_tray(self):
        self._tray_icon_state = None  # 当前托盘图标状态（缓存，避免每节拍重绘）
        self.tray = QSystemTrayIcon(self)
        self.tray.setIcon(_tray_icon("idle", THEMES[getattr(self, "_theme", "dark")]))
        self._tray_icon_state = "idle"
        self.tray.setToolTip("SwiftDM - 下载管理器")

        tray_menu = QMenu()
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self.show)
        tray_menu.addAction(show_action)
        pause_all_action = QAction("⏸ 全部暂停", self)
        pause_all_action.triggered.connect(self._pause_all)
        tray_menu.addAction(pause_all_action)
        resume_all_action = QAction("▶ 全部恢复", self)
        resume_all_action.triggered.connect(self._resume_all)
        tray_menu.addAction(resume_all_action)
        tray_menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit_app)
        tray_menu.addAction(quit_action)

        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.messageClicked.connect(self._tray_message_clicked)
        self.tray.show()

    def _update_tray_icon(self, active, failed):
        """托盘图标随任务状态切换（下载中/空闲/有失败）；状态未变则跳过。"""
        state = _tray_icon_state(active, failed)
        if state == self._tray_icon_state:
            return
        self._tray_icon_state = state
        self.tray.setIcon(_tray_icon(state, THEMES[getattr(self, "_theme", "dark")]))

    def _refresh_tray_icon(self):
        """主题切换后重画托盘图标：状态不变也要重绘，否则颜色不跟着变。"""
        self.tray.setIcon(_tray_icon(getattr(self, "_tray_icon_state", None) or "idle",
                                     THEMES[getattr(self, "_theme", "dark")]))

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.activateWindow()

    def closeEvent(self, event):
        """关闭窗口时隐藏到托盘"""
        _save_window_geometry(self, _window_settings())
        event.ignore()
        self.hide()
        _tray_bubble(self, "SwiftDM", "已最小化到系统托盘，下载任务继续运行",
                          QSystemTrayIcon.MessageIcon.Information, 2000)

    # ==================== 数据刷新 ====================

    def _get_manager(self):
        """获取全局下载管理器（延迟导入避免循环引用）"""
        from downloader import manager
        return manager

    def _check_clipboard(self):
        """剪贴板监听：新出现的下载链接用拖盘提示，点击拖盘才添加。"""
        import config
        if not config.get("clipboard_watch"):
            return
        try:
            text = QApplication.clipboard().text()
        except Exception:
            return
        url = _clipboard_download_url(text)
        if not url or url == self._clip_last:
            return
        self._clip_last = url
        # 任务榜里已有相同链接（否则每次复制都会弹提示）
        try:
            mgr = self._get_manager()
            if any(t.url == url for t in mgr.get_all_tasks()):
                return
        except Exception:
            pass
        self._clip_pending = url
        _tray_bubble(self, "SwiftDM", f"检测到下载链接，点击添加: {url[:60]}",
                          QSystemTrayIcon.MessageIcon.Information, 6000,
                          kind="clip_prompt")

    def _tray_message_clicked(self):
        """拖盘气泡点击：仅「剪贴板链接待确认」气泡触发添加。"""
        if getattr(self, "_tray_msg_kind", None) != "clip_prompt":
            return
        self._tray_msg_kind = None
        url = self._clip_pending
        self._clip_pending = None
        if url:
            self._create_and_start(url)

    def notify_capture(self, filename=""):
        """浏览器监控线程入口：捕获到下载时通知 UI（线程安全，走信号队列）。"""
        self.capture_notified.emit(filename or "")

    def _notify_capture(self, filename):
        """浏览器捕获提示：让用户知道新任务从哪来，而不是列表里凭空多一项。"""
        name = filename or "文件"
        self.status_bar.showMessage(f"🌐 浏览器捕获: {name}", 5000)
        _tray_bubble(self, "SwiftDM", f"已捕获下载: {name}",
                          QSystemTrayIcon.MessageIcon.Information, 4000)

    def _refresh(self):
        """定时刷新 UI"""
        try:
            mgr = self._get_manager()
            tasks = mgr.get_all_tasks()
            stats = mgr.get_stats()

            # 更新统计
            self.total_speed_label.setText(f"总速度: {format_speed(stats['total_speed'])}")
            self.stats_label.setText(
                f"下载中: {stats['active']}  |  已完成: {stats['completed']}  |  "
                f"失败: {stats['failed']}  |  暂停: {stats['paused']}  |  总计: {stats['total']}"
            )
            self.setWindowTitle(_status_title(stats["active"], stats["total_speed"], stats["total"]))
            self.tray.setToolTip(_tray_tip(stats["active"], stats["total_speed"],
                                           stats["total"], stats.get("failed", 0),
                                           self._finish_countdown_text_now()))
            self._update_tray_icon(stats["active"], stats["failed"])
            self._update_overall(tasks)
            self._update_finish_countdown()

            if not tasks:
                self._update_filter_counts({})
                self._selected_ids = set()
                self._update_select_bar({})
                # 清理所有卡片
                for card in list(self._cards.values()):
                    self.task_layout.removeWidget(card)
                    card.deleteLater()
                self._cards.clear()
                self._prev_statuses.clear()
                self.empty_label.setText(self._filter_hint())
                self.empty_label.show()
                return

            self.empty_label.hide()

            # 检测新完成的任务
            task_dict = {}
            for t in tasks:
                d = t.to_dict()
                d["scheduled_at"] = _scheduled_at(d["task_id"])
                task_dict[d["task_id"]] = d

            if self._first_refresh:
                # 首刷：从历史加载的任务直接作为「已知」状态，避免重启时弹一堆旧通知
                self._prev_statuses = {tid: d["status"] for tid, d in task_dict.items()}
                self._first_refresh = False
            else:
                failed_now = []
                for tid, d in task_dict.items():
                    prev_status = self._prev_statuses.get(tid)
                    if prev_status != "completed" and d["status"] == "completed":
                        if tid not in self._completed_tasks:
                            self._completed_tasks.add(tid)
                            self._notify_complete(d)
                    # 新失败的任务：先收集，循环结束后聚合成一条提示
                    elif prev_status != "failed" and d["status"] == "failed":
                        failed_now.append(d)

                self._prev_statuses = {tid: d["status"] for tid, d in task_dict.items()}
                if failed_now:
                    self._notify_failures(failed_now)

            # 移除不存在的任务卡片
            removed = set(self._cards.keys()) - set(task_dict.keys())
            if self._selected_task_id in removed:
                self._selected_task_id = None
            for tid in removed:
                card = self._cards.pop(tid)
                self.task_layout.removeWidget(card)
                card.deleteLater()

            # 更新或创建卡片
            for task_id, data in task_dict.items():
                card = self._cards.get(task_id)
                if card is not None and getattr(card, "_built_status", None) != data["status"]:
                    # 操作按钮按状态生成（下载中→暂停/取消，失败→重试/删除…），
                    # 状态变化时重建卡片，否则会一直显示旧状态对应的按钮
                    self.task_layout.removeWidget(card)
                    card.deleteLater()
                    card = None
                if card is not None:
                    card.update_data(data)
                else:
                    card = TaskCard(data, theme=self._theme)
                    card.set_compact(self._compact)
                    card.action_triggered.connect(self._handle_action)
                    card.selected.connect(lambda tid: self._select_task(tid))
                    card.toggled.connect(lambda tid, on: self._toggle_selection(tid, on))
                    card.activated.connect(lambda tid: self._show_task_detail(tid))
                    self._cards[task_id] = card
                    # 插入到布局中（在 stretch 之前）
                    self.task_layout.insertWidget(self.task_layout.count() - 1, card)

            # 排序：按所选键重排卡片；顺序未变则跳过，避免每 500ms 重排引发风样
            _desired = _sorted_task_ids(task_dict, self._sort)
            _current = []
            for _i in range(self.task_layout.count()):
                _item = self.task_layout.itemAt(_i)
                _widget = _item.widget() if _item else None
                _tid = getattr(_widget, "task_id", None)
                if _tid is not None:
                    _current.append(_tid)
            if _current != _desired:
                for _tid in _desired:
                    _card = self._cards.get(_tid)
                    if _card is not None:
                        self.task_layout.removeWidget(_card)
                        self.task_layout.insertWidget(self.task_layout.count() - 1, _card)

            # 分类过滤：只显示当前分段可见的卡片
            self._update_filter_counts(task_dict)
            _visible = 0
            for _tid, _card in self._cards.items():
                _show = (self._match_filter(task_dict.get(_tid, {}))
                         and self._match_search(task_dict.get(_tid, {}), self._search))
                _card.setVisible(_show)
                if _show:
                    _visible += 1
            if _visible == 0:
                self.empty_label.setText(
                    f"没有匹配「{self._search}」的任务" if self._search
                    else self._filter_hint())
                self.empty_label.show()
            else:
                self.empty_label.hide()

            # 多选集合跟着任务生死走（被清理/删除后不能继续对空气操作），
            # 时机与 Web 端 pruneSelection 相同
            self._selected_ids.intersection_update(task_dict)
            self._sync_selection_visual()
            self._update_select_bar(task_dict)

        except Exception as e:
            self.logger.exception("刷新任务列表失败")

    def _notify_complete(self, task_data):
        """下载完成通知（托盘气泡 + 状态栏 + 可选提示音）"""
        try:
            import notify_sound
            notify_sound.play()
        except Exception:
            pass
        name = task_data.get("filename", "文件")
        _tray_bubble(self, 
            "✅ 下载完成",
            f"{name} 已下载完成！",
            QSystemTrayIcon.MessageIcon.Information,
            4000
        )
        self.status_bar.showMessage(f"✓ 下载完成: {name}", 5000)

    def _show_task_detail(self, task_id):
        """打开任务详情面板：非模态、500ms 自轮询，任务被删除后自动关闭。

        同一时刻只保留一个详情面板：重复打开先关掉旧面板，避免引用被覆盖后
        旧面板的信号处理链路悬空。
        """
        mgr = self._get_manager()
        task = mgr.get_task(task_id)
        if not task:
            return
        old_dlg = self._detail_dialog
        if old_dlg is not None:
            old_dlg.reject()      # 走 done()，确保 finished 触发后再换新面板
            old_dlg.deleteLater()
        dlg = TaskDetailDialog(
            task_id, task.to_dict(),
            fetch=lambda: self._task_detail_data(task_id),
            parent=self, theme=self._theme)
        dlg.action_requested.connect(self._handle_action)
        dlg.finished.connect(self._detail_dialog_closed)
        self._detail_dialog = dlg
        dlg.show()

    def _detail_dialog_closed(self):
        """详情面板关闭后释放引用（下次打开时重建）。"""
        self._detail_dialog = None

    def _task_detail_data(self, task_id):
        """详情面板的数据源：返回最新任务 dict；任务已删除返回 None。"""
        task = self._get_manager().get_task(task_id)
        return task.to_dict() if task is not None else None

    def _notify_failures(self, items):
        """新失败任务的聚合通知（托盘气泡 + 状态栏）。"""
        summary = _fail_summary_text(items)
        self.status_bar.showMessage(summary, 12000)
        _tray_bubble(self, "SwiftDM", summary,
                          QSystemTrayIcon.MessageIcon.Warning, 8000)

    # ==================== 操作处理 ====================

    def _add_download(self):
        url = self.url_input.text().strip()
        dlg = AddDialog(self, url, theme=self._theme)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            if not data["url"]:
                return
            try:
                self._create_and_start(data["url"], data["filename"],
                                       data["segments"], data["dir"] or self.download_dir,
                                       data.get("start_at"))
                self.url_input.clear()
            except Exception as e:
                self.logger.exception("添加下载失败")
                self.status_bar.showMessage(f"添加失败: {e}", 8000)
                QMessageBox.warning(self, "错误", f"添加失败: {e}")

    def _handle_action(self, action, task_id):
        mgr = self._get_manager()
        task = mgr.get_task(task_id)
        if not task:
            return

        if action == "pause":
            task.pause()
        elif action == "resume":
            task.resume()
        elif action == "retry":
            if task.retry():
                self.status_bar.showMessage(f"正在重试: {task.filename}")
            else:
                self.status_bar.showMessage(f"当前状态不支持重试: {task.status}", 5000)
        elif action == "cancel":
            task.cancel()
        elif action == "remove":
            mgr.remove_task(task_id)
        elif action == "open":
            filepath = task.filepath
            if os.path.exists(filepath):
                open_in_system(filepath)
            else:
                open_in_system(os.path.dirname(filepath))
        elif action == "open_folder":
            filepath = task.filepath
            if filepath:
                folder = os.path.dirname(filepath)
                if os.path.isdir(folder):
                    open_in_system(folder)
                    self.status_bar.showMessage(f"已打开文件夹: {folder}", 3000)
                else:
                    self.status_bar.showMessage("文件所在文件夹不存在", 5000)
            else:
                self.status_bar.showMessage("未知文件路径，无法打开文件夹", 5000)
        elif action == "copy_link":
            _link = getattr(task, "url", "") or ""
            if _link:
                QApplication.clipboard().setText(_link)
                self.status_bar.showMessage("已复制下载链接", 3000)
            else:
                self.status_bar.showMessage("该任务没有可复制的链接", 3000)
        elif action == "details":
            self._show_task_detail(task_id)
        # 操作后即时落盘，避免仅依赖 5s 定时保存
        mgr.save_history()

    def _set_filter(self, key):
        """切换状态分段过滤，并持久化以便重启后保持。"""
        # 手工互斥：每次切换都把四颗芯片刷一遍，点已选中的那颗也要按回去，
        # 不然视觉上会掉成「没选中」而 self._filter 还指向它。
        for _k, _b in self._filter_btns.items():
            _b.setChecked(_k == key)
        if key == self._filter:
            return
        self._filter = key
        try:
            import config
            config.set("filter", key)
        except Exception:
            pass
        self._refresh()

    def _set_sort(self, index):
        """切换任务列表排序方式，并持久化便于重启后保持。"""
        try:
            key = SORT_KEYS[index]
        except IndexError:
            return
        if key == self._sort:
            return
        self._sort = key
        try:
            import config
            config.set("sort", key)
        except Exception:
            pass
        self._refresh()

    def _set_compact(self, checked):
        """切换任务列表紧凑模式，并持久化便于重启后保持。"""
        compact = bool(checked)
        if compact == self._compact:
            return
        self._compact = compact
        try:
            import config
            config.set("compact", compact)
        except Exception:
            pass
        for card in self._cards.values():
            card.set_compact(compact)

    def _set_search(self, text):
        """按文件名/链接关键字过滤任务列表。"""
        q = (text or "").strip().lower()
        if q == self._search:
            return
        self._search = q
        self._refresh()

    def _visible_tasks_snapshot(self):
        """当前过滤 + 排序下的任务字典与 id 序列（导航/复制/导出共用）。"""
        mgr = self._get_manager()
        task_dict = {d["task_id"]: d
                     for d in (t.to_dict() for t in mgr.get_all_tasks())}
        ordered = [tid for tid in _sorted_task_ids(task_dict, self._sort)
                   if self._match_filter(task_dict[tid])
                   and self._match_search(task_dict[tid], self._search)]
        return task_dict, ordered

    def _ordered_visible_ids(self):
        """当前过滤 + 排序下可见的任务 id 序列（键盘导航的移动域）。"""
        _, ordered = self._visible_tasks_snapshot()
        return ordered

    def _copy_task_links(self):
        """复制当前可见任务的下载链接到剪贴板（去重、保序）。"""
        task_dict, ordered = self._visible_tasks_snapshot()
        text = _links_text(task_dict[t].get("url", "") for t in ordered)
        if not text:
            self.status_bar.showMessage("没有可复制的链接", 3000)
            return
        QApplication.clipboard().setText(text)
        self.status_bar.showMessage(
            f"已复制 {len(ordered)} 个任务的链接到剪贴板", 4000)

    def _export_task_list(self):
        """导出当前可见任务列表为 CSV（默认落到下载目录）。"""
        task_dict, ordered = self._visible_tasks_snapshot()
        if not ordered:
            self.status_bar.showMessage("没有可导出的任务", 3000)
            return
        default_path = os.path.join(self.download_dir, "swiftdm-tasks.csv")
        path, _ = QFileDialog.getSaveFileName(
            self, "导出任务列表", default_path, "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8-sig", newline="") as f:
                f.write(_tasks_export_text(task_dict, ordered))
        except OSError as e:
            QMessageBox.warning(self, "导出失败", f"无法写入文件: {e}")
            return
        self.status_bar.showMessage(
            f"已导出 {len(ordered)} 个任务到 {path}", 5000)

    def _move_selection(self, delta):
        """↑/↓ 在可见任务间移动选中项；焦点在输入控件上时让位。"""
        if not _keyboard_nav_allowed(QApplication.focusObject()):
            return
        target = _step_selection(self._ordered_visible_ids(),
                                 self._selected_task_id, delta)
        if target is not None:
            self._select_task(target)

    def _open_selected_task(self):
        """回车打开选中任务的文件（与卡片「打开文件」按钮同一路径）。"""
        if not _keyboard_nav_allowed(QApplication.focusObject()):
            return
        if not self._selected_task_id:
            return
        self._handle_action("open", self._selected_task_id)

    def _select_task(self, task_id):
        """选中任务卡片（键盘导航与点击共用）并滚动到可见区域。"""
        # 普通点击/方向键即单选：收起批量选择（与资源管理器行为一致，
        # Ctrl+点击走 _toggle_selection，不经过这里）
        if self._selected_ids:
            self._selected_ids = set()
        previous = self._selected_task_id
        self._selected_task_id = task_id
        if previous and previous != task_id:
            prev_card = self._cards.get(previous)
            if prev_card is not None:
                prev_card.set_selected(False)
        card = self._cards.get(task_id)
        if card is None:
            return
        card.set_selected(True)
        card.setFocus()
        try:
            self.scroll.ensureWidgetVisible(card)
        except Exception:
            pass
        self._update_select_bar()

    def _sync_selection_visual(self):
        """卡片可能因状态变化被重建，按 _selected_task_id 重放选中态。"""
        for tid, card in self._cards.items():
            card.set_selected(tid == self._selected_task_id)
            card.set_checked(tid in self._selected_ids)

    def _toggle_selection(self, task_id, checked):
        """勾选/取消勾选单个任务（卡片勾选框与 Ctrl+点击卡片共用）。"""
        if checked:
            self._selected_ids.add(task_id)
            # 勾选即把键盘导航锚点移过来，Ctrl+A / Esc 才有着力点
            self._selected_task_id = task_id
        else:
            self._selected_ids.discard(task_id)
        self._sync_selection_visual()
        self._update_select_bar()

    def _select_all_visible(self):
        """Ctrl+A 选中当前过滤 + 搜索下可见的全部任务。"""
        if not _keyboard_nav_allowed(QApplication.focusObject()):
            return
        ordered = self._ordered_visible_ids()
        self._selected_ids = set(ordered)
        if ordered:
            self._selected_task_id = ordered[-1]
        self._sync_selection_visual()
        self._update_select_bar()
        self.status_bar.showMessage(f"已选中 {len(ordered)} 个任务", 3000)

    def _clear_selection(self):
        """Esc / 取消选择：清空多选并收起操作条（单选焦点不动）。"""
        if not self._selected_ids:
            return
        self._selected_ids = set()
        self._sync_selection_visual()
        self._update_select_bar()

    def _update_select_bar(self, task_dict=None):
        """按当前多选刷新操作条：计数文案、显隐、四个按钮的可用性。"""
        if task_dict is None:
            mgr = self._get_manager()
            task_dict = {t.task_id: t.to_dict() for t in mgr.get_all_tasks()}
        n = len(self._selected_ids)
        self.select_count.setText(f"已选 {n} 项")
        self.select_bar.setVisible(n > 0)
        for action, btn in self._batch_btns.items():
            btn.setEnabled(bool(batch_targets(self._selected_ids, task_dict, action)))

    def _batch_action(self, action):
        """批量执行动作：状态过滤与 Web 端一致，删除前确认，完成后清空多选。"""
        mgr = self._get_manager()
        task_dict = {t.task_id: t.to_dict() for t in mgr.get_all_tasks()}
        targets = batch_targets(self._selected_ids, task_dict, action)
        if not targets:
            self.status_bar.showMessage("勾选的任务都不支持该操作", 3000)
            return
        if action == "remove":
            reply = QMessageBox.question(
                self, "确认删除", _clear_confirm_text(len(targets)),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        for tid in targets:
            self._handle_action(action, tid)
        self.status_bar.showMessage(
            f"已{BATCH_LABELS[action]} {len(targets)} 个任务", 4000)
        self._clear_selection()

    @staticmethod
    def _match_search(data, query):
        """关键字匹配文件名或下载链接（大小写不敏感）；空关键字时全部通过。"""
        q = (query or "").strip().lower()
        if not q:
            return True
        return (q in str(data.get("filename", "")).lower()
                or q in str(data.get("url", "")).lower())

    def _match_filter(self, data):
        f = self._filter
        status = data.get("status")
        if f == "all":
            return True
        if f == "active":
            return status in ("downloading", "pending", "paused")
        if f == "completed":
            return status == "completed"
        if f == "failed":
            return status in ("failed", "cancelled")
        return True

    def _filter_hint(self):
        return {
            "all": "还没有下载任务\n粘贴链接，或从浏览器捕获，或把链接拖入窗口",
            "active": "当前没有进行中的任务",
            "completed": "还没有已完成任务",
            "failed": "没有失败的任务",
        }[self._filter] if self._filter else "还没有下载任务\n粘贴链接，或从浏览器捕获，或把链接拖入窗口"

    def _update_filter_counts(self, task_dict):
        labels = {"all": "全部", "active": "进行中", "completed": "已完成", "failed": "失败"}
        counts = {
            "all": len(task_dict),
            "active": sum(1 for d in task_dict.values() if d.get("status") in ("downloading", "pending", "paused")),
            "completed": sum(1 for d in task_dict.values() if d.get("status") == "completed"),
            "failed": sum(1 for d in task_dict.values() if d.get("status") in ("failed", "cancelled")),
        }
        for _key, _btn in getattr(self, "_filter_btns", {}).items():
            _btn.setText(f"{labels[_key]} {counts[_key]}")

    def _update_overall(self, tasks):
        _dl = _tt = 0
        for _t in tasks:
            _total = getattr(_t, "total_size", 0) or 0
            _done = getattr(_t, "downloaded", 0) or 0
            if _total > 0:
                _dl += _done
                _tt += _total
        if _tt > 0:
            _pct = int(_dl * 100 / _tt)
            self.overall_label.setText(f"总下载 {format_size(_dl)} / {format_size(_tt)} ({_pct}%)")
        else:
            self.overall_label.setText("")

    def _threads_default(self):
        """读取设置面板的默认线程数；配置是唯一事实来源，
        不再在函数签名里固定 8。"""
        import config as _cfg
        return _cfg.clamp_segments(_cfg.get("segments"))


    def _create_and_start(self, url, filename=None, segments=None, save_dir=None,
                          start_at=None):
        from downloader import manager
        save_dir = save_dir or self.download_dir
        os.makedirs(save_dir, exist_ok=True)
        task = manager.create_task(
            url, save_dir, filename, segments or self._threads_default())
        if start_at:
            # 与 Web /api/add 一致：定时任务先不启动，交给调度器到点拉起
            from scheduler import scheduler as _dl_scheduler
            _dl_scheduler.schedule(task.task_id, float(start_at))
            manager.save_history()
            self.status_bar.showMessage(
                f"已定时: {task.filename}", 5000)
        else:
            task.start()
            manager.save_history()
            self.status_bar.showMessage(f"已添加: {task.filename}")
        return task

    @staticmethod
    def _torrent_paths_from_mime(mime):
        """从拖入数据提取本地 .torrent 文件路径（拖种子文件进窗口即下载）。"""
        out = []
        try:
            if mime.hasUrls():
                for u in mime.urls():
                    if (u.isLocalFile() and u.toLocalFile().lower().endswith(".torrent")):
                        out.append(u.toLocalFile())
        except Exception:
            pass
        seen, res = set(), []
        for p in out:
            if p and p not in seen:
                seen.add(p)
                res.append(p)
        return res

    @staticmethod
    def _urls_from_mime(mime):
        """从拖入数据提取 http(s)/magnet 链接（忽略本地文件）。"""
        out = []
        try:
            if mime.hasUrls():
                for u in mime.urls():
                    if not u.isLocalFile() and u.scheme().lower() in ("http", "https", "magnet"):
                        out.append(u.toString())
        except Exception:
            pass
        try:
            if mime.hasText():
                for tok in re.split(r"\s+", mime.text().strip()):
                    if tok.startswith(("magnet:", "http://", "https://")):
                        out.append(tok)
        except Exception:
            pass
        seen, res = set(), []
        for u in out:
            if u and u not in seen:
                seen.add(u)
                res.append(u)
        return res

    def dragEnterEvent(self, event):
        if (self._urls_from_mime(event.mimeData())
                or self._torrent_paths_from_mime(event.mimeData())):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        mime = event.mimeData()
        urls = self._urls_from_mime(mime)
        torrents = self._torrent_paths_from_mime(mime)
        if not urls and not torrents:
            super().dropEvent(event)
            return
        event.acceptProposedAction()
        added = 0
        for u in urls:
            try:
                self._create_and_start(u)
                added += 1
            except Exception as e:
                self.logger.exception("拖入添加下载失败")
                self.status_bar.showMessage(f"添加失败: {e}", 8000)
        for path in torrents:
            try:
                self._create_and_start(path)
                added += 1
            except Exception as e:
                self.logger.exception("拖入种子文件失败")
                self.status_bar.showMessage(f"添加种子失败: {e}", 8000)
        if added > 1:
            self.status_bar.showMessage(f"已添加 {added} 个下载任务")

    def _pause_all(self):
        mgr = self._get_manager()
        for t in mgr.get_all_tasks():
            if t.status == "downloading":
                t.pause()
        mgr.save_history()
        self.status_bar.showMessage("已暂停全部下载")

    def _resume_all(self):
        mgr = self._get_manager()
        for t in mgr.get_all_tasks():
            if t.status == "paused":
                t.resume()
        mgr.save_history()
        self.status_bar.showMessage("已恢复全部下载")

    def _retry_all_failed(self):
        """批量重试全部失败/已取消任务（失败任务从分片断点续传）。"""
        mgr = self._get_manager()
        failed = [t for t in mgr.get_all_tasks() if t.status in ("failed", "cancelled")]
        if not failed:
            self.status_bar.showMessage("没有需要重试的任务", 3000)
            return
        for t in failed:
            t.retry()
        mgr.save_history()
        self._refresh()
        self.status_bar.showMessage(f"已重新开始 {len(failed)} 个任务")

    def _clear_completed(self):
        mgr = self._get_manager()
        finished = [t for t in mgr.get_all_tasks()
                    if t.status in ("completed", "cancelled", "failed")]
        if not finished:
            self.status_bar.showMessage("没有可清除的任务", 3000)
            return
        reply = QMessageBox.question(
            self, "确认清除", _clear_confirm_text(len(finished)),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        mgr.clear_completed()
        self._completed_tasks.clear()
        self.status_bar.showMessage(f"已清除 {len(finished)} 个任务")

    def _open_download_dir(self):
        """在系统文件管理器中打开当前下载目录。"""
        import config as _cfg
        path = _cfg.get_download_dir()
        try:
            os.makedirs(path, exist_ok=True)
            open_in_system(path)
        except OSError as e:
            self.status_bar.showMessage(f"打开目录失败: {e}", 5000)
            return
        self.status_bar.showMessage(f"已打开下载目录: {path}", 4000)

    def _finish_countdown_text_now(self):
        """当前「全部完成后动作」倒计时文案；无动作/已到点返回 None。"""
        try:
            from scheduler import scheduler as _dl_scheduler
            st = _dl_scheduler.status()
        except Exception:
            return None
        return _finish_countdown_text(st.get("finish_action"), st.get("remaining"))

    def _update_finish_countdown(self):
        """按调度器状态刷新工具栏倒计时按钮（桌面/Web 共用 scheduler 单例）。"""
        text = self._finish_countdown_text_now()
        if text:
            self.finish_btn.setText(text)
            self.finish_btn.setVisible(True)
        else:
            self.finish_btn.setVisible(False)

    def _cancel_finish_action(self):
        """取消「全部下载完成后」倒计时（工具栏按钮点击）。"""
        from scheduler import scheduler as _dl_scheduler
        res = _dl_scheduler.cancel_finish_action()
        if res.get("cancelled"):
            label = FINISH_ACTION_LABELS.get(res.get("action"), "完成后动作")
            self.status_bar.showMessage(f"已取消「{label}」倒计时", 5000)
        self._update_finish_countdown()

    def _show_settings(self):
        dlg = SettingsDialog(self, http_port=self.http_port)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            settings = dlg.get_settings()
            if settings.get("rate_limit") is None:
                QMessageBox.warning(self, "输入无效",
                                    "下载限速请输入非负数字（KB/s），0 或留空表示不限速")
                return
            import config
            # 应用下载目录（真正生效）并持久化 —— Web/浏览器捕获/桌面三端统一读取
            if settings["dir"]:
                self.download_dir = config.set_download_dir(settings["dir"])
            # 应用浏览器监控启停（真正生效，start/stop 已幂等）
            if self.monitor is not None:
                if settings["monitor"]:
                    self.monitor.start()
                else:
                    self.monitor.stop()
            config.set("monitor_enabled", settings["monitor"])
            self.monitor_label.setText(
                "  🌐 监控已启用" if settings["monitor"] else "  🌐 监控已禁用"
            )
            self._set_monitor_state(settings["monitor"])
            # 同步开关到 Flask 捕获端点：禁用时连 5000 端口也不会接管浏览器下载
            try:
                import app as _flask_app
                _flask_app.BROWSER_CAPTURE_ENABLED = settings["monitor"]
            except Exception:
                pass
            # 应用下载代理模式并持久化（重启后仍生效）
            from downloader import set_proxy_mode
            set_proxy_mode(settings.get("proxy_mode", "env"))
            config.set("proxy_mode", settings.get("proxy_mode", "env"))
            config.set("segments", settings.get("segments", 8))
            # 全局限速：应用并持久化（重启后仍生效）
            from throttle import set_rate
            rl = settings.get("rate_limit") or 0
            set_rate(rl)
            config.set("rate_limit", rl)
            # 失败自动重试：管理器按任务失败时实时读取，修改当即生效
            config.set("auto_retry", settings.get("auto_retry", 0))
            # 剪贴板监听：关闭时不再提示；开启时下一个节拍生效
            config.set("clipboard_watch", settings.get("clipboard_watch", False))
            # 「全部下载完成后」动作：应用并持久化（与 Web 端共用 scheduler 单例，重启后仍生效）
            from scheduler import scheduler as _dl_scheduler
            _dl_scheduler.set_finish_action(settings.get("finish_action", "none"))
            config.set("finish_action", _dl_scheduler.get_finish_action())
            # 完成提示音：应用并持久化（重启后仍生效）
            import notify_sound as _notify_sound
            _notify_sound.set_sound(settings.get("notify_sound", "none"))
            config.set("notify_sound", _notify_sound.get_sound())
            # 界面主题：立即生效并持久化（重启后仍是该主题）
            _theme = settings.get("theme", "dark")
            if _theme in THEMES:
                config.set("theme", _theme)
                self._apply_theme(_theme)
            self.logger.info("设置已保存，下载代理模式: %s，下载目录: %s，监控: %s",
                             settings.get("proxy_mode", "env"), self.download_dir, settings["monitor"])
            self.status_bar.showMessage(
                f"设置已保存 | 代理: {settings.get('proxy_mode', 'env')} | 目录: {self.download_dir}", 5000)

    def _quit_app(self):
        # 确认退出
        mgr = self._get_manager()
        active = sum(1 for t in mgr.get_all_tasks() if t.status == "downloading")
        if active > 0:
            reply = QMessageBox.question(
                self, "确认退出",
                f"有 {active} 个下载任务正在进行中，确定要退出吗？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return
        self.tray.hide()
        _save_window_geometry(self, _window_settings())
        QApplication.quit()
