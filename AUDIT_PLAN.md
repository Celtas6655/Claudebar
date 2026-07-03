# Audit remediation plan — 2026-07-03

Source: full-app audit (security / performance / gaps / upgrades) performed on
2026-07-03 against `develop` @ 6eafd7e. Each item below states the decision
taken and the concrete change. Checkboxes are ticked as work lands. Items are
grouped into phases so pure-logic changes (fully testable via `--test`) land
before GUI/runtime changes and build/CI changes.

Conventions honoured throughout (per CLAUDE.md / ARCHITECTURE.md):
pure logic at module level with tests in `run_tests()`; GUI code inside
`run_app()`; all file I/O fail-soft; atomic cache writes; no cross-thread
Tkinter; no glyph-based visuals; `--test` stays GUI-free and never touches the
real `~/.claude`.

---

## Phase 1 — pure-logic correctness fixes (+ tests for each)

- [x] **1.1 "Today" uses UTC dates against a local calendar (audit #5).**
  `UsageTracker._apply` compares the JSONL UTC timestamp's date prefix with
  the *local* `date.today()`. Fix: new pure helper `record_local_date(ts)`
  (ISO-8601 `Z`-aware → local date) used by `_apply`. Also de-flakes the test
  suite, which writes UTC-now timestamps and asserts they count as "today".

- [x] **1.2 Stale rate-limit data rendered as current (audit #2).**
  New pure helper `effective_rate_limits(cache, now)` that (a) coerces
  percentages/timestamps to numbers (else `None`), and (b) nulls a window's
  pct+reset once its `resets_at` has passed (the old % is definitionally
  wrong after a reset; real numbers arrive on the next Claude turn). Used by
  the widget render, tray tooltip, and tray menu.

- [x] **1.3 Type-hostile cache values can crash renderers (audit #3, part 1).**
  `pct_tag()` returns `"dim"` for non-numeric input instead of raising.
  `process_statusline_payload()` coerces `used_percentage`/`resets_at` via the
  same numeric guard so a malformed payload can't kill the statusline hook;
  `run_statusline_hook()` additionally wraps processing so it always prints
  *something*.

- [x] **1.4 `None` counted as a session (audit: smaller items).**
  `UsageTracker._apply` only adds `session_id` to `self.sessions` when truthy.

- [x] **1.5 Per-session working state (audit #9) + SessionEnd (audit #10).**
  State cache schema gains a `"sessions"` map (`sid -> {state, updated_at}`);
  top-level `state`/`updated_at` remain as the write-time aggregate for
  back-compat. New pure helpers:
  `update_state_sessions(cache, session_id, state, now)` (prunes stale
  entries, `SessionEnd` removes the session) and
  `aggregate_working_state(cache, now)` → `(state, updated_at)` with priority
  **waiting > working > done** (red means "needs you" and must win).
  All readers go through a new `read_working_state(cache, now)` that prefers
  the sessions map and falls back to legacy top-level fields.
  `derive_working_state` maps `SessionEnd` (session removal handled by the
  writer); `STATE_HOOK_EVENTS` gains `SessionEnd`.

- [x] **1.6 Off-screen widget positions (audit #11) — pure part.**
  `clamp_position(x, y, w, h, bounds)` pure helper +
  `virtual_screen_bounds()` (ctypes `GetSystemMetrics(SM_*VIRTUALSCREEN)`,
  fail-soft `None` off-Windows).

- [x] **1.7 VERSION / `__version__` drift (audit #14).**
  `run_tests()` asserts the sibling `VERSION` file (when present) matches
  `__version__`.

- [x] **1.8 Weak malformed-JSON test assertion (audit: smaller items).**
  The `install_statusline_hook` malformed-JSON case now compares file content
  before/after (must be byte-identical) instead of the vacuous `and/or` chain.

- [x] **1.9 `--test` under `python -O` silently passes (audit: smaller items).**
  `run_tests()` aborts loudly when `__debug__` is false.

## Phase 2 — runtime / GUI fixes (inside `run_app()`)

- [x] **2.1 `_tick` exception safety (audit #3, part 2).**
  `_tick` body wrapped so the `after(1000, ...)` reschedule always runs;
  a bad cache value degrades one render, never freezes the widget.

- [x] **2.2 Single-instance guard (audit #4).**
  `acquire_single_instance_lock()` (module level, ctypes `CreateMutexW`,
  `ERROR_ALREADY_EXISTS` → `False`, non-Windows → `None`, fail-soft).
  `run_app()` exits quietly when another instance holds the mutex.

- [x] **2.3 Clamp saved/favorite positions on screen (audit #11, GUI part).**
  `_place_initial()` and `_apply_favorite_position()` clamp through
  `clamp_position()`/`virtual_screen_bounds()` before applying geometry.

- [x] **2.4 Pin must also stop re-alignment (audit #12).**
  `_tick`'s 30s `_recheck_alignment()` is skipped while `position_pinned`
  is set.

- [x] **2.5 Reduce 1s topmost churn (audit #17).**
  The per-second safety net only re-asserts when `_zorder_vs_taskbar()`
  reports `"below"` (the WinEvent hook remains the primary mechanism).
  Off-Windows / `"unknown"` keeps the old always-reassert behaviour.

- [x] **2.6 Don't hold `state_lock` across the full-tree walk (audit #16).**
  `UsageTracker.poll(files=None)` accepts a pre-scanned file list;
  `fallback_sweep_loop` calls `find_session_files()` *outside* the lock.

- [x] **2.7 Exact cache-file matching in the watcher (audit: smaller items).**
  `SessionFileHandler._handle` compares `normcase(abspath(...))` against the
  real cache paths instead of basenames anywhere in the tree.

## Phase 3 — hook installation & hook performance (audit #1, #10)

- [x] **3.1 Drop `PostToolUse` from registered events.**
  `PreToolUse` alone keeps "working" fresh (each tool start refreshes it;
  `Stop` ends the turn) — this halves per-tool hook spawns. The
  `derive_working_state` mapping keeps `PostToolUse -> working` so
  already-installed entries still behave.

- [x] **3.2 Prune our own stale event registrations.**
  `install_state_hooks()` now also removes *our exact command* from events we
  no longer register (`REMOVED_STATE_HOOK_EVENTS = ("PostToolUse",)`).
  User-owned hooks are never touched.

- [x] **3.3 Recognise our own older command shapes as upgradeable.**
  Any existing `statusLine.command` ending in `--statusline-hook` is treated
  as "ours" and may be replaced by the auto (force=False) path; same for
  `--state-hook` entries in the hooks array (replaced, not duplicated, when
  the registered command differs from the current resolve). A genuinely
  foreign statusLine (no our-flag suffix) is still never clobbered.

- [x] **3.4 Slim companion hook exe to cut per-event latency.**
  The dominant hook cost for frozen installs is PyInstaller onefile
  self-extraction of the *full* bundle (Pillow/pystray/watchdog/tkinter) on
  every statusline render and every tool call. Fix: build a second, slim,
  windowed exe `ClaudeUsageTrayHook.exe` (same script, GUI packages
  excluded), embed it into the main exe as bundled data, extract it to
  `~/.claude/bin/` at startup (atomic, hash-compared, fail-soft), and
  register *it* as the statusLine/state-hook command. Fallback at every step
  is the old behaviour (register the main exe). Single-file download UX is
  preserved.
  - `ClaudeUsageTrayHook.spec` (new), `ClaudeUsageTray.spec` bundles
    `dist/ClaudeUsageTrayHook.exe` when present.
  - `_resolve_hook_command()` / `_resolve_state_hook_command()` prefer the
    extracted hook exe when running frozen.
  - CI smoke-tests the slim exe's hook path too.

## Phase 4 — build, CI, supply chain

- [x] **4.1 Pin dependencies (audit #6).** `requirements.txt` pinned to the
  exact versions verified on real hardware (pystray 0.19.5, Pillow 12.2.0,
  watchdog 6.0.0); release workflow pins pyinstaller 6.21.0.

- [x] **4.2 One canonical build definition (audit #13).** The two `.spec`
  files are canonical; `release.yml` and `build_exe.bat` both run
  `pyinstaller <spec>` instead of ad-hoc flag soup.

- [x] **4.3 UPX off (audit #7).** `upx=False` in both specs (AV
  false-positive trigger; was only ambiently applied anyway).

- [x] **4.4 Release checksums (audit #7).** `release.yml` publishes
  `SHA256SUMS.txt` alongside the exes. (Authenticode signing noted as a
  future paid option, not done here.)

- [x] **4.5 Windows leg in PR CI (audit: smaller items).** `ci.yml` runs the
  suite on `ubuntu-latest` **and** `windows-latest` (the winreg round-trip
  only exercises on Windows).

- [x] **4.6 Ruff lint (audit: smaller items).** `ruff check` added to CI;
  codebase brought clean under default rules.

## Phase 5 — docs, version, deliberate non-fixes

- [x] **5.1 Docs updated** for everything above: ARCHITECTURE.md §2c (state
  cache schema, event set, aggregation priority), §8 (new
  `~/.claude/bin/ClaudeUsageTrayHook.exe` row), §9 (hook-latency
  characteristics, settings.json read-modify-write race note, startup
  full-rescan cost), README (checksums, hook exe, staleness behaviour),
  CLAUDE.md where its summaries changed.

- [x] **5.2 Version bump** to 1.2.0 (`__version__` + `VERSION`) — behaviour
  changes (hook events, cache schema, staleness display) warrant a minor
  bump. Tray menu gains a disabled `ClaudeUsageTray v1.2.0` line so users can
  tell which build they run (audit #14).

- [x] **5.3 Priced-as-of marker (audit: smaller items).** `PRICES_AS_OF`
  constant; shown in the tray menu's cost lines context and README.

- [x] **5.4 Help-text note about manual hook runs blocking on stdin**
  (audit: smaller items) — added to the `--statusline-hook`/`--state-hook`
  argparse help.

### Explicitly documented, not coded (with rationale)

- **Startup full-history rescan / unbounded `seen_uuids` (audit #15):**
  persisting aggregates+offsets is a real feature with real corruption/
  invalidation edge cases; current cost is acceptable on this hardware.
  Documented in ARCHITECTURE §9 as the known scaling limit and the shape of
  the fix. Revisit if startup becomes slow.
- **settings.json concurrent-write race (audit #8):** probability is low
  (writes happen only when something changed) and a robust fix needs file
  locking across two independent programs. Documented in ARCHITECTURE §9.
- **Authenticode signing (audit #7):** requires a paid certificate —
  checksums shipped instead; signing listed in ARCHITECTURE §12.

## Verification gate (all must pass before done)

- [x] `python claude_usage_tray.py --test` — all tests green on real Windows.
- [x] `ruff check .` clean.
- [x] Local PyInstaller build of both exes; `cmd`-redirection smoke test of
  `--statusline-hook` on the slim exe (per ARCHITECTURE §9: never a
  PowerShell pipe).
- [x] Hook-exe extraction + settings upgrade path exercised via `--test`
  (pure parts) and a manual run gated on fail-soft behaviour.
