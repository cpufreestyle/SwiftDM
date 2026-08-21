"""
SwiftDM 主窗口 —— PyQt6 原生桌面 UI（IDM 风格）
"""
import os
import sys
import time
import logging
import subprocess
from PyQt6.QtWidgets import (
    QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton,
    QLineEdit, QLabel, QProgressBar, QScrollArea, QFrame,
    QToolBar, QStatusBar, QSystemTrayIcon, QMenu, QApplication,
    QMessageBox, QFileDialog, QDialog, QDialogButtonBox,
    QFormLayout, QSpinBox, QComboBox, QListWidget, QListWidgetItem,
    QSizePolicy, QSplitter, QHeaderView, QDockWidget, QPlainTextEdit
)
from PyQt6.QtCore import Qt, QTimer, QSize, pyqtSignal, QThread
from PyQt6.QtGui import QAction, QIcon, QFont, QColor, QPalette, QPixmap, QPainter, QBrush
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
"""


class TaskCard(QFrame):
    """单个下载任务卡片"""
    action_triggered = pyqtSignal(str, str)  # action, task_id

    def __init__(self, task_data, parent=None):
        super().__init__(parent)
        self.task_id = task_data["task_id"]
        self._built_status = task_data.get("status", "pending")  # 卡片按钮按此状态生成
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
        """)
        self.setMinimumHeight(120)
        self._base_max_height = 140
        self.setMaximumHeight(self._base_max_height)
        self._build_ui(task_data)

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
            btn_open = QPushButton("📂 打开")
            btn_open.setStyleSheet(self._btn_style("#4da6ff"))
            btn_open.clicked.connect(lambda: self.action_triggered.emit("open", self.task_id))
            bottom.addWidget(btn_open)

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

    def update_data(self, task_data):
        """更新卡片显示"""
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

        self.dir_edit = QLineEdit()
        self.dir_edit.setPlaceholderText("默认: ~/Downloads/IDM_Downloads")
        btn_browse = QPushButton("浏览...")
        btn_browse.clicked.connect(self._browse_dir)
        dir_row = QHBoxLayout()
        dir_row.addWidget(self.dir_edit, 1)
        dir_row.addWidget(btn_browse)
        layout.addRow("下载目录:", dir_row)

        self.segments_spin = QSpinBox()
        self.segments_spin.setRange(1, 32)
        self.segments_spin.setValue(8)
        self.segments_spin.setToolTip("多线程分段数，越大速度越快但占用更多资源")
        layout.addRow("下载线程数:", self.segments_spin)

        self.monitor_check = QComboBox()
        self.monitor_check.addItems(["启用", "禁用"])
        layout.addRow("浏览器监控:", self.monitor_check)

        # 代理模式：系统代理 / 直连 / 自定义
        from downloader import get_proxy_mode
        self.proxy_combo = QComboBox()
        self.proxy_combo.addItems(["系统代理 (env)", "直连 (direct)"])
        self.proxy_combo.setToolTip(
            "系统代理: 继承 HTTP_PROXY/HTTPS_PROXY，GitHub 等资源必须走代理\n"
            "直连: 忽略代理，仅适合代理宕机时\n"
            "自定义: 在下方填写代理地址（如 http://127.0.0.1:7890 或 socks5://...）")
        cur = get_proxy_mode()
        self.proxy_combo.setCurrentIndex(0 if cur in ("env", "system", "") else 1)
        layout.addRow("下载代理:", self.proxy_combo)

        self.proxy_custom = QLineEdit()
        self.proxy_custom.setPlaceholderText("自定义代理地址（选「直连」时可填，留空=纯直连）")
        if cur not in ("env", "system", "direct", ""):
            self.proxy_combo.setCurrentIndex(1)
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
        if self.proxy_combo.currentIndex() == 0:
            proxy_mode = "env"
        else:
            custom = self.proxy_custom.text().strip()
            proxy_mode = custom or "direct"
        return {
            "dir": self.dir_edit.text(),
            "segments": self.segments_spin.value(),
            "monitor": self.monitor_check.currentIndex() == 0,
            "proxy_mode": proxy_mode,
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
        self.url_edit.setPlaceholderText("https://example.com/file.zip")
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
        self.download_dir = os.path.join(os.path.expanduser("~"), "Downloads", "IDM_Downloads")
        # 日志：文件 + 控制台 + UI 面板
        self.logger = setup_logging()
        self._log_handler = QtLogHandler(self.log_signal)
        self._log_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))
        self.logger.addHandler(self._log_handler)
        self.log_signal.connect(self._append_log)
        self._cards = {}  # task_id -> TaskCard
        self._completed_tasks = set()  # 追踪新完成的任务用于通知
        self._prev_statuses = {}  # task_id -> status

        self.setWindowTitle("SwiftDM - 高速下载管理器")
        self.setMinimumSize(780, 560)
        self.resize(860, 640)

        # 暗色主题
        self.setStyleSheet(QSS)

        self._setup_log_panel()
        self._setup_toolbar()
        self._setup_central()
        self._setup_statusbar()
        self._setup_tray()

        # 定时刷新
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh)
        self._timer.start(500)

        # 初始加载
        self._refresh()

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
        self.url_input.setPlaceholderText("粘贴下载链接...")
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

        btn_clear = QPushButton("🗑 清除已完成")
        btn_clear.clicked.connect(self._clear_completed)
        toolbar.addWidget(btn_clear)

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

        header_layout.addStretch()

        self.stats_label = QLabel("下载中: 0  |  已完成: 0  |  失败: 0  |  总计: 0")
        self.stats_label.setStyleSheet("font-size: 12px; color: #8888a0;")
        header_layout.addWidget(self.stats_label)

        layout.addWidget(header)

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
        self.tray = QSystemTrayIcon(self)
        # 创建一个简单的托盘图标
        pixmap = QPixmap(32, 32)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setBrush(QBrush(QColor("#6c5ce7")))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawRoundedRect(2, 4, 28, 24, 6, 6)
        painter.setBrush(QBrush(QColor("#fff")))
        painter.drawRoundedRect(8, 12, 16, 4, 2, 2)
        painter.end()
        self.tray.setIcon(QIcon(pixmap))
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

    def _tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show()
            self.activateWindow()

    def closeEvent(self, event):
        """关闭窗口时隐藏到托盘"""
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

            if not tasks:
                self.empty_label.show()
                # 清理所有卡片
                for card in list(self._cards.values()):
                    self.task_layout.removeWidget(card)
                    card.deleteLater()
                self._cards.clear()
                self._prev_statuses.clear()
                return

            self.empty_label.hide()

            # 检测新完成的任务
            task_dict = {}
            for t in tasks:
                d = t.to_dict()
                task_dict[d["task_id"]] = d
                prev_status = self._prev_statuses.get(d["task_id"])
                if prev_status != "completed" and d["status"] == "completed":
                    if d["task_id"] not in self._completed_tasks:
                        self._completed_tasks.add(d["task_id"])
                        self._notify_complete(d)
                # 新失败的任务：状态栏直接提示失败原因
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
                    self._cards[task_id] = card
                    # 插入到布局中（在 stretch 之前）
                    self.task_layout.insertWidget(self.task_layout.count() - 1, card)

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
                import requests as req
                from downloader import manager
                # 优先用用户设置的下载目录，否则回退默认
                save_dir = data["dir"] or self.download_dir
                os.makedirs(save_dir, exist_ok=True)
                task = manager.create_task(data["url"], save_dir, data["filename"], data["segments"])
                task.start()
                self.url_input.clear()
                self.status_bar.showMessage(f"已添加: {task.filename}")
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

    def _pause_all(self):
        mgr = self._get_manager()
        for t in mgr.get_all_tasks():
            if t.status == "downloading":
                t.pause()
        self.status_bar.showMessage("已暂停全部下载")

    def _resume_all(self):
        mgr = self._get_manager()
        for t in mgr.get_all_tasks():
            if t.status == "paused":
                t.resume()
        self.status_bar.showMessage("已恢复全部下载")

    def _clear_completed(self):
        mgr = self._get_manager()
        mgr.clear_completed()
        self._completed_tasks.clear()
        self.status_bar.showMessage("已清除已完成的任务")

    def _show_settings(self):
        dlg = SettingsDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            settings = dlg.get_settings()
            # 应用下载目录（真正生效）
            if settings["dir"]:
                self.download_dir = settings["dir"]
            # 应用浏览器监控启停（真正生效）
            if self.monitor is not None:
                if settings["monitor"]:
                    self.monitor.start()
                else:
                    self.monitor.stop()
            self.monitor_label.setText(
                "  🌐 监控已启用" if settings["monitor"] else "  🌐 监控已禁用"
            )
            self.monitor_label.setStyleSheet(
                f"font-size: 11px; color: {'#00d2a0' if settings['monitor'] else '#ff5e7a'}; font-weight: 600;"
            )
            # 应用下载代理模式
            from downloader import set_proxy_mode
            set_proxy_mode(settings.get("proxy_mode", "env"))
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
        QApplication.quit()
