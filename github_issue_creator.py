"""
GitHub Issue Creator
────────────────────
Creates an issue in a GitHub repository via the REST API v3.

API endpoint: POST /repos/{owner}/{repo}/issues
Requires: GITHUB_TOKEN environment variable (classic PAT or fine-grained token
          with at least "issues: write" permission).

Usage:
    from github_issue_creator import create_issue

    url = create_issue("owner", "repo", "Fix the thing", "Details...")
    print(url)  # https://api.github.com/repos/owner/repo/issues/42
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ── Constants ──────────────────────────────────────────────────────────────────

GITHUB_API_BASE = "https://api.github.com"
ENV_TOKEN = "GITHUB_TOKEN"
USER_AGENT = "hermes-github-issue-creator/1.0"

# ── Exceptions ─────────────────────────────────────────────────────────────────


class GitHubAPIError(Exception):
    """Base exception for GitHub API errors."""


class AuthenticationError(GitHubAPIError):
    """Invalid or missing GITHUB_TOKEN."""


class RateLimitError(GitHubAPIError):
    """GitHub API rate limit has been exceeded."""


class ValidationError(GitHubAPIError):
    """The request payload failed validation (e.g. missing required fields)."""


class NotFoundError(GitHubAPIError):
    """The repository or resource was not found."""


class NetworkError(GitHubAPIError):
    """A network-level error occurred (DNS, connection refused, timeout)."""


# ── Helpers ────────────────────────────────────────────────────────────────────


def _resolve_token(token: str | None = None) -> str:
    """Resolve the GitHub token from argument or environment.

    Args:
        token: Explicit token (optional).

    Returns:
        The token string.

    Raises:
        AuthenticationError: No token found anywhere.
    """
    if token:
        return token
    env_token = os.environ.get(ENV_TOKEN)
    if env_token:
        return env_token
    raise AuthenticationError(
        f"No GitHub token provided. Set the {ENV_TOKEN} environment variable "
        "or pass a token explicitly."
    )


def _build_url(owner: str, repo: str) -> str:
    """Build the issue creation endpoint URL.

    Args:
        owner: Repository owner (user or organisation).
        repo: Repository name.

    Returns:
        Full API URL for creating issues.
    """
    return f"{GITHUB_API_BASE}/repos/{owner}/{repo}/issues"


def _make_headers(token: str) -> dict[str, str]:
    """Build HTTP headers for a GitHub API request.

    Args:
        token: GitHub personal access token.

    Returns:
        Header dictionary.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
    }


def _parse_rate_limit_headers(headers: Any) -> dict[str, Any]:
    """Extract rate-limit information from response headers.

    Args:
        headers: The response header object (dict-like).

    Returns:
        Dictionary with 'remaining', 'limit', 'reset_timestamp'.
        Returns empty dict if headers are absent.
    """
    remaining = headers.get("X-RateLimit-Remaining")
    limit = headers.get("X-RateLimit-Limit")
    reset = headers.get("X-RateLimit-Reset")

    info: dict[str, Any] = {}
    if remaining is not None:
        info["remaining"] = int(remaining)
    if limit is not None:
        info["limit"] = int(limit)
    if reset is not None:
        info["reset_timestamp"] = int(reset)
    return info


def _is_rate_limited(headers: Any, status: int) -> bool:
    """Check whether a response indicates rate limiting.

    Args:
        headers: The response header object.
        status: HTTP status code.

    Returns:
        True if the status is 403 or 429 AND the rate-limit remaining is 0.
    """
    if status not in (403, 429):
        return False
    remaining = headers.get("X-RateLimit-Remaining")
    if remaining is not None and int(remaining) == 0:
        return True
    # GitHub sends 429 with a Retry-After header when the secondary rate limit
    # has been hit, even if the primary rate limit is not exhausted.
    if status == 429:
        return True
    return False


def _extract_error_message(body: bytes) -> str:
    """Extract a human-readable error message from a GitHub API error response.

    Args:
        body: Raw response body bytes.

    Returns:
        A message string, or a fallback if parsing fails.
    """
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return body.decode("utf-8", errors="replace")

    # GitHub errors come in several shapes:
    #   {"message": "...", "errors": [...]}
    #   {"error": "..."}
    messages: list[str] = []
    if isinstance(data, dict):
        if "message" in data:
            messages.append(str(data["message"]))
        if "error" in data:
            messages.append(str(data["error"]))
        if "errors" in data and isinstance(data["errors"], list):
            for err in data["errors"]:
                if isinstance(err, dict):
                    parts = [
                        err.get("resource", ""),
                        err.get("field", ""),
                        err.get("code", ""),
                        err.get("message", ""),
                    ]
                    msg = ": ".join(p for p in parts if p)
                    if msg:
                        messages.append(msg)
                elif isinstance(err, str):
                    messages.append(err)
    return " | ".join(messages) if messages else body.decode("utf-8", errors="replace")


# ── Main function ──────────────────────────────────────────────────────────────


def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: str = "",
    labels: list[str] | None = None,
    token: str | None = None,
) -> str:
    """Create an issue in a GitHub repository.

    Args:
        owner: Repository owner (user or organisation).
        repo: Repository name.
        title: Issue title (required).
        body: Issue body text (optional).
        labels: List of label names to apply (optional).
        token: GitHub personal access token. Falls back to GITHUB_TOKEN env var.

    Returns:
        The URL of the newly created issue.

    Raises:
        AuthenticationError: Token is missing or invalid (401).
        RateLimitError: API rate limit exceeded (403 with no remaining / 429).
        NotFoundError: Repository not found (404).
        ValidationError: Request payload invalid (422).
        GitHubAPIError: Any other API error.
        NetworkError: A network-level failure.
    """
    resolved_token = _resolve_token(token)
    url = _build_url(owner, repo)
    headers = _make_headers(resolved_token)

    # Build payload
    payload: dict[str, Any] = {"title": title}
    if body:
        payload["body"] = body
    if labels:
        payload["labels"] = labels

    data = json.dumps(payload).encode("utf-8")

    request = Request(url, data=data, headers=headers, method="POST")

    try:
        with urlopen(request, timeout=30) as response:
            response_body = response.read()
    except HTTPError as exc:
        status = exc.code
        err_body = exc.read()
        err_msg = _extract_error_message(err_body)

        # Check rate limiting from the error response headers
        if _is_rate_limited(exc.headers, status):
            reset_info = _parse_rate_limit_headers(exc.headers)
            reset_ts = reset_info.get("reset_timestamp")
            raise RateLimitError(
                f"GitHub API rate limit exceeded. "
                f"{'Resets at timestamp ' + str(reset_ts) if reset_ts else 'Try again later.'}"
                f" Message: {err_msg}"
            ) from exc

        if status == 401:
            raise AuthenticationError(
                f"GitHub authentication failed (401). "
                f"Check that your GITHUB_TOKEN is valid and has 'issues: write' scope. "
                f"Details: {err_msg}"
            ) from exc
        if status == 403:
            raise GitHubAPIError(
                f"GitHub API returned 403 Forbidden. "
                f"You may lack permission to create issues in this repository. "
                f"Details: {err_msg}"
            ) from exc
        if status == 404:
            raise NotFoundError(
                f"Repository '{owner}/{repo}' not found (404). "
                f"Check that the owner and repo name are correct. "
                f"Details: {err_msg}"
            ) from exc
        if status == 422:
            raise ValidationError(
                f"GitHub API validation error (422). "
                f"The issue payload was rejected. "
                f"Details: {err_msg}"
            ) from exc

        raise GitHubAPIError(
            f"GitHub API returned HTTP {status}. Details: {err_msg}"
        ) from exc
    except URLError as exc:
        reason = str(exc.reason) if hasattr(exc, "reason") and exc.reason else "Unknown"
        raise NetworkError(
            f"Network error while contacting the GitHub API: {reason}"
        ) from exc
    except TimeoutError as exc:
        raise NetworkError(
                        "Request to GitHub API timed out after 30 seconds."
                    ) from exc

    # Parse response to extract issue URL
    try:
        issue_data: dict[str, Any] = json.loads(response_body)
    except (json.JSONDecodeError, ValueError) as exc:
        raise GitHubAPIError(f"Invalid JSON in GitHub API response: {exc}") from exc

    issue_url: str | None = issue_data.get("html_url")
    if not issue_url:
        raise GitHubAPIError(
            "GitHub API response did not contain an issue URL. "
            f"Response: {response_body.decode('utf-8', errors='replace')[:500]}"
        )

    return issue_url
