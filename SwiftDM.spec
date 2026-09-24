# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_all

datas = [('D:/ai share/repo/SwiftDM/templates', 'templates')]
binaries = []
hiddenimports = ['PyQt6', 'PyQt6.QtWidgets', 'PyQt6.QtCore', 'PyQt6.QtGui', 'flask', 'flask_cors', 'jinja2', 'markupsafe', 'itsdangerous', 'click', 'werkzeug', 'requests', 'pyperclip', 'pkg_resources', 'libtorrent', 'torrent', 'yt_dlp', 'winsound', 'yt_dlp.utils', 'mutagen']
tmp_ret = collect_all('yt_dlp')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['D:/ai share/repo/SwiftDM/main.py'],
    pathex=['D:/ai share/repo/SwiftDM'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='SwiftDM',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    version='D:/ai share/repo/SwiftDM/version_info.txt',
    icon=['D:/ai share/repo/SwiftDM/icon.ico'],
)
