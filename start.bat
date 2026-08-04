@echo off
chcp 65001 >nul
title SwiftDM - 下载管理器
echo.
echo ============================================
echo   SwiftDM - 高速多线程下载管理器
echo   IDM 风格 ^| 浏览器监控 ^| 多线程分段下载
echo ============================================
echo.
echo   正在检查依赖...

cd /d "%~dp0"

:: 安装依赖
pip install -r requirements.txt -q 2>nul

echo   正在启动...
echo.

:: 启动
python main.py

pause
