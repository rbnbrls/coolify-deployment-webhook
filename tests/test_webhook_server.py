"""Tests for the Coolify Deployment Webhook Server.

These tests cover the helper functions (payload parsing, event validation,
deployment ID extraction) and the HTTP handler logic. They do NOT require
a live Coolify or GitHub instance — API calls are exercised by the parent
module tests.

The server uses stdlib ``http.server`` so it can be tested with
standard library tools.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Generator

import pytest

# Import helpers from the webhook server
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from webhook_server import (
    _get_deployment_id,
    _get_event_type,
    _parse_webhook_payload,
    handle_webhook,
    _make_response,
)

# ── Helpers ────────────────────────────────────────────────────────────────────


def payload(**overrides: object) -> bytes:
    """Build a JSON-encoded webhook payload with sensible defaults."""
    data: dict[str, object] = {
        "event": "deployment.failed",
        "deployment_uuid": "cm37r6cqj000008jm0veg5tkm",
        "status": "failed",
    }
    data.update(overrides)
    return json.dumps(data).encode("utf-8")


# ── _parse_webhook_payload ─────────────────────────────────────────────────────


class TestParseWebhookPayload:
    def test_valid_json(self) -> None:
        result = _parse_webhook_payload(payload())
        assert isinstance(result, dict)
        assert result["event"] == "deployment.failed"

    def test_empty_body(self) -> None:
        with pytest.raises(ValueError, match="Empty request body"):
            _parse_webhook_payload(b"")

    def test_whitespace_only(self) -> None:
        with pytest.raises(ValueError, match="Empty request body"):
            _parse_webhook_payload(b"   \n   ")

    def test_invalid_json(self) -> None:
        with pytest.raises(ValueError, match="Invalid JSON"):
            _parse_webhook_payload(b"not json")

    def test_array_not_object(self) -> None:
        with pytest.raises(ValueError, match="Expected JSON object, got list"):
            _parse_webhook_payload(b'["a", "b"]')


# ── _get_event_type ────────────────────────────────────────────────────────────


class TestGetEventType:
    def test_event_key(self) -> None:
        assert _get_event_type({"event": "deployment.failed"}) == "deployment.failed"

    def test_type_key(self) -> None:
        assert _get_event_type({"type": "deployment.failed"}) == "deployment.failed"

    def test_event_type_key(self) -> None:
        assert _get_event_type({"event_type": "deployment.failed"}) == "deployment.failed"

    def test_missing_event(self) -> None:
        assert _get_event_type({"status": "failed"}) is None

    def test_empty_dict(self) -> None:
        assert _get_event_type({}) is None

    def test_none_value(self) -> None:
        assert _get_event_type({"event": None}) is None

    def test_non_string_value(self) -> None:
        assert _get_event_type({"event": 123}) is None

    def test_event_key_preferred(self) -> None:
        """If multiple keys exist, 'event' is checked first."""
        result = _get_event_type({"event": "deployment.failed", "type": "other"})
        assert result == "deployment.failed"


# ── _get_deployment_id ─────────────────────────────────────────────────────────


class TestGetDeploymentId:
    def test_top_level_deployment_uuid(self) -> None:
        result = _get_deployment_id({"deployment_uuid": "abc-123"})
        assert result == "abc-123"

    def test_top_level_id(self) -> None:
        result = _get_deployment_id({"id": "def-456"})
        assert result == "def-456"

    def test_top_level_uuid(self) -> None:
        result = _get_deployment_id({"uuid": "ghi-789"})
        assert result == "ghi-789"

    def test_data_envelope(self) -> None:
        result = _get_deployment_id({
            "event": "deployment.failed",
            "data": {"deployment_uuid": "nested-id"},
        })
        assert result == "nested-id"

    def test_data_envelope_fallback_keys(self) -> None:
        result = _get_deployment_id({
            "data": {"id": "nested-id-2"},
        })
        assert result == "nested-id-2"

    def test_data_envelope_uuid(self) -> None:
        result = _get_deployment_id({
            "data": {"uuid": "nested-uuid"},
        })
        assert result == "nested-uuid"

    def test_resource_sub_object(self) -> None:
        result = _get_deployment_id({
            "event": "deployment.failed",
            "resource": {"deployment_uuid": "resource-id"},
        })
        assert result == "resource-id"

    def test_deployment_sub_object(self) -> None:
        result = _get_deployment_id({
            "event": "deployment.failed",
            "deployment": {"id": "deploy-123"},
        })
        assert result == "deploy-123"

    def test_no_deployment_id(self) -> None:
        result = _get_deployment_id({"event": "deployment.failed"})
        assert result is None

    def test_empty_dict(self) -> None:
        assert _get_deployment_id({}) is None

    def test_data_envelope_takes_precedence(self) -> None:
        """'data' envelope is checked before top level."""
        result = _get_deployment_id({
            "deployment_uuid": "top-level",
            "data": {"deployment_uuid": "nested"},
        })
        assert result == "nested"


# ── handle_webhook ──────────────────────────────────────────────────────────────


class TestHandleWebhook:
    """Unit tests for ``handle_webhook`` logic (without API calls).

    Tests that require a real Coolify/GitHub connection use pre-configured
    environment variables or are skipped.
    """

    def test_ignores_non_failure_event(self) -> None:
        result = handle_webhook({
            "event": "deployment.successful",
            "deployment_uuid": "abc-123",
        })
        assert result["status"] == "ignored"
        assert "not a deployment failure" in result["detail"]

    def test_ignores_unknown_event_type(self) -> None:
        result = handle_webhook({
            "event": "health.check",
            "deployment_uuid": "abc-123",
        })
        assert result["status"] == "ignored"

    def test_missing_deployment_id_raises(self) -> None:
        with pytest.raises(ValueError, match="Could not extract deployment UUID"):
            handle_webhook({"event": "deployment.failed"})

    def test_empty_payload_data(self) -> None:
        with pytest.raises(ValueError, match="Could not extract deployment UUID"):
            handle_webhook({"event": "deployment.failed", "data": {}})

    @pytest.mark.skipif(
        not os.environ.get("COOLIFY_API_URL") or not os.environ.get("COOLIFY_API_TOKEN"),
        reason="COOLIFY_API_URL and COOLIFY_API_TOKEN required for integration test",
    )
    def test_integration_with_real_coolify_and_github(self) -> None:
        """End-to-end: send a known deployment failure webhook and verify a
        GitHub issue is created.

        Requires:
        - COOLIFY_API_URL, COOLIFY_API_TOKEN, GITHUB_TOKEN,
          GITHUB_REPO_OWNER, GITHUB_REPO_NAME all set as env vars.
        - A real failed deployment UUID to test with.
        """
        # These are required for the underlying modules
        os.environ["COOLIFY_BASE_URL"] = os.environ["COOLIFY_API_URL"]

        payload_data: dict[str, object] = {
            "event": "deployment.failed",
            "deployment_uuid": os.environ.get(
                "TEST_DEPLOYMENT_UUID", "cm37r6cqj000008jm0veg5tkm"
            ),
        }

        result = handle_webhook(payload_data)
        assert result["status"] == "ok"
        assert "issue_url" in result
        assert "github.com" in result["issue_url"]


# ── _make_response ─────────────────────────────────────────────────────────────


class TestMakeResponse:
    def test_returns_bytes_and_content_type(self) -> None:
        body_bytes, content_type = _make_response(200, {"status": "ok"})
        assert isinstance(body_bytes, bytes)
        assert content_type == "application/json"
        assert json.loads(body_bytes) == {"status": "ok"}


# ── Integration: server startup and HTTP handling ──────────────────────────────


@ pytest.fixture(scope="module")
def server_url() -> Generator[str, None, None]:
    """Start the webhook server as a subprocess and return its base URL.

    Requires the 5 env vars needed for real API calls; if they are not all
    set, the server still starts but will return 502 on real webhooks.
    """
    port = 18972  # non-standard port to avoid collisions
    host = "127.0.0.1"
    url = f"http://{host}:{port}"

    # Inject dummy required env vars so the server starts.
    # The actual values don't matter for basic HTTP tests — they only
    # affect real API calls (which return 502 without valid credentials).
    env = os.environ.copy()
    env.setdefault("COOLIFY_API_URL", "http://coolify.test")
    env.setdefault("COOLIFY_API_TOKEN", "test-token")
    env.setdefault("GITHUB_TOKEN", "test-token")
    env.setdefault("GITHUB_REPO_OWNER", "test-owner")
    env.setdefault("GITHUB_REPO_NAME", "test-repo")
    env["COOLIFY_BASE_URL"] = env["COOLIFY_API_URL"]
    env["HOST"] = host
    env["PORT"] = str(port)

    proc = subprocess.Popen(
        [sys.executable, "-m", "webhook_server"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    # Wait for the server to be ready
    max_wait = 10
    for _ in range(max_wait):
        try:
            with urllib.request.urlopen(f"{url}/health", timeout=2):
                break
        except (urllib.error.URLError, ConnectionError, OSError):
            time.sleep(0.5)
    else:
        proc.terminate()
        proc.wait()
        pytest.fail(f"Server did not start within {max_wait}s")

    yield url

    proc.terminate()
    proc.wait(timeout=5)


class TestServerHTTP:
    """Live HTTP tests against a running server subprocess."""

    def test_health_endpoint(self, server_url: str) -> None:
        with urllib.request.urlopen(f"{server_url}/health") as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["status"] == "healthy"

    def test_root_endpoint(self, server_url: str) -> None:
        with urllib.request.urlopen(server_url) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["service"] == "coolify-deployment-webhook"

    def test_healthz_endpoint(self, server_url: str) -> None:
        with urllib.request.urlopen(f"{server_url}/healthz") as resp:
            assert resp.status == 200

    def test_unknown_path_returns_404(self, server_url: str) -> None:
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(f"{server_url}/unknown")
        assert exc_info.value.code == 404

    def test_post_to_webhook_with_valid_json(self, server_url: str) -> None:
        """Sending a valid JSON payload returns 200 (may be ignored or processed)."""
        req = urllib.request.Request(
            f"{server_url}/webhook",
            data=payload(event="deployment.successful"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200
            data = json.loads(resp.read().decode())
            assert data["status"] == "ignored"

    def test_post_with_invalid_json(self, server_url: str) -> None:
        """Sending invalid JSON returns 400."""
        req = urllib.request.Request(
            f"{server_url}/webhook",
            data=b"not json",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400
        data = json.loads(exc_info.value.read().decode())
        assert "Invalid JSON" in data["error"]

    def test_post_to_unknown_path(self, server_url: str) -> None:
        """POST to a non-/webhook path returns 404."""
        req = urllib.request.Request(
            f"{server_url}/other",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 404

    def test_post_with_no_body(self, server_url: str) -> None:
        """Empty body returns 400."""
        req = urllib.request.Request(
            f"{server_url}/webhook",
            data=b"",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as exc_info:
            urllib.request.urlopen(req)
        assert exc_info.value.code == 400

    def test_post_without_content_type(self, server_url: str) -> None:
        """Even without Content-Type, valid JSON body is parsed."""
        import http.client

        conn = http.client.HTTPConnection("127.0.0.1", 18972)
        conn.request(
            "POST",
            "/webhook",
            body=payload(event="deployment.successful"),
            headers={},
        )
        resp = conn.getresponse()
        assert resp.status == 200
        data = json.loads(resp.read().decode())
        assert data["status"] == "ignored"
        conn.close()
