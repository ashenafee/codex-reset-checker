---
name: codex-reset-checker
description: Check banked Codex reset-credit expiry times from the local Codex Desktop login.
---

# Codex Reset Checker

Use this skill when the user asks when banked Codex reset credits, rate-limit
resets, or reset-credit expirations occur.

The tool reads the local Codex Desktop auth file and calls the reset-credit
endpoint. It is intended for the same machine where Codex Desktop is signed in.

## Run

This is a quick read-only check. Do not narrate command selection, fallback
attempts, auth details, endpoint details, or raw account data. Run one command
and answer from its human-readable output.

Run from the directory containing this `SKILL.md` so the bundled script fallback
is available. Use `python3`, not `python`, for the source-script fallback.

Default command:

```bash
if command -v codex-reset-checker >/dev/null 2>&1; then
  codex-reset-checker --timezone America/Toronto
else
  python3 scripts/check_reset_expiry.py --timezone America/Toronto
fi
```

From a source checkout:

```bash
python3 scripts/check_reset_expiry.py --timezone America/Toronto
```

Only inspect files or search for the checkout if the default command fails
because `scripts/check_reset_expiry.py` is missing.

Useful options:

- `--json`: emit structured JSON.
- `--compact-json`: emit single-line JSON with `--json`.
- `--include-inactive`: include used, expired, pending, or inactive rows.
- `--input-json PATH|-`: parse a saved endpoint response or stdin.
- `--codex-home PATH`: read auth from a non-default Codex home.
- `--timezone NAME`: format local times in a named timezone.
- `--no-local-time`: omit local timezone fields.
- `--quiet`: suppress human-readable stdout.
- `--exit-code-if-none`: return `2` when no available credits remain.

## Response

Keep the final answer compact:

- First sentence: available reset-credit count.
- Then list each expiry time and remaining duration.
- End with the checked-at time if the command output includes it.
- Mention troubleshooting only when the command fails.

## Safety

- Do not print raw auth files, bearer tokens, or endpoint responses that may
  contain account-specific data.
- If login is missing, rejected, or expired, tell the user to open Codex
  Desktop and sign in again.
- If `auth.json` permissions are broad, report the warning and suggest
  `chmod 600 ~/.codex/auth.json`.
