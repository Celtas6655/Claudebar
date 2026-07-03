# -*- mode: python ; coding: utf-8 -*-
# Slim companion hook exe: the same claude_usage_tray.py, but with every GUI
# package excluded. Claude Code spawns the hook command on EVERY statusline
# render and EVERY registered lifecycle event (PreToolUse is blocking), and a
# --onefile build self-extracts its whole bundle on each launch — so the hook
# must not carry Pillow/pystray/watchdog/tkinter, only the stdlib-only hook
# code paths. Built FIRST; ClaudeUsageTray.spec then embeds the result so the
# release stays a single downloadable exe (see install_hook_exe()).

a = Analysis(
    ['claude_usage_tray.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', '_tkinter', 'tkinter.font',
        'PIL',
        'pystray',
        'watchdog',
    ],
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
    name='ClaudeUsageTrayHook',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # UPX off: a classic antivirus false-positive trigger, and the size win
    # doesn't matter for a helper that lives in ~/.claude/bin.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    # Windowed subsystem, same as the main exe: Claude Code redirects fds 0/1
    # via pipes, and the hook path reads/writes raw fds (_read_hook_stdin) —
    # a console build would flash a window on every hook invocation.
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['icon.ico'],
)
