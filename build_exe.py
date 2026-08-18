#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把 SwiftDM 打包为跨平台可执行程序（PyInstaller）。

支持平台：
    Windows  -> dist/SwiftDM.exe            （单文件，--windowed 无控制台）
    macOS    -> dist/SwiftDM.app            （应用包，--windowed）
    Linux    -> dist/SwiftDM                （单文件可执行）

用法：
    python build_exe.py                 # 按当前平台自动选择目标
    python build_exe.py --target win    # 强制 Windows 目标（需在 Windows 上跑）
    python build_exe.py --target mac    # 强制 macOS 目标
    python build_exe.py --target linux  # 强制 Linux 目标
    python build_exe.py --onefile off   # macOS/Linux 也构建目录式（--onedir）
"""
import argparse
import os
import platform
import shutil
import sys

from PyInstaller.__main__ import run

HERE = os.path.dirname(os.path.abspath(__file__))


def _detect_target():
    """根据当前平台推断打包目标。"""
    s = platform.system().lower()
    if s == "windows":
        return "win"
    if s == "darwin":
        return "mac"
    return "linux"


def _target_meta(target):
    """返回 (输出文件名, 是否单文件, 是否需要 kill 命令, app 包后缀)。"""
    if target == "win":
        return {"name": "SwiftDM.exe", "onefile": True, "kill": "SwiftDM.exe",
                "app_suffix": "", "version_file": True}
    if target == "mac":
        return {"name": "SwiftDM.app", "onefile": False, "kill": "SwiftDM",
                "app_suffix": ".app", "version_file": False}
    # linux
    return {"name": "SwiftDM", "onefile": True, "kill": "SwiftDM",
            "app_suffix": "", "version_file": False}


def build_icon():
    """生成图标（下载风格）。

    项目自带的 extension/icons/*.png 已损坏（内容为文本转义而非真实 PNG），
    因此这里用 PIL 直接绘制一个简洁的下载箭头图标。
    Windows 用 .ico；macOS 用 .icns（由 .png 转）；Linux 用 .png。
    无 PIL 或无法转换时返回 None，由 PyInstaller 使用默认图标。
    """
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        print(">>> 未安装 Pillow，跳过图标生成（使用默认图标）", file=sys.stderr)
        return None

    S = 256
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角背景
    bg = (45, 120, 245, 255)
    d.rounded_rectangle([16, 16, S - 16, S - 16], radius=48, fill=bg)

    # 白色下载箭头：竖直杆 + 三角箭头 + 底部托盘
    white = (255, 255, 255, 255)
    cx = S // 2
    # 杆
    d.rectangle([cx - 14, 70, cx + 14, 150], fill=white)
    # 箭头三角
    d.polygon([(cx, 196), (cx - 52, 132), (cx + 52, 132)], fill=white)
    # 底部托盘
    d.rounded_rectangle([cx - 64, 206, cx + 64, 226], radius=10, fill=white)

    target = _detect_target()
    if target == "win":
        out = os.path.join(HERE, "icon.ico")
        img.save(out, format="ICO",
                 sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
        return out

    if target == "mac":
        # 生成 .icns：先写多尺寸 png，再借 iconutil（macOS 自带）或直接存 png
        png = os.path.join(HERE, "icon.png")
        img.save(png, format="PNG")
        icns = os.path.join(HERE, "icon.icns")
        iconset = os.path.join(HERE, "icon.iconset")
        try:
            os.makedirs(iconset, exist_ok=True)
            for sz in (16, 32, 64, 128, 256):
                img.resize((sz, sz)).save(
                    os.path.join(iconset, f"icon_{sz}x{sz}.png"), format="PNG")
            import subprocess as _sp
            _sp.run(["iconutil", "--convert", "icns", "--output", icns, iconset],
                    stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=30, check=True)
            return icns
        except Exception:
            return png  # 无法生成 icns 时退回 png（PyInstaller 也能用）

    # linux：直接用 png
    out = os.path.join(HERE, "icon.png")
    img.save(out, format="PNG")
    return out


def _kill_process(name):
    """跨平台结束占用旧产物的进程，避免清理/覆盖失败。"""
    import subprocess as _sp
    if platform.system().lower() == "windows":
        _sp.run(["taskkill", "/F", "/IM", name],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=10)
    else:
        # 尝试用 pkill 结束进程（macOS / Linux）
        _sp.run(["pkill", "-f", name],
                stdout=_sp.DEVNULL, stderr=_sp.DEVNULL, timeout=10)


def main():
    ap = argparse.ArgumentParser(description="构建 SwiftDM 跨平台可执行程序")
    ap.add_argument("--target", default=None,
                    choices=("win", "mac", "linux"),
                    help="强制指定目标平台（默认按当前平台推断）")
    ap.add_argument("--onefile", default=None,
                    choices=("on", "off"),
                    help="是否单文件（macOS 默认 off / 目录式，其它默认 on）")
    args = ap.parse_args()

    target = args.target or _detect_target()
    meta = _target_meta(target)

    # onefile 选项：命令行 > 平台默认值
    if args.onefile == "off":
        onefile = False
    elif args.onefile == "on":
        onefile = True
    else:
        onefile = meta["onefile"]

    # 结束可能占用旧产物的进程
    _kill_process(meta["kill"])

    # 清理旧的构建产物（容错：被占用时跳过）
    for d in ("build", "dist"):
        p = os.path.join(HERE, d)
        if os.path.isdir(p):
            try:
                shutil.rmtree(p)
            except Exception as e:
                print(f">>> 清理 {d} 失败（可能被占用）：{e}", file=sys.stderr)

    icon = build_icon()

    opts = [
        os.path.join(HERE, "main.py"),
        "--name", "SwiftDM",
        "--noconfirm",
        "--clean",
        "--paths", HERE,
        # Flask 渲染模板需要 index.html
        "--add-data", os.pathsep.join([os.path.join(HERE, "templates"), "templates"]),
        # 隐藏导入：PyQt6 / Flask / 其它运行时依赖
        "--hidden-import", "PyQt6",
        "--hidden-import", "PyQt6.QtWidgets",
        "--hidden-import", "PyQt6.QtCore",
        "--hidden-import", "PyQt6.QtGui",
        "--hidden-import", "flask",
        "--hidden-import", "flask_cors",
        "--hidden-import", "jinja2",
        "--hidden-import", "markupsafe",
        "--hidden-import", "itsdangerous",
        "--hidden-import", "click",
        "--hidden-import", "werkzeug",
        "--hidden-import", "requests",
        "--hidden-import", "pyperclip",
        "--hidden-import", "pkg_resources",
    ]

    if onefile:
        opts.append("--onefile")
    else:
        opts.append("--onedir")

    # 桌面窗口程序（非控制台）。Web-only 模式也可运行，只是不弹窗口。
    if target in ("win", "mac"):
        opts.append("--windowed")
    else:
        # Linux 默认带控制台，便于看日志；如需无窗口可用 --onefile 配合
        opts.append("--windowed")

    if icon:
        opts += ["--icon", icon]

    if meta["version_file"] and os.path.isfile(os.path.join(HERE, "version_info.txt")):
        opts += ["--version-file", os.path.join(HERE, "version_info.txt")]

    print(f">>> 目标平台: {target} | 单文件: {onefile} | 产物: {meta['name']}")
    print(">>> 开始构建 SwiftDM ...")
    run(opts)

    dist_path = os.path.join(HERE, "dist", meta["name"])
    if os.path.exists(dist_path):
        if os.path.isfile(dist_path):
            size_mb = os.path.getsize(dist_path) / (1024 * 1024)
            print(f">>> 构建完成：{dist_path}  ({size_mb:.1f} MB)")
        else:
            print(f">>> 构建完成：{dist_path}")
    else:
        print(f">>> 构建失败：未找到 {dist_path}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
