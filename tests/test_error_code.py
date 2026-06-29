"""Unit tests for `_error_code`, the last_error_code classifier.

`_error_code` is a pure function: it maps an exception to the short string the
log line + the `last_error_code` diagnostic sensor report. These tests pin two
things:

1. HTTP status codes pytoyoda embeds as "Request Failed. <code>, <body>." are
   surfaced as "HTTP <code>" (incl. 403/401, which used to fall through to the
   generic "api error" label and masked the 2026-06-28 rate-limit incident).
2. `THROTTLE_HTTP_CODES` — the subset Phase B keys adaptive backoff off — is
   exactly {403, 429}: a throttle, NOT 401 (auth) and NOT 5xx (server-side).
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from custom_components.toyota import (
    THROTTLE_HTTP_CODES,
    _error_code,
)
from pytoyoda.exceptions import ToyotaApiError, ToyotaLoginError


def _api_error(status: str) -> ToyotaApiError:
    """Build a ToyotaApiError shaped like pytoyoda controller.request_raw."""
    return ToyotaApiError(
        f'Request Failed. {status}, {{"status": {{"messages": '
        f'[{{"responseCode": "APIGW-{status}", "description": "Unauthorized"}}]}}}}.'
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("401", "HTTP 401"),
        ("403", "HTTP 403"),
        ("429", "HTTP 429"),
        ("500", "HTTP 500"),
        ("502", "HTTP 502"),
        ("503", "HTTP 503"),
        ("504", "HTTP 504"),
    ],
)
def test_http_status_extracted_from_api_error(status: str, expected: str) -> None:
    """A 'Request Failed. <code>,' ToyotaApiError is labelled 'HTTP <code>'."""
    assert _error_code(_api_error(status)) == expected


def test_403_was_the_masked_case() -> None:
    """Regression guard: the exact incident shape resolves to HTTP 403.

    Before this fix the 403 fell through to 'api error', which is why the
    poll-path throttle was invisible and the incident read as climate-only.
    """
    assert _error_code(_api_error("403")) == "HTTP 403"
    assert _error_code(_api_error("403")) != "api error"


def test_api_error_without_status_falls_back_to_label() -> None:
    """A ToyotaApiError with no 'Request Failed. <code>,' is the generic label."""
    assert _error_code(ToyotaApiError("something else went wrong")) == "api error"


def test_login_error_is_not_misread_as_http() -> None:
    """An auth-endpoint 401/403 is a ToyotaLoginError with a different message.

    Its "Authentication Failed. 401," text must NOT match the "Request Failed."
    extractor — it falls through to the 'login error' label, so Phase B never
    backs off on what is actually a reauth condition.
    """
    err = ToyotaLoginError("Authentication Failed. 401, denied.")
    assert _error_code(err) == "login error"


def test_timeout_families_labelled() -> None:
    """Non-HTTP transient families keep their existing labels."""
    assert _error_code(httpx.ReadTimeout("slow")) == "read timeout"
    assert _error_code(asyncio.TimeoutError()) == "read timeout"
    assert _error_code(httpx.ConnectTimeout("no route")) == "connect timeout"


def test_unknown_exception_returns_type_name() -> None:
    """An unmapped exception degrades to its class name, not a crash."""
    assert _error_code(ValueError("nope")) == "ValueError"


def test_throttle_set_is_exactly_403_and_429() -> None:
    """Phase B's backoff trigger set: throttle codes only, not auth/server."""
    assert THROTTLE_HTTP_CODES == frozenset({"HTTP 403", "HTTP 429"})
    assert _error_code(_api_error("403")) in THROTTLE_HTTP_CODES
    assert _error_code(_api_error("429")) in THROTTLE_HTTP_CODES
    # Not throttles: must NOT trigger backoff.
    assert _error_code(_api_error("401")) not in THROTTLE_HTTP_CODES
    assert _error_code(_api_error("500")) not in THROTTLE_HTTP_CODES
