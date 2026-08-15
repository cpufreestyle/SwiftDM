#!/usr/bin/env bash
# SwiftDM 一键启动脚本（macOS / Linux）
cd "$(dirname "$0")"

echo ""
echo "============================================"
echo "  SwiftDM - 高速多线程下载管理器"
echo "  IDM 风格 | 浏览器监控 | 多线程分段下载"
echo "============================================"
echo ""
echo "  用法:"
echo "    ./start.sh          正常启动 (桌面UI + Web + 浏览器监控)"
echo "    ./start.sh --web    仅 Web 模式 (无桌面UI，适合无界面/服务器环境)"
echo ""

# 找 Python 3
PY=""
for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)'; then
        PY="$c"
        break
    fi
done
if [ -z "$PY" ]; then
    echo "[错误] 未找到 Python 3.8+，请先安装："
    echo "  macOS:  brew install python   (或安装 Xcode Command Line Tools)"
    echo "  Linux:  sudo apt install python3 python3-pip"
    exit 1
fi

# 安装依赖（静默；失败不阻断，仅警告）
"$PY" -m pip install -r requirements.txt -q --user 2>/dev/null \
    || "$PY" -m pip install -r requirements.txt -q 2>/dev/null \
    || echo "[警告] 依赖安装失败，若启动报错请手动执行: $PY -m pip install -r requirements.txt"

echo "  正在启动..."
echo ""

if [ "$1" = "--web" ] || [ "$1" = "--web-only" ]; then
    echo "  [Web 模式] 启动中..."
    exec "$PY" main.py --web-only
else
    exec "$PY" main.py
fi
