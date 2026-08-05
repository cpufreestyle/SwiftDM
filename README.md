# SwiftDM

> IDM 风格的高速多线程下载管理器 —— PyQt6 桌面 UI + 浏览器监控 + 多线程分段下载

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

SwiftDM 是一款仿 Internet Download Manager 的下载工具，支持多线程分段下载、断点续传、浏览器下载自动捕获，并提供了现代化的原生桌面界面。

## ✨ 功能特性

- 🚀 **多线程分段下载**：默认 8 线程并行下载，可自定义 1-32 线程
- ⏸ **断点续传**：随时暂停 / 继续，恢复时自动从断点续下
- 🌐 **浏览器监控**：Chrome 扩展自动捕获浏览器中的下载
- 📋 **剪贴板监听**：复制下载链接自动检测并提示
- 🖥 **原生桌面 UI**：基于 PyQt6 的暗色主题界面，IDM 风格任务卡片
- 📊 **实时进度**：速度、ETA 倒计时、进度百分比实时显示
- 🔔 **完成通知**：系统托盘气泡 + 状态栏通知
- 🗂 **批量管理**：全部暂停 / 恢复、清除已完成任务

## 📦 安装

```bash
# 克隆仓库
git clone https://github.com/cpufreestyle/SwiftDM.git
cd SwiftDM

# 安装依赖
pip install -r requirements.txt

# 启动（Windows 双击也可）
python main.py
```

或直接使用 `start.bat` 一键启动。

## 🚀 使用方法

1. 运行 `python main.py`（或双击 `start.bat`）
2. 在顶部输入框粘贴下载链接，点击「＋ 下载」或回车
3. 在弹出的对话框中可自定义文件名、保存目录、线程数
4. 下载过程中可随时暂停 / 继续 / 取消
5. 关闭窗口会最小化到系统托盘，下载任务继续运行

## 🌐 浏览器扩展安装

SwiftDM 可监控浏览器下载，需安装 Chrome 扩展：

1. 打开 `chrome://extensions/`
2. 开启右上角「开发者模式」
3. 点击「加载已解压的扩展程序」
4. 选择 `extension/` 目录
5. 扩展安装后，浏览器中点击下载会自动发送到 SwiftDM

扩展支持：
- `chrome.downloads` 监听浏览器下载事件
- `webRequest` 拦截响应头，检测文件下载（Content-Disposition / Content-Type / 扩展名）
- 弹出面板可暂停监控、查看已捕获次数

## 📁 项目结构

```
SwiftDM/
├── main.py              # 入口（启动监控 + 桌面 UI）
├── main_window.py       # PyQt6 桌面 UI 主窗口
├── downloader.py        # 下载引擎（多线程分段下载、暂停/恢复）
├── browser_monitor.py   # 浏览器监控（剪贴板 + HTTP 端点）
├── app.py               # Flask 后端 API（含浏览器捕获端点）
├── templates/
│   └── index.html       # Web UI（备用）
├── extension/           # Chrome 浏览器扩展
│   ├── manifest.json
│   ├── background.js
│   ├── popup.html / .js
│   └── icons/
├── requirements.txt
└── start.bat
```

## ⚙️ 工作原理

下载引擎把文件按大小切分为 N 段，每段开启独立线程通过 HTTP `Range` 请求并行下载，
写入各自的临时分片文件，全部完成后合并为最终文件。暂停时停止所有线程，恢复时
从断点处继续写入对应分片。

浏览器监控通过本地 HTTP 端点（端口 5001）接收 Chrome 扩展捕获的下载链接，
并在剪贴板出现下载 URL 时自动检测。

## 📄 许可证

MIT License

## 🔧 故障排查

### 浏览器打开 `http://127.0.0.1:5000` 显示 `HTTP 502`

**根因**：系统设置了 `HTTP_PROXY`（如 `127.0.0.1:7897`），浏览器把 `127.0.0.1` 也送进代理，
而代理连不上本地服务，于是返回 502。后端本身是正常的（直连端口是通的）。

**自动规避（已内置）**：`main.py` 启动时会：
1. 把 `127.0.0.1 / localhost` 加入 `NO_PROXY`，避免进程内请求被代理拦截；
2. 自动用一个**独立临时用户目录 + `--no-proxy-server`** 的 Chrome/Edge 实例打开界面，
   该实例完全不走系统代理，直连本地服务。

**手动规避**（若你用自己的浏览器访问）：
- 在浏览器设置的「代理」里，把 `127.0.0.1` 和 `localhost` 加入「代理绕过列表」；
- 或访问前临时关闭系统代理；
- 或使用纯 Web 模式：`start.bat --web`，然后用上述任一方式打开 `http://127.0.0.1:5000`。

> 注意：下载引擎在 Python 后端运行，仍会走 `HTTP_PROXY` 访问外网下载，因此禁用浏览器
> 代理不会影响实际下载功能。

### 无界面 / 预览环境运行

使用纯 Web 模式（不启动 PyQt6 桌面 UI）：

```bash
python main.py --web-only      # 或 start.bat --web
```

启动后监听 `0.0.0.0:5000`（含局域网 IP），方便预览代理 / 远程访问。

