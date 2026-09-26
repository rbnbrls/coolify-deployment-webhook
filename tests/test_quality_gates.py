"""Contract tests for the repository's quality gates.

The gates live in configuration — ``.coveragerc``, ``ruff.toml``, ``mypy.ini``,
``requirements-dev.txt`` — and in the CI workflow. Deleting any one of them does
not break a single unit test, so these tests pin the wiring that the coverage,
lint and type-check gates depend on.
"""

from __future__ import annotations

import configparser
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
APPLICATION_MODULES = {
    "webhook_server",
    "coolify_deployment_logs",
    "github_issue_creator",
}


def _coverage_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    with (REPO_ROOT / ".coveragerc").open(encoding="utf-8") as handle:
        parser.read_file(handle)
    return parser


def _workflow_text() -> str:
    return CI_WORKFLOW.read_text(encoding="utf-8")


def test_coverage_measures_the_application_modules_not_the_test_suite() -> None:
    config = _coverage_config()
    sources = {line.strip() for line in config["run"]["source"].splitlines() if line.strip()}
    assert sources == APPLICATION_MODULES
    assert config.getboolean("run", "branch") is True


def test_coverage_floor_is_defined() -> None:
    floor = _coverage_config().getint("report", "fail_under")
    assert 0 < floor <= 100


def test_pytest_cov_is_a_pinned_development_dependency() -> None:
    pinned = (REPO_ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    assert "pytest-cov==" in pinned


def test_lint_and_type_check_configs_exist() -> None:
    assert (REPO_ROOT / "ruff.toml").is_file()
    assert (REPO_ROOT / "mypy.ini").is_file()


def test_ci_runs_the_coverage_lint_and_type_gates() -> None:
    workflow = _workflow_text()
    assert "quality:" in workflow
    assert "ruff check ." in workflow
    assert "mypy" in workflow
    assert "--cov" in workflow
    assert "scripts/coverage_report.py" in workflow
