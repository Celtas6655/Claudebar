# Security Policy

## Supported versions

Only the latest release is supported. Since this is a small, actively
developed project, please update to the newest `.exe` (or `git pull`) before
reporting an issue, in case it's already fixed.

## Reporting a vulnerability

Please **do not open a public GitHub issue** for a suspected security
vulnerability. Instead, email **michangielski@gmail.com** with:

- A description of the issue and its potential impact.
- Steps to reproduce, if possible.
- The version (see `VERSION`, or the release tag) you tested against.

You should get an acknowledgment within a few days. There's no bug bounty —
this is a small hobby-scale project — but reports are taken seriously and a
fix or mitigation will be prioritized.

## Scope and what this app touches

Useful context for anyone evaluating a report:

- Claudebar has **no network access of its own** — it never uploads
  anything. Its only outbound-shaped behavior is the standalone `.exe`
  download itself (see verification below).
- It reads Claude Code's local session logs (`~/.claude/projects/**/*.jsonl`)
  and writes small local cache files, all under `~/.claude/`, plus one
  registry value (`HKCU\...\Run\Claudebar`) if the user enables
  "Run on Windows startup." See `ARCHITECTURE.md` §8 for the full file/data
  inventory.
- It edits `~/.claude/settings.json` to register a `statusLine` command and
  `hooks` entries, but only ever **appends** its own entries — it never
  removes or rewrites a user's existing configuration there (see
  `ARCHITECTURE.md` §2b/§2c for exact behavior).
- The released `.exe` is **not code-signed** (no paid Authenticode
  certificate). Each release ships a `SHA256SUMS.txt` — verify a download
  against it (`Get-FileHash Claudebar.exe` in PowerShell) if you want
  integrity assurance beyond GitHub's own release hosting.

Reports about any of the above — e.g. a way the hook/settings install could
be tricked into running unintended commands, or a way the cache files could
be abused — are exactly what this policy is for.
