#!/usr/bin/env python3
"""Publish this repository's coverage report in a form a reader can trust.

`pytest --cov --cov-report=xml` writes `coverage.xml` (see `[xml] output` in
`.coveragerc`), and that file on its own is not evidence anyone can read later:
CI uploads it as a run artifact, which expires, and the darkfactory quality lane
does not read an artifact. The lane reads a *committed*, machine-readable report
from the default branch -- `coverage.xml` first -- and reports the coverage rung
as `enforced` with `capability:coverage_not_published` while that file is
absent, which is exactly the state this repository was measured in on
2026-09-26.

This script is the one step between the two: it parses the Cobertura report,
drops the volatile `timestamp` attribute and writes the normalised copy to the
published path (`coverage.xml` at the repository root, which is the same path
pytest-cov wrote). Dropping that one field -- together with `relative_files` in
`.coveragerc`, which keeps `<sources>` repository-relative -- is what makes the
CI check in `.github/workflows/ci.yml` meaningful: the `quality` job regenerates
the report in the working tree, normalises it, and requires
`git diff --exit-code -- coverage.xml` to be clean. A change that moves coverage
must republish the report in the same pull request; an unchanged measurement
produces no diff at all. The report is never written back by CI: a commit from
the workflow would need a write grant on the workflow token, so it travels
through the ordinary merge instead.

Three invariants are load-bearing, and `tests/test_coverage_report.py` asserts
them:

* the report at the repository root parses to a line percentage -- the quality
  lane reads `coverage.xml` from the default branch and only moves the coverage
  rung on when `line-rate` parses to a number;
* the published report carries no absolute checkout path and no timestamp, so a
  run on any machine produces the same bytes;
* normalising an already-normalised report is a no-op.

Usage:
    python3 scripts/coverage_report.py              # normalise coverage.xml in place
    python3 scripts/coverage_report.py --summary    # ... and print the line total
    python3 scripts/coverage_report.py --check      # parse the published report, write nothing
    python3 scripts/coverage_report.py --output other.xml
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

#: What `pytest --cov --cov-report=xml` writes, per `[xml] output` in .coveragerc.
DEFAULT_INPUT = "coverage.xml"
#: Where a reader (and the quality lane) looks first: the repository root.
DEFAULT_OUTPUT = "coverage.xml"


@dataclass(frozen=True)
class CoverageSummary:
    """The numbers a committed coverage report is read for."""

    line_rate: float
    lines_covered: int
    lines_valid: int
    branch_rate: float | None
    branches_valid: int = 0

    @property
    def line_percent(self) -> float:
        return round(self.line_rate * 100.0, 2)

    def summary_line(self) -> str:
        text = (
            f"coverage: {self.line_percent:.2f}% lines "
            f"({self.lines_covered}/{self.lines_valid})"
        )
        # A repository that does not measure branches (branches-valid="0") would
        # otherwise read as measured-and-empty rather than not-measured.
        if self.branch_rate is not None and self.branches_valid:
            text += f", branches {round(self.branch_rate * 100.0, 2):.2f}%"
        return text


def parse_report(path: str | Path) -> CoverageSummary:
    """Parse a Cobertura report, raising ``ValueError`` when it is not readable.

    A report that cannot be parsed is an error and never a percentage: the whole
    point of publishing the file is that a reader can trust the number in it.
    """

    report_path = Path(path)
    try:
        root = ElementTree.parse(report_path).getroot()
    except (OSError, ElementTree.ParseError) as exc:
        raise ValueError(f"{report_path}: not readable as a coverage report ({exc})") from exc

    if root.tag != "coverage":
        raise ValueError(f"{report_path}: root element is <{root.tag}>, expected <coverage>")

    raw_rate = root.attrib.get("line-rate") or root.attrib.get("line_rate")
    if raw_rate is None:
        raise ValueError(f"{report_path}: no line-rate attribute")
    try:
        line_rate = float(raw_rate)
    except ValueError as exc:
        raise ValueError(f"{report_path}: line-rate {raw_rate!r} is not a number") from exc

    def _int(name: str) -> int:
        try:
            return int(root.attrib.get(name, "0"))
        except ValueError:
            return 0

    branch_raw = root.attrib.get("branch-rate") or root.attrib.get("branch_rate")
    try:
        branch_rate = float(branch_raw) if branch_raw is not None else None
    except ValueError:
        branch_rate = None

    return CoverageSummary(
        line_rate=line_rate,
        lines_covered=_int("lines-covered"),
        lines_valid=_int("lines-valid"),
        branch_rate=branch_rate,
        branches_valid=_int("branches-valid"),
    )


def normalise_report(path: str | Path, destination: str | Path | None = None) -> CoverageSummary:
    """Drop the volatile ``timestamp`` attribute, publishing the report.

    ``destination`` defaults to the input path, so normalising in place is the
    documented behaviour; an explicit ``--output`` publishes somewhere else.
    """

    source = Path(path)
    target = Path(destination) if destination is not None else source
    summary = parse_report(source)

    tree = ElementTree.parse(source)
    root = tree.getroot()
    root.attrib.pop("timestamp", None)
    # `relative_files` measures the modules relative to the working directory.
    # With the three application modules at the repository root there is no
    # common prefix left to write, so coverage emits an empty <source> and a
    # reader has nothing to resolve `filename` against. The report is published
    # at the repository root, so "." is the base it actually describes.
    for element in root.findall("./sources/source"):
        if not (element.text or "").strip():
            element.text = "."
    if not (root.text or "").strip():
        # The reporter's header comments are dropped by the XML parser and leave
        # their surrounding indent behind; keep one clean indent level instead.
        root.text = "\n\t"
    # ElementTree keeps attribute order, so only the one field is dropped: a
    # re-run of this function on its own output rewrites identical bytes.
    if target != source:
        target.parent.mkdir(parents=True, exist_ok=True)
    tree.write(target, encoding="utf-8", xml_declaration=True)

    body = target.read_bytes()
    if not body.endswith(b"\n"):
        # ElementTree writes no trailing newline; a committed text file should
        # end with one.
        target.write_bytes(body + b"\n")
    return summary


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    check_only = "--check" in args
    emit_summary = "--summary" in args

    output = DEFAULT_OUTPUT
    if "--output" in args:
        index = args.index("--output")
        try:
            output = args[index + 1]
        except IndexError:
            print("error: --output needs a path", file=sys.stderr)
            return 2
        del args[index : index + 2]

    paths = [arg for arg in args if not arg.startswith("-")]
    report_path = paths[0] if paths else DEFAULT_INPUT

    try:
        summary = parse_report(report_path) if check_only else normalise_report(report_path, output)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if emit_summary:
        print(summary.summary_line())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
