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
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

## Quality gates

CI runs exactly these commands; run them locally before pushing:

```bash
ruff check .                                                # lint  — config: ruff.toml
mypy                                                        # types — config: mypy.ini
python -m pytest tests/ --cov --cov-report=term-missing     # coverage — config: .coveragerc
```

Coverage is measured by `pytest-cov` over the three application modules — the test
suite itself is excluded — and enforced by the `fail_under` floor in
`.coveragerc`, the single definition of that number, so the same gate applies in
CI and locally. The CI `quality` job writes the line total into the job summary
and uploads `coverage.xml` as a run artifact.

The durable number is the committed report: `coverage.xml` at the repository
root, regenerated and republished by `scripts/coverage_report.py` on every run of
the `quality` job. It is normalised, so it carries no timestamp and no absolute
checkout path, and the job fails while the committed copy no longer describes the
tree — a change that moves coverage has to republish the report in the same pull
request:

```bash
python -m pytest tests/ --cov --cov-report=term-missing --cov-report=xml:coverage.xml
python scripts/coverage_report.py --summary   # normalise coverage.xml and print the total
```

`scripts/coverage_report.py --check --summary` parses the committed report and
prints its percentage without writing anything; it exits non-zero on a report it
cannot read rather than reporting a number it did not parse.

`mypy` runs in `strict` mode over the application modules; the test suite is not
type-checked yet.

Fix applied: ensured webhook server handles Coolify deployment webhooks correctly for finance-sync staging app.
