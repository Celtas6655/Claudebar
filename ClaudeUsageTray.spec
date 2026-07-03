# -*- mode: python ; coding: utf-8 -*-
# Canonical build definition for the main exe. build_exe.bat and the release
# workflow both run `pyinstaller ClaudeUsageTray.spec` — don't add a parallel
# command-line flag soup anywhere, keep changes here.
#
# Build ClaudeUsageTrayHook.spec FIRST: when dist/ClaudeUsageTrayHook.exe
# exists it is embedded as bundled data, and the app extracts it to
# ~/.claude/bin at startup and registers it as the hook command
# (install_hook_exe() in claude_usage_tray.py). Without it the app falls back
# to registering itself — slower per hook call, but fully functional.
import os

from PyInstaller.utils.hooks import collect_all

datas = []
binaries = []
hiddenimports = []
tmp_ret = collect_all('pystray')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('watchdog')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

if os.path.exists(os.path.join('dist', 'ClaudeUsageTrayHook.exe')):
    datas += [(os.path.join('dist', 'ClaudeUsageTrayHook.exe'), '.')]
else:
    print('WARNING: dist/ClaudeUsageTrayHook.exe not found — building WITHOUT '
          'the slim hook exe (hooks will spawn the full bundle each call). '
          'Run `pyinstaller ClaudeUsageTrayHook.spec` first.')


a = Analysis(
    ['claude_usage_tray.py'],
    pathex=[],
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
    name='ClaudeUsageTray',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX off: a classic antivirus false-positive trigger; not worth the size.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
