@echo off
REM Build the standalone Windows exe (tray app + statusLine hook + auto-install
REM in one file). The two .spec files are the canonical build definitions and
REM are also what .github/workflows/release.yml runs -- edit the specs, not
REM command-line flags here.
REM Requires: pip install pyinstaller (and the runtime deps in requirements.txt).

python generate_icon.py || exit /b 1

REM Slim hook exe first -- ClaudeUsageTray.spec embeds it when present, so the
REM installed app can register a fast, GUI-free hook command (see
REM install_hook_exe in claude_usage_tray.py).
pyinstaller ClaudeUsageTrayHook.spec || exit /b 1
pyinstaller ClaudeUsageTray.spec || exit /b 1

echo.
echo Built dist\ClaudeUsageTray.exe (with dist\ClaudeUsageTrayHook.exe embedded)
