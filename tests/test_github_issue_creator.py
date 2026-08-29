"""Unit tests for the GitHub issue creator."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from github_issue_creator import (
    AuthenticationError,
    GitHubAPIError,
    NetworkError,
    NotFoundError,
    RateLimitError,
    ValidationError,
    create_issue,
    _extract_error_message,
    _is_rate_limited,
    _make_headers,
    _parse_rate_limit_headers,
    _resolve_token,
)

# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_env():
    """Ensure GITHUB_TOKEN env var is absent unless set by the test."""
    old_token = os.environ.pop("GITHUB_TOKEN", None)
    yield
    if old_token is not None:
        os.environ["GITHUB_TOKEN"] = old_token


# ── _resolve_token tests ───────────────────────────────────────────────────────


def test_resolve_token_from_env():
    os.environ["GITHUB_TOKEN"] = "ghp_env_token_123"
    assert _resolve_token() == "ghp_env_token_123"


def test_resolve_token_explicit():
    assert _resolve_token("explicit_token") == "explicit_token"


def test_resolve_token_explicit_overrides_env():
    os.environ["GITHUB_TOKEN"] = "env_token"
    assert _resolve_token("explicit_token") == "explicit_token"


def test_resolve_token_missing():
    with pytest.raises(AuthenticationError, match="token"):
        _resolve_token()


def test_resolve_token_trimmed():
    """Whitespace should be preserved as-is (GitHub tokens are sensitive)."""
    assert _resolve_token("  ghp_abc  ") == "  ghp_abc  "


# ── _make_headers tests ────────────────────────────────────────────────────────


def test_make_headers_contains_auth():
    headers = _make_headers("mytoken")
    assert headers["Authorization"] == "Bearer mytoken"


def test_make_headers_accept():
    headers = _make_headers("t")
    assert "application/vnd.github+json" in headers["Accept"]


def test_make_headers_content_type():
    headers = _make_headers("t")
    assert headers["Content-Type"] == "application/json"


def test_make_headers_user_agent():
    headers = _make_headers("t")
    assert headers["User-Agent"].startswith("hermes-")


# ── _parse_rate_limit_headers tests ────────────────────────────────────────────


class FakeHeaders(dict):
    """Dict subclass that also supports .get() like response headers."""

    def get(self, key: str, default: Any = None) -> Any:
        return super().get(key, default)


def test_parse_rate_limit_all_present():
    headers = FakeHeaders(
        {
            "X-RateLimit-Remaining": "42",
            "X-RateLimit-Limit": "5000",
            "X-RateLimit-Reset": "1712345678",
        }
    )
    info = _parse_rate_limit_headers(headers)
    assert info["remaining"] == 42
    assert info["limit"] == 5000
    assert info["reset_timestamp"] == 1712345678


def test_parse_rate_limit_partial():
    headers = FakeHeaders({"X-RateLimit-Remaining": "0"})
    info = _parse_rate_limit_headers(headers)
    assert info["remaining"] == 0
    assert "limit" not in info


def test_parse_rate_limit_empty():
    info = _parse_rate_limit_headers(FakeHeaders())
    assert info == {}


# ── _is_rate_limited tests ─────────────────────────────────────────────────────


def test_rate_limited_403_zero_remaining():
    headers = FakeHeaders({"X-RateLimit-Remaining": "0"})
    assert _is_rate_limited(headers, 403) is True


def test_rate_limited_429_no_remaining_header():
    headers = FakeHeaders()
    assert _is_rate_limited(headers, 429) is True


def test_rate_limited_403_has_remaining():
    headers = FakeHeaders({"X-RateLimit-Remaining": "5"})
    assert _is_rate_limited(headers, 403) is False


def test_rate_limited_non_rate_status():
    headers = FakeHeaders({"X-RateLimit-Remaining": "0"})
    assert _is_rate_limited(headers, 401) is False
    assert _is_rate_limited(headers, 404) is False
    assert _is_rate_limited(headers, 422) is False


# ── _extract_error_message tests ───────────────────────────────────────────────


def test_extract_message_simple():
    body = json.dumps({"message": "Not Found"}).encode()
    assert _extract_error_message(body) == "Not Found"


def test_extract_message_with_errors_array():
    body = json.dumps(
        {
            "message": "Validation Failed",
            "errors": [
                {"resource": "Issue", "field": "title", "code": "missing_field"},
            ],
        }
    ).encode()
    result = _extract_error_message(body)
    assert "title" in result
    assert "missing_field" in result


def test_extract_message_with_error_field():
    body = json.dumps({"error": "Repository access blocked"}).encode()
    assert _extract_error_message(body) == "Repository access blocked"


def test_extract_message_unparseable():
    body = b"not json at all"
    result = _extract_error_message(body)
    assert "not json" in result


def test_extract_message_empty():
    result = _extract_error_message(b"")
    assert result == ""


# ── create_issue: success cases ────────────────────────────────────────────────


def _make_success_response(
    issue_number: int = 42,
    html_url: str = "https://github.com/owner/repo/issues/42",
) -> MagicMock:
    data = {
        "id": 123456789,
        "number": issue_number,
        "title": "Test issue",
        "state": "open",
        "html_url": html_url,
        "url": f"https://api.github.com/repos/owner/repo/issues/{issue_number}",
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode("utf-8")
    return mock_resp


def _make_mock_response(data: dict[str, Any]) -> MagicMock:
    """Create a mock response object from a dict."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(data).encode("utf-8")
    return mock_resp


@patch("github_issue_creator.urlopen")
def test_create_issue_basic(mock_urlopen):
    """Create a basic issue with just title and body."""
    os.environ["GITHUB_TOKEN"] = "ghp_test_token"
    mock_resp = _make_success_response(42)
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    url = create_issue("owner", "repo", "Test issue", body="Body text")

    assert url == "https://github.com/owner/repo/issues/42"

    # Verify the request was built correctly
    call_args, call_kwargs = mock_urlopen.call_args
    request = call_args[0]
    assert isinstance(request, __import__("urllib").request.Request)

    # Verify method
    assert request.method == "POST"

    # Verify URL
    assert "api.github.com/repos/owner/repo/issues" in request.full_url

    # Verify auth header
    assert request.headers["Authorization"] == "Bearer ghp_test_token"

    # Verify payload
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["title"] == "Test issue"
    assert payload["body"] == "Body text"
    assert "labels" not in payload


@patch("github_issue_creator.urlopen")
def test_create_issue_with_labels(mock_urlopen):
    """Create an issue with labels attached."""
    mock_resp = _make_success_response(
        issue_number=101,
        html_url="https://github.com/owner/repo/issues/101",
    )
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    url = create_issue(
        "owner",
        "repo",
        "Bug: login broken",
        body="Details here",
        labels=["bug", "coolify-error"],
        token="ghp_explicit",
    )

    assert url == "https://github.com/owner/repo/issues/101"

    call_args, _ = mock_urlopen.call_args
    request = call_args[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["labels"] == ["bug", "coolify-error"]
    assert payload["title"] == "Bug: login broken"
    assert payload["body"] == "Details here"

    # Verify explicit token was used
    assert request.headers["Authorization"] == "Bearer ghp_explicit"


@patch("github_issue_creator.urlopen")
def test_create_issue_minimal(mock_urlopen):
    """Create an issue with only a title, no body."""
    mock_resp = _make_success_response(1)
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    create_issue("owner", "repo", "Minimal", token="t")

    call_args, _ = mock_urlopen.call_args
    request = call_args[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["title"] == "Minimal"
    assert "body" not in payload


@patch("github_issue_creator.urlopen")
def test_create_issue_empty_body(mock_urlopen):
    """Body='' should not be included in the payload."""
    mock_resp = _make_success_response(2)
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    create_issue("owner", "repo", "Title", body="", token="t")

    call_args, _ = mock_urlopen.call_args
    request = call_args[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["title"] == "Title"
    assert "body" not in payload


@patch("github_issue_creator.urlopen")
def test_create_issue_empty_labels(mock_urlopen):
    """An empty labels list should not be in the payload."""
    mock_resp = _make_success_response(3)
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    create_issue("owner", "repo", "Title", token="t", labels=[])

    call_args, _ = mock_urlopen.call_args
    request = call_args[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert "labels" not in payload


# ── create_issue: error cases ──────────────────────────────────────────────────


def test_create_issue_401():
    """Invalid token should raise AuthenticationError."""
    from urllib.error import HTTPError

    mock_fp = MagicMock()
    mock_fp.read.return_value = b'{"message": "Bad credentials"}'

    exc = HTTPError(
        "http://api.github.com/repos/o/r/issues",
        401,
        "Unauthorized",
        {},
        mock_fp,
    )

    with patch("github_issue_creator.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = exc

        with pytest.raises(AuthenticationError, match="Bad credentials"):
            create_issue("o", "r", "Title", token="bad")


def test_create_issue_404():
    """Non-existent repo should raise NotFoundError."""
    from urllib.error import HTTPError

    mock_fp = MagicMock()
    mock_fp.read.return_value = b'{"message": "Not Found"}'

    exc = HTTPError(
        "http://api.github.com/repos/no/such",
        404,
        "Not Found",
        {},
        mock_fp,
    )

    with patch("github_issue_creator.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = exc

        with pytest.raises(NotFoundError, match="Not Found"):
            create_issue("no", "such", "Title", token="t")


def test_create_issue_422():
    """Invalid payload should raise ValidationError."""
    from urllib.error import HTTPError

    body = json.dumps(
        {
            "message": "Validation Failed",
            "errors": [{"resource": "Issue", "field": "title", "code": "missing"}],
        }
    ).encode()

    mock_fp = MagicMock()
    mock_fp.read.return_value = body

    exc = HTTPError(
        "http://api.github.com/repos/o/r/issues",
        422,
        "Unprocessable Entity",
        {},
        mock_fp,
    )

    with patch("github_issue_creator.urlopen") as mock_urlopen:
        mock_urlopen.side_effect = exc

        with pytest.raises(ValidationError, match="title"):
            create_issue("o", "r", "", token="t")


@patch("github_issue_creator.urlopen")
def test_create_issue_rate_limit_403(mock_urlopen):
    """Rate limit exceeded (403 with 0 remaining) should raise RateLimitError."""
    from urllib.error import HTTPError

    headers = {
        "X-RateLimit-Remaining": "0",
        "X-RateLimit-Limit": "5000",
        "X-RateLimit-Reset": "1712345678",
    }
    body = b'{"message": "API rate limit exceeded"}'

    class FakeFPRL:
        def read(self):
            return body

        def close(self):
            pass

    exc = HTTPError(
        "http://api.github.com/repos/o/r/issues",
        403,
        "Forbidden",
        headers,
        FakeFPRL(),
    )
    if hasattr(exc, "headers"):
        exc.headers = headers

    mock_urlopen.side_effect = exc

    with pytest.raises(RateLimitError, match="rate limit"):
        create_issue("o", "r", "Title", token="t")


@patch("github_issue_creator.urlopen")
def test_create_issue_rate_limit_429(mock_urlopen):
    """Secondary rate limit (429) should raise RateLimitError."""
    from urllib.error import HTTPError

    body = b'{"message": "Too many requests"}'

    class FakeFPRL2:
        def read(self):
            return body

        def close(self):
            pass

    exc = HTTPError(
        "http://api.github.com/repos/o/r/issues",
        429,
        "Too Many Requests",
        {},
        FakeFPRL2(),
    )

    mock_urlopen.side_effect = exc

    with pytest.raises(RateLimitError, match="rate limit"):
        create_issue("o", "r", "Title", token="t")


@patch("github_issue_creator.urlopen")
def test_create_issue_403_no_rate_limit(mock_urlopen):
    """A 403 that is not rate-limiting should raise a generic GitHubAPIError."""
    from urllib.error import HTTPError

    headers = {"X-RateLimit-Remaining": "123"}
    body = b'{"message": "Resource protected by repository rules"}'

    class FakeFP:
        def read(self):
            return body

    exc = HTTPError(
        "http://api.github.com/repos/o/r/issues",
        403,
        "Forbidden",
        headers,
        FakeFP(),
    )
    if hasattr(exc, "headers"):
        exc.headers = headers

    mock_urlopen.side_effect = exc

    with pytest.raises(GitHubAPIError) as exc_info:
        create_issue("o", "r", "Title", token="t")
    assert "403" in str(exc_info.value)
    assert "RateLimitError" not in type(exc_info.value).__name__


@patch("github_issue_creator.urlopen")
def test_create_issue_network_error(mock_urlopen):
    """A URLError should be wrapped in NetworkError."""
    from urllib.error import URLError

    mock_urlopen.side_effect = URLError(reason="Name or service not known")

    with pytest.raises(NetworkError, match="Name or service not known"):
        create_issue("o", "r", "Title", token="t")


@patch("github_issue_creator.urlopen")
def test_create_issue_timeout(mock_urlopen):
    """A TimeoutError should be wrapped in NetworkError."""
    mock_urlopen.side_effect = TimeoutError("timed out")

    with pytest.raises(NetworkError, match="timed out"):
        create_issue("o", "r", "Title", token="t")


@patch("github_issue_creator.urlopen")
def test_create_issue_unexpected_status(mock_urlopen):
    """An unexpected HTTP status (e.g. 500) should raise a generic GitHubAPIError."""
    from urllib.error import HTTPError

    mock_fp = MagicMock()
    mock_fp.read.return_value = b'{"message": "Internal Server Error"}'

    exc = HTTPError(
        "http://api.github.com/repos/o/r/issues",
        500,
        "Internal Server Error",
        {},
        mock_fp,
    )

    mock_urlopen.side_effect = exc

    with pytest.raises(GitHubAPIError, match="500"):
        create_issue("o", "r", "Title", token="t")


@patch("github_issue_creator.urlopen")
def test_create_issue_empty_response(mock_urlopen):
    """An empty response should raise GitHubAPIError."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b""
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    with pytest.raises(GitHubAPIError, match="Invalid JSON"):
        create_issue("o", "r", "Title", token="t")


@patch("github_issue_creator.urlopen")
def test_create_issue_invalid_json_response(mock_urlopen):
    """Non-JSON response should raise GitHubAPIError."""
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"<html>Server Error</html>"
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    with pytest.raises(GitHubAPIError, match="Invalid JSON"):
        create_issue("o", "r", "Title", token="t")


@patch("github_issue_creator.urlopen")
def test_create_issue_missing_html_url(mock_urlopen):
    """Response without html_url should raise GitHubAPIError."""
    mock_resp = _make_mock_response({"id": 1, "number": 1, "title": "Test"})
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    with pytest.raises(GitHubAPIError, match="issue URL"):
        create_issue("o", "r", "Title", token="t")


@patch("github_issue_creator.urlopen")
def test_create_issue_token_from_env(mock_urlopen):
    """When no explicit token, GITHUB_TOKEN from env should be used."""
    os.environ["GITHUB_TOKEN"] = "ghp_from_env"
    mock_resp = _make_success_response(7)
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    create_issue("o", "r", "Issue from env", token=None)

    call_args, _ = mock_urlopen.call_args
    request = call_args[0]
    assert request.headers["Authorization"] == "Bearer ghp_from_env"


# ── Edge cases ─────────────────────────────────────────────────────────────────


@patch("github_issue_creator.urlopen")
def test_create_issue_empty_title(mock_urlopen):
    """Empty title should be sent to API (GitHub will reject it, but our code should
    not silently drop it or modify it)."""
    mock_resp = _make_success_response(1)
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    create_issue("o", "r", "", token="t")
    call_args, _ = mock_urlopen.call_args
    request = call_args[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["title"] == ""


@patch("github_issue_creator.urlopen")
def test_create_issue_special_chars_in_body(mock_urlopen):
    """Body with special characters should be handled correctly."""
    mock_resp = _make_success_response(99)
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    body = "Line 1\nLine 2\nSpecial: ñ, ü, €, 👋"
    create_issue("o", "r", "Special chars", body=body, token="t")

    call_args, _ = mock_urlopen.call_args
    request = call_args[0]
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["body"] == body


@patch("github_issue_creator.urlopen")
def test_create_issue_owner_with_special_chars(mock_urlopen):
    """Test with org/repo names containing dots or hyphens (common pattern)."""
    mock_resp = _make_success_response(42)
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    url = create_issue("my-org", "my.repo", "Title", token="t")
    assert url == "https://github.com/owner/repo/issues/42"

    call_args, _ = mock_urlopen.call_args
    request = call_args[0]
    assert "my-org" in request.full_url
    assert "my.repo" in request.full_url
