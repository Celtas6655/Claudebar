---
name: Bug report
about: Report something that's broken or behaving unexpectedly
title: "[Bug] "
labels: bug
assignees: ''
---

## What happened

<!-- A clear description of the bug. -->

## What you expected

<!-- What you thought should happen instead. -->

## Steps to reproduce

1.
2.
3.

## Which part is affected

<!-- Check all that apply. -->

- [ ] Tray icon / menu
- [ ] Floating widget (layout, position, clipping, drag)
- [ ] Token counts / cost figures
- [ ] Session (5h) / weekly (7d) rate-limit %
- [ ] `--statusline-hook` (cache not updating, wrong status line)
- [ ] "Run on Windows startup"
- [ ] Something else

## Rate-limit % specifically

<!-- Only if the 5h/7d percentages are the problem. These come from Claude Code's
     statusLine hook, not local logs — there is no local fallback. -->

- Is `statusLine.command` configured in `~/.claude/settings.json`? [ ] yes / [ ] no / [ ] not sure
- Does this print `test | 5h 50%`?
  ```
  echo '{"model":{"display_name":"test"},"rate_limits":{"five_hour":{"used_percentage":50,"resets_at":1782500000}}}' | python claude_usage_tray.py --statusline-hook
  ```
  Output:
- Have you sent at least **two** full Claude Code turns since starting/clearing the session?
  (`rate_limits` is empty on the very first render — see the README/ARCHITECTURE.)

## Environment

- App version / commit:
- Running as: [ ] `python claude_usage_tray.py` / [ ] bundled `.exe`
- Windows version:
- Python version (`python --version`):
- Claude Code version:

## Logs / screenshots

<!-- Paste any error output, or drag in a screenshot of the widget/tray. -->
