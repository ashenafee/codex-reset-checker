from __future__ import annotations

import argparse
import base64
import email.message
import email.utils
import io
import json
import sys
import time
import urllib.error
import urllib.request
import zoneinfo
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codex_reset_checker.cli import (
    AUTH_NAMESPACE,
    Credit,
    ResetCheckerError,
    account_id_from_token,
    as_text,
    auth_permissions_warning,
    build_parser,
    build_result,
    check_credits,
    check_token_expiration,
    decode_unverified_jwt_payload,
    fetch_reset_credits,
    http_error_to_reset_checker_error,
    humanize_duration,
    load_endpoint_response,
    main,
    non_negative_int,
    parse_iso_datetime,
    parse_retry_after,
    positive_timeout,
    print_text,
    urgency_for,
    validate_payload_shape,
)


def make_jwt(payload: dict[str, object]) -> str:
    header = {"alg": "none", "typ": "JWT"}

    def encode(part: dict[str, object]) -> str:
        raw = json.dumps(part, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode(header)}.{encode(payload)}.signature"


def test_parse_iso_datetime_accepts_z_suffix() -> None:
    parsed = parse_iso_datetime("2026-06-27T12:34:56Z")
    assert parsed == datetime(2026, 6, 27, 12, 34, 56, tzinfo=UTC)


def test_parse_iso_datetime_accepts_lowercase_z_suffix() -> None:
    parsed = parse_iso_datetime("2026-06-27T12:34:56z")
    assert parsed == datetime(2026, 6, 27, 12, 34, 56, tzinfo=UTC)


def test_parse_iso_datetime_treats_naive_as_utc() -> None:
    parsed = parse_iso_datetime("2026-06-27T12:34:56")
    assert parsed == datetime(2026, 6, 27, 12, 34, 56, tzinfo=UTC)


def test_parse_iso_datetime_returns_none_for_bad_values() -> None:
    assert parse_iso_datetime(None) is None
    assert parse_iso_datetime("") is None
    assert parse_iso_datetime("not a date") is None


def test_decode_unverified_jwt_payload() -> None:
    token = make_jwt({"sub": "user_123"})
    assert decode_unverified_jwt_payload(token) == {"sub": "user_123"}


def test_decode_unverified_jwt_payload_returns_none_for_malformed_token() -> (
    None
):
    assert decode_unverified_jwt_payload("not-a-jwt") is None
    assert decode_unverified_jwt_payload("a.b.c") is None


def test_account_id_from_token() -> None:
    token = make_jwt({AUTH_NAMESPACE: {"chatgpt_account_id": "acct_123"}})
    assert account_id_from_token(token) == "acct_123"


def test_account_id_from_token_returns_none_when_namespace_missing() -> None:
    token = make_jwt({"sub": "user_123"})
    assert account_id_from_token(token) is None


def test_validate_payload_shape_rejects_missing_credits() -> None:
    with pytest.raises(ResetCheckerError, match="credits field"):
        validate_payload_shape({})


def test_validate_payload_shape_rejects_non_list_credits() -> None:
    with pytest.raises(ResetCheckerError, match="unexpected format"):
        validate_payload_shape({"credits": {}})


def test_build_result_basic() -> None:
    now = datetime.now(UTC)
    later = now + timedelta(days=5)
    sooner = now + timedelta(minutes=1)
    payload = {
        "available_count": 2,
        "credits": [
            {
                "id": "credit_later",
                "status": "available",
                "expires_at": later.isoformat(),
            },
            {
                "id": "credit_used",
                "status": "used",
                "expires_at": sooner.isoformat(),
            },
            {
                "id": "credit_sooner",
                "status": "available",
                "expires_at": sooner.isoformat(),
            },
        ],
    }

    result = build_result(
        payload, include_inactive=False, include_local_time=False
    )

    assert result["available_count"] == 2
    assert result["parseable_credit_count"] == 3
    assert result["skipped_credit_count"] == 0
    assert [credit["id_suffix"] for credit in result["credits"]] == [
        "sooner",
        "_later",
    ]
    assert result["credits"][0]["urgency"] == "Ends today"
    assert "expires_at_local" not in result["credits"][0]


def test_build_result_includes_inactive_status_without_collapsing_to_used() -> (
    None
):
    payload = {
        "credits": [
            {
                "id": "credit_pending",
                "status": "pending",
                "expires_at": "2026-06-27T00:00:00Z",
            }
        ]
    }

    result = build_result(
        payload, include_inactive=True, include_local_time=False
    )

    assert result["available_count"] == 0
    assert result["credits"][0]["urgency"] == "Inactive (pending)"


def test_build_result_recomputes_invalid_available_count() -> None:
    payload = {
        "available_count": True,
        "credits": [
            {"id": "credit_a", "status": "available", "expires_at": None},
            {"id": "credit_b", "status": "used", "expires_at": None},
        ],
    }

    result = build_result(
        payload, include_inactive=True, include_local_time=False
    )

    assert result["available_count"] == 1


def test_build_result_reports_skipped_rows() -> None:
    payload = {
        "credits": [
            {"id": "credit_a", "status": "available", "expires_at": None},
            {"status": "available", "expires_at": None},
            "bad row",
        ],
    }

    result = build_result(
        payload, include_inactive=True, include_local_time=False
    )

    assert result["parseable_credit_count"] == 1
    assert result["skipped_credit_count"] == 2
    assert result["warnings"] == ["Skipped 2 malformed credit rows."]


def test_humanize_duration() -> None:
    assert humanize_duration(None) is None
    assert humanize_duration(30) == "0m"
    assert humanize_duration(3600 + 120) == "1h 2m"
    assert humanize_duration(2 * 86_400 + 3600) == "2d 1h"


@pytest.mark.parametrize("value", ["0", "-1", "nan", "inf", "not-a-number"])
def test_positive_timeout_rejects_invalid_values(value: str) -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        positive_timeout(value)


def test_positive_timeout_accepts_positive_finite_value() -> None:
    assert positive_timeout("1.5") == 1.5


def test_as_text_does_not_treat_bool_as_int() -> None:
    assert as_text("x") == "x"
    assert as_text(123) == "123"
    assert as_text(True) is None


def test_non_negative_int_rejects_invalid_values() -> None:
    for val in ["not-an-int", "1.5", "-1"]:
        with pytest.raises(argparse.ArgumentTypeError):
            non_negative_int(val)


def test_non_negative_int_accepts_valid_values() -> None:
    assert non_negative_int("0") == 0
    assert non_negative_int("4") == 4


def test_load_endpoint_response_interactive_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with pytest.raises(
        ResetCheckerError,
        match="cannot read from stdin: stdin is an interactive terminal",
    ):
        load_endpoint_response("-")


def test_load_endpoint_response_non_interactive_stdin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    mock_input = io.StringIO('{"credits": []}')
    monkeypatch.setattr(sys, "stdin", mock_input)
    assert load_endpoint_response("-") == {"credits": []}


def test_http_error_to_reset_checker_error_parsing() -> None:
    body_json = json.dumps({"detail": "Rate limit exceeded detail"}).encode(
        "utf-8"
    )

    mock_fp = io.BytesIO(body_json)
    exc = urllib.error.HTTPError(
        url="http://example.com",
        code=429,
        msg="Too Many Requests",
        hdrs=email.message.Message(),
        fp=mock_fp,
    )
    res = http_error_to_reset_checker_error(exc)
    assert "Rate limit exceeded detail" in str(res)


def test_http_error_to_reset_checker_error_fallback_message() -> None:
    body_json = json.dumps(
        {"error": {"message": "Custom error message"}}
    ).encode("utf-8")
    mock_fp = io.BytesIO(body_json)
    exc = urllib.error.HTTPError(
        url="http://example.com",
        code=403,
        msg="Forbidden",
        hdrs=email.message.Message(),
        fp=mock_fp,
    )
    res = http_error_to_reset_checker_error(exc)
    assert "Custom error message" in str(res)


def test_http_error_to_reset_checker_error_ignores_bad_error_shape() -> None:
    body_json = json.dumps({"error": "not an object"}).encode("utf-8")
    exc = urllib.error.HTTPError(
        url="http://example.com",
        code=403,
        msg="Forbidden",
        hdrs=email.message.Message(),
        fp=io.BytesIO(body_json),
    )
    res = http_error_to_reset_checker_error(exc)
    assert "Codex rejected the saved login" in str(res)


def test_http_error_to_reset_checker_error_404() -> None:
    exc = urllib.error.HTTPError(
        url="http://example.com",
        code=404,
        msg="Not Found",
        hdrs=email.message.Message(),
        fp=None,
    )
    res = http_error_to_reset_checker_error(exc)
    assert "endpoint not found" in str(res)


def test_http_error_to_reset_checker_error_500() -> None:
    exc = urllib.error.HTTPError(
        url="http://example.com",
        code=500,
        msg="Server Error",
        hdrs=email.message.Message(),
        fp=None,
    )
    res = http_error_to_reset_checker_error(exc)
    assert "server error" in str(res)


def test_http_error_to_reset_checker_error_generic() -> None:
    exc = urllib.error.HTTPError(
        url="http://example.com",
        code=418,
        msg="I'm a teapot",
        hdrs=email.message.Message(),
        fp=None,
    )
    res = http_error_to_reset_checker_error(exc)
    assert "returned HTTP 418" in str(res)


def test_ssl_certificate_verification_failure_hint() -> None:
    err = urllib.error.URLError("SSL: CERTIFICATE_VERIFY_FAILED")

    with (
        patch("urllib.request.urlopen", side_effect=err),
        patch(
            "codex_reset_checker.cli.load_auth",
            return_value={"tokens": {"access_token": "dummy"}},
        ),
    ):
        with pytest.raises(ResetCheckerError) as exc_info:
            fetch_reset_credits(Path("/dummy"), timeout=5.0)

        assert "SSL certificate verification failed" in str(exc_info.value)
        assert "Install Certificates.command" in str(exc_info.value)


def test_keyboard_interrupt_graceful_exit() -> None:
    with patch(
        "codex_reset_checker.cli.load_endpoint_response",
        side_effect=KeyboardInterrupt,
    ):
        stderr_capture = io.StringIO()
        with patch("sys.stderr", stderr_capture):
            exit_code = main(["--input-json", "dummy.json"])

        assert exit_code == 130
        assert "Execution interrupted by user" in stderr_capture.getvalue()


def test_cli_indentation_control() -> None:
    payload = {
        "credits": [
            {"id": "credit_a", "status": "available", "expires_at": None}
        ]
    }

    with patch(
        "codex_reset_checker.cli.load_endpoint_response", return_value=payload
    ):
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main(
                [
                    "--input-json",
                    "dummy.json",
                    "--json",
                    "--indent",
                    "4",
                ]
            )

        assert exit_code == 0
        output_json = stdout_capture.getvalue()
        assert '    "available_count"' in output_json


def test_auth_permissions_warning_on_posix() -> None:
    with patch("os.name", "posix"):
        mock_stat = MagicMock()
        mock_stat.st_mode = 0o644

        mock_path = MagicMock()
        mock_path.stat.return_value = mock_stat

        warning = auth_permissions_warning(mock_path)
        assert warning is not None
        assert "readable by group or others" in warning


def test_print_text_formatting() -> None:
    result = {
        "checked_at": "2026-06-27T12:00:00Z",
        "local_timezone": "EDT",
        "available_count": 1,
        "warnings": ["Dummy warning"],
        "credits": [
            {
                "index": 1,
                "id_suffix": "abc",
                "status": "available",
                "urgency": "Ends today",
                "expires_at_local": "2026-06-27T08:00:00-04:00",
                "expires_at_utc": "2026-06-27T12:00:00Z",
                "time_remaining_human": "8h 0m",
            }
        ],
    }

    stream = io.StringIO()
    print_text(result, stream=stream)
    output = stream.getvalue()

    assert "Available credits: 1" in output
    assert "Warning: Dummy warning" in output
    assert "- Credit 1 ending abc: Ends today" in output
    assert "Expires local: 2026-06-27T08:00:00-04:00" in output


def test_fetch_reset_credits_sends_user_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers_sent = {}

    def mock_urlopen(
        request: urllib.request.Request, timeout: float = 20.0
    ) -> MagicMock:
        nonlocal headers_sent
        headers_sent = request.headers
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{"credits": []}'
        return response

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    with patch(
        "codex_reset_checker.cli.load_auth",
        return_value={"tokens": {"access_token": "dummy_token"}},
    ):
        fetch_reset_credits(Path("/dummy"), timeout=5.0)

    ua_header = next(
        (
            val
            for key, val in headers_sent.items()
            if key.lower() == "user-agent"
        ),
        None,
    )
    assert ua_header is not None
    assert "CodexDesktop" in ua_header
    assert "Mozilla/5.0" in ua_header


def test_fetch_reset_credits_retries_and_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps = []

    def mock_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def mock_urlopen(
        request: urllib.request.Request, timeout: float = 20.0
    ) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            mock_fp = io.BytesIO(b"")
            raise urllib.error.HTTPError(
                url="http://example.com",
                code=503,
                msg="Service Unavailable",
                hdrs=email.message.Message(),
                fp=mock_fp,
            )
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{"credits": []}'
        return response

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    monkeypatch.setattr(time, "sleep", mock_sleep)

    with patch(
        "codex_reset_checker.cli.load_auth",
        return_value={"tokens": {"access_token": "dummy_token"}},
    ):
        payload = fetch_reset_credits(Path("/dummy"), timeout=5.0)
        assert payload == {"credits": []}
        assert attempts == 3
        assert sleeps == [1.0, 2.0]


def test_fetch_reset_credits_respects_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps = []

    def mock_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def mock_urlopen(
        request: urllib.request.Request, timeout: float = 20.0
    ) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            hdrs = email.message.Message()
            hdrs["Retry-After"] = "5.5"
            mock_fp = io.BytesIO(b"")
            raise urllib.error.HTTPError(
                url="http://example.com",
                code=429,
                msg="Too Many Requests",
                hdrs=hdrs,
                fp=mock_fp,
            )
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{"credits": []}'
        return response

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    monkeypatch.setattr(time, "sleep", mock_sleep)

    with patch(
        "codex_reset_checker.cli.load_auth",
        return_value={"tokens": {"access_token": "dummy_token"}},
    ):
        payload = fetch_reset_credits(Path("/dummy"), timeout=5.0)
        assert payload == {"credits": []}
        assert attempts == 2
        assert sleeps == [5.5]


def test_fetch_reset_credits_fails_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps = []

    def mock_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def mock_urlopen(
        request: urllib.request.Request, timeout: float = 20.0
    ) -> MagicMock:
        nonlocal attempts
        attempts += 1
        mock_fp = io.BytesIO(b"")
        raise urllib.error.HTTPError(
            url="http://example.com",
            code=503,
            msg="Service Unavailable",
            hdrs=email.message.Message(),
            fp=mock_fp,
        )

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    monkeypatch.setattr(time, "sleep", mock_sleep)

    with patch(
        "codex_reset_checker.cli.load_auth",
        return_value={"tokens": {"access_token": "dummy_token"}},
    ):
        with pytest.raises(ResetCheckerError, match="server error"):
            fetch_reset_credits(Path("/dummy"), timeout=5.0)
        assert attempts == 3
        assert sleeps == [1.0, 2.0]


def test_main_handles_broken_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    def mock_print_text(result: dict, stream=None) -> None:
        raise BrokenPipeError()

    monkeypatch.setattr("codex_reset_checker.cli.print_text", mock_print_text)

    payload = {
        "credits": [
            {"id": "credit_a", "status": "available", "expires_at": None}
        ]
    }
    with patch(
        "codex_reset_checker.cli.load_endpoint_response", return_value=payload
    ):
        import os

        mock_open = MagicMock(return_value=999)
        mock_dup2 = MagicMock()
        monkeypatch.setattr(os, "open", mock_open)
        monkeypatch.setattr(os, "dup2", mock_dup2)

        exit_code = main(["--input-json", "dummy.json"])

        assert exit_code == 0
        mock_open.assert_called_once()
        mock_dup2.assert_called_once()


def test_fetch_reset_credits_handles_negative_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps = []

    def mock_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def mock_urlopen(
        request: urllib.request.Request, timeout: float = 20.0
    ) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            hdrs = email.message.Message()
            hdrs["Retry-After"] = "-5.0"
            mock_fp = io.BytesIO(b"")
            raise urllib.error.HTTPError(
                url="http://example.com",
                code=429,
                msg="Too Many Requests",
                hdrs=hdrs,
                fp=mock_fp,
            )
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{"credits": []}'
        return response

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    monkeypatch.setattr(time, "sleep", mock_sleep)

    with patch(
        "codex_reset_checker.cli.load_auth",
        return_value={"tokens": {"access_token": "dummy_token"}},
    ):
        payload = fetch_reset_credits(Path("/dummy"), timeout=5.0)
        assert payload == {"credits": []}
        assert attempts == 2
        assert sleeps == [1.0]


def test_fetch_reset_credits_clamps_large_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps = []

    def mock_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def mock_urlopen(
        request: urllib.request.Request, timeout: float = 20.0
    ) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            hdrs = email.message.Message()
            hdrs["Retry-After"] = "3600.0"
            mock_fp = io.BytesIO(b"")
            raise urllib.error.HTTPError(
                url="http://example.com",
                code=429,
                msg="Too Many Requests",
                hdrs=hdrs,
                fp=mock_fp,
            )
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{"credits": []}'
        return response

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    monkeypatch.setattr(time, "sleep", mock_sleep)

    with patch(
        "codex_reset_checker.cli.load_auth",
        return_value={"tokens": {"access_token": "dummy_token"}},
    ):
        payload = fetch_reset_credits(Path("/dummy"), timeout=5.0)
        assert payload == {"credits": []}
        assert attempts == 2
        assert sleeps == [30.0]


def test_fetch_reset_credits_respects_retry_after_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps = []

    def mock_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def mock_urlopen(
        request: urllib.request.Request, timeout: float = 20.0
    ) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            hdrs = email.message.Message()
            future_dt = datetime.now(UTC) + timedelta(seconds=10)
            hdrs["Retry-After"] = email.utils.format_datetime(
                future_dt, usegmt=True
            )
            mock_fp = io.BytesIO(b"")
            raise urllib.error.HTTPError(
                url="http://example.com",
                code=429,
                msg="Too Many Requests",
                hdrs=hdrs,
                fp=mock_fp,
            )
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{"credits": []}'
        return response

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)

    monkeypatch.setattr(time, "sleep", mock_sleep)

    with patch(
        "codex_reset_checker.cli.load_auth",
        return_value={"tokens": {"access_token": "dummy_token"}},
    ):
        payload = fetch_reset_credits(Path("/dummy"), timeout=5.0)
        assert payload == {"credits": []}
        assert attempts == 2
        assert len(sleeps) == 1
        assert 8.0 <= sleeps[0] <= 12.0


def test_check_token_expiration_expired() -> None:
    expired_time = int(time.time() - 3600)
    token = make_jwt({"exp": expired_time})
    warning = check_token_expiration(token)
    assert warning is not None
    assert "access token appears to be expired" in warning


def test_check_token_expiration_valid() -> None:
    future_time = int(time.time() + 3600)
    token = make_jwt({"exp": future_time})
    warning = check_token_expiration(token)
    assert warning is None


def test_check_token_expiration_invalid_or_missing() -> None:
    assert check_token_expiration(None) is None
    assert check_token_expiration("not-a-jwt") is None

    token_no_exp = make_jwt({"sub": "user_123"})
    assert check_token_expiration(token_no_exp) is None

    token_bad_exp = make_jwt({"exp": "not-a-timestamp"})
    assert check_token_expiration(token_bad_exp) is None

    token_bool_exp = make_jwt({"exp": True})
    assert check_token_expiration(token_bool_exp) is None


def test_main_timezone_valid_and_serialized() -> None:
    payload = {
        "credits": [
            {
                "id": "credit_xyz",
                "status": "available",
                "expires_at": "2026-06-27T12:00:00Z",
            }
        ]
    }

    with patch(
        "codex_reset_checker.cli.load_endpoint_response", return_value=payload
    ):
        stdout_capture = io.StringIO()
        with patch("sys.stdout", stdout_capture):
            exit_code = main(
                [
                    "--input-json",
                    "dummy.json",
                    "--json",
                    "--timezone",
                    "Europe/London",
                ]
            )

        assert exit_code == 0
        output_json = json.loads(stdout_capture.getvalue())
        assert (
            output_json["credits"][0]["expires_at_local"]
            == "2026-06-27T13:00:00+01:00"
        )
        assert output_json["local_timezone"]["abbreviation"] in {
            "BST",
            "Europe/London",
            "GMT+1",
        }
        assert output_json["local_timezone"]["name"] == "Europe/London"
        assert output_json["local_timezone"]["utc_offset"] == "+01:00"


def test_main_timezone_invalid() -> None:
    stderr_capture = io.StringIO()
    with patch("sys.stderr", stderr_capture):
        exit_code = main(
            [
                "--input-json",
                "dummy.json",
                "--timezone",
                "Invalid/Zone_Name",
            ]
        )

    assert exit_code == 1
    assert "Invalid/Zone_Name" in stderr_capture.getvalue()


def test_urgency_for_calendar_date_boundary() -> None:
    now = datetime(2026, 6, 27, 23, 0, 0, tzinfo=UTC)
    expiry_a = datetime(2026, 6, 28, 1, 0, 0, tzinfo=UTC)
    credit_a = Credit(
        status="available",
        expires_at=expiry_a,
        expires_raw=None,
        id_suffix=None,
    )

    assert urgency_for(credit_a, now, target_tz=UTC) == "Expires soon"

    ny_tz = zoneinfo.ZoneInfo("America/New_York")
    assert urgency_for(credit_a, now, target_tz=ny_tz) == "Ends today"


def test_fetch_reset_credits_retries_on_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps = []

    def mock_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def mock_urlopen(
        request: urllib.request.Request, timeout: float = 20.0
    ) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            mock_fp = io.BytesIO(b"")
            raise urllib.error.HTTPError(
                url="http://example.com",
                code=500,
                msg="Internal Server Error",
                hdrs=email.message.Message(),
                fp=mock_fp,
            )
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{"credits": []}'
        return response

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    monkeypatch.setattr(time, "sleep", mock_sleep)

    with patch(
        "codex_reset_checker.cli.load_auth",
        return_value={"tokens": {"access_token": "dummy_token"}},
    ):
        payload = fetch_reset_credits(Path("/dummy"), timeout=5.0)
        assert payload == {"credits": []}
        assert attempts == 3
        assert sleeps == [1.0, 2.0]


def test_fetch_reset_credits_retries_on_connection_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0
    sleeps = []

    def mock_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    def mock_urlopen(
        request: urllib.request.Request, timeout: float = 20.0
    ) -> MagicMock:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionResetError("Connection reset by peer")
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {"Content-Type": "application/json"}
        response.read.return_value = b'{"credits": []}'
        return response

    monkeypatch.setattr(urllib.request, "urlopen", mock_urlopen)
    monkeypatch.setattr(time, "sleep", mock_sleep)

    with patch(
        "codex_reset_checker.cli.load_auth",
        return_value={"tokens": {"access_token": "dummy_token"}},
    ):
        payload = fetch_reset_credits(Path("/dummy"), timeout=5.0)
        assert payload == {"credits": []}
        assert attempts == 3
        assert sleeps == [1.0, 2.0]


def test_build_result_rejects_bool_available_count() -> None:
    payload = {
        "available_count": True,
        "credits": [
            {"id": "credit_a", "status": "available", "expires_at": None},
            {"id": "credit_b", "status": "used", "expires_at": None},
        ],
    }

    result = build_result(
        payload, include_inactive=True, include_local_time=False
    )
    assert result["available_count"] == 1


def test_import_from_package_root() -> None:
    import codex_reset_checker

    assert hasattr(codex_reset_checker, "check_credits")
    assert hasattr(codex_reset_checker, "ResetCheckerError")


def test_check_credits_programmatic() -> None:
    payload = {
        "available_count": 1,
        "credits": [
            {
                "id": "credit_xyz",
                "status": "available",
                "expires_at": "2026-06-27T12:00:00Z",
            }
        ],
    }

    mock_target = "codex_reset_checker.cli.fetch_reset_credits"
    with patch(mock_target, return_value=payload) as mock_fetch:
        result = check_credits(
            codex_home="/dummy/path",
            timeout=10.0,
            timezone="America/New_York",
            include_inactive=True,
        )

        mock_fetch.assert_called_once_with(
            Path("/dummy/path"),
            10.0,
        )
        assert result["available_count"] == 1
        assert result["credits"][0]["id_suffix"] == "it_xyz"
        expected_local = "2026-06-27T08:00:00-04:00"
        assert result["credits"][0]["expires_at_local"] == expected_local


def test_cli_short_flags() -> None:
    parser = build_parser()

    args = parser.parse_args(["-c", "/custom/home", "-j", "-i", "-q"])
    assert args.codex_home == "/custom/home"
    assert args.json is True
    assert args.include_inactive is True
    assert args.quiet is True


def test_print_text_ansi_colors(monkeypatch: pytest.MonkeyPatch) -> None:
    result = {
        "checked_at": "2026-06-27T12:00:00Z",
        "available_count": 1,
        "warnings": ["Low credits warning"],
        "credits": [
            {
                "index": 1,
                "id_suffix": "xyz",
                "status": "available",
                "urgency": "Ends today",
                "expires_at_utc": "2026-06-27T12:00:00Z",
                "time_remaining_human": "2h",
            }
        ],
    }

    class TtyStringIO(io.StringIO):
        def isatty(self) -> bool:
            return True

    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = TtyStringIO()
    print_text(result, stream=stream)
    output = stream.getvalue()

    assert "\033[1mCodex reset credits" in output
    assert "\033[31mEnds today" in output

    monkeypatch.setenv("NO_COLOR", "1")
    stream_no_color = TtyStringIO()
    print_text(result, stream=stream_no_color)
    output_no_color = stream_no_color.getvalue()
    assert "\033[" not in output_no_color

    monkeypatch.delenv("NO_COLOR", raising=False)

    class NonTtyStringIO(io.StringIO):
        def isatty(self) -> bool:
            return False

    stream_non_tty = NonTtyStringIO()
    print_text(result, stream=stream_non_tty)
    output_non_tty = stream_non_tty.getvalue()
    assert "\033[" not in output_non_tty


def test_parse_retry_after() -> None:
    assert parse_retry_after(None, attempt=2) == 2.0

    assert parse_retry_after("5.5", attempt=1) == 5.5
    assert parse_retry_after("nan", attempt=2) == 2.0

    future_time = datetime.now(UTC) + timedelta(seconds=12)
    date_str = email.utils.format_datetime(future_time, usegmt=True)
    val = parse_retry_after(date_str, attempt=1)
    assert 10.0 <= val <= 14.0

    assert parse_retry_after("invalid-date-format", attempt=3) == 3.0
