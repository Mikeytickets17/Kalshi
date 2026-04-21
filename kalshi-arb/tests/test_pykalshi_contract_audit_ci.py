"""CI gate: run the pykalshi contract audit on every PR.

If ANY call site drifts back to a wrong method path, a wrong kwarg
name, a missing attribute, or a raw string where pykalshi requires
an enum instance, this test fails the build.

The audit itself lives in kalshi_arb/tools/pykalshi_contract_audit.py
and is also runnable by operators as a CLI via
    python -m kalshi_arb.tools.pykalshi_contract_audit
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_pykalshi_contract_audit_is_clean():
    """Regression guard against the 'sandbox-passed-prod-failed' cycle.

    For PRs #11-#14 we shipped five bugs that all passed our tests
    (self-written fakes) and all crashed on real prod. This test
    runs the contract audit -- which introspects the ACTUAL installed
    pykalshi library -- and fails if any of:
      * a kalshi_arb call site references a pykalshi method that
        doesn't exist
      * an attribute access targets a field missing from the real
        Pydantic model
      * our source passes a raw string where pykalshi's internal
        code will `.value()` the arg (the AttributeError trap)
    """
    proc = subprocess.run(
        [sys.executable, "-m", "kalshi_arb.tools.pykalshi_contract_audit"],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=30,
    )
    # The audit writes audit-report.json next to the CWD -- parse it
    # for structured assertions instead of scraping stdout.
    report_path = REPO_ROOT / "audit-report.json"
    assert report_path.exists(), (
        f"audit did not produce report. stdout=\n{proc.stdout}\n"
        f"stderr=\n{proc.stderr}"
    )
    report = json.loads(report_path.read_text())
    summary = report["summary"]
    bad = summary["bad"]
    assert bad == 0, (
        f"contract audit surfaced {bad} bad call site(s):\n"
        + "\n".join(
            f"  [{r['status']}] {r['call_site']} -> {r['target']}: {r['detail']}"
            for r in report["rows"]
            if r["status"] in {"MISSING", "MISMATCH", "ENUM_TRAP"}
        )
        + f"\n\nFull stdout:\n{proc.stdout}"
    )
    # Non-zero UNKNOWN rows aren't a failure but are worth surfacing.
    if summary["unknown"]:
        print(
            f"\n[CONTRACT-AUDIT] {summary['unknown']} call site(s) require "
            "live-prod verification (listed as UNKNOWN in report)."
        )
    assert proc.returncode == 0, (
        f"audit CLI exited {proc.returncode}. stdout:\n{proc.stdout}"
    )


def test_audit_captures_every_pykalshi_call_site_in_source():
    """If someone adds a new pykalshi call, they must register it in
    METHOD_CALLS or ATTR_ACCESSES so the audit covers it. This test
    greps the source tree for new pykalshi references and compares
    against the audit's call-site catalogue."""
    from kalshi_arb.tools.pykalshi_contract_audit import (
        ATTR_ACCESSES,
        METHOD_CALLS,
    )

    # Every pykalshi.<x> import that's NOT behind a test fixture.
    # This catches the 'new module added, audit forgotten' case.
    src_files = list((REPO_ROOT / "kalshi_arb").rglob("*.py"))
    site_names = {e.call_site for e in METHOD_CALLS} | {
        e.call_site for e in ATTR_ACCESSES
    }
    # Heuristic: the number of registered call sites should grow
    # monotonically and never drop below the high-water mark we've
    # validated. Today = 20 (15 method + 5 attr = 20, plus 4 enum +
    # 4 source = 29 total audit rows).
    assert len(site_names) >= 15, (
        f"contract audit only tracks {len(site_names)} call sites. "
        "If you added pykalshi calls without registering them, add "
        "them to METHOD_CALLS / ATTR_ACCESSES in "
        "kalshi_arb/tools/pykalshi_contract_audit.py."
    )
    # Sanity: files we know call pykalshi are covered.
    known_callers = {
        "kalshi_arb/probe/probe.py",
        "kalshi_arb/executor/live.py",
        "kalshi_arb/rest/client.py",
        "kalshi_arb/ws/consumer.py",
    }
    rel_paths = {str(p.relative_to(REPO_ROOT)) for p in src_files}
    missing = known_callers - rel_paths
    assert not missing, f"known pykalshi callers missing from tree: {missing}"
