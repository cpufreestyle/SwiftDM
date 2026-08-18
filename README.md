# SwiftDM

> IDM 风格的高速多线程下载管理器 —— PyQt6 桌面 UI + 浏览器监控 + 多线程分段下载

![Python](https://img.shields.io/badge/Python-3.9%2B-blue)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)
![License](https://img.shields.io/badge/license-MIT-green)

SwiftDM 是一款仿 Internet Download Manager 的下载工具，支持多线程分段下载、断点续传、浏览器下载自动捕获，并提供了现代化的原生桌面界面。支持 **Windows / macOS / Linux**。

## ✨ 功能特性

- 🚀 **多线程分段下载**：默认 8 线程并行下载，可自定义 1-32 线程（服务器不支持 Range 时自动降级为单线程，避免文件损坏）
- ⏸ **断点续传**：随时暂停 / 继续，恢复时自动从断点续下；分段下载中途断流自动重试
- ✅ **完整性校验**：合并前校验每个分段与最终文件大小，杜绝静默截断
- 🌐 **浏览器监控**：Chrome 扩展自动捕获浏览器中的下载
- 📋 **剪贴板监听**：复制下载链接自动检测并提示
- 🖥 **原生桌面 UI**：基于 PyQt6 的暗色主题界面，IDM 风格任务卡片
- 📊 **实时进度**：速度、ETA 倒计时、进度百分比实时显示
- 🔔 **完成通知**：系统托盘气泡 + 状态栏通知
- 🗂 **批量管理**：全部暂停 / 恢复、清除已完成任务
- 🍎 **全平台支持**：Windows / macOS（Intel & Apple Silicon）/ Linux

## 📦 安装

### Windows

```bash
git clone https://github.com/cpufreestyle/SwiftDM.git
cd SwiftDM
pip install -r requirements.txt
python main.py
```

或直接双击 `start.bat` 一键启动。

### macOS

```bash
git clone https://github.com/cpufreestyle/SwiftDM.git
cd SwiftDM

# macOS 自带 Python3（终端首次运行会提示安装 Xcode Command Line Tools）
# 若无 Homebrew Python 也可：brew install python

pip3 install -r requirements.txt
python3 main.py
```

或直接运行 `./start.sh` 一键启动。

> macOS 说明：
> - 托盘图标位于屏幕顶部**菜单栏右侧**（等效于 Windows 系统托盘），点击右键可显示主窗口/退出；
> - 关闭窗口同样会最小化到菜单栏，下载任务继续运行；
> - 「打开文件」按钮调用系统 `open` 命令（Finder 打开）。

### Linux

```bash
sudo apt install python3 python3-pip   # 或对应发行版包管理器
pip3 install -r requirements.txt
./start.sh
```

> Linux 无界面/服务器环境可使用纯 Web 模式：`./start.sh --web`

## 🚀 使用方法

1. 运行 `python main.py`（Windows 双击 `start.bat`，macOS/Linux 运行 `./start.sh`）
2. 在顶部输入框粘贴下载链接，点击「＋ 下载」或回车
3. 在弹出的对话框中可自定义文件名、保存目录、线程数
4. 下载过程中可随时暂停 / 继续 / 取消
5. 关闭窗口会最小化到系统托盘（macOS 为菜单栏），下载任务继续运行

## 🌐 浏览器扩展安装

SwiftDM 可监控浏览器下载，需安装 Chrome 扩展（Windows / macOS / Linux 通用）：

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
├── start.bat            # Windows 一键启动
└── start.sh             # macOS / Linux 一键启动
```

## ⚙️ 工作原理

下载引擎先用一次 `GET + Range: bytes=0-0` 探测（比 HEAD 兼容性更好）：同时拿到
文件大小、最终文件名、以及服务器是否支持 Range 分段。

- **支持 Range**：文件按大小切分为 N 段，每段独立线程通过 HTTP `Range` 请求并行下载，
  写入各自的临时分片文件，全部完成后合并并校验最终大小；
- **不支持 Range / 未知大小**：自动降级为单线程整文件下载，避免多段并发各自拿到
  整个文件导致合并后损坏。

暂停/取消通过「代数计数」让所有分段线程立即退出（连接随即释放），恢复时从各分段
已有字节断点续传；分段线程内置 3 次自动重试，覆盖连接中途断开导致的分片不完整。

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
- 或使用纯 Web 模式：`start.bat --web`（macOS/Linux：`./start.sh --web`），然后用上述任一方式打开 `http://127.0.0.1:5000`。

> 注意：下载引擎在 Python 后端运行，仍会走 `HTTP_PROXY` 访问外网下载，因此禁用浏览器
> 代理不会影响实际下载功能。

### macOS 启动时提示端口 5000 被占用

macOS 的 **AirPlay 接收**功能（控制中心）默认监听 TCP 5000 端口，与 SwiftDM 默认端口冲突。

**自动规避（已内置）**：`main.py` 启动时会探测端口，若 5000 被占用会自动改用后续可用端口
（如 5050），并在控制台打印提示，无需手动处理。

**手动释放 5000 端口**：系统设置 → 通用 → 隔空投送与接力 → 关闭「AirPlay 接收」。

### 无界面 / 预览环境运行

使用纯 Web 模式（不启动 PyQt6 桌面 UI）：

```bash
python main.py --web-only      # Windows: start.bat --web  |  macOS/Linux: ./start.sh --web
```

启动后监听 `0.0.0.0:5000`（含局域网 IP），方便预览代理 / 远程访问。

## 📦 构建与发布（跨平台二进制）

项目提供构建脚本，**跨 Windows / macOS / Linux** 打包，无需用户安装 Python 即可运行，
并自动上传到 GitHub Release 作为二进制资源。

```bash
# 1) 构建当前平台的二进制（自动按系统推断目标）
python build_exe.py
#   Windows  -> dist/SwiftDM.exe         （单文件）
#   macOS    -> dist/SwiftDM.app         （应用包，建议 onedir）
#   Linux    -> dist/SwiftDM             （单文件 ELF 可执行）

# 也可强制指定目标平台（需在该平台对应的系统上运行）
python build_exe.py --target win
python build_exe.py --target mac
python build_exe.py --target linux

# 2) 发布到 GitHub Release（创建/复用 v0.1.0，并上传对应平台产物）
python release.py                 # 上传当前平台产物
python release.py --target win    # 上传 Windows 产物 (SwiftDM-v0.1.0-windows.exe)
python release.py --target mac    # 上传 macOS 产物 (SwiftDM-v0.1.0-macos.zip)
python release.py --target linux  # 上传 Linux 产物 (SwiftDM-v0.1.0-linux)
```

说明：
- `build_exe.py`：用 PyInstaller 打包，内嵌 `templates/index.html` 与图标，
  Windows 产出 `dist/SwiftDM.exe`（窗口模式，无控制台黑框），macOS 产出 `SwiftDM.app`，
  Linux 产出 `SwiftDM`。图标按平台生成（`.ico` / `.icns` / `.png`），清理/结束进程命令也跨平台适配。
- `release.py`：从 `~/.git-credentials` 读取 GitHub token，幂等地创建/复用 Release 并上传
  对应平台的二进制资源（同名资源会先删除再上传）。
- 启动脚本：`start.bat`（Windows）、`start.sh`（macOS/Linux，需 `chmod +x start.sh`）。
- 构建/发布产物（`build/`、`dist/`、`icon.*` 等）已写入 `.gitignore`。

