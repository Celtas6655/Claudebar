# Contributing to Claudebar

Thanks for your interest in improving this project. It's a small, single-file
Windows tray app, and it's intentionally kept simple — these notes exist so a
contribution lands cleanly the first time.

Please also read [`ARCHITECTURE.md`](ARCHITECTURE.md) before any non-trivial
change. It captures *why* the code is shaped the way it is — the two data
sources, the threading model, two real bug postmortems, and what's verified vs.
not. A lot of the current design is the result of real debugging, not a clean
first draft, and it's easy to reintroduce a fixed bug by "simplifying."

## Getting set up

- **Windows** for the full app (the tray icon, floating-widget placement, and
  startup toggle are Windows-specific). The test suite and the statusLine hook
  run anywhere.
- **Python 3.10+**
- Install dependencies:

  ```bash
  pip install -r requirements.txt
  ```

- Run the app: `python claudebar.py`
- Run the tests: `python claudebar.py --test`

## Before you open a pull request

1. **`python claudebar.py --test` must pass** — and it must stay
   GUI-free. The suite imports no `pystray`/`Pillow`/`tkinter` at module level
   and never touches your real `~/.claude` directory. Don't break either
   property.
2. **Keep code on the right side of the line.** Pure, testable logic lives at
   **module level**; GUI-only code lives as closures **inside `run_app()`**.
   That split is exactly what lets `--test` run without a display.
3. **Match the surrounding style.** Small, focused changes; no new dependencies
   unless there's a clear reason.
4. **Update the docs** (`README.md`, `CLAUDE.md`, `ARCHITECTURE.md`) if you
   change behavior or design.

## Non-negotiable constraints (regressions we've paid for before)

These are the ones most likely to bite. Each maps to a real bug or a real
requirement — see `ARCHITECTURE.md` for the full stories.

- **The two data sources are unrelated.** Token/cost data comes from local JSONL
  logs; session/weekly % comes from a cache written by the `--statusline-hook`.
  Never assume one can substitute for the other, and never try to derive
  rate-limit % from local token counts — it's account-level server state.
- **The single `.exe` handles every mode, including the hook.** The hook path
  reads/writes raw file descriptors (`_read_hook_stdin`/`_write_hook_stdout`)
  because a windowed PyInstaller build has `sys.stdin`/`sys.stdout == None`.
  Don't "simplify" it back to `json.load(sys.stdin)`/`print()`. Test the hook
  with **`cmd` redirection** (`exe < in.txt > out.txt`), never a PowerShell pipe.
- **No cross-thread Tkinter calls.** Use a `threading.Event` set elsewhere and
  polled from inside the Tk thread's own `after()` loop.
- **Don't build precise visuals out of Unicode characters** — glyph widths
  aren't uniform across fonts/platforms (this caused a real clipping bug). Draw
  them with a `Canvas`.
- **Render content into a Tk window before sizing it** from `winfo_reqwidth()` /
  `winfo_reqheight()`.
- **File I/O fails soft** (`try/except OSError`, return `None`/no-op). **Cache
  writes are atomic** (temp file + `os.replace`).

## Be honest about verification

This project has a strong norm: **be direct about what's verified vs. not.**
Several bugs and limitations were found by a real user on real Windows hardware
that the dev environment (Linux, no display) couldn't reproduce. If you couldn't
test something — say so in the PR rather than implying you did. That's not a
weakness; it's how this project avoids shipping false confidence.

## Reporting bugs & requesting features

Use the issue templates:

- **Bug report** — includes a rate-limit troubleshooting checklist, since "5h/7d
  % is blank" is almost always a statusLine-hook setup issue, not a code bug.
- **Feature request** — flags the deliberate design constraints so proposals
  start informed.

For usage questions or setup help, open a
[Discussion](https://github.com/Celtas6655/claudebar-usage/discussions) instead.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating, you're expected to uphold it.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
