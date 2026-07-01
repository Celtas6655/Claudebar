@echo off
REM Build the standalone Windows exe (tray app + statusLine hook + auto-install
REM in one file). Mirrors the GitHub Actions build in .github/workflows/release.yml.
REM Requires: pip install pyinstaller (and the runtime deps in requirements.txt).

python generate_icon.py || exit /b 1
pyinstaller --onefile --noconsole --icon=icon.ico --name ClaudeUsageTray ^
    --collect-all pystray --collect-all watchdog claude_usage_tray.py || exit /b 1

echo.
echo Built dist\ClaudeUsageTray.exe
