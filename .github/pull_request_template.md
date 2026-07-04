<!-- Thanks for contributing! Please fill this in so review is quick. -->

## What this changes

<!-- A short summary of the change and why. -->

## Related issue

<!-- e.g. "Closes #12". Optional. -->

## Type of change

- [ ] Bug fix
- [ ] New feature
- [ ] Refactor / cleanup (no behavior change)
- [ ] Docs
- [ ] Build / CI

## How it was tested

- [ ] `python claudebar.py --test` passes
- [ ] Ran the tray app manually
- [ ] Tested the hook with `cmd` redirection (`exe < in.txt > out.txt`),
      **not** a PowerShell pipe (which doesn't capture a GUI-subsystem exe's stdout)
- [ ] Verified on real Windows hardware
- [ ] Not applicable

<!-- Be direct about what's verified vs. not. Several bugs in this project were
     found on real Windows hardware the dev environment couldn't reproduce —
     if you couldn't test something, say so rather than implying you did. -->

## Checklist

- [ ] `--test` still runs with **zero GUI dependencies** (no `pystray`/`Pillow`/`tkinter`
      imports at module level; tests never touch the real `~/.claude`)
- [ ] Pure/testable logic stayed at module level; GUI-only code stayed inside `run_app()`
- [ ] File I/O fails soft; any new cache write is atomic (temp file + `os.replace`)
- [ ] Read the relevant section of `ARCHITECTURE.md` before any architectural change
- [ ] Updated `CLAUDE.md` / `ARCHITECTURE.md` / `README.md` if behavior or design changed

## Screenshots

<!-- If this touches the widget or tray UI, include before/after. -->
