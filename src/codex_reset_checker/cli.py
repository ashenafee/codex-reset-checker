"""Command-line interface for Codex reset-credit expiry checks."""

from __future__ import annotations

import argparse
import base64
import binascii
import email.utils
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import zoneinfo
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

ENDPOINT = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
AUTH_NAMESPACE = "https://api.openai.com/auth"
DEFAULT_CODEX_HOME = "~/.codex"
DEFAULT_TIMEOUT_SECONDS = 20.0
SECONDS_PER_DAY = 86_400


class ResetCheckerError(Exception):
    """Raised for expected operational errors that should be shown cleanly."""


@dataclass(frozen=True)
class Credit:
    """A safely parsed reset-credit row."""

    status: str
    expires_at: datetime | None
    expires_raw: str | None
    id_suffix: str | None

    @property
    def is_available(self) -> bool:
        return self.status.casefold() == "available"


@dataclass(frozen=True)
class ParseResult:
    """Parsed credits plus non-fatal parser diagnostics."""

    credits: list[Credit]
    skipped_count: int
    warnings: list[str]


def check_credits(
    codex_home: Path | str | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    timezone: str | None = None,
    include_inactive: bool = False,
) -> dict[str, Any]:
    """Return parsed Codex reset-credit data.

    Parameters
    ----------
    codex_home
        Directory containing `auth.json`. Defaults to `CODEX_HOME` or
        `~/.codex`.
    timeout
        Network timeout in seconds.
    timezone
        IANA timezone name used for local expiry formatting.
    include_inactive
        Include returned credits that are not currently available.

    Raises
    ------
    ResetCheckerError
        Auth, network, or endpoint response failure.
    zoneinfo.ZoneInfoNotFoundError
        Invalid timezone name.
    """
    if codex_home is None:
        codex_home = os.environ.get("CODEX_HOME", DEFAULT_CODEX_HOME)
    codex_home_path = Path(codex_home).expanduser()

    target_tz = None
    if timezone:
        target_tz = zoneinfo.ZoneInfo(timezone)

    payload = fetch_reset_credits(codex_home_path, timeout)

    return build_result(
        payload,
        include_inactive=include_inactive,
        include_local_time=True,
        target_tz=target_tz,
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        target_tz = None
        if args.timezone:
            target_tz = zoneinfo.ZoneInfo(args.timezone)

        if args.input_json:
            payload = load_endpoint_response(args.input_json)
        else:
            codex_home = Path(args.codex_home).expanduser()
            auth_warning = auth_permissions_warning(codex_home / "auth.json")
            if auth_warning and not args.json and not args.quiet:
                print(auth_warning, file=sys.stderr)

            payload = fetch_reset_credits(codex_home, args.timeout)

        result = build_result(
            payload,
            include_inactive=args.include_inactive,
            include_local_time=not args.no_local_time,
            target_tz=target_tz,
        )

        if args.json:
            indent = None if args.compact_json else args.indent
            separators = (",", ":") if args.compact_json else None
            print(
                json.dumps(
                    result, indent=indent, separators=separators, sort_keys=True
                )
            )
        elif not args.quiet:
            print_text(result)

        if args.exit_code_if_none and result["available_count"] == 0:
            return 2
        return 0
    except KeyboardInterrupt:
        if not args.quiet:
            print(
                "\nreset-checker: Execution interrupted by user.",
                file=sys.stderr,
            )
        return 130
    except zoneinfo.ZoneInfoNotFoundError as exc:
        if not args.quiet:
            print(f"reset-checker: {exc}", file=sys.stderr)
        return 1
    except ResetCheckerError as exc:
        if not args.quiet:
            print(f"reset-checker: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except OSError:
            pass
        return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show when banked Codex rate-limit reset credits expire.",
        epilog=(
            "Important: this tool uses an internal, unsupported ChatGPT/Codex "
            "endpoint. It may stop working if the endpoint changes."
        ),
    )
    parser.add_argument(
        "-c",
        "--codex-home",
        default=os.environ.get("CODEX_HOME", DEFAULT_CODEX_HOME),
        help="Codex home containing auth.json. Defaults to CODEX_HOME or"
        " ~/.codex.",
    )
    parser.add_argument(
        "--timeout",
        type=positive_timeout,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Network timeout in seconds. Defaults to"
        f" {DEFAULT_TIMEOUT_SECONDS:g}.",
    )
    parser.add_argument(
        "-j", "--json", action="store_true", help="Emit machine-readable JSON."
    )
    parser.add_argument(
        "--compact-json",
        action="store_true",
        help="Emit compact single-line JSON. Only applies with --json.",
    )
    parser.add_argument(
        "--indent",
        type=non_negative_int,
        default=2,
        help="JSON indentation level. Defaults to 2. Only applies with --json.",
    )
    parser.add_argument(
        "-i",
        "--include-inactive",
        action="store_true",
        help="Include redeemed, expired, or otherwise inactive credit rows in"
        " the output.",
    )
    parser.add_argument(
        "--input-json",
        metavar="PATH|-",
        help=(
            "Parse a saved endpoint response instead of reading auth or making "
            "a network request. Use '-' to read JSON from stdin."
        ),
    )
    parser.add_argument(
        "--no-local-time",
        action="store_true",
        help="Omit local-time fields from output and show UTC only.",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress human-readable output. Exit status still indicates"
        " success or failure.",
    )
    parser.add_argument(
        "--exit-code-if-none",
        action="store_true",
        help="Return exit code 2 when the check succeeds but no available"
        " credits are present.",
    )
    parser.add_argument(
        "-t",
        "--timezone",
        help="Target timezone name (e.g. America/New_York) to format local"
        "times. Defaults to the system local timezone.",
    )
    return parser


def non_negative_int(value: str) -> int:
    try:
        ivalue = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"indent must be an integer: {value}"
        ) from exc
    if ivalue < 0:
        raise argparse.ArgumentTypeError(
            f"indent must be non-negative: {value}"
        )
    return ivalue


def positive_timeout(value: str) -> float:
    try:
        timeout = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("timeout must be a number") from exc
    if not math.isfinite(timeout) or timeout <= 0:
        raise argparse.ArgumentTypeError(
            "timeout must be a positive finite number"
        )
    return timeout


def load_endpoint_response(path_arg: str) -> dict[str, Any]:
    try:
        if path_arg == "-":
            if sys.stdin.isatty():
                raise ResetCheckerError(
                    "cannot read from stdin: stdin is an interactive terminal"
                )
            payload = json.load(sys.stdin)
        else:
            path = Path(path_arg).expanduser()
            with path.open("r", encoding="utf-8") as handle:
                payload = json.load(handle)
    except FileNotFoundError as exc:
        raise ResetCheckerError(
            f"input JSON not found at {Path(path_arg).expanduser()}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ResetCheckerError(f"input JSON is not valid JSON: {exc}") from exc
    except OSError as exc:
        raise ResetCheckerError(f"could not read input JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ResetCheckerError("input JSON must be an object")
    return payload


def check_token_expiration(token: str | None) -> str | None:
    payload = decode_unverified_jwt_payload(token)
    if not payload:
        return None
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool):
        return None

    if exp < time.time():
        exp_dt = datetime.fromtimestamp(exp, UTC)
        exp_str = exp_dt.isoformat(timespec="seconds").replace("+00:00", "Z")
        return (
            f"Codex access token appears to be expired (expired at {exp_str}). "
            "Open Codex Desktop and sign in again."
        )
    return None


def fetch_reset_credits(codex_home: Path, timeout: float) -> dict[str, Any]:
    auth = load_auth(codex_home)
    tokens = auth.get("tokens")
    auth_path = codex_home / "auth.json"
    if not isinstance(tokens, dict):
        raise ResetCheckerError(f"could not read Codex tokens from {auth_path}")

    access_token = as_text(tokens.get("access_token"))
    if not access_token:
        raise ResetCheckerError(
            f"could not find tokens.access_token in {auth_path}; "
            "open Codex Desktop and sign in again"
        )

    token_warning = check_token_expiration(access_token)

    account_id = (
        account_id_from_token(as_text(tokens.get("id_token")))
        or account_id_from_token(access_token)
        or as_text(tokens.get("account_id"))
    )

    headers = {
        "Authorization": f"Bearer {access_token}",
        "originator": "Codex Desktop",
        "OAI-Product-Sku": "CODEX",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "CodexDesktop/0.1.0 Chrome/120.0.0.0 "
            "Electron/28.0.0 Safari/537.36"
        ),
    }
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id

    request = urllib.request.Request(ENDPOINT, headers=headers, method="GET")
    max_attempts = 3
    body = b""
    content_type = ""
    for attempt in range(1, max_attempts + 1):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                body = response.read()
            break
        except urllib.error.HTTPError as exc:
            if attempt < max_attempts and exc.code in {429, 500, 502, 503, 504}:
                retry_after = exc.headers.get("Retry-After")
                sleep_time = parse_retry_after(retry_after, attempt)
                if sleep_time <= 0:
                    sleep_time = 1.0 * attempt
                sleep_time = min(sleep_time, 30.0)
                time.sleep(sleep_time)
                continue
            raise http_error_to_reset_checker_error(exc) from exc
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            if isinstance(exc, urllib.error.URLError):
                reason = getattr(exc, "reason", exc)
                reason_str = str(reason)
                if "CERTIFICATE_VERIFY_FAILED" in reason_str:
                    hint = (
                        "\n\nHint: SSL certificate verification "
                        "failed. If you are on macOS, this is "
                        "often fixed by running the 'Install "
                        "Certificates.command' script located in "
                        "your Python application folder."
                    )
                    raise ResetCheckerError(
                        "could not reach Codex reset-credit "
                        "endpoint due to SSL/TLS certificate "
                        f"verification failure.{hint}"
                    ) from exc
                err_msg = (
                    f"could not reach Codex reset-credit endpoint: {reason}"
                )
            elif isinstance(exc, TimeoutError):
                err_msg = "timed out contacting Codex reset-credit endpoint"
            else:
                err_msg = (
                    "connection failure contacting Codex reset-credit"
                    f" endpoint: {exc}"
                )

            if attempt < max_attempts:
                time.sleep(1.0 * attempt)
                continue
            raise ResetCheckerError(err_msg) from exc

    if not body:
        raise ResetCheckerError(
            "Codex reset-credit endpoint returned an empty response"
        )
    if content_type and "json" not in content_type.lower():
        raise ResetCheckerError(
            f"Codex reset-credit endpoint returned {content_type} instead of"
            " JSON"
        )

    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ResetCheckerError(
            "Codex reset-credit endpoint returned invalid JSON"
        ) from exc

    if not isinstance(payload, dict):
        raise ResetCheckerError(
            "Codex reset-credit endpoint returned an unexpected JSON shape"
        )

    if token_warning:
        payload["_token_warning"] = token_warning

    return payload


def parse_retry_after(retry_after: str | None, attempt: int) -> float:
    fallback = 1.0 * attempt
    if not retry_after:
        return fallback
    try:
        seconds = float(retry_after)
        return seconds if math.isfinite(seconds) else fallback
    except ValueError:
        pass
    try:
        target_time = email.utils.parsedate_to_datetime(retry_after)
        if target_time.tzinfo is None:
            target_time = target_time.replace(tzinfo=UTC)
        return (target_time - datetime.now(UTC)).total_seconds()
    except (TypeError, ValueError):
        return fallback


def http_error_to_reset_checker_error(
    exc: urllib.error.HTTPError,
) -> ResetCheckerError:
    detail = None
    try:
        body = exc.read()
        if body:
            err_payload = json.loads(body)
            if isinstance(err_payload, dict):
                detail = as_text(err_payload.get("detail"))
                error = err_payload.get("error")
                if detail is None and isinstance(error, dict):
                    detail = as_text(error.get("message"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        pass

    suffix_detail = f" Details: {detail}" if detail else ""

    if exc.code == 429:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
        suffix = (
            f" Try again after {retry_after} seconds." if retry_after else ""
        )
        return ResetCheckerError(
            f"Codex rate-limited this check.{suffix}{suffix_detail}"
        )
    if exc.code in {401, 403}:
        return ResetCheckerError(
            "Codex rejected the saved login. Open Codex Desktop and sign in"
            f" again.{suffix_detail}"
        )
    if exc.code == 404:
        return ResetCheckerError(
            "Codex reset-credit endpoint not found (HTTP 404). The internal "
            f"API may have changed or been removed.{suffix_detail}"
        )
    if exc.code >= 500:
        return ResetCheckerError(
            "Codex server error (HTTP "
            f"{exc.code}). The service might be temporarily "
            f"down.{suffix_detail}"
        )
    return ResetCheckerError(
        f"Codex endpoint returned HTTP {exc.code}.{suffix_detail}"
    )


def load_auth(codex_home: Path) -> dict[str, Any]:
    auth_path = codex_home / "auth.json"
    try:
        with auth_path.open("r", encoding="utf-8") as handle:
            auth = json.load(handle)
    except FileNotFoundError as exc:
        raise ResetCheckerError(
            f"could not find Codex login at {auth_path}; open Codex Desktop "
            "and sign in first"
        ) from exc
    except json.JSONDecodeError as exc:
        raise ResetCheckerError(
            f"could not parse Codex login at {auth_path}; open Codex Desktop "
            "and sign in again"
        ) from exc
    except OSError as exc:
        raise ResetCheckerError(
            f"could not read Codex login at {auth_path}: {exc}"
        ) from exc

    if not isinstance(auth, dict):
        raise ResetCheckerError(
            f"Codex login at {auth_path} has an unexpected JSON shape"
        )
    return auth


def auth_permissions_warning(auth_path: Path) -> str | None:
    if os.name != "posix":
        return None
    try:
        mode = auth_path.stat().st_mode
    except OSError:
        return None
    if mode & 0o077:
        return (
            f"warning: {auth_path} is readable by group or others; "
            "consider restricting it with: chmod 600 " + str(auth_path)
        )
    return None


def account_id_from_token(token: str | None) -> str | None:
    payload = decode_unverified_jwt_payload(token)
    if not payload:
        return None
    auth = payload.get(AUTH_NAMESPACE)
    if not isinstance(auth, dict):
        return None
    return as_text(auth.get("chatgpt_account_id"))


def decode_unverified_jwt_payload(token: str | None) -> dict[str, Any] | None:
    """Decode a JWT payload without verifying the signature.

    This is only used to extract a non-authoritative account-id hint from a
    token already present in the user's local Codex Desktop auth file. Do not
    use this helper for authentication or trust decisions.
    """

    if not token:
        return None
    parts = token.split(".")
    if len(parts) < 2:
        return None
    segment = parts[1].replace("-", "+").replace("_", "/")
    segment += "=" * (-len(segment) % 4)
    try:
        decoded = base64.b64decode(segment)
        payload = json.loads(decoded)
    except (
        binascii.Error,
        ValueError,
        json.JSONDecodeError,
        UnicodeDecodeError,
    ):
        return None
    return payload if isinstance(payload, dict) else None


def build_result(
    payload: dict[str, Any],
    include_inactive: bool,
    include_local_time: bool = True,
    target_tz: zoneinfo.ZoneInfo | None = None,
) -> dict[str, Any]:
    validate_payload_shape(payload)
    parse_result = parse_credits(payload["credits"])
    credits = parse_result.credits
    visible = [
        credit for credit in credits if include_inactive or credit.is_available
    ]
    visible.sort(key=credit_sort_key)

    available_count = payload.get("available_count")
    if (
        not isinstance(available_count, int)
        or isinstance(available_count, bool)
        or available_count < 0
    ):
        available_count = sum(1 for credit in credits if credit.is_available)

    checked_at = datetime.now(UTC)
    local_now = checked_at.astimezone(target_tz)

    warnings = list(parse_result.warnings)
    token_warning = as_text(payload.get("_token_warning"))
    if token_warning:
        warnings.append(token_warning)

    result: dict[str, Any] = {
        "available_count": available_count,
        "checked_at": format_datetime_utc(checked_at),
        "credits": [
            serialize_credit(
                credit,
                index,
                checked_at,
                include_local_time=include_local_time,
                target_tz=target_tz,
            )
            for index, credit in enumerate(visible, 1)
        ],
        "parseable_credit_count": len(credits),
        "skipped_credit_count": parse_result.skipped_count,
        "warnings": warnings,
    }
    if include_local_time:
        offset = local_now.strftime("%z")
        formatted_offset = (
            f"{offset[:-2]}:{offset[-2:]}" if len(offset) == 5 else offset
        )
        result["local_timezone"] = {
            "name": str(local_now.tzinfo),
            "abbreviation": local_now.tzname() or str(local_now.tzinfo),
            "utc_offset": formatted_offset,
        }
    return result


def validate_payload_shape(payload: dict[str, Any]) -> None:
    if "credits" not in payload:
        raise ResetCheckerError(
            "Codex endpoint response did not contain a credits field; "
            "the internal endpoint may have changed."
        )
    if not isinstance(payload["credits"], list):
        raise ResetCheckerError(
            "Codex endpoint returned credits in an unexpected format; "
            "the internal endpoint may have changed."
        )


def parse_credits(raw_credits: list[Any]) -> ParseResult:
    credits: list[Credit] = []
    skipped_count = 0

    for raw in raw_credits:
        if not isinstance(raw, dict):
            skipped_count += 1
            continue
        credit_id = as_text(raw.get("id"))
        if not credit_id:
            skipped_count += 1
            continue
        status = as_text(raw.get("status")) or "unknown"
        expires_raw = as_text(raw.get("expires_at"))
        credits.append(
            Credit(
                status=status,
                expires_at=parse_iso_datetime(expires_raw),
                expires_raw=expires_raw,
                id_suffix=credit_id[-6:],
            )
        )

    warnings: list[str] = []
    if skipped_count:
        row = "row" if skipped_count == 1 else "rows"
        warnings.append(f"Skipped {skipped_count} malformed credit {row}.")

    return ParseResult(
        credits=credits, skipped_count=skipped_count, warnings=warnings
    )


def credit_sort_key(credit: Credit) -> tuple[int, datetime]:
    """Sort known expiries first, soonest before latest."""

    if credit.expires_at is None:
        return (1, datetime.max.replace(tzinfo=UTC))
    return (0, credit.expires_at.astimezone(UTC))


def serialize_credit(
    credit: Credit,
    index: int,
    now: datetime,
    include_local_time: bool,
    target_tz: zoneinfo.ZoneInfo | None = None,
) -> dict[str, Any]:
    expires_utc = (
        credit.expires_at.astimezone(UTC) if credit.expires_at else None
    )
    seconds_remaining = seconds_until_expiry(credit, now)
    serialized: dict[str, Any] = {
        "index": index,
        "id_suffix": credit.id_suffix,
        "status": credit.status,
        "urgency": urgency_for(credit, now, target_tz),
        "expires_at_raw": credit.expires_raw,
        "expires_at_utc": format_datetime_utc(expires_utc),
        "time_remaining_seconds": seconds_remaining,
        "time_remaining_human": humanize_duration(seconds_remaining),
    }
    if include_local_time:
        expires_local = (
            credit.expires_at.astimezone(target_tz)
            if credit.expires_at
            else None
        )
        serialized["expires_at_local"] = format_datetime(expires_local)
    return serialized


def urgency_for(
    credit: Credit, now: datetime, target_tz: zoneinfo.ZoneInfo | None = None
) -> str:
    """Return a concise user-facing urgency label for a credit."""

    if not credit.is_available:
        return f"Inactive ({credit.status})"
    if credit.expires_at is None:
        return "Available (expiry unknown)"

    seconds = (credit.expires_at - now).total_seconds()
    if seconds <= 0:
        return "Expired"

    local_expiry = credit.expires_at.astimezone(target_tz)
    local_now = now.astimezone(target_tz)
    if local_expiry.date() == local_now.date():
        return "Ends today"

    if seconds <= 3 * SECONDS_PER_DAY:
        return "Expires soon"
    if seconds <= 7 * SECONDS_PER_DAY:
        return "This week"
    return "Available"


def seconds_until_expiry(credit: Credit, now: datetime) -> int | None:
    if not credit.is_available or credit.expires_at is None:
        return None
    delta = credit.expires_at - now
    return max(0, int(delta.total_seconds()))


def humanize_duration(seconds: int | None) -> str | None:
    """Convert seconds to a compact human-readable duration."""

    if seconds is None:
        return None
    days, remainder = divmod(seconds, SECONDS_PER_DAY)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def parse_iso_datetime(value: str | None) -> datetime | None:
    """Parse common ISO datetime strings, treating naive timestamps as UTC."""

    if not value:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if normalized.upper().endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def print_text(result: dict[str, Any], stream: TextIO = sys.stdout) -> None:
    use_color = False
    if (
        hasattr(stream, "isatty")
        and stream.isatty()
        and "NO_COLOR" not in os.environ
    ):
        use_color = True

    CLR_RESET = "\033[0m" if use_color else ""
    CLR_BOLD = "\033[1m" if use_color else ""
    CLR_GREEN = "\033[32m" if use_color else ""
    CLR_YELLOW = "\033[33m" if use_color else ""
    CLR_RED = "\033[31m" if use_color else ""
    CLR_CYAN = "\033[36m" if use_color else ""

    print(f"{CLR_BOLD}Codex reset credits{CLR_RESET}", file=stream)
    print(f"Checked: {result['checked_at']}", file=stream)
    if "local_timezone" in result:
        tz = result["local_timezone"]
        if isinstance(tz, dict):
            abbr = f"{CLR_CYAN}{tz['abbreviation']}{CLR_RESET}"
            name = tz["name"]
            offset = tz["utc_offset"]
            print(
                f"Local timezone: {abbr} ({name}, UTC{offset})",
                file=stream,
            )
        else:
            print(f"Local timezone: {tz}", file=stream)

    for warning in result.get("warnings", []):
        print(f"{CLR_YELLOW}Warning: {warning}{CLR_RESET}", file=stream)

    available = result["available_count"]
    avail_color = CLR_GREEN if available > 0 else CLR_RED
    print(
        f"Available credits: {avail_color}{available}{CLR_RESET}",
        file=stream,
    )

    credits = result["credits"]
    if not credits:
        print("No reset-credit expiry rows to show.", file=stream)
        return

    for credit in credits:
        id_part = (
            f" ending {credit['id_suffix']}" if credit.get("id_suffix") else ""
        )
        urgency = credit["urgency"]
        if urgency == "Ends today":
            urgency_colored = f"{CLR_RED}{urgency}{CLR_RESET}"
        elif urgency in ("Expires soon", "This week"):
            urgency_colored = f"{CLR_YELLOW}{urgency}{CLR_RESET}"
        elif urgency == "Available":
            urgency_colored = f"{CLR_GREEN}{urgency}{CLR_RESET}"
        elif urgency == "Expired" or urgency.startswith("Inactive"):
            urgency_colored = f"{CLR_RED}{urgency}{CLR_RESET}"
        else:
            urgency_colored = urgency

        print(
            f"- Credit {credit['index']}{id_part}: {urgency_colored}",
            file=stream,
        )
        status = credit["status"]
        if status == "available":
            status_colored = f"{CLR_GREEN}{status}{CLR_RESET}"
        else:
            status_colored = f"{CLR_YELLOW}{status}{CLR_RESET}"
        print(f"  Status: {status_colored}", file=stream)
        if "expires_at_local" in credit:
            print(
                f"  Expires local: {credit['expires_at_local'] or '-'}",
                file=stream,
            )
        print(f"  Expires UTC: {credit['expires_at_utc'] or '-'}", file=stream)

        remaining = credit["time_remaining_human"] or "-"
        if remaining != "-":
            remaining_colored = f"{CLR_BOLD}{remaining}{CLR_RESET}"
        else:
            remaining_colored = "-"
        print(
            f"  Time remaining: {remaining_colored}",
            file=stream,
        )


def format_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="seconds")


def format_datetime_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    return (
        value.astimezone(UTC)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def as_text(value: Any) -> str | None:
    """Return a safe text representation for simple JSON scalar values."""

    if isinstance(value, str):
        return value
    if type(value) is int:
        return str(value)
    return None


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
