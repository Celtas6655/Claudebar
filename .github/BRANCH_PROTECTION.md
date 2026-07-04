# Branch protection

`master` is a protected branch. Protection is **not** stored in this repo — it's
a GitHub setting applied to the repository. This file documents what the rules
are and how to (re)apply them, so the config isn't lost knowledge.

## What's enforced on `master`

- **No direct pushes** — all changes land via a pull request.
- **1 approving review** required before merge; stale approvals are dismissed
  when new commits are pushed.
- **CI must pass** — the `test` status check (from
  [`ci.yml`](workflows/ci.yml), which runs `python claudebar.py --test`)
  must be green, and the branch must be up to date with `master` before merging.
- **Conversations must be resolved** before merge.
- **No force pushes** (`non_fast_forward`) and **no branch deletion**.
- **Repo admins can bypass** in a pinch (e.g. an emergency hotfix). Remove the
  `bypass_actors` entry / set `enforce_admins` if you want rules to apply to
  everyone.

`develop` is intentionally left unprotected as the day-to-day working branch;
CI still runs on it.

## Applying it

### Option A — import the ruleset in the UI (no CLI)

1. Repo → **Settings → Rules → Rulesets → New ruleset → Import a ruleset**.
2. Select [`.github/rulesets/protect-master.json`](rulesets/protect-master.json).
3. Review and **Create**.

### Option B — `gh` CLI (classic branch protection)

Requires the [GitHub CLI](https://cli.github.com/) authenticated with admin
rights (`gh auth login`). This applies the equivalent classic protection:

```bash
gh api -X PUT repos/Celtas6655/Claudebar/branches/master/protection \
  -H "Accept: application/vnd.github+json" \
  --input - <<'JSON'
{
  "required_status_checks": { "strict": true, "contexts": ["test"] },
  "enforce_admins": false,
  "required_pull_request_reviews": {
    "dismiss_stale_reviews": true,
    "require_code_owner_reviews": false,
    "required_approving_review_count": 1
  },
  "restrictions": null,
  "required_linear_history": false,
  "allow_force_pushes": false,
  "allow_deletions": false,
  "required_conversation_resolution": true
}
JSON
```

### Option C — `gh` CLI (import the ruleset instead of classic protection)

```bash
gh api -X POST repos/Celtas6655/Claudebar/rulesets \
  -H "Accept: application/vnd.github+json" \
  --input .github/rulesets/protect-master.json
```

## Verifying

```bash
gh api repos/Celtas6655/Claudebar/branches/master/protection    # classic
gh api repos/Celtas6655/Claudebar/rulesets                      # rulesets
```

> **Note:** the required check is named `test`. If you rename that job in
> `ci.yml`, update the required-status-check context here and in the ruleset, or
> merges will hang waiting on a check that never reports.
