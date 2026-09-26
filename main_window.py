"""
SwiftDM 主窗口 —— PyQt6 原生桌面 UI（IDM 风格）
"""
import os
import sys
import time
import logging
import re
import subprocess
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLineEdit, QLabel, QProgressBar, QScrollArea, QFrame,
    QToolBar, QStatusBar, QSystemTrayIcon, QMenu, QApplication,
    QMessageBox, QFileDialog, QDialog, QDialogButtonBox,
    QFormLayout, QSpinBox, QComboBox, QListWidget, QListWidgetItem,
    QButtonGroup,
    QSizePolicy, QSplitter, QHeaderView, QDockWidget, QPlainTextEdit
)
from PyQt6.QtCore import Qt, QTimer, QSize, QSettings, pyqtSignal, QThread, QMimeData, QUrl, QPoint
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
    return format_size(bps) + "/s"


def _status_title(active, total_speed, total):
    """When downloads are active, surface speed + count in the title bar."""
    if active > 0:
        return f"↓ {format_speed(total_speed)} · {active} 个下载中 - SwiftDM"
    return "SwiftDM - 高速下载管理器"


def _tray_tip(active, total_speed, total):
    """Tray tooltip: live speed and running count."""
    if active > 0:
        return f"SwiftDM · ↓{format_speed(total_speed)} · 下载中 {active}/{total}"
    return "SwiftDM - 下载管理器"


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


def _keyboard_nav_allowed(focus_widget):
    """焦点在输入类控件（输入框/下拉/数字框）上时，让位给控件自身的按键处理。"""
    if focus_widget is None:
        return True
    return not isinstance(focus_widget, NAV_INPUT_TYPES)


TRAY_ICON_STATES = ("idle", "downloading", "attention")
TRAY_ICON_STYLE = {
    "idle": {"bg": "#6c5ce7", "fg": "#ffffff"},
    "downloading": {"bg": "#00d2a0", "fg": "#06281f"},
    "attention": {"bg": "#ff5e7a", "fg": "#2a0a12"},
}


def _tray_icon_state(active, failed):
    """按活跃/失败任务数推导托盘图标状态：下载中 > 有失败 > 空闲。"""
    if active > 0:
        return "downloading"
    if failed > 0:
        return "attention"
    return "idle"


def _tray_icon(icon_state):
    """按状态绘制托盘图标（圆角方块 + 向下箭头；attention 加白色角标）。"""
    style = TRAY_ICON_STYLE.get(icon_state, TRAY_ICON_STYLE["idle"])
    pixmap = QPixmap(32, 32)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(QColor(style["bg"])))
    painter.drawRoundedRect(2, 4, 28, 24, 6, 6)
    painter.setBrush(QBrush(QColor(style["fg"])))
    painter.drawRoundedRect(14, 8, 4, 12, 2, 2)          # 箭头杆
    painter.drawPolygon(QPolygon([QPoint(11, 17), QPoint(21, 17), QPoint(16, 25)]))
    if icon_state == "attention":
        painter.setBrush(QBrush(QColor("#ffffff")))
        painter.drawEllipse(20, 20, 9, 9)               # 右下角提示点
    painter.end()
    return QIcon(pixmap)


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
QSS = """
QMainWindow {
    background-color: #0f0f14;
}
QWidget {
    background-color: #0f0f14;
    color: #e0e0e8;
    font-family: "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif;
    font-size: 13px;
}
QToolBar {
    background-color: #16161f;
    border-bottom: 1px solid #2a2a3a;
    padding: 6px 10px;
    spacing: 8px;
}
QToolBar QPushButton {
    background-color: #22222e;
    border: 1px solid #2a2a3a;
    border-radius: 6px;
    padding: 7px 16px;
    color: #e0e0e8;
    font-weight: 600;
    font-size: 12px;
}
QToolBar QPushButton:hover {
    background-color: #2e2e3e;
    border-color: #6c5ce7;
}
QToolBar QPushButton#btnAdd {
    background-color: #6c5ce7;
    color: #fff;
    border: none;
}
QToolBar QPushButton#btnAdd:hover {
    background-color: #7d6ff0;
}
QToolBar QPushButton#btnFinishCountdown {
    background-color: #3d3320;
    color: #ffa502;
    border: 1px solid #ffa502;
}
QToolBar QPushButton#btnFinishCountdown:hover {
    background-color: #4d3f28;
}
QLineEdit {
    background-color: #1a1a26;
    border: 1px solid #2a2a3a;
    border-radius: 6px;
    padding: 8px 14px;
    color: #e0e0e8;
    font-size: 13px;
    selection-background-color: #6c5ce7;
}
QLineEdit:focus {
    border-color: #6c5ce7;
}
QScrollArea {
    border: none;
    background-color: transparent;
}
QScrollBar:vertical {
    background: #0f0f14;
    width: 8px;
    border-radius: 4px;
}
QScrollBar::handle:vertical {
    background: #2a2a3a;
    border-radius: 4px;
    min-height: 40px;
}
QScrollBar::handle:vertical:hover {
    background: #3a3a4a;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QStatusBar {
    background-color: #16161f;
    border-top: 1px solid #2a2a3a;
    color: #8888a0;
    font-size: 12px;
}
QProgressBar {
    background-color: #1a1a26;
    border: none;
    border-radius: 3px;
    height: 6px;
    text-align: center;
    font-size: 0px;
}
QProgressBar::chunk {
    background-color: #6c5ce7;
    border-radius: 3px;
}
QMenu {
    background-color: #1a1a23;
    border: 1px solid #2a2a3a;
    border-radius: 6px;
    padding: 4px;
}
QMenu::item {
    padding: 8px 30px;
    border-radius: 4px;
}
QMenu::item:selected {
    background-color: #2e2e3e;
}
QLabel#titleLabel {
    font-size: 16px;
    font-weight: 700;
    color: #fff;
}
QLabel#speedLabel {
    color: #a29bfe;
    font-weight: 700;
    font-size: 13px;
}
QDialog {
    background-color: #1a1a23;
}
QSpinBox, QComboBox {
    background-color: #22222e;
    border: 1px solid #2a2a3a;
    border-radius: 4px;
    padding: 5px 8px;
    color: #e0e0e8;
}
QSpinBox:focus, QComboBox:focus {
    border-color: #6c5ce7;
}
QComboBox QAbstractItemView {
    background-color: #1a1a23;
    border: 1px solid #2a2a3a;
    selection-background-color: #2e2e3e;
}
QHeaderView::section {
    background-color: #16161f;
    border: none;
    border-bottom: 1px solid #2a2a3a;
    padding: 6px;
    color: #8888a0;
    font-weight: 600;
}
QPushButton#filterBtn {
    background:#1a1a26; border:1px solid #2a2a3a; border-radius:13px;
    padding:4px 14px; color:#8888a0; font-size:12px; font-weight:600;
}
QPushButton#filterBtn:hover { color:#e0e0e8; border-color:#3a3a52; }
QPushButton#filterBtn:checked { background:#6c5ce7; border-color:#6c5ce7; color:#fff; }
QMenu::separator { height:1px; background:#2a2a3a; margin:4px 10px; }
"""


class TaskCard(QFrame):
    """单个下载任务卡片"""
    action_triggered = pyqtSignal(str, str)  # action, task_id
    selected = pyqtSignal(str)              # task_id：点击卡片即选中（键盘导航配合）

    def __init__(self, task_data, parent=None):
        super().__init__(parent)
        self.task_id = task_data["task_id"]
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.filepath = task_data.get("filepath", "")
        self._drag_start_pos = None
        self._built_status = task_data.get("status", "pending")  # 卡片按钮按此状态生成
        self.url = task_data.get("url", "")
        self.setObjectName("taskCard")
        self.setStyleSheet("""
            TaskCard {
                background-color: #1a1a23;
                border: 1px solid #2a2a3a;
                border-radius: 8px;
                padding: 2px;
            }
            TaskCard:hover {
                border-color: #3a3a52;
            }
            TaskCard[selected="true"] {
                border: 1px solid #6c5ce7;
                background-color: #20202e;
            }
        """)
        self.setMinimumHeight(120)
        self._base_max_height = 140
        self.setMaximumHeight(self._base_max_height)
        self._build_ui(task_data)

    def set_selected(self, on):
        """选中态：高亮边框；配合 dynamic property 重算样式。"""
        self.setProperty("selected", bool(on))
        self.style().unpolish(self)
        self.style().polish(self)

    def _build_ui(self, task_data):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(6)

        # 第一行：文件名 + 状态
        top = QHBoxLayout()
        top.setSpacing(10)

        name = task_data.get("filename", "unknown")
        self.name_label = QLabel(name)
        self.name_label.setStyleSheet("font-size: 13px; font-weight: 600; color: #fff;")
        self.name_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.name_label.setToolTip(name)
        top.addWidget(self.name_label, 1)

        status = task_data.get("status", "pending")
        status_text = {
            "downloading": "● 下载中", "paused": "⏸ 已暂停",
            "completed": "✓ 完成", "failed": "✗ 失败",
            "pending": "⏳ 等待中", "cancelled": "✗ 已取消"
        }
        status_color = {
            "downloading": "#a29bfe", "paused": "#ffa502",
            "completed": "#00d2a0", "failed": "#ff5e7a",
            "pending": "#8888a0", "cancelled": "#8888a0"
        }
        self.status_label = QLabel(status_text.get(status, status))
        self.status_label.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {status_color.get(status, '#8888a0')}; "
            f"background-color: {status_color.get(status, '#8888a0')}22; "
            f"border-radius: 10px; padding: 2px 10px;"
        )
        top.addWidget(self.status_label)
        layout.addLayout(top)

        # 进度条
        prog = task_data.get("progress", 0)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(int(prog))
        self.progress_bar.setTextVisible(False)

        color = status_color.get(status, "#6c5ce7")
        if status == "completed":
            self.progress_bar.setStyleSheet(
                "QProgressBar{background:#1a1a26;border:none;border-radius:3px;height:6px;}"
                "QProgressBar::chunk{background:#00d2a0;border-radius:3px;}"
            )
        elif status == "paused":
            self.progress_bar.setStyleSheet(
                "QProgressBar{background:#1a1a26;border:none;border-radius:3px;height:6px;}"
                "QProgressBar::chunk{background:#ffa502;border-radius:3px;}"
            )
        else:
            self.progress_bar.setStyleSheet(
                "QProgressBar{background:#1a1a26;border:none;border-radius:3px;height:6px;}"
                "QProgressBar::chunk{background:#6c5ce7;border-radius:3px;}"
            )

        prog_layout = QHBoxLayout()
        prog_layout.setSpacing(8)
        prog_layout.addWidget(self.progress_bar, 1)
        self.prog_label = QLabel(f"{prog:.1f}%")
        self.prog_label.setStyleSheet("font-size: 12px; font-weight: 700; color: #fff; min-width: 42px;")
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
        self.size_label = QLabel(info_text)
        self.size_label.setStyleSheet("font-size: 11px; color: #8888a0;")
        bottom.addWidget(self.size_label)

        self.speed_label = QLabel(f"⚡ {format_speed(speed)}" if speed > 0 else "")
        self.speed_label.setStyleSheet("font-size: 11px; color: #a29bfe; font-weight: 600;")
        bottom.addWidget(self.speed_label)

        self.eta_label = QLabel(f"⏱ {eta}" if eta else "")
        self.eta_label.setStyleSheet("font-size: 11px; color: #8888a0;")
        bottom.addWidget(self.eta_label)

        bottom.addStretch(1)

        # 操作按钮
        if status == "downloading":
            btn_pause = QPushButton("⏸ 暂停")
            btn_pause.setStyleSheet(self._btn_style("#ffa502"))
            btn_pause.clicked.connect(lambda: self.action_triggered.emit("pause", self.task_id))
            bottom.addWidget(btn_pause)

            btn_cancel = QPushButton("✕ 取消")
            btn_cancel.setStyleSheet(self._btn_style("#ff5e7a"))
            btn_cancel.clicked.connect(lambda: self.action_triggered.emit("cancel", self.task_id))
            bottom.addWidget(btn_cancel)

        elif status == "paused":
            btn_resume = QPushButton("▶ 继续")
            btn_resume.setStyleSheet(self._btn_style("#00d2a0"))
            btn_resume.clicked.connect(lambda: self.action_triggered.emit("resume", self.task_id))
            bottom.addWidget(btn_resume)

            btn_cancel = QPushButton("✕ 取消")
            btn_cancel.setStyleSheet(self._btn_style("#ff5e7a"))
            btn_cancel.clicked.connect(lambda: self.action_triggered.emit("cancel", self.task_id))
            bottom.addWidget(btn_cancel)

        elif status in ("failed", "cancelled", "completed"):
            if status in ("failed", "cancelled"):
                btn_retry = QPushButton("↻ 重试")
                btn_retry.setStyleSheet(self._btn_style("#4da6ff"))
                btn_retry.clicked.connect(lambda: self.action_triggered.emit("retry", self.task_id))
                bottom.addWidget(btn_retry)

            btn_remove = QPushButton("🗑 删除")
            btn_remove.setStyleSheet(self._btn_style("#ff5e7a"))
            btn_remove.clicked.connect(lambda: self.action_triggered.emit("remove", self.task_id))
            bottom.addWidget(btn_remove)

        if status == "completed" and task_data.get("total_size", 0) > 0:
            btn_open = QPushButton("📂 打开文件")
            btn_open.setStyleSheet(self._btn_style("#4da6ff"))
            btn_open.clicked.connect(lambda: self.action_triggered.emit("open", self.task_id))
            bottom.addWidget(btn_open)

            btn_open_folder = QPushButton("🗁 打开文件夹")
            btn_open_folder.setStyleSheet(self._btn_style("#a29bfe"))
            btn_open_folder.clicked.connect(lambda: self.action_triggered.emit("open_folder", self.task_id))
            bottom.addWidget(btn_open_folder)

        layout.addLayout(bottom)

        # 失败原因（默认隐藏，失败且有错误信息时显示）
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(
            "font-size: 11px; color: #ffb3c0; background-color: #2a1620; "
            "border: 1px solid #ff5e7a55; border-radius: 6px; padding: 6px 8px; margin-top: 2px;"
        )
        layout.addWidget(self.error_label)
        self._apply_error_visibility(status, task_data.get("error", ""))

    # 允许从已完成卡片的文件区域拖出文件（如拖到资源管理器、聊天窗口等）
    def mousePressEvent(self, event):
        self.selected.emit(self.task_id)
        if (event.button() == Qt.MouseButton.LeftButton
                and self.filepath and os.path.exists(self.filepath)):
            self._drag_start_pos = event.pos()
        else:
            self._drag_start_pos = None
        super().mousePressEvent(event)

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
        )

    def _apply_error_visibility(self, status, err):
        """失败且有错误信息时显示原因，并解除高度限制以保证完整可见。"""
        if status == "failed" and err:
            self.error_label.setText(f"⚠ 失败原因: {err}")
            self.error_label.show()
            self.setMaximumHeight(16777215)  # 解除上限，完整显示多行错误
        else:
            self.error_label.hide()
            self.setMaximumHeight(self._base_max_height)

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
            add("打开文件夹", "open_folder")
        elif status in ("failed", "cancelled"):
            add("重试", "retry")
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
        status_color = {
            "downloading": "#a29bfe", "paused": "#ffa502",
            "completed": "#00d2a0", "failed": "#ff5e7a",
            "pending": "#8888a0", "cancelled": "#8888a0"
        }
        color = status_color.get(status, "#8888a0")
        self.status_label.setText(status_text.get(status, status))
        self.status_label.setStyleSheet(
            f"font-size: 11px; font-weight: 600; color: {color}; "
            f"background-color: {color}22; border-radius: 10px; padding: 2px 10px;"
        )

        self.progress_bar.setValue(int(prog))
        if status == "completed":
            self.progress_bar.setStyleSheet(
                "QProgressBar{background:#1a1a26;border:none;border-radius:3px;height:6px;}"
                "QProgressBar::chunk{background:#00d2a0;border-radius:3px;}"
            )
        elif status == "paused":
            self.progress_bar.setStyleSheet(
                "QProgressBar{background:#1a1a26;border:none;border-radius:3px;height:6px;}"
                "QProgressBar::chunk{background:#ffa502;border-radius:3px;}"
            )
        else:
            self.progress_bar.setStyleSheet(
                "QProgressBar{background:#1a1a26;border:none;border-radius:3px;height:6px;}"
                "QProgressBar::chunk{background:#6c5ce7;border-radius:3px;}"
            )
        self.prog_label.setText(f"{prog:.1f}%")

        info = f"📦 {format_size(downloaded)}"
        if total_size > 0:
            info += f" / {format_size(total_size)}"
        if task_data.get("protocol") == "torrent":
            seeds = task_data.get("seeds", 0)
            peers = task_data.get("peers", 0)
            info += f"  🌱 {seeds}  👥 {peers}"
        self.size_label.setText(info)

        self.speed_label.setText(f"⚡ {format_speed(speed)}" if speed > 0 and status == "downloading" else "")
        self.eta_label.setText(f"⏱ {eta}" if eta and status == "downloading" else "")

        # 失败原因显示
        self._apply_error_visibility(status, task_data.get("error", ""))


class SettingsDialog(QDialog):
    """设置对话框"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("⚙ 设置")
        self.setMinimumWidth(400)
        self.setStyleSheet("""
            QDialog { background-color: #1a1a23; border: 1px solid #2a2a3a; border-radius: 10px; }
            QLabel { font-size: 13px; color: #ccc; }
        """)

        layout = QFormLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(24, 24, 24, 24)

        # 下载目录：预填当前生效目录（共享配置），而非空白占位
        import config
        self.dir_edit = QLineEdit(config.get_download_dir())
        btn_browse = QPushButton("浏览...")
        btn_browse.clicked.connect(self._browse_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(btn_browse)
        layout.addRow("下载目录:", dir_row)

        self.segments_spin = QSpinBox()
        self.segments_spin.setRange(1, 32)
        self.segments_spin.setValue(int(config.get("segments")))
        self.segments_spin.setToolTip("多线程分段数，越大速度越快但占用更多资源")
        layout.addRow("下载线程数:", self.segments_spin)

        import throttle
        self.rate_edit = QLineEdit(_format_rate_kbps(throttle.get_rate()))
        self.rate_edit.setPlaceholderText("0 = 不限速")
        self.rate_edit.setToolTip("全局下载限速（KB/s），0 或留空表示不限速；所有任务与分段共享")
        rate_row = QHBoxLayout()
        rate_row.addWidget(self.rate_edit, 1)
        rate_row.addWidget(QLabel("KB/s"))
        layout.addRow("下载限速:", rate_row)

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
        layout.addRow("全部下载完成后:", self.finish_combo)

        self.monitor_check = QComboBox()
        self.monitor_check.addItems(["启用", "禁用"])
        self.monitor_check.setCurrentIndex(0 if config.get("monitor_enabled") else 1)
        layout.addRow("浏览器监控:", self.monitor_check)

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
        layout.addRow("下载代理:", self.proxy_combo)

        self.proxy_custom = QLineEdit()
        self.proxy_custom.setPlaceholderText("如 http://127.0.0.1:7890 或 socks5://...")
        if cur not in ("env", "system", "direct", ""):
            self.proxy_custom.setText(cur)
        layout.addRow("自定义代理:", self.proxy_custom)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        btns.setStyleSheet("QPushButton{padding:6px 18px;border-radius:4px;}")
        layout.addRow(btns)

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
            "proxy_mode": proxy_mode,
            "rate_limit": _parse_rate_kbps(self.rate_edit.text()),
            "finish_action": self.finish_combo.currentData() or "none",
        }


class AddDialog(QDialog):
    """添加下载对话框"""
    def __init__(self, parent=None, url=""):
        super().__init__(parent)
        self.setWindowTitle("📥 新建下载任务")
        self.setMinimumWidth(520)
        self.setStyleSheet("""
            QDialog { background-color: #1a1a23; border: 1px solid #2a2a3a; border-radius: 10px; }
            QLabel { font-size: 13px; color: #ccc; }
        """)

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

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addRow(btns)

    def _browse_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择下载目录")
        if d:
            self.dir_edit.setText(d)

    def get_data(self):
        return {
            "url": self.url_edit.text().strip(),
            "filename": self.name_edit.text().strip() or None,
            "dir": self.dir_edit.text().strip() or None,
            "segments": self.seg_spin.value(),
        }


class MainWindow(QMainWindow):
    """SwiftDM 主窗口"""
    log_signal = pyqtSignal(str)

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
        self._cards = {}  # task_id -> TaskCard
        self._selected_task_id = None  # 键盘/鼠标选中的任务（↑/↓ 导航）
        self._search = ""  # 任务搜索关键字（文件名/链接，大小写不敏感）
        self._shortcuts = []  # [(seq, QShortcut)] for tests/extensibility
        self._completed_tasks = set()  # 追踪新完成的任务用于通知
        self._prev_statuses = {}  # task_id -> status
        self._first_refresh = True  # 首刷：从历史加载的任务作为已知状态，不再弹完成/失败通知

        self.setWindowTitle("SwiftDM - 高速下载管理器")
        self.setMinimumSize(780, 560)
        if not _restore_window_geometry(self, _window_settings()):
            self.resize(860, 640)  # 无历史记录时的默认尺寸

        # 暗色主题
        self.setStyleSheet(QSS)
        self.setAcceptDrops(True)  # 支持把链接/磁力拖入窗口即新建下载

        self._setup_log_panel()
        self._setup_toolbar()
        self._setup_central()
        self._setup_statusbar()
        self._setup_tray()
        self._setup_shortcuts()

        # 定时刷新
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(500)

        # 初始加载
        self._refresh()

    def _setup_shortcuts(self):
        """Global shortcuts: Ctrl+N new download, Ctrl+F focus the link box."""
        for _seq, _slot in (
            ("Ctrl+N", self._add_download),
            ("Ctrl+F", self.url_input.setFocus),
            ("Up", lambda: self._move_selection(-1)),
            ("Down", lambda: self._move_selection(1)),
            ("Return", lambda: self._open_selected_task()),
        ):
            _sc = QShortcut(QKeySequence(_seq), self)
            _sc.activated.connect(_slot)
            self._shortcuts.append((_seq, _sc))

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

        btn_add = QPushButton("＋ 下载")
        btn_add.setObjectName("btnAdd")
        btn_add.clicked.connect(self._add_download)
        toolbar.addWidget(btn_add)

        toolbar.addSeparator()

        btn_pause_all = QPushButton("⏸ 全部暂停")
        btn_pause_all.clicked.connect(self._pause_all)
        toolbar.addWidget(btn_pause_all)

        btn_resume_all = QPushButton("▶ 全部恢复")
        btn_resume_all.clicked.connect(self._resume_all)
        toolbar.addWidget(btn_resume_all)

        btn_retry_failed = QPushButton("↻ 重试失败")
        btn_retry_failed.clicked.connect(self._retry_all_failed)
        toolbar.addWidget(btn_retry_failed)

        btn_clear = QPushButton("🗑 清除已完成")
        btn_clear.clicked.connect(self._clear_completed)
        toolbar.addWidget(btn_clear)

        btn_open_dir = QPushButton("📂 打开目录")
        btn_open_dir.clicked.connect(self._open_download_dir)
        toolbar.addWidget(btn_open_dir)

        toolbar.addSeparator()

        self.btn_log = QPushButton("📜 日志")
        self.btn_log.setCheckable(True)
        self.btn_log.toggled.connect(self.log_dock.setVisible)
        toolbar.addWidget(self.btn_log)

        btn_settings = QPushButton("⚙ 设置")
        btn_settings.clicked.connect(self._show_settings)
        toolbar.addWidget(btn_settings)

        # 浏览器监控状态
        self.monitor_label = QLabel("  🌐 监控已启用")
        self.monitor_label.setStyleSheet("font-size: 11px; color: #00d2a0; font-weight: 600;")
        toolbar.addWidget(self.monitor_label)

        # 「全部下载完成后」倒计时：仅倒计时进行中显示，点击取消
        self.finish_btn = QPushButton("")
        self.finish_btn.setObjectName("btnFinishCountdown")
        self.finish_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.finish_btn.setVisible(False)
        self.finish_btn.setToolTip("全部下载完成后的倒计时进行中，点击取消")
        self.finish_btn.clicked.connect(self._cancel_finish_action)
        toolbar.addWidget(self.finish_btn)

    def _setup_central(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 头部信息栏
        header = QWidget()
        header.setStyleSheet("background-color: #16161f; border-bottom: 1px solid #2a2a3a;")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 8, 16, 8)

        self.total_speed_label = QLabel("总速度: 0 B/s")
        self.total_speed_label.setObjectName("speedLabel")
        header_layout.addWidget(self.total_speed_label)

        self.overall_label = QLabel("")
        self.overall_label.setStyleSheet("font-size: 12px; color:#8888a0; margin-left: 14px;")
        header_layout.addWidget(self.overall_label)

        header_layout.addStretch()

        self.stats_label = QLabel("下载中: 0  |  已完成: 0  |  失败: 0  |  总计: 0")
        self.stats_label.setStyleSheet("font-size: 12px; color: #8888a0;")
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
        except Exception:
            self._sort = "default"
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        self._filter_btns = {}
        for _key, _label in [("all", "全部"), ("active", "进行中"),
                             ("completed", "已完成"), ("failed", "失败")]:
            _b = QPushButton(_label)
            _b.setObjectName("filterBtn")
            _b.setCheckable(True)
            _b.setCursor(Qt.CursorShape.PointingHandCursor)
            _b.clicked.connect(lambda _=False, k=_key: self._set_filter(k))
            self._filter_group.addButton(_b)
            self._filter_btns[_key] = _b
            fb.addWidget(_b)
        fb.addStretch(1)
        self.sort_combo = QComboBox()
        for _key in SORT_KEYS:
            self.sort_combo.addItem(SORT_LABELS[_key], _key)
        self.sort_combo.setCurrentIndex(max(0, SORT_KEYS.index(self._sort)))
        self.sort_combo.setToolTip("任务列表排序方式")
        self.sort_combo.currentIndexChanged.connect(self._set_sort)
        fb.addWidget(self.sort_combo)
        self.search_input = QLineEdit()
        self.search_input.setPlaceholderText("搜索任务…")
        self.search_input.setClearButtonEnabled(True)
        self.search_input.setFixedWidth(200)
        self.search_input.textChanged.connect(self._set_search)
        fb.addWidget(self.search_input)
        if self._filter in self._filter_btns:
            self._filter_btns[self._filter].setChecked(True)
        layout.addWidget(filter_bar)

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
        self.empty_label.setStyleSheet("font-size: 15px; color: #555; padding: 60px;")
        self.task_layout.addWidget(self.empty_label)

    def _setup_statusbar(self):
        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("就绪  |  下载目录: ~/Downloads/IDM_Downloads")

    def _setup_log_panel(self):
        """底部可展开的运行日志面板"""
        self.log_dock = QDockWidget("运行日志", self)
        self.log_dock.setAllowedAreas(Qt.DockWidgetArea.BottomDockWidgetArea |
                                      Qt.DockWidgetArea.RightDockWidgetArea)
        self.log_dock.setStyleSheet(
            "QDockWidget::title{background:#16161f;color:#8888a0;padding:4px 10px;}")
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setStyleSheet(
            "QPlainTextEdit{background:#0a0a0f;color:#cfcfe0;"
            "font-family:'Consolas','Menlo','Courier New',monospace;font-size:12px;border:none;}"
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
        self.tray.setIcon(_tray_icon("idle"))
        self._tray_icon_state = "idle"
        self.tray.setToolTip("SwiftDM - 下载管理器")

        tray_menu = QMenu()
        show_action = QAction("显示主窗口", self)
        show_action.triggered.connect(self.show)
        tray_menu.addAction(show_action)
        tray_menu.addSeparator()
        quit_action = QAction("退出", self)
        quit_action.triggered.connect(self._quit_app)
        tray_menu.addAction(quit_action)

        self.tray.setContextMenu(tray_menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _update_tray_icon(self, active, failed):
        """托盘图标随任务状态切换（下载中/空闲/有失败）；状态未变则跳过。"""
        state = _tray_icon_state(active, failed)
        if state == self._tray_icon_state:
            return
        self._tray_icon_state = state
        self.tray.setIcon(_tray_icon(state))

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.activateWindow()

    def closeEvent(self, event):
        """关闭窗口时隐藏到托盘"""
        _save_window_geometry(self, _window_settings())
        event.ignore()
        self.hide()
        self.tray.showMessage("SwiftDM", "已最小化到系统托盘，下载任务继续运行",
                              QSystemTrayIcon.MessageIcon.Information, 2000)

    # ==================== 数据刷新 ====================

    def _get_manager(self):
        """获取全局下载管理器（延迟导入避免循环引用）"""
        from downloader import manager
        return manager

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
            self.tray.setToolTip(_tray_tip(stats["active"], stats["total_speed"], stats["total"]))
            self._update_tray_icon(stats["active"], stats["failed"])
            self._update_overall(tasks)
            self._update_finish_countdown()

            if not tasks:
                self._update_filter_counts({})
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
                task_dict[d["task_id"]] = d

            if self._first_refresh:
                # 首刷：从历史加载的任务直接作为「已知」状态，避免重启时弹一堆旧通知
                self._prev_statuses = {tid: d["status"] for tid, d in task_dict.items()}
                self._first_refresh = False
            else:
                for tid, d in task_dict.items():
                    prev_status = self._prev_statuses.get(tid)
                    if prev_status != "completed" and d["status"] == "completed":
                        if tid not in self._completed_tasks:
                            self._completed_tasks.add(tid)
                            self._notify_complete(d)
                    # 新失败的任务：状态栁直接提示失败原因
                    elif prev_status != "failed" and d["status"] == "failed":
                        err = d.get("error", "")
                        name = d.get("filename", "文件")
                        if err:
                            self.status_bar.showMessage(f"✗ 下载失败 [{name}]: {err}", 10000)
                        else:
                            self.status_bar.showMessage(f"✗ 下载失败: {name}", 10000)

                self._prev_statuses = {tid: d["status"] for tid, d in task_dict.items()}

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
                    card = TaskCard(data)
                    card.action_triggered.connect(self._handle_action)
                    card.selected.connect(lambda tid: self._select_task(tid))
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

            self._sync_selection_visual()

        except Exception as e:
            self.logger.exception("刷新任务列表失败")

    def _notify_complete(self, task_data):
        """下载完成通知"""
        name = task_data.get("filename", "文件")
        self.tray.showMessage(
            "✅ 下载完成",
            f"{name} 已下载完成！",
            QSystemTrayIcon.MessageIcon.Information,
            4000
        )
        self.status_bar.showMessage(f"✓ 下载完成: {name}", 5000)

    # ==================== 操作处理 ====================

    def _add_download(self):
        url = self.url_input.text().strip()
        dlg = AddDialog(self, url)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            data = dlg.get_data()
            if not data["url"]:
                return
            try:
                self._create_and_start(data["url"], data["filename"],
                                       data["segments"], data["dir"] or self.download_dir)
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
        # 操作后即时落盘，避免仅依赖 5s 定时保存
        mgr.save_history()

    def _set_filter(self, key):
        """切换状态分段过滤，并持久化以便重启后保持。"""
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

    def _set_search(self, text):
        """按文件名/链接关键字过滤任务列表。"""
        q = (text or "").strip().lower()
        if q == self._search:
            return
        self._search = q
        self._refresh()

    def _ordered_visible_ids(self):
        """当前过滤 + 排序下可见的任务 id 序列（键盘导航的移动域）。"""
        mgr = self._get_manager()
        task_dict = {d["task_id"]: d
                     for d in (t.to_dict() for t in mgr.get_all_tasks())}
        ordered = _sorted_task_ids(task_dict, self._sort)
        return [tid for tid in ordered
                if self._match_filter(task_dict[tid])
                and self._match_search(task_dict[tid], self._search)]

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

    def _sync_selection_visual(self):
        """卡片可能因状态变化被重建，按 _selected_task_id 重放选中态。"""
        for tid, card in self._cards.items():
            card.set_selected(tid == self._selected_task_id)

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

    def _create_and_start(self, url, filename=None, segments=8, save_dir=None):
        from downloader import manager
        save_dir = save_dir or self.download_dir
        os.makedirs(save_dir, exist_ok=True)
        task = manager.create_task(url, save_dir, filename, segments or 8)
        task.start()
        manager.save_history()
        self.status_bar.showMessage(f"已添加: {task.filename}")
        return task

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
        if self._urls_from_mime(event.mimeData()):
            event.acceptProposedAction()
        else:
            super().dragEnterEvent(event)

    def dropEvent(self, event):
        urls = self._urls_from_mime(event.mimeData())
        if not urls:
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

    def _update_finish_countdown(self):
        """按调度器状态刷新工具栏倒计时按钮（桌面/Web 共用 scheduler 单例）。"""
        try:
            from scheduler import scheduler as _dl_scheduler
            st = _dl_scheduler.status()
        except Exception:
            return
        text = _finish_countdown_text(st.get("finish_action"), st.get("remaining"))
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
        dlg = SettingsDialog(self)
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
            self.monitor_label.setStyleSheet(
                f"font-size: 11px; color: {'#00d2a0' if settings['monitor'] else '#ff5e7a'}; font-weight: 600;"
            )
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
            # 「全部下载完成后」动作：应用并持久化（与 Web 端共用 scheduler 单例，重启后仍生效）
            from scheduler import scheduler as _dl_scheduler
            _dl_scheduler.set_finish_action(settings.get("finish_action", "none"))
            config.set("finish_action", _dl_scheduler.get_finish_action())
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
