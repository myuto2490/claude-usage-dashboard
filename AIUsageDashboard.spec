# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

# 独立ウィンドウ表示に使う pywebview (Windows では pythonnet/WebView2 経由) を同梱する。
datas, binaries, hiddenimports = [], [], []
for _pkg in ("webview", "clr_loader", "pythonnet"):
    try:
        _d, _b, _h = collect_all(_pkg)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception:
        pass  # 未インストールでもビルドは通す (実行時はブラウザにフォールバック)

a = Analysis(
    ['ai_usage_dashboard.py'],
    pathex=[],
    binaries=binaries,
    datas=datas + [('app_icon.ico', '.')],
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
    name='AI使用状況ダッシュボード',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # コンソール窓を出さず、アプリウィンドウのみ表示する
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app_icon.ico',
)
