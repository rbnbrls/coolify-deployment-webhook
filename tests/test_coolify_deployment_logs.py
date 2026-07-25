"""Unit tests for the Coolify deployment log fetcher."""

from __future__ import annotations

import json
import os
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from coolify_deployment_logs import (
    AuthenticationError,
    CoolifyAPIError,
    DeploymentData,
    DeploymentNotFoundError,
    LogEntry,
    NetworkError,
    PermissionError_,
    extract_error_logs,
    fetch_deployment,
    get_failed_deployment_diagnostics,
    get_log_summary,
    _resolve_config,
)


# ── Fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def clear_env():
    """Ensure COOLIFY_* env vars are absent unless set by the test."""
    old_token = os.environ.pop("COOLIFY_API_TOKEN", None)
    old_url = os.environ.pop("COOLIFY_BASE_URL", None)
    yield
    if old_token is not None:
        os.environ["COOLIFY_API_TOKEN"] = old_token
    if old_url is not None:
        os.environ["COOLIFY_BASE_URL"] = old_url


def make_log_entry(
    output: str,
    type_: str = "stdout",
    command: str | None = None,
    hidden: bool = False,
    batch: int = 1,
    order: int = 1,
    timestamp: str = "2026-07-23T12:00:00Z",
) -> dict[str, Any]:
    return {
        "command": command,
        "output": output,
        "type": type_,
        "timestamp": timestamp,
        "hidden": hidden,
        "batch": batch,
        "order": order,
    }


def make_deployment_response(
    status: str = "failed",
    logs: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "deployment_uuid": "test-uuid-12345",
        "status": status,
        "application_name": "my-app",
        "server_name": "my-server",
        "commit": "abc123",
        "commit_message": "Fix the thing",
        "logs": json.dumps(logs) if logs is not None else None,
    }
    data.update(overrides)
    return data


# ── _resolve_config tests ────────────────────────────────────────────────────


def test_resolve_config_from_env():
    os.environ["COOLIFY_API_TOKEN"] = "env-token"
    os.environ["COOLIFY_BASE_URL"] = "https://coolify.example.com"
    url, token = _resolve_config()
    assert token == "env-token"
    assert url == "https://coolify.example.com"


def test_resolve_config_from_args():
    url, token = _resolve_config(
        base_url="https://custom.example.com",
        api_token="custom-token",
    )
    assert token == "custom-token"
    assert url == "https://custom.example.com"


def test_resolve_config_args_override_env():
    os.environ["COOLIFY_API_TOKEN"] = "env-token"
    os.environ["COOLIFY_BASE_URL"] = "https://env.example.com"
    url, token = _resolve_config(
        base_url="https://override.example.com",
        api_token="override-token",
    )
    assert token == "override-token"
    assert url == "https://override.example.com"


def test_resolve_config_missing_token():
    os.environ.pop("COOLIFY_API_TOKEN", None)
    with pytest.raises(CoolifyAPIError, match="token"):
        _resolve_config()


def test_resolve_config_default_url():
    os.environ["COOLIFY_API_TOKEN"] = "token"
    os.environ.pop("COOLIFY_BASE_URL", None)
    url, token = _resolve_config()
    assert url == "http://localhost:8000"


def test_resolve_config_trailing_slash():
    os.environ["COOLIFY_API_TOKEN"] = "token"
    url, token = _resolve_config(base_url="https://coolify.example.com/")
    assert url == "https://coolify.example.com"


# ── fetch_deployment tests ───────────────────────────────────────────────────


@patch("coolify_deployment_logs.urlopen")
def test_fetch_deployment_success(mock_urlopen):
    logs = [
        make_log_entry("Cloning repository...", "stdout", order=1),
        make_log_entry("Building Docker image...", "stdout", order=2),
        make_log_entry("Error: build failed", "stderr", order=3),
        make_log_entry("npm ERR! code ELIFECYCLE", "stderr", order=4),
    ]
    resp_data = make_deployment_response(status="failed", logs=logs)

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(resp_data).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    deployment = fetch_deployment("test-uuid-12345", api_token="valid-token")

    assert deployment.deployment_uuid == "test-uuid-12345"
    assert deployment.status == "failed"
    assert deployment.application_name == "my-app"
    assert deployment.commit == "abc123"
    assert len(deployment.logs) == 4
    assert deployment.logs[0].output == "Cloning repository..."
    assert deployment.logs[2].type == "stderr"


@patch("coolify_deployment_logs.urlopen")
def test_fetch_deployment_no_logs_field(mock_urlopen):
    """Logs field may be null/none when token lacks can_read_sensitive."""
    resp_data = make_deployment_response(status="failed", logs=None)

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(resp_data).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    deployment = fetch_deployment("test-uuid", api_token="restricted-token")
    assert len(deployment.logs) == 0
    assert deployment.status == "failed"


@patch("coolify_deployment_logs.urlopen")
def test_fetch_deployment_404(mock_urlopen):
    mock_err = MagicMock()
    mock_err.code = 404
    mock_err.read.return_value = b'{"message": "Deployment not found."}'
    mock_urlopen.side_effect = __import__("urllib").error.HTTPError(
        url="http://example.com/api/v1/deployments/nonexistent",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=MagicMock(),
    )
    # We won't use the fancy mock_err; patch the mock to raise the real error
    from urllib.error import HTTPError

    class FakeFP:
        def read(self):
            return b'{"message": "Deployment not found."}'

    mock_urlopen.side_effect = HTTPError(
        "http://example.com/api/v1/deployments/nonexistent",
        404,
        "Not Found",
        {},
        FakeFP(),
    )

    with pytest.raises(DeploymentNotFoundError, match="not found"):
        fetch_deployment("nonexistent", api_token="token")


@patch("coolify_deployment_logs.urlopen")
def test_fetch_deployment_401(mock_urlopen):
    from urllib.error import HTTPError

    mock_fp = MagicMock()
    mock_fp.read.return_value = b"Unauthorized"

    mock_urlopen.side_effect = HTTPError(
        "http://example.com/api/v1/deployments/uuid",
        401,
        "Unauthorized",
        {},
        mock_fp,
    )

    with pytest.raises(AuthenticationError, match="token"):
        fetch_deployment("uuid", api_token="bad-token")


@patch("coolify_deployment_logs.urlopen")
def test_fetch_deployment_403(mock_urlopen):
    from urllib.error import HTTPError

    mock_fp = MagicMock()
    mock_fp.read.return_value = b"Forbidden"

    mock_urlopen.side_effect = HTTPError(
        "http://example.com/api/v1/deployments/uuid",
        403,
        "Forbidden",
        {},
        mock_fp,
    )

    with pytest.raises(PermissionError_, match="permission"):
        fetch_deployment("uuid", api_token="no-perm-token")


@patch("coolify_deployment_logs.urlopen")
def test_fetch_deployment_network_error(mock_urlopen):
    mock_urlopen.side_effect = __import__("urllib").error.URLError(
        reason="Connection refused"
    )

    with pytest.raises(NetworkError, match="Connection refused"):
        fetch_deployment("uuid", api_token="token")


@patch("coolify_deployment_logs.urlopen")
def test_fetch_deployment_timeout(mock_urlopen):
    mock_urlopen.side_effect = TimeoutError("timed out")

    with pytest.raises(NetworkError, match="timed out"):
        fetch_deployment("uuid", api_token="token")


@patch("coolify_deployment_logs.urlopen")
def test_fetch_deployment_empty_response(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.read.return_value = b""
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    with pytest.raises(CoolifyAPIError, match="Empty response"):
        fetch_deployment("uuid", api_token="token")


@patch("coolify_deployment_logs.urlopen")
def test_fetch_deployment_invalid_json(mock_urlopen):
    mock_resp = MagicMock()
    mock_resp.read.return_value = b"not json"
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    with pytest.raises(CoolifyAPIError, match="Invalid JSON"):
        fetch_deployment("uuid", api_token="token")


# ── extract_error_logs tests ──────────────────────────────────────────────────


def test_extract_stderr_lines():
    logs = [
        LogEntry(command=None, output="Cloning...", type="stdout", timestamp="2026-01-01T00:00:00Z"),
        LogEntry(command=None, output="Error: build failed", type="stderr", timestamp="2026-01-01T00:00:01Z"),
        LogEntry(command=None, output="npm ERR! code ELIFECYCLE", type="stderr", timestamp="2026-01-01T00:00:02Z"),
        LogEntry(command=None, output="Build succeeded", type="stdout", timestamp="2026-01-01T00:00:03Z"),
    ]
    deployment = DeploymentData(deployment_uuid="u1", status="failed", logs=logs)

    errors = extract_error_logs(deployment)

    assert len(errors) == 2
    assert "Error: build failed" in errors[0]
    assert "npm ERR!" in errors[1]


def test_extract_stdout_with_error_keywords():
    """Even stdout lines can be flagged if they contain error keywords."""
    logs = [
        LogEntry(command=None, output="Step 1/5 : FROM node:18", type="stdout", timestamp="t1"),
        LogEntry(command=None, output="failed to solve: process did not complete", type="stdout", timestamp="t2"),
        LogEntry(command=None, output="Successfully built abc123", type="stdout", timestamp="t3"),
    ]
    deployment = DeploymentData(deployment_uuid="u1", status="failed", logs=logs)

    errors = extract_error_logs(deployment)

    assert len(errors) == 1
    assert "failed to solve" in errors[0]


def test_extract_with_warnings_flag():
    """'warning' is in ERROR_KEYWORDS, so both entries are caught either way.
    Use a line with a non-keyword cautionary phrasing to test the flag.
    """
    logs = [
        LogEntry(command=None, output="caution: this might be a problem", type="stdout", timestamp="t1"),
        LogEntry(command=None, output="Error: build failed", type="stderr", timestamp="t2"),
    ]
    deployment = DeploymentData(deployment_uuid="u1", status="failed", logs=logs)

    # Without include_warnings — only stderr
    errors = extract_error_logs(deployment, include_warnings=False)
    assert len(errors) == 1
    assert "Error: build failed" in errors[0]

    # With include_warnings — also catches 'caution' (not a keyword)
    errors = extract_error_logs(deployment, include_warnings=True)
    assert len(errors) == 1  # 'caution' isn't in ERROR_KEYWORDS either

    # Actually test a warning keyword that IS a match
    logs2 = [
        LogEntry(command=None, output="warning: deprecated syntax", type="stdout", timestamp="t1"),
        LogEntry(command=None, output="Error: build failed", type="stderr", timestamp="t2"),
    ]
    deployment2 = DeploymentData(deployment_uuid="u1", status="failed", logs=logs2)

    # Without include_warnings — 'warning' keyword still matches (it's in ERROR_KEYWORDS)
    errors = extract_error_logs(deployment2, include_warnings=False)
    assert len(errors) == 2
    assert "warning" in errors[0]


def test_extract_min_batch_filter():
    logs = [
        LogEntry(command=None, output="Setup...", type="stdout", batch=1, order=1, timestamp="t1"),
        LogEntry(command=None, output="Error in build", type="stderr", batch=1, order=2, timestamp="t2"),
        LogEntry(command=None, output="Final error", type="stderr", batch=2, order=1, timestamp="t3"),
    ]
    deployment = DeploymentData(deployment_uuid="u1", status="failed", logs=logs)

    errors = extract_error_logs(deployment, min_batch=2)
    assert len(errors) == 1
    assert "Final error" in errors[0]


def test_extract_hidden_entries_skipped():
    logs = [
        LogEntry(command=None, output="Error visible", type="stderr", hidden=False, order=1, timestamp="t1"),
        LogEntry(command=None, output="Error hidden", type="stderr", hidden=True, order=2, timestamp="t2"),
    ]
    deployment = DeploymentData(deployment_uuid="u1", status="failed", logs=logs)

    errors = extract_error_logs(deployment)
    assert len(errors) == 1
    assert "Error visible" in errors[0]


def test_extract_empty_output_skipped():
    logs = [
        LogEntry(command=None, output="", type="stderr", order=1, timestamp="t1"),
        LogEntry(command=None, output="   ", type="stderr", order=2, timestamp="t2"),
    ]
    deployment = DeploymentData(deployment_uuid="u1", status="failed", logs=logs)

    errors = extract_error_logs(deployment)
    assert len(errors) == 0


def test_extract_no_logs():
    deployment = DeploymentData(deployment_uuid="u1", status="failed", logs=[])
    errors = extract_error_logs(deployment)
    assert errors == []


# ── get_failed_deployment_diagnostics tests ────────────────────────────────────


@patch("coolify_deployment_logs.fetch_deployment")
def test_diagnostics_success(mock_fetch):
    logs = [
        LogEntry(command=None, output="Error: build step failed", type="stderr", timestamp="t"),
    ]
    mock_fetch.return_value = DeploymentData(
        deployment_uuid="u1", status="failed", logs=logs
    )

    errors = get_failed_deployment_diagnostics(
        "u1", api_token="token", include_warnings=False
    )

    assert len(errors) == 1
    assert "Error: build step failed" in errors[0]


@patch("coolify_deployment_logs.fetch_deployment")
def test_diagnostics_no_logs(mock_fetch):
    mock_fetch.return_value = DeploymentData(
        deployment_uuid="u1", status="failed", logs=[]
    )

    errors = get_failed_deployment_diagnostics("u1", api_token="token")

    assert len(errors) == 1
    assert "No logs available" in errors[0]


@patch("coolify_deployment_logs.fetch_deployment")
def test_diagnostics_failed_but_no_error_lines(mock_fetch):
    logs = [
        LogEntry(command=None, output="Everything looks fine", type="stdout", timestamp="t"),
        LogEntry(command=None, output="Build complete", type="stdout", timestamp="t"),
    ]
    mock_fetch.return_value = DeploymentData(
        deployment_uuid="u1", status="failed", logs=logs
    )

    errors = get_failed_deployment_diagnostics("u1", api_token="token")

    assert len(errors) == 1
    assert "failed" in errors[0]
    assert "no error lines" in errors[0]


@patch("coolify_deployment_logs.fetch_deployment")
def test_diagnostics_successful_deployment_no_errors(mock_fetch):
    logs = [
        LogEntry(command=None, output="Build succeeded", type="stdout", timestamp="t"),
        LogEntry(command=None, output="Deploying...", type="stdout", timestamp="t"),
    ]
    mock_fetch.return_value = DeploymentData(
        deployment_uuid="u1", status="success", logs=logs
    )

    errors = get_failed_deployment_diagnostics("u1", api_token="token")
    assert errors == []


# ── get_log_summary tests ──────────────────────────────────────────────────────


def test_log_summary_counts():
    logs = [
        LogEntry(command=None, output="std1", type="stdout", hidden=False, batch=1, timestamp="t1"),
        LogEntry(command=None, output="std2", type="stdout", hidden=False, batch=1, timestamp="t2"),
        LogEntry(command=None, output="err1", type="stderr", hidden=False, batch=1, timestamp="t3"),
        LogEntry(command=None, output="hidden1", type="stderr", hidden=True, batch=1, timestamp="t4"),
    ]
    deployment = DeploymentData(
        deployment_uuid="u1",
        status="failed",
        application_name="my-app",
        logs=logs,
    )

    summary = get_log_summary(deployment)

    assert summary["deployment_uuid"] == "u1"
    assert summary["status"] == "failed"
    assert summary["application"] == "my-app"
    assert summary["total_log_entries"] == 4
    assert summary["stdout_entries"] == 2
    assert summary["stderr_entries"] == 1
    assert summary["hidden_entries"] == 1
    assert summary["logs_accessible"] is True
    assert summary["no_logs_available"] is False


def test_log_summary_no_logs():
    deployment = DeploymentData(deployment_uuid="u1", status="running", logs=[])
    summary = get_log_summary(deployment)
    assert summary["total_log_entries"] == 0
    assert summary["logs_accessible"] is False
    assert summary["no_logs_available"] is True


# ── LogEntry.from_dict tests ──────────────────────────────────────────────────


def test_log_entry_from_dict_full():
    entry = LogEntry.from_dict({
        "command": "npm run build",
        "output": "Error: build failed",
        "type": "stderr",
        "timestamp": "2026-07-23T12:00:00Z",
        "hidden": False,
        "batch": 2,
        "order": 5,
    })
    assert entry.command == "npm run build"
    assert entry.output == "Error: build failed"
    assert entry.type == "stderr"
    assert entry.timestamp == "2026-07-23T12:00:00Z"
    assert entry.hidden is False
    assert entry.batch == 2
    assert entry.order == 5


def test_log_entry_from_dict_minimal():
    entry = LogEntry.from_dict({
        "output": "Hello",
        "type": "stdout",
        "timestamp": "",
    })
    assert entry.output == "Hello"
    assert entry.type == "stdout"
    assert entry.command is None
    assert entry.hidden is False
    assert entry.batch == 1
    assert entry.order == 0


# ── DeploymentData.from_api_response tests ────────────────────────────────────


def test_deployment_data_from_api_json_logs():
    logs_data = [
        {"output": "line1", "type": "stdout", "timestamp": "t1"},
        {"output": "line2", "type": "stderr", "timestamp": "t2"},
    ]
    api_response = {
        "deployment_uuid": "u1",
        "status": "failed",
        "logs": json.dumps(logs_data),
    }

    dd = DeploymentData.from_api_response(api_response)
    assert len(dd.logs) == 2
    assert dd.logs[0].output == "line1"
    assert dd.logs[1].type == "stderr"


def test_deployment_data_from_api_list_logs():
    """Handle when logs is already a list (not a JSON string)."""
    logs_data = [
        {"output": "line1", "type": "stdout", "timestamp": "t1"},
    ]
    api_response = {
        "deployment_uuid": "u1",
        "status": "success",
        "logs": logs_data,
    }

    dd = DeploymentData.from_api_response(api_response)
    assert len(dd.logs) == 1


def test_deployment_data_from_api_null_logs():
    api_response = {"deployment_uuid": "u1", "status": "running", "logs": None}
    dd = DeploymentData.from_api_response(api_response)
    assert len(dd.logs) == 0


def test_deployment_data_from_api_missing_logs():
    api_response = {"deployment_uuid": "u1", "status": "running"}
    dd = DeploymentData.from_api_response(api_response)
    assert len(dd.logs) == 0


# ── Full integration-style test (mocked HTTP round-trip) ──────────────────────


@patch("coolify_deployment_logs.urlopen")
def test_integration_round_trip(mock_urlopen):
    """Mock the full HTTP round-trip and verify end-to-end parsing."""
    logs_data = [
        make_log_entry("Cloning into repo...", "stdout", order=1),
        make_log_entry("Step 1: Install deps", "stdout", order=2),
        make_log_entry("npm ERR! code 1", "stderr", order=3),
        make_log_entry("npm ERR! Failed at the build script", "stderr", order=4),
        make_log_entry("Build completed", "stdout", order=5),
    ]
    resp_data = make_deployment_response(
        status="failed",
        logs=logs_data,
        application_name="finance-sync",
        commit="deadbeef",
    )

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(resp_data).encode("utf-8")
    mock_urlopen.return_value.__enter__.return_value = mock_resp

    # Full pipeline
    errors = get_failed_deployment_diagnostics(
        "deploy-123", api_token="test-token", include_warnings=False
    )

    assert len(errors) == 2
    assert all("npm ERR!" in e for e in errors)

    # Also verify summary
    deployment = fetch_deployment("deploy-123", api_token="test-token")
    summary = get_log_summary(deployment)
    assert summary["total_log_entries"] == 5
    assert summary["stdout_entries"] == 3
    assert summary["stderr_entries"] == 2


# ── Edge cases ─────────────────────────────────────────────────────────────────


def test_extract_various_error_keywords():
    """Exercise the ERROR_KEYWORDS list comprehensively."""
    outputs = [
        "Error: something went wrong",
        "Deployment failed",
        "Failure in stage 2",
        "Process exited with code 1",
        "exit code 137",
        "Traceback (most recent call last)",
        "Exception: out of memory",
        "Warning: deprecated config",  # only caught with include_warnings
        "module not found",
        "Cannot find package",
        "unable to resolve dependency",
        "permission denied",
        "connection refused",
        "Connection reset by peer",
        "Operation timed out",
        "Killed by OOM killer",
        "Segmentation fault (core dumped)",
        "Fatal error",
        "Build failed at step 5",
    ]
    logs = [
        LogEntry(command=None, output=out, type="stderr", timestamp=f"t{i}")
        for i, out in enumerate(outputs)
    ]
    warn_output = "Warning: deprecated config"
    logs.append(
        LogEntry(command=None, output=warn_output, type="stdout", timestamp="tw")
    )

    deployment = DeploymentData(deployment_uuid="u1", status="failed", logs=logs)

    # Without warnings — all entries have error keywords or are stderr
    errors = extract_error_logs(deployment, include_warnings=False)
    assert len(errors) == len(logs)  # all 20 entries match
    # Without include_warnings, the 'Warning' line is caught by keyword match on stdout
    assert warn_output in errors[-1]

    # With include_warnings, still the same set (all outputs already have keywords)
    errors_w = extract_error_logs(deployment, include_warnings=True)
    assert len(errors_w) >= len(errors)
