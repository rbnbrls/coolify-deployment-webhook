"""
Coolify Deployment Log Fetcher
───────────────────────────────
Fetches and analyses deployment logs from a Coolify instance via the REST API.

API endpoint: GET /api/v1/deployments/{deployment_id}
Returns the ApplicationDeploymentQueue model, which includes a `logs` field
(a JSON string containing an array of log entries).

Log entry schema:
    {
        "command":   str | null,   # command that produced this output
        "output":    str,           # the log line content
        "type":      str,           # "stdout" | "stderr"
        "timestamp": str,           # ISO-8601 datetime (UTC)
        "hidden":    bool,          # hidden from regular output
        "batch":     int,
        "order":     int
    }

Usage:
    from coolify_deployment_logs import get_failed_deployment_diagnostics

    errors = get_failed_deployment_diagnostics("deploy-uuid-here")
    for err in errors:
        print(err)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ── Constants ──────────────────────────────────────────────────────────────────

DEFAULT_BASE_URL = "http://localhost:8000"
ENV_API_TOKEN = "COOLIFY_API_TOKEN"
ENV_BASE_URL = "COOLIFY_BASE_URL"

ERROR_KEYWORDS: list[str] = [
    "error",
    "failed",
    "failure",
    "exit code",
    "exit code ",
    "exited with",
    "traceback",
    "exception",
    "warning",
    "not found",
    "cannot",
    "unable to",
    "permission denied",
    "connection refused",
    "connection reset",
    "timeout",
    "killed",
    "segmentation fault",
    "fatal",
    "build failed",
    "deployment failed",
]


# ── Exceptions ─────────────────────────────────────────────────────────────────


class CoolifyAPIError(Exception):
    """Base exception for Coolify API errors."""


class DeploymentNotFoundError(CoolifyAPIError):
    """The deployment UUID does not exist."""


class AuthenticationError(CoolifyAPIError):
    """Invalid or missing API token."""


class PermissionError_(CoolifyAPIError):
    """Token lacks required permissions (e.g., cannot read sensitive data)."""


class NetworkError(CoolifyAPIError):
    """Network-level failure (connection, timeout, DNS)."""


# ── Data classes ───────────────────────────────────────────────────────────────


@dataclass
class LogEntry:
    """A single log entry from a Coolify deployment."""

    command: str | None
    output: str
    type: str  # "stdout" | "stderr"
    timestamp: str
    hidden: bool = False
    batch: int = 1
    order: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "LogEntry":
        return cls(
            command=d.get("command"),
            output=d.get("output", ""),
            type=d.get("type", "stdout"),
            timestamp=d.get("timestamp", ""),
            hidden=d.get("hidden", False),
            batch=d.get("batch", 1),
            order=d.get("order", 0),
        )


@dataclass
class DeploymentData:
    """Parsed deployment record returned by the Coolify API."""

    deployment_uuid: str
    status: str
    application_name: str = ""
    server_name: str = ""
    commit: str = ""
    commit_message: str = ""
    logs: list[LogEntry] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "DeploymentData":
        """Build a DeploymentData from the raw API JSON response."""
        raw_logs = data.get("logs")
        log_entries: list[LogEntry] = []
        if raw_logs:
            if isinstance(raw_logs, str):
                try:
                    parsed = json.loads(raw_logs)
                except (json.JSONDecodeError, TypeError):
                    parsed = []
            elif isinstance(raw_logs, list):
                parsed = raw_logs
            else:
                parsed = []
            log_entries = [LogEntry.from_dict(e) for e in parsed]

        return cls(
            deployment_uuid=data.get("deployment_uuid", ""),
            status=data.get("status", ""),
            application_name=data.get("application_name", ""),
            server_name=data.get("server_name", ""),
            commit=data.get("commit", ""),
            commit_message=data.get("commit_message", ""),
            logs=log_entries,
            raw=data,
        )


# ── Core API client ────────────────────────────────────────────────────────────


def _resolve_config(
    base_url: str | None = None,
    api_token: str | None = None,
) -> tuple[str, str]:
    """Resolve base URL and API token from args or environment."""
    token = api_token or os.environ.get(ENV_API_TOKEN)
    if not token:
        raise CoolifyAPIError(
            f"Coolify API token not provided. Set the {ENV_API_TOKEN} "
            f"environment variable or pass `api_token=`."
        )

    url = (base_url or os.environ.get(ENV_BASE_URL) or DEFAULT_BASE_URL).rstrip("/")
    return url, token


def _make_request(url: str, api_token: str) -> dict[str, Any]:
    """Make an authenticated GET request to the Coolify API.

    Raises:
        DeploymentNotFoundError: 404
        AuthenticationError:     401
        PermissionError_:        403
        NetworkError:            connection / timeout / DNS failures
        CoolifyAPIError:         other non-2xx responses
    """
    req = Request(
        url,
        method="GET",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {api_token}",
            "User-Agent": "coolify-deployment-logs/1.0",
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            if not body.strip():
                raise CoolifyAPIError(f"Empty response from {url}")
            return json.loads(body)
    except HTTPError as e:
        if e.code == 404:
            raise DeploymentNotFoundError(f"Deployment not found at {url}") from e
        if e.code == 401:
            raise AuthenticationError(
                "Invalid or missing Coolify API token (401)"
            ) from e
        if e.code == 403:
            raise PermissionError_(
                "Token lacks required permissions (403). "
                "The token may need the 'can_read_sensitive' ability "
                "to view deployment logs."
            ) from e
        try:
            err_body = e.read().decode("utf-8")
            detail = json.loads(err_body) if err_body else {}
            msg = detail.get("message", detail.get("detail", str(e)))
        except Exception:
            msg = str(e)
        raise CoolifyAPIError(f"Coolify API returned HTTP {e.code}: {msg}") from e
    except URLError as e:
        # Network-level errors (connection refused, timeout, DNS)
        reason = str(e.reason) if hasattr(e, "reason") else str(e)
        raise NetworkError(f"Network error contacting Coolify API: {reason}") from e
    except (OSError, ConnectionError, TimeoutError) as e:
        raise NetworkError(f"Network error: {e}") from e
    except json.JSONDecodeError as e:
        raise CoolifyAPIError(f"Invalid JSON response from Coolify API: {e}") from e


# ── Public API ─────────────────────────────────────────────────────────────────


def fetch_deployment(
    deployment_id: str,
    base_url: str | None = None,
    api_token: str | None = None,
) -> DeploymentData:
    """Fetch a single deployment (with logs if accessible) from the Coolify API.

    Args:
        deployment_id: The deployment UUID (e.g. ``"cm37r6cqj000008jm0veg5tkm"``).
        base_url:      Coolify instance base URL (e.g. ``"https://coolify.example.com"``).
                       Falls back to ``COOLIFY_BASE_URL`` env var, then
                       ``http://localhost:8000``.
        api_token:     Coolify API bearer token. Falls back to ``COOLIFY_API_TOKEN``
                       env var.

    Returns:
        A ``DeploymentData`` object with parsed fields and log entries.

    Raises:
        DeploymentNotFoundError: Deployment UUID does not exist.
        AuthenticationError:     API token is invalid or missing.
        PermissionError_:        Token lacks ``can_read_sensitive`` permission,
                                 so the ``logs`` field is hidden.
        NetworkError:            Connection / DNS / timeout failures.
        CoolifyAPIError:         Other API errors.
    """
    base, token = _resolve_config(base_url, api_token)
    url = f"{base}/api/v1/deployments/{deployment_id}"
    data = _make_request(url, token)

    # The API returns the model directly (not wrapped in a "data" envelope)
    # for single-resource endpoints.
    deployment = DeploymentData.from_api_response(data)

    # If logs are empty but status indicates a failure, flag it
    if not deployment.logs and deployment.status in (
        "failed",
        "cancelled",
        "cancelled-by-user",
    ):
        # Logs may be hidden due to insufficient token permissions
        pass  # we surface this later via a note

    return deployment


def extract_error_logs(
    deployment: DeploymentData,
    *,
    include_warnings: bool = False,
    min_batch: int | None = None,
) -> list[str]:
    """Extract error-related log lines from a deployment's logs.

    Filters entries whose ``type`` is ``"stderr"`` or whose ``output``
    matches common error/failure keywords.

    Args:
        deployment:      The deployment data (from ``fetch_deployment``).
        include_warnings: Also include lines matching warning-level keywords.
        min_batch:       Only consider entries from batches >= this value.
                         Helpful to skip early setup noise when the last batch
                         is the one that failed.

    Returns:
        List of error message strings, each prefixed with its timestamp
        and log type.
    """
    errors: list[str] = []

    for entry in deployment.logs:
        if entry.hidden:
            continue
        if min_batch is not None and entry.batch < min_batch:
            continue

        output_lower = entry.output.lower().strip()
        if not output_lower:
            continue

        is_error = entry.type == "stderr"

        if not is_error and include_warnings:
            is_error = any(kw in output_lower for kw in ERROR_KEYWORDS)

        if not is_error and not include_warnings:
            # Even for stdout lines, check for obvious failure markers
            is_error = any(kw in output_lower for kw in ERROR_KEYWORDS)

        if is_error:
            ts = entry.timestamp or "(no timestamp)"
            errors.append(f"[{ts}] [{entry.type}] {entry.output.strip()}")

    return errors


def get_log_summary(deployment: DeploymentData) -> dict[str, Any]:
    """Return a concise summary of a deployment's log contents.

    Useful for diagnostics without dumping every line.
    """
    total = len(deployment.logs)
    stderr_count = sum(
        1 for e in deployment.logs if e.type == "stderr" and not e.hidden
    )
    stdout_count = sum(
        1 for e in deployment.logs if e.type == "stdout" and not e.hidden
    )
    hidden_count = sum(1 for e in deployment.logs if e.hidden)

    # Find the last batch number
    last_batch = max((e.batch for e in deployment.logs), default=0)

    # Detect if logs appear truncated (no status in final entries)
    no_logs = total == 0

    return {
        "deployment_uuid": deployment.deployment_uuid,
        "status": deployment.status,
        "application": deployment.application_name,
        "server": deployment.server_name,
        "commit": deployment.commit,
        "commit_message": deployment.commit_message,
        "total_log_entries": total,
        "stdout_entries": stdout_count,
        "stderr_entries": stderr_count,
        "hidden_entries": hidden_count,
        "last_batch": last_batch,
        "no_logs_available": no_logs,
        "logs_accessible": not no_logs,
    }


def get_failed_deployment_diagnostics(
    deployment_id: str,
    base_url: str | None = None,
    api_token: str | None = None,
    *,
    include_warnings: bool = False,
) -> list[str]:
    """High-level helper: fetch a deployment and return its error log lines.

    This is the primary entry point for the use case described in the task.

    Args:
        deployment_id:     Deployment UUID.
        base_url:          Coolify base URL (optional — env fallback).
        api_token:         API token (optional — env fallback).
        include_warnings:  Also flag warning-level lines.

    Returns:
        List of error message strings. Empty list means no errors were found
        (or logs were inaccessible).
    """
    deployment = fetch_deployment(deployment_id, base_url, api_token)

    if not deployment.logs:
        return [
            f"No logs available for deployment {deployment_id}. "
            "The API token may need the 'can_read_sensitive' permission "
            "to view deployment logs, or the deployment may have no logs yet."
        ]

    errors = extract_error_logs(deployment, include_warnings=include_warnings)

    if not errors and deployment.status == "failed":
        errors.append(
            f"Deployment {deployment_id} failed (status: {deployment.status}) "
            "but no error lines were detected in the logs."
        )

    return errors


def get_deployment_status(
    deployment_id: str,
    base_url: str | None = None,
    api_token: str | None = None,
) -> str:
    """Quick check: return just the deployment status string."""
    deployment = fetch_deployment(deployment_id, base_url, api_token)
    return deployment.status


# ── CLI entry point ────────────────────────────────────────────────────────────


def main() -> None:
    """CLI usage: python coolify_deployment_logs.py <deployment-uuid>"""
    import sys

    if len(sys.argv) < 2:
        print(
            "Usage: python coolify_deployment_logs.py <deployment-uuid>",
            file=sys.stderr,
        )
        print(
            "       COOLIFY_API_TOKEN and COOLIFY_BASE_URL are read from env.",
            file=sys.stderr,
        )
        sys.exit(1)

    deployment_id = sys.argv[1]

    try:
        errors = get_failed_deployment_diagnostics(deployment_id)
    except CoolifyAPIError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if not errors:
        print("No errors found in deployment logs.")
        sys.exit(0)

    print(f"Found {len(errors)} error(s) in deployment {deployment_id}:\n")
    for err in errors:
        print(err)

    sys.exit(0 if all("No logs available" in e for e in errors) else 1)


if __name__ == "__main__":
    main()
