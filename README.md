# Coolify Deployment Webhook Server

Webhook server that receives Coolify deployment failure events and automatically creates GitHub issues.

Built with Python stdlib `http.server` — no framework dependencies.

## Quick Start

```bash
pip install -r requirements.txt
python webhook_server.py
```

## Environment Variables

| Variable | Description |
|----------|-------------|
| `COOLIFY_API_URL` | Coolify instance URL |
| `COOLIFY_API_TOKEN` | Coolify API token |
| `GITHUB_TOKEN` | GitHub personal access token |
| `GITHUB_REPO_OWNER` | GitHub repository owner |
| `GITHUB_REPO_NAME` | GitHub repository name |
| `HOST` | Server bind address (default: `0.0.0.0`) |
| `PORT` | Server port (default: `8000`) |

## Testing

```bash
pip install pytest requests
python -m pytest tests/ -v
```
# CI verification - trigger workflow
