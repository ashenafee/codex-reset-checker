# Codex Reset Checker

Codex Reset Checker is a small command-line tool for seeing when banked Codex
reset credits expire. Codex can bank reset credits, but it does not currently
surface the expiry timestamps directly; this CLI prints them in a readable form
and can also emit JSON for scripts.

The tool uses the same local auth file that Codex writes. It does not redeem
credits, mutate account state, store tokens, or print bearer tokens.

## Status

This is an unofficial project and is not affiliated with or supported by
OpenAI. It calls an internal, undocumented ChatGPT/Codex endpoint:

```text
GET https://chatgpt.com/backend-api/wham/rate-limit-reset-credits
```

That endpoint is not a public API. If the endpoint changes, the tool may need
to change with it.

## Requirements

You need Python 3.12 or newer, Codex signed in on the same machine, and a
readable Codex auth file at `$CODEX_HOME/auth.json` or `~/.codex/auth.json`.
The package has no required third-party runtime dependencies on macOS or Linux.
On Windows, it installs `tzdata` so named timezones work reliably.

## Installation

From a local clone:

```bash
uv tool install .
```

or:

```bash
pipx install .
```

For development, install the project environment with:

```bash
uv sync
```

Then run the CLI through uv:

```bash
uv run codex-reset-checker
```

You can also run the source checkout directly:

```bash
python scripts/check_reset_expiry.py
```

## Usage

Run `codex-reset-checker` with no arguments to show available reset credits:

```bash
codex-reset-checker
```

The default output is human-readable and includes the check time, local
timezone, available count, expiry timestamps, and time remaining for each
available credit.

Use `--json` when you want structured output:

```bash
codex-reset-checker --json
codex-reset-checker --json --compact-json
```

If your Codex auth file lives outside the default location, pass a Codex home:

```bash
codex-reset-checker --codex-home ~/.codex-alt
```

By default, the CLI only shows available credits. To include used, expired,
pending, or otherwise inactive rows returned by the endpoint:

```bash
codex-reset-checker --include-inactive
```

Timezone handling is local by default. You can choose a named timezone or omit
local timestamps entirely:

```bash
codex-reset-checker --timezone America/New_York
codex-reset-checker --no-local-time
```

For testing parsers or examples without making a network request, pass a saved
endpoint-shaped JSON file:

```bash
codex-reset-checker --input-json examples/reset_credits.json
cat examples/reset_credits.json | codex-reset-checker --input-json - --json
```

Scripts can ask for exit code `2` when the check succeeds but no available
credits remain:

```bash
codex-reset-checker --exit-code-if-none
```

## Security

Codex Reset Checker reads the saved Codex access token from `auth.json` only to
request reset-credit information from ChatGPT. It does not write to that file or
send credentials anywhere else.

On POSIX systems, the CLI warns if `auth.json` is readable by group or others.
You can restrict the default auth file with:

```bash
chmod 600 ~/.codex/auth.json
```

Do not commit `auth.json`, raw endpoint captures, shell history, or logs that
contain credentials.

## Troubleshooting

If the CLI says it could not find a Codex login, sign in to Codex on the same
machine. If you use a non-default Codex home, pass `--codex-home` or set
`CODEX_HOME`.

If Codex rejects the saved login, the local token may have expired. Sign in to
Codex again.

If the response does not contain a `credits` field, or if the endpoint returns
HTML instead of JSON, the internal endpoint may have changed or may be
temporarily unavailable.

## Development

The usual local checks are:

```bash
uv run ruff format .
uv run ruff check .
uv run pytest
uv build
```

Install the pre-commit hooks if you want them:

```bash
uv run pre-commit install
```

## Development Note

This project was developed with help from Codex.

## Exit Codes

| Code | Meaning |
| ---- | ------- |
| `0` | Check completed successfully. |
| `1` | Missing auth, network failure, invalid response, or another operational error. |
| `2` | No available credits were found. Only used with `--exit-code-if-none`. |
| `130` | Interrupted with Ctrl+C. |

## License

MIT. See [LICENSE](LICENSE).
