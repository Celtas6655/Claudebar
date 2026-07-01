---
name: Feature request
about: Suggest an idea or improvement
title: "[Feature] "
labels: enhancement
assignees: ''
---

## The problem

<!-- What are you trying to do that's awkward or impossible today? -->

## Proposed solution

<!-- What you'd like to see. -->

## Alternatives considered

<!-- Other approaches, or how you work around this now. -->

## Scope notes

<!-- Optional. A few things worth knowing before proposing big changes: -->

- The app is deliberately a **single Python file** (`claude_usage_tray.py`).
  Splitting into a package is a conscious tradeoff, not a default.
- Session/weekly **% can't be derived locally** — it's account-level server
  state exposed only through Claude Code's statusLine payload.
- Cost figures are a **static approximate table** (`PRICES_PER_MILLION`),
  not a live pricing feed.

See `ARCHITECTURE.md` for the full design history — some ideas are already
recorded there under "Possible future directions."

## Additional context

<!-- Anything else: mockups, links, examples. -->
