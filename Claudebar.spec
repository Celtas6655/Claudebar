# -*- mode: python ; coding: utf-8 -*-
# Canonical build definition for the main exe. build_exe.bat and the release
# workflow both run `pyinstaller Claudebar.spec` — don't add a parallel
# command-line flag soup anywhere, keep changes here.
#
# Build ClaudebarHook.spec FIRST: when dist/ClaudebarHook.exe
# exists it is embedded as bundled data, and the app extracts it to
# ~/.claude/bin at startup and registers it as the hook command
# (install_hook_exe() in claudebar.py). Without it the app falls back
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

if os.path.exists(os.path.join('dist', 'ClaudebarHook.exe')):
    datas += [(os.path.join('dist', 'ClaudebarHook.exe'), '.')]
else:
    print('WARNING: dist/ClaudebarHook.exe not found — building WITHOUT '
          'the slim hook exe (hooks will spawn the full bundle each call). '
          'Run `pyinstaller ClaudebarHook.spec` first.')


a = Analysis(
    ['claudebar.py'],
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
    name='Claudebar',
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
