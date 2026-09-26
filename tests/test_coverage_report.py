"""Guards the published coverage contract.

The darkfactory quality lane reads a repository's coverage from a *committed*,
machine-readable report — `coverage.xml` at the repository root, the first path
it probes — and reported this repository's rung as `enforced` with the number
`unavailable` (`capability:coverage_not_published`) while that file was absent.
That was the state measured on `main` at `99608db` (2026-09-26): CI ran
`pytest --cov` with a `fail_under` floor and uploaded `coverage.xml` as a run
artifact, so coverage was enforced but published nowhere a reader could reach.

Four invariants keep the rung at `measured`:

1. `coverage.xml` exists at the repository root and parses to a line percentage,
   and git keeps tracking it — an ignore rule that swallows the published report
   would silently un-publish it;
2. the report is machine-independent: no timestamp, no absolute checkout path,
   and a re-run of the normaliser on it rewrites identical bytes, which is what
   makes the CI comparison meaningful instead of always-red;
3. the CI `quality` job measures the tree, republishes the report into the
   working tree and refuses to pass while the committed copy no longer describes
   it — so the report cannot rot;
4. publishing needs no write access: the report travels through the ordinary
   merge, and CI neither pushes to the repository nor widens the workflow token.
"""

from __future__ import annotations

import configparser
import importlib.util
import re
import shutil
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "coverage_report.py"
REPORT = ROOT / "coverage.xml"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
GITIGNORE = ROOT / ".gitignore"
COVERAGERC = ROOT / ".coveragerc"
QUALITY_JOB = "quality"
#: The modules .coveragerc declares as the measured set.
APPLICATION_MODULES = {
    "webhook_server.py",
    "coolify_deployment_logs.py",
    "github_issue_creator.py",
}

VALID_REPORT = """<?xml version="1.0" ?>
<coverage version="7.15.2" timestamp="1790441685500" lines-valid="4" lines-covered="3"
\tline-rate="0.75" branches-valid="0" branches-covered="0" branch-rate="0"
\tcomplexity="0">
\t<sources>
\t\t<source></source>
\t</sources>
\t<packages>
\t\t<package name="." line-rate="0.75" branch-rate="0" complexity="0">
\t\t\t<classes>
\t\t\t\t<class name="a.py" filename="a.py" complexity="0" line-rate="0.75" branch-rate="0">
\t\t\t\t\t<methods />
\t\t\t\t\t<lines>
\t\t\t\t\t\t<line number="1" hits="1" />
\t\t\t\t\t\t<line number="2" hits="0" />
\t\t\t\t\t\t<line number="3" hits="1" />
\t\t\t\t\t\t<line number="4" hits="1" />
\t\t\t\t\t</lines>
\t\t\t\t</class>
\t\t\t</classes>
\t\t</package>
\t</packages>
</coverage>
"""


@lru_cache(maxsize=1)
def _report_module() -> Any:
    spec = importlib.util.spec_from_file_location("_coverage_report", SCRIPT)
    assert spec and spec.loader, f"cannot load {SCRIPT}"
    module = importlib.util.module_from_spec(spec)
    # A dynamically loaded module must be registered before execution, or
    # `dataclasses` cannot resolve the module of the classes it decorates.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@lru_cache(maxsize=1)
def _workflow_text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@lru_cache(maxsize=1)
def _job_block(name: str) -> str:
    """The text of one top-level job, so a check cannot pass on another job."""
    match = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", _workflow_text(), re.M | re.S)
    assert match, f"no job named {name!r} in {WORKFLOW}"
    return match.group(1)


def _coverage_config() -> configparser.ConfigParser:
    parser = configparser.ConfigParser()
    with COVERAGERC.open(encoding="utf-8") as handle:
        parser.read_file(handle)
    return parser


def _quality_job_text() -> str:
    return _job_block(QUALITY_JOB)


# ---------------------------------------------------------------------------
# Invariant 1 — the published report exists, parses, and stays tracked
# ---------------------------------------------------------------------------


def test_report_is_committed_at_the_path_the_lane_reads_first() -> None:
    assert REPORT.is_file(), (
        "coverage.xml must be committed at the repository root: it is the first path the "
        "quality lane probes, and without it the coverage rung stays 'enforced' with reason "
        "capability:coverage_not_published"
    )


def test_committed_report_parses_to_a_percentage() -> None:
    summary = _report_module().parse_report(REPORT)

    assert 0.0 < summary.line_rate <= 1.0
    assert summary.lines_valid > 0
    assert summary.lines_covered <= summary.lines_valid
    assert 0.0 < summary.line_percent <= 100.0


def test_committed_report_describes_this_repository() -> None:
    """A report of some other tree would parse but prove nothing about this one."""
    root = ElementTree.parse(REPORT).getroot()
    filenames = {
        element.attrib["filename"]
        for element in root.iter("class")
        if element.attrib.get("filename")
    }

    assert filenames == APPLICATION_MODULES


def test_published_line_coverage_is_at_or_above_the_enforced_floor() -> None:
    """The floor CI enforces must be satisfiable by the published number."""
    floor = _coverage_config().getint("report", "fail_under")
    summary = _report_module().parse_report(REPORT)

    assert floor <= summary.line_percent, (
        f"fail_under={floor} sits above the published line coverage {summary.line_percent}% — "
        f"CI would fail and the committed report could never match a run"
    )


def test_the_published_report_is_not_ignored_by_git() -> None:
    """An ignore rule that swallows `coverage.xml` would un-publish it silently."""
    rules = [line.strip() for line in GITIGNORE.read_text().splitlines()]
    patterns = [rule for rule in rules if rule and not rule.startswith("#")]

    assert "coverage.xml" not in patterns, (
        "a `coverage.xml` rule ignores the published report at the repository root, so it can "
        "never be committed"
    )
    for per_run in (".coverage", "htmlcov/"):
        assert per_run in patterns, f"{per_run} is a per-run artifact and stays ignored"


# ---------------------------------------------------------------------------
# Invariant 2 — the report is machine-independent, and the reader never invents
# ---------------------------------------------------------------------------


def test_committed_report_carries_no_volatile_timestamp() -> None:
    assert "timestamp=" not in REPORT.read_text(encoding="utf-8"), (
        "a timestamp makes every run differ from the committed report, so the CI comparison "
        "would be red on every push with nothing changed"
    )


def test_committed_report_carries_no_absolute_checkout_path() -> None:
    text = REPORT.read_text(encoding="utf-8")

    assert "/home/" not in text and "/Users/" not in text, (
        "the report must describe the measurement, not where it ran: `relative_files` in "
        ".coveragerc keeps <sources> repository-relative"
    )


def test_committed_report_names_a_relative_source_base() -> None:
    """`filename` is resolved against `<sources>`; an empty base resolves nothing."""
    root = ElementTree.parse(REPORT).getroot()
    sources = [element.text or "" for element in root.iter("source")]

    assert sources == ["."], f"expected exactly one relative source base, got {sources!r}"


def test_relative_files_is_enabled_for_coverage() -> None:
    config = _coverage_config()

    assert config.getboolean("run", "relative_files") is True, (
        "without relative_files coverage writes the absolute checkout path into <sources>, so a "
        "report committed from one machine can never match a run on another"
    )
    assert config.get("xml", "output") == "coverage.xml", (
        "the publisher and pytest-cov must write the same file"
    )
    assert config.getboolean("run", "branch") is True, (
        "the published branch numbers are only honest while branch coverage is measured"
    )


def test_committed_report_is_already_normalised(tmp_path: Path) -> None:
    """Normalising a copy of the committed report must not change its bytes."""
    path = tmp_path / "coverage.xml"
    shutil.copyfile(REPORT, path)
    before = path.read_bytes()

    _report_module().normalise_report(path)

    assert path.read_bytes() == before
    assert before.endswith(b"\n")


def test_parse_rejects_a_report_without_a_rate(tmp_path: Path) -> None:
    path = tmp_path / "coverage.xml"
    path.write_text("<?xml version='1.0'?><coverage version='7' />")

    with pytest.raises(ValueError, match="line-rate"):
        _report_module().parse_report(path)


def test_parse_rejects_a_non_cobertura_document(tmp_path: Path) -> None:
    path = tmp_path / "coverage.xml"
    path.write_text("<?xml version='1.0'?><html><body/></html>")

    with pytest.raises(ValueError, match="coverage"):
        _report_module().parse_report(path)


def test_parse_rejects_unparsable_input(tmp_path: Path) -> None:
    path = tmp_path / "coverage.xml"
    path.write_text("not xml at all")

    with pytest.raises(ValueError, match="not readable"):
        _report_module().parse_report(path)


def test_missing_report_is_an_error_not_zero(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _report_module().parse_report(tmp_path / "absent.xml")


def test_normalise_drops_the_timestamp_and_names_the_source_base(tmp_path: Path) -> None:
    path = tmp_path / "coverage.xml"
    path.write_text(VALID_REPORT)

    summary = _report_module().normalise_report(path)

    assert summary.line_percent == 75.0
    assert summary.lines_covered == 3
    assert summary.lines_valid == 4
    text = path.read_text(encoding="utf-8")
    assert "timestamp" not in text
    assert 'line-rate="0.75"' in text
    assert 'filename="a.py"' in text
    assert "<source>.</source>" in text


def test_normalise_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "coverage.xml"
    path.write_text(VALID_REPORT)

    _report_module().normalise_report(path)
    once = path.read_bytes()
    _report_module().normalise_report(path)

    assert path.read_bytes() == once


def test_normalise_publishes_to_the_requested_path(tmp_path: Path) -> None:
    source = tmp_path / "build" / "coverage.xml"
    source.parent.mkdir()
    source.write_text(VALID_REPORT)
    target = tmp_path / "coverage.xml"

    summary = _report_module().normalise_report(source, target)

    assert target.is_file()
    assert summary.line_percent == 75.0
    assert source.read_text(encoding="utf-8") == VALID_REPORT, (
        "an explicit destination leaves the input untouched"
    )


def test_main_reports_the_percentage_on_stdout(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    path = tmp_path / "coverage.xml"
    shutil.copyfile(REPORT, path)

    assert _report_module().main([str(path), "--check", "--summary"]) == 0
    assert "coverage: " in capsys.readouterr().out


def test_main_fails_loudly_on_an_unreadable_report(tmp_path: Path) -> None:
    path = tmp_path / "coverage.xml"
    path.write_text("<html/>")

    assert _report_module().main([str(path), "--check"]) == 1


def test_publish_defaults_are_the_paths_this_repository_uses() -> None:
    module = _report_module()

    assert module.DEFAULT_INPUT == "coverage.xml"
    assert module.DEFAULT_OUTPUT == "coverage.xml"
    assert not Path(module.DEFAULT_OUTPUT).is_absolute(), (
        "the published path is repository-relative, so a local run and CI write the same file"
    )


# ---------------------------------------------------------------------------
# Invariant 3 — CI measures the tree, republishes, and refuses a stale report
# ---------------------------------------------------------------------------


def test_ci_runs_the_suite_under_coverage_with_a_floor() -> None:
    workflow = _quality_job_text()

    assert "--cov" in workflow
    assert "--cov-report=xml:coverage.xml" in workflow, "the report must be written, not printed"
    assert "--cov-report=term-missing" in workflow, "the number must stay visible in the log"
    assert "--cov-fail-under" not in workflow, (
        "the floor has one definition — [report] fail_under in .coveragerc — so CI and a local "
        "`pytest --cov` cannot disagree about it"
    )


def test_ci_publishes_the_report_into_the_working_tree() -> None:
    workflow = _quality_job_text()

    assert "scripts/coverage_report.py" in workflow, (
        "the run measures coverage but never publishes it, so the committed report can only rot"
    )
    assert "$GITHUB_STEP_SUMMARY" in workflow, "the line total belongs in the job summary"
    assert "PIPESTATUS" in workflow, "a failed publish must not be hidden by the pipeline"


def test_ci_fails_when_the_committed_report_is_stale() -> None:
    """The committed report is evidence only while it describes this tree."""
    workflow = _quality_job_text()

    assert "git diff --exit-code -- coverage.xml" in workflow
    assert "exit 1" in workflow, "a stale report must fail the job, not warn"
    assert "pytest --cov" in workflow, "the failure must say how to republish it"


def test_ci_compares_the_report_after_it_measures_and_publishes_it() -> None:
    workflow = _quality_job_text()
    measured = workflow.index("--cov-report=xml:coverage.xml")
    published = workflow.index("scripts/coverage_report.py")
    compared = workflow.index("git diff --exit-code -- coverage.xml")

    assert measured < published < compared


def test_ci_uploads_the_published_report_as_an_artifact() -> None:
    workflow = _quality_job_text()

    assert "actions/upload-artifact" in workflow
    assert "coverage.xml" in workflow.split("actions/upload-artifact", 1)[1]


def test_ci_runs_on_pull_requests() -> None:
    """A report can only be proven stale before the merge if PRs are checked."""
    assert re.search(r"^\s{2}pull_request:", _workflow_text(), re.M), (
        "without a pull-request trigger the committed report can only be proven stale after it "
        "has already landed on main"
    )


# ---------------------------------------------------------------------------
# Invariant 4 — publishing needs no write access in CI
# ---------------------------------------------------------------------------


def test_publishing_needs_no_write_access_in_ci() -> None:
    """The report reaches `main` through the merge, not through a push from CI.

    Committing it from the workflow would need `contents: write` on the workflow
    token — a privilege change a human has to release — and would make CI a
    second writer of a file the merge already carries.
    """
    workflow = _workflow_text()

    assert re.search(r"^permissions:\n  contents: read\n", workflow, re.M), (
        "the workflow token must stay read-only"
    )
    assert "contents: write" not in workflow
    assert "git push" not in _quality_job_text(), "CI must not push to the repository"
