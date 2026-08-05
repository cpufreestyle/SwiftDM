@echo off
chcp 65001 >nul
title SwiftDM - 下载管理器
echo.
echo ============================================
echo   SwiftDM - 高速多线程下载管理器
echo   IDM 风格 ^| 浏览器监控 ^| 多线程分段下载
echo ============================================
echo.
echo   用法:
echo     start.bat          正常启动 (桌面UI + Web + 浏览器监控)
echo     start.bat --web    仅 Web 模式 (无桌面UI，适合无界面/预览环境)
echo.

cd /d "%~dp0"

:: 安装依赖
pip install -r requirements.txt -q 2>nul

echo   正在启动...
echo.

if "%1"=="--web" (
    echo   [Web 模式] 启动中...
    python main.py --web-only
) else (
    python main.py
)

pause
