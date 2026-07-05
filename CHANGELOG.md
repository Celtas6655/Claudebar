# Changelog

All notable changes to this project are documented in this file. Format is
based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); this
project does not currently follow strict semantic versioning guarantees
(pre-1.0 conventions may apply to breaking changes).

## [Unreleased]

## [1.7.0] - 2026-07-05

### Fixed

- Release workflow's automatic version-bump push now authenticates with an
  admin-owned PAT (`RELEASE_PUSH_TOKEN`) so it satisfies `master`'s ruleset
  bypass rule — the default `GITHUB_TOKEN` push wasn't covered by that
  bypass and could be rejected.

### Added

- GitHub social preview image.

### Changed

- Centered status badges in the README; busted a stale cached license badge
  image.

## [1.6.0] - 2026-07-05

### Fixed

- The working-state indicator no longer treats every `Notification` hook
  subtype as "waiting for input." Only genuine input-needed subtypes
  (`permission_prompt`, `agent_needs_input`, `elicitation_dialog`) turn the
  dot red — `idle_prompt` (which also fires after a normal `Stop`) and
  others no longer incorrectly flip a finished turn back to red.

## [1.5.0] - 2026-07-05

### Changed

- Centered README screenshots.

## [1.4.0] - 2026-07-05

### Changed

- Updated the floating-widget screenshot in the README.

## [1.3.0] - 2026-07-05

### Added

- GitHub Pages landing page (`docs/index.html`), set as the repo homepage.

### Changed

- Bumped `actions/checkout` to v7 and `actions/setup-python` to v6 in CI
  workflows.
- Bumped Pillow from 12.2.0 to 12.3.0.

## [1.2.0] - 2026-07-04

Initial public release.

### Added

- Tray icon + floating widget showing live token usage, estimated cost, and
  today/all-time totals read directly from Claude Code's local session logs.
- Session (5h) and weekly (7d) rate-limit percentages with reset times, via a
  `--statusline-hook` entry point that auto-installs into Claude Code's
  `statusLine` config.
- Red/Amber/Green working-state indicator (widget dot + tray pip) via a
  `--state-hook` entry point wired to Claude Code's `hooks` events
  (`UserPromptSubmit`, `PreToolUse`, `Stop`, `Notification`, `SessionEnd`),
  with per-session tracking so concurrent sessions don't clobber each other's
  status.
- Notify-on-waiting Windows toast when Claude needs input, toggleable from
  the tray menu.
- Drag-to-reposition the floating widget, with save/load of a single
  favorite position.
- Run-on-Windows-startup toggle (registry-based, no admin rights required).
- Standalone single-file `.exe` distribution that self-installs its own
  hooks, built and published automatically via GitHub Releases with
  SHA256 checksums.

### Fixed

- Fatal crash on tray-menu click caused by a WinEvent callback re-entering
  Tk/Tcl from an unsafe point in the message pump.
- Text clipping and a stray blank row in the floating widget, caused by
  assuming uniform Unicode glyph widths across fonts — bars are now drawn on
  a `Canvas` instead of built from block characters.
- Filesystem watcher missing atomic cache writes on Windows, where an
  `os.replace` surfaces as a "moved" event rather than modified/created.

[Unreleased]: https://github.com/Celtas6655/Claudebar/compare/v1.7.0...HEAD
[1.7.0]: https://github.com/Celtas6655/Claudebar/compare/v1.6.0...v1.7.0
[1.6.0]: https://github.com/Celtas6655/Claudebar/compare/v1.5.0...v1.6.0
[1.5.0]: https://github.com/Celtas6655/Claudebar/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/Celtas6655/Claudebar/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/Celtas6655/Claudebar/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/Celtas6655/Claudebar/releases/tag/v1.2.0
