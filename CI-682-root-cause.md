# CI #682 Root-Cause Investigation — finance-sync

Run: https://github.com/rbnbrls/finance-sync/actions/runs/33010519199 (run 682, CI workflow 317067044)
Commit: `03e7b08f6944277826820784419eb214905d2c7a` (branch `codex/provider-connector-lifecycle`, PR #436 "feat: complete provider connector lifecycle")
Event: pull_request | Conclusion: failure | 20 jobs → 18 success, 2 failure

## Verdict: TWO INDEPENDENT FAILURES

The two failing jobs do NOT share a root cause:

| Job (id) | Failing step | Root cause |
|---|---|---|
| Test (3.12) (98315414681) | 7. Run unit tests with coverage | 7 test failures, 4 distinct causes (all test/code mismatches introduced by the feature branch) |
| Build & Push (98315415018) | 7. Scan image with Trivy (fail on HIGH/CRITICAL) | 2 NEW HIGH sqlite CVEs in Trivy DB, not in `.trivyignore` at that commit |

---

## Failure 1: Test (3.12) — `##[error]Process completed with exit code 1.`
Final line: `= 7 failed, 3432 passed, 8 skipped, 182 deselected, 146 warnings in 529.77s =`
Coverage gate PASSED (`Required test coverage of 73% reached. Total coverage: 79.61%`).

### 1a. `tests/test_control_plane_contract.py::test_error_category_contract_is_closed`
```
E  AssertionError: assert {'authenticat...unknown', ...} == frozenset({'a...ilable', ...})
E  Extra items in the right set: 'token_expired', 'reauth_required', 'cancelled', 'incompatible', 'timeout'
tests/test_control_plane_contract.py:59: AssertionError
```
**Cause:** `CONTROL_PLANE_ERROR_CATEGORIES` (src/finance_sync/control_plane_contract.py) gained 5 categories (`token_expired`, `reauth_required`, `cancelled`, `incompatible`, `timeout`) in the feature branch, but the test's expected set was not updated. **Fix:** add the 5 categories to the test literal (done in current main).

### 1b. `tests/test_coverage_gaps.py::test_sync_error_classification_covers_operational_categories`
```
E  AssertionError: assert 'reauth_required' == 'authentication'
>  assert categorize_export_error("401 credential error") == "authentication"
tests/test_coverage_gaps.py:4277: AssertionError
```
**Cause:** `categorize_export_error` (src/finance_sync/sync/errors.py:80) matches `"expired"/"revoked"/"reauth"/"401"/"403"` FIRST → returns `reauth_required`; the test still expected `authentication`. The implementation was intentionally extended; the test assertion was stale. **Fix:** update the test to expect `reauth_required` (done in current main).

### 1c. 4× `TypeError: object supporting the buffer API required` — TestAutoReconciliationAfterSync/Disabled
```
tests/test_sync_orchestrator.py:1737: in test_reconciliation_runs_after_successful_sync
src/finance_sync/sync/orchestrator.py:403: in run_sync
    record_connector_operation(...)
src/finance_sync/observability/connector_metrics.py:64: in record_connector_operation
    connection_hash(connection_id),
src/finance_sync/observability/connector_metrics.py:42: in connection_hash
    return hashlib.sha256(connection_id.encode("utf-8")).hexdigest()[:16]
E  TypeError: object supporting the buffer API required
```
Failing tests:
- `tests/test_sync_orchestrator.py::TestAutoReconciliationAfterSync::test_reconciliation_runs_after_successful_sync`
- `...::test_reconciliation_skipped_on_failed_sync`
- `...::test_reconciliation_error_does_not_crash_sync`
- `tests/test_sync_orchestrator.py::TestAutoReconciliationDisabled::test_reconciliation_skipped_when_disabled`

**Cause:** Tests build `config = MagicMock()`; `getattr(config, "connection_id", None)` in `run_sync` (orchestrator.py:263) returns a `MagicMock` (truthy, non-None), which flows into `connection_hash()` where `if not connection_id` passes and `.encode()` blows up on the mock. New `record_connector_operation` telemetry call (added in this feature branch) exposed it. **Fix:** tests set `config.connection_id = "test-connection-id"` (string) — done in current main.

### 1d. `tests/test_sync_orchestrator_boundary.py::test_orchestrator_is_coordinating_only`
```
E  assert 1009 <= 900
tests/test_sync_orchestrator_boundary.py:23: AssertionError
```
**Cause:** orchestrator.py grew to 1009 lines (feature branch added provider lifecycle + reconciliation) past the boundary test's 900-line guard. **Fix:** raise guard to ≤1100 in the test (done in current main).

---

## Failure 2: Build & Push — Trivy gate
Step 7 `Scan image with Trivy (fail on HIGH/CRITICAL)`:
```
Running Trivy with options: trivy image finance-sync:scan
Detected OS family="debian" version="12.15"
[debian] Detecting vulnerabilities... os_version="12" pkg_num=116
##[error]Process completed with exit code 1.
```
Push step skipped afterward (gate fail). Results captured in artifact `trivy-7fafbf675b63ff78b6e627de0a02dbddaec99f59` (`trivy-results.json`, 492 KB).

**Exactly 2 findings tripped the gate** (both HIGH, 0 CRITICAL), both `libsqlite3-0` 3.40.1-2+deb12u2 with **no fixed version**:
```
CVE-2026-11822 HIGH libsqlite3-0 3.40.1-2+deb12u2 fixed=None  sqlite: arbitrary code execution via crafted FTS5 full-text search data
CVE-2026-11824 HIGH libsqlite3-0 3.40.1-2+deb12u2 fixed=None  sqlite: arbitrary code execution/crash via heap-based buffer overflow in FTS5
```
**Cause:** These two CVEs appeared in the (updated 2026-08-26) Trivy DB and were NOT in `.trivyignore` at commit `03e7b08f` (that commit's ignore file had 28 entries; the two sqlite entries were added in a later commit `58f5a29`). The ignore policy only permits ignoring findings with no upstream fix; both qualify (FixedVersion=None in bookworm). **Fix:** add both CVEs to `.trivyignore` with expiry — already done in commit `58f5a29` (see below). Do NOT upgrade-fix: no bookworm package exists yet.

---

## Resolution status: ALREADY FIXED on main
The failing commit was merged as PR #436 (merge `094673e6`, 2026-08-26). All fixes landed in subsequent commits now on main:

| Fix | Commit | PR |
|---|---|---|
| .trivyignore += CVE-2026-11822, CVE-2026-11824 | `58f5a29` | — |
| Test expectation updates (1a/1b/1c/1d) + error-category extension | `8ea7392` | #439 |
| Style/lint reconciliation | `500166c`, `c719266` | #444/#447 |

Main CI is green since: runs 704/706/708/712/714 all success (latest verified: run 714, main @ af796dcf, 2026-08-27T12:06). No further action required for CI #682 itself.

## Actionable next steps (only if revisiting old branch)
1. If PR #436 branch is ever re-created: cherry-pick `58f5a29` (.trivyignore) and `8ea7392` (test fixes) before CI.
2. `.trivyignore` entries expire 2026-12-31 — re-review then for bookworm fixes (policy says delete entry once a fixed package ships, so the gate re-verifies).
3. Boundary guard (≤1100) is a smell: next time orchestrator.py crosses 1100 lines, refactor rather than raise the limit again.

## Evidence files (this run)
- `/tmp/test312.log` — full Test (3.12) job log
- `/tmp/buildpush.log` — full Build & Push job log
- `/tmp/trivy-art/trivy-results.json` — Trivy scan results (492 KB)
- `/tmp/jobs33010519199.json` — job list for run 682
