# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\michael\\CodeBuddy\\20260803155239\\download_manager\\main.py'],
    pathex=['C:\\Users\\michael\\CodeBuddy\\20260803155239\\download_manager'],
    binaries=[],
    datas=[('C:\\Users\\michael\\CodeBuddy\\20260803155239\\download_manager\\templates', 'templates')],
    hiddenimports=['PyQt6', 'PyQt6.QtWidgets', 'PyQt6.QtCore', 'PyQt6.QtGui', 'flask', 'flask_cors', 'jinja2', 'markupsafe', 'itsdangerous', 'click', 'werkzeug', 'requests', 'pyperclip', 'pkg_resources'],
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
    version='C:\\Users\\michael\\CodeBuddy\\20260803155239\\download_manager\\version_info.txt',
    icon=['C:\\Users\\michael\\CodeBuddy\\20260803155239\\download_manager\\icon.ico'],
)
