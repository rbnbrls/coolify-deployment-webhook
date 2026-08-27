"""
Coolify Deployment Webhook → GitHub Issue Automation
─────────────────────────────────────────────────────
A lightweight webhook server that listens for Coolify deployment failure
webhooks, fetches the error logs, and creates a GitHub issue with the details.

Usage:
    COOLIFY_API_URL=https://coolify.example.com \\
    COOLIFY_API_TOKEN=... \\
    GITHUB_TOKEN=ghp_... \\
    GITHUB_REPO_OWNER=my-org \\
    GITHUB_REPO_NAME=my-repo \\
    python webhook_server.py

The server listens on 0.0.0.0:8000 (configurable via PORT and HOST env vars).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any

# ── Module imports ──────────────────────────────────────────────────────────────

from coolify_deployment_logs import (
    CoolifyAPIError,
    get_failed_deployment_diagnostics,
)
from github_issue_creator import (
    GitHubAPIError,
    create_issue,
)

# ── Logging ─────────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S%z",
)
logger = logging.getLogger("webhook_server")

# ── Configuration from environment ──────────────────────────────────────────────

COOLIFY_API_URL = os.environ.get("COOLIFY_API_URL", "")
COOLIFY_API_TOKEN = os.environ.get("COOLIFY_API_TOKEN", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO_OWNER = os.environ.get("GITHUB_REPO_OWNER", "")
GITHUB_REPO_NAME = os.environ.get("GITHUB_REPO_NAME", "")

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "8000"))

# ── Helper ──────────────────────────────────────────────────────────────────────


def _validate_config() -> None:
    """Check that all required env vars are set; exit with a clear message if not."""
    missing: list[str] = []
    if not COOLIFY_API_URL:
        missing.append("COOLIFY_API_URL")
    if not COOLIFY_API_TOKEN:
        missing.append("COOLIFY_API_TOKEN")
    if not GITHUB_TOKEN:
        missing.append("GITHUB_TOKEN")
    if not GITHUB_REPO_OWNER:
        missing.append("GITHUB_REPO_OWNER")
    if not GITHUB_REPO_NAME:
        missing.append("GITHUB_REPO_NAME")

    if missing:
        logger.error(
            "Missing required environment variable(s): %s",
            ", ".join(missing),
        )
        sys.exit(1)


def _parse_webhook_payload(body: bytes) -> dict[str, Any]:
    """Parse and validate the incoming webhook JSON payload.

    Returns the parsed dict on success.
    Raises ``ValueError`` if the payload is not valid JSON or is empty.
    """
    if not body or not body.strip():
        raise ValueError("Empty request body")

    try:
        payload: dict[str, Any] = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in webhook payload: {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Expected JSON object, got {type(payload).__name__}")

    return payload


def _get_event_type(payload: dict[str, Any]) -> str | None:
    """Extract the event type from a Coolify webhook payload.

    Coolify may send the event in different shapes:
    - ``{"event": "deployment.failed", ...}``
    - ``{"type": "deployment.failed", ...}``
    - ``{"event_type": "deployment.failed", ...}``

    Returns the event type string, or ``None`` if not found.
    """
    for key in ("event", "type", "event_type"):
        value = payload.get(key)
        if value and isinstance(value, str):
            return value
    return None


def _get_deployment_id(payload: dict[str, Any]) -> str | None:
    """Extract the deployment UUID from the webhook payload.

    Coolify may nest deployment data in a ``data`` envelope:
    ``{"event": "...", "data": {"deployment_uuid": "..."}}``
    or flatten it at the top level:
    ``{"event": "...", "deployment_uuid": "..."}``
    """
    # Check data envelope first
    data = payload.get("data")
    if isinstance(data, dict):
        uid = data.get("deployment_uuid") or data.get("id") or data.get("uuid")
        if uid:
            return str(uid)

    # Fall back to top-level keys
    for key in ("deployment_uuid", "deployment_id", "id", "uuid"):
        value = payload.get(key)
        if value:
            return str(value)

    # Also check inside a 'resource' or 'deployment' sub-object
    for sub_key in ("resource", "deployment"):
        sub = payload.get(sub_key)
        if isinstance(sub, dict):
            for key in ("deployment_uuid", "deployment_id", "id", "uuid"):
                value = sub.get(key)
                if value:
                    return str(value)

    return None


# ── Webhook handler ─────────────────────────────────────────────────────────────


def handle_webhook(payload: dict[str, Any]) -> dict[str, Any]:
    """Process a Coolify deployment webhook payload.

    Steps:
        1. Validate the event type is a deployment failure.
        2. Extract the deployment UUID.
        3. Fetch error logs from Coolify.
        4. Create a GitHub issue with the logs.

    Returns a dict with ``"status"`` (``"ok"`` or ``"error"``) and additional
    fields depending on the outcome.
    """
    # ── Step 1: Validate event type ─────────────────────────────────────────
    event_type = _get_event_type(payload)
    logger.info("Received webhook event type: %s", event_type)

    # Accept known failure event types
    failure_events = (
        "deployment.failed",
        "deployment_failed",
        "deployment.failure",
        "deployment.fail",
        "deployment.cancelled",
        "deployment_cancelled",
    )

    if event_type and event_type not in failure_events:
        # Non-failure events are silently acknowledged (no-op).
        logger.info(
            "Ignoring event type '%s' (not a deployment failure). "
            "Only %s events trigger an issue.",
            event_type,
            " / ".join(failure_events[:3]),
        )
        return {
            "status": "ignored",
            "event_type": event_type,
            "detail": f"Event type '{event_type}' is not a deployment failure; no issue created.",
        }

    # ── Step 2: Extract deployment UUID ─────────────────────────────────────
    deployment_id = _get_deployment_id(payload)
    if not deployment_id:
        raise ValueError(
            "Could not extract deployment UUID from webhook payload. "
            "Expected 'deployment_uuid', 'id', or 'uuid' in the payload body "
            "or inside a 'data' envelope."
        )

    logger.info("Processing deployment %s", deployment_id)

    # ── Step 3: Fetch error logs from Coolify ───────────────────────────────
    # Map COOLIFY_API_URL → COOLIFY_BASE_URL (the env var the log fetcher expects)
    os.environ["COOLIFY_BASE_URL"] = COOLIFY_API_URL

    error_lines = get_failed_deployment_diagnostics(
        deployment_id,
        base_url=COOLIFY_API_URL,
        api_token=COOLIFY_API_TOKEN,
        include_warnings=True,
    )

    if not error_lines:
        error_lines = [
            f"Deployment {deployment_id} failed, but no error lines were "
            "found in the logs. Check the Coolify dashboard for details."
        ]

    logger.info(
        "Fetched %d error log lines for deployment %s",
        len(error_lines),
        deployment_id,
    )

    # Build the GitHub issue body
    issue_body = (
        f"## Coolify Deployment Failed\n\n"
        f"**Deployment ID:** `{deployment_id}`\n"
        f"**Event Type:** `{event_type or 'unknown'}`\n\n"
        f"### Error Logs\n\n"
        f"```\n"
        f"{chr(10).join(error_lines)}\n"
        f"```\n\n"
        f"---\n"
        f"_Automatically created by coolify-deployment-webhook_"
    )

    # Truncate body if it exceeds GitHub's limit (65536 chars)
    # Keep the beginning and the end — the most important context.
    MAX_BODY = 60000
    if len(issue_body) > MAX_BODY:
        head = issue_body[: MAX_BODY // 2]
        tail = issue_body[-(MAX_BODY // 2) :]
        issue_body = (
            f"{head}\n\n"
            f"[... {len(issue_body) - MAX_BODY + 100} characters truncated ...]\n\n"
            f"{tail}"
        )

    # ── Step 4: Create GitHub issue ─────────────────────────────────────────
    issue_title = f"Coolify deployment failed: {deployment_id}"

    issue_url = create_issue(
        owner=GITHUB_REPO_OWNER,
        repo=GITHUB_REPO_NAME,
        title=issue_title,
        body=issue_body,
        labels=["bug", "coolify-deployment"],
        token=GITHUB_TOKEN,
    )

    logger.info(
        "Created GitHub issue for deployment %s → %s",
        deployment_id,
        issue_url,
    )

    return {
        "status": "ok",
        "deployment_id": deployment_id,
        "event_type": event_type,
        "issue_url": issue_url,
        "error_count": len(error_lines),
    }


# ── Server (stdlib http.server) ────────────────────────────────────────────────


def _make_response(
    status: int,
    body: dict[str, Any],
) -> tuple[bytes, str]:
    """Build an HTTP response tuple ``(body_bytes, content_type)``."""
    return json.dumps(body).encode("utf-8"), "application/json"


def run_server() -> None:
    """Start the webhook server using stdlib ``http.server``.

    Zero external dependencies — only Python 3.10+ required.
    """
    from http.server import BaseHTTPRequestHandler, HTTPServer

    _validate_config()

    class WebhookHandler(BaseHTTPRequestHandler):
        """HTTP request handler for the /webhook endpoint."""

        # Silence default logging — we use our own logger
        # (BaseHTTPRequestHandler logs every request by default).
        # We'll supress it by setting a log method that uses our logger.
        def log_message(self, format: str, *args: Any) -> None:
            logger.debug("HTTP: %s", format % args)

        def _send_json(
            self,
            status: int,
            data: dict[str, Any],
        ) -> None:
            body_bytes, content_type = _make_response(status, data)
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

        def _read_body(self) -> bytes:
            content_length = int(self.headers.get("Content-Length", "0"))
            if content_length > 1_000_000:  # 1 MB limit
                raise ValueError("Request body too large (>1 MB)")
            return self.rfile.read(content_length)

        def do_GET(self) -> None:
            """GET / → health check."""
            if self.path == "/":
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "service": "coolify-deployment-webhook",
                        "endpoints": {
                            "/": "Health check (this page)",
                            "/webhook": "POST — Accept Coolify deployment webhooks",
                            "/health": "Health check (legacy)",
                        },
                    },
                )
            elif self.path in ("/health", "/healthz"):
                self._send_json(200, {"status": "healthy"})
            else:
                self._send_json(404, {"error": "Not found"})

        def do_POST(self) -> None:
            """POST /webhook → process deployment webhook."""
            if self.path != "/webhook":
                self._send_json(404, {"error": "Not found"})
                return

            try:
                body = self._read_body()
                payload = _parse_webhook_payload(body)
            except ValueError as exc:
                logger.warning("Bad request: %s", exc)
                self._send_json(400, {"error": str(exc)})
                return

            try:
                result = handle_webhook(payload)
                self._send_json(200, result)
            except ValueError as exc:
                logger.error("Webhook processing error: %s", exc)
                self._send_json(400, {"error": str(exc)})
            except CoolifyAPIError as exc:
                logger.error(
                    "Coolify API error for deployment: %s",
                    exc,
                )
                self._send_json(
                    502,
                    {
                        "error": f"Coolify API error: {exc}",
                    },
                )
            except GitHubAPIError as exc:
                logger.error("GitHub API error: %s", exc)
                self._send_json(
                    502,
                    {
                        "error": f"GitHub API error: {exc}",
                    },
                )
            except Exception:
                logger.exception("Unexpected error processing webhook")
                self._send_json(500, {"error": "Internal server error"})

    server = HTTPServer((HOST, PORT), WebhookHandler)
    logger.info(
        "Coolify Deployment Webhook Server listening on %s:%s",
        HOST,
        PORT,
    )
    logger.info("Endpoints:")
    logger.info("  POST /webhook  — Accept Coolify deployment webhooks")
    logger.info("  GET  /health   — Health check")
    logger.info("  GET  /         — Service info")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.server_close()
        sys.exit(0)


# ── Entry point ─────────────────────────────────────────────────────────────────


def main() -> None:
    """Entry point: validate config and start the server."""
    _validate_config()
    run_server()


if __name__ == "__main__":
    main()
