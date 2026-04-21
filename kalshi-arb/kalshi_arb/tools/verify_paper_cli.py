"""One-shot verifier for the `kalshi-arb paper` CLI.

Exercises every gate + the pipeline end-to-end against a throwaway
SQLite DB and a freshly-written detected_limits.yaml:

  Gate 1: CLI refuses without a probe file (exit != 0, 'GATE REFUSED')
  Gate 2: CLI refuses when probe environment=demo (exit != 0)
  Gate 3: CLI refuses when probe is > 24h old (exit != 0)
  Gate 4: CLI refuses when LIVE_TRADING=true (exit != 0)
  Gate 5: Happy-path --smoke-test <SMOKE_SECONDS> runs, exits 0,
          prints the startup banner
  Gate 6: Event store contains >0 opportunity rows + >0 emit rows +
          >=2 orders_placed rows (arb-viable fake WS guaranteed this)

The Windows popup reads the final HEADLINE: line; PASS only when all
six gates are green.

All work happens in a tmp directory the verifier creates + cleans up.
Does NOT touch the operator's real data/ event store. Does NOT require
the dashboard to be running (unlike verify_dashboard).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path


SMOKE_SECONDS = 6          # small enough to stay quick, large enough for emits
HAPPY_PATH_TIMEOUT = 30.0


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _check(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    color_on = "\033[32m" if ok else "\033[31m"
    color_off = "\033[0m"
    suffix = f"  [{detail}]" if detail else ""
    print(f"  {color_on}{mark}{color_off}  {label}{suffix}")


def _write_probe(path: Path, *, env: str = "prod", age_hours: float = 0.0) -> None:
    import yaml

    ts = datetime.now(tz=UTC) - timedelta(hours=age_hours)
    body = {
        "environment": env,
        "ts_utc": ts.isoformat().replace("+00:00", "Z"),
        "rest_latency_p50_ms": 35,
        "ws_max_tickers_per_conn": 200,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(body))


def _run_cli(
    args: list[str],
    env: dict[str, str],
    *,
    timeout: float,
    cwd: Path,
) -> subprocess.CompletedProcess:
    cmd = [sys.executable, "-m", "kalshi_arb.cli", *args]
    return subprocess.run(
        cmd,
        env=env,
        cwd=str(cwd),
        timeout=timeout,
        capture_output=True,
        text=True,
    )


def _base_env(db_path: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.pop("LIVE_TRADING", None)
    env.pop("PAPER_MODE", None)
    env["EVENT_STORE_PATH"] = str(db_path)
    env["KALSHI_API_KEY_ID"] = "verify-key-id"
    env["KALSHI_PRIVATE_KEY_PATH"] = "/nonexistent-key"
    env["KALSHI_USE_DEMO"] = "true"
    env["HARD_CAP_USD"] = "9.0"
    env["MIN_EXPECTED_PROFIT_USD"] = "0.05"
    env["PYTHONPATH"] = str(_repo_root()) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _verify(work: Path) -> int:
    print()
    print("=" * 70)
    print("  Verifying `kalshi-arb paper` CLI end-to-end")
    print("=" * 70)

    failures: list[str] = []

    probe = work / "detected_limits.yaml"
    db = work / "paper.db"

    # ---- Gate 1: missing probe ------------------------------------
    env = _base_env(db)
    try:
        proc = _run_cli(
            [
                "paper", "--smoke-test", "2",
                "--probe-path", str(work / "nope.yaml"),
            ],
            env=env, timeout=20, cwd=work,
        )
        ok = proc.returncode != 0 and "not found" in proc.stderr
        _check("Gate 1: refuses when probe file missing",
               ok, f"exit={proc.returncode}")
        if not ok:
            failures.append("gate-missing")
    except Exception as exc:  # noqa: BLE001
        _check("Gate 1: refuses when probe file missing", False, str(exc))
        failures.append("gate-missing")

    # ---- Gate 2: probe environment != prod ------------------------
    _write_probe(probe, env="demo")
    try:
        proc = _run_cli(
            ["paper", "--smoke-test", "2", "--probe-path", str(probe)],
            env=env, timeout=20, cwd=work,
        )
        ok = proc.returncode != 0 and "must be 'prod'" in proc.stderr
        _check("Gate 2: refuses when probe environment != prod",
               ok, f"exit={proc.returncode}")
        if not ok:
            failures.append("gate-not-prod")
    except Exception as exc:  # noqa: BLE001
        _check("Gate 2: refuses when probe environment != prod", False, str(exc))
        failures.append("gate-not-prod")

    # ---- Gate 3: stale probe --------------------------------------
    _write_probe(probe, env="prod", age_hours=48)
    try:
        proc = _run_cli(
            ["paper", "--smoke-test", "2", "--probe-path", str(probe)],
            env=env, timeout=20, cwd=work,
        )
        ok = proc.returncode != 0 and "old" in proc.stderr
        _check("Gate 3: refuses when probe is stale (>24h)",
               ok, f"exit={proc.returncode}")
        if not ok:
            failures.append("gate-stale")
    except Exception as exc:  # noqa: BLE001
        _check("Gate 3: refuses when probe is stale (>24h)", False, str(exc))
        failures.append("gate-stale")

    # ---- Gate 4: LIVE_TRADING env refusal -------------------------
    _write_probe(probe, env="prod")
    live_env = dict(env)
    live_env["LIVE_TRADING"] = "true"
    try:
        proc = _run_cli(
            ["paper", "--smoke-test", "2", "--probe-path", str(probe)],
            env=live_env, timeout=20, cwd=work,
        )
        ok = proc.returncode != 0 and "LIVE_TRADING" in proc.stderr
        _check("Gate 4: refuses when LIVE_TRADING=true",
               ok, f"exit={proc.returncode}")
        if not ok:
            failures.append("gate-live")
    except Exception as exc:  # noqa: BLE001
        _check("Gate 4: refuses when LIVE_TRADING=true", False, str(exc))
        failures.append("gate-live")

    # ---- Gate 5: happy-path smoke-test runs cleanly ---------------
    # Fresh DB to avoid state from gate tests leaking in.
    db.unlink(missing_ok=True)
    _write_probe(probe, env="prod")
    try:
        t0 = time.monotonic()
        proc = _run_cli(
            [
                "paper", "--smoke-test", str(SMOKE_SECONDS),
                "--smoke-rate", "25", "--smoke-seed", "11",
                "--probe-path", str(probe),
            ],
            env=env, timeout=HAPPY_PATH_TIMEOUT, cwd=work,
        )
        elapsed = time.monotonic() - t0
        banner_ok = "kalshi-arb paper mode starting" in proc.stderr
        ok = proc.returncode == 0 and banner_ok
        _check(
            "Gate 5: --smoke-test runs cleanly and exits 0",
            ok,
            f"exit={proc.returncode} elapsed={elapsed:.1f}s "
            f"banner={'yes' if banner_ok else 'missing'}",
        )
        if not ok:
            failures.append("gate-happy-path")
            # Surface the first few non-banner error lines for debugging.
            last_err = "\n".join(proc.stderr.strip().splitlines()[-10:])
            print(f"  --- last stderr lines ---\n{last_err}")
    except Exception as exc:  # noqa: BLE001
        _check("Gate 5: --smoke-test runs cleanly", False, str(exc))
        failures.append("gate-happy-path")

    # ---- Gate 6: event store rows present -------------------------
    try:
        from kalshi_arb.store import EventStore, SqliteBackend

        store = EventStore(SqliteBackend(db))
        store.connect()
        try:
            opp_total = int(
                store.read_one("SELECT COUNT(*) FROM opportunities_detected")[0]
            )
            emit_total = int(
                store.read_one(
                    "SELECT COUNT(*) FROM opportunities_detected"
                    " WHERE decision = 'emit'"
                )[0]
            )
            order_total = int(
                store.read_one("SELECT COUNT(*) FROM orders_placed")[0]
            )
        finally:
            store.backend.close()

        ok = opp_total > 0 and emit_total > 0 and order_total >= 2
        _check(
            "Gate 6: smoke-test wrote opportunities + orders to event store",
            ok,
            f"opps={opp_total} emits={emit_total} orders={order_total}",
        )
        if not ok:
            failures.append("gate-events")
    except Exception as exc:  # noqa: BLE001
        _check("Gate 6: event store rows present", False, str(exc))
        failures.append("gate-events")

    print()
    print("=" * 70)
    if failures:
        first = failures[0]
        action = {
            "gate-missing": (
                "CLI should refuse without a probe file but accepted it. "
                "Check common/gates.require_prod_probe enforcement in "
                "paper/runner.py startup_gate."
            ),
            "gate-not-prod": (
                "CLI should refuse on environment=demo. Check the is_prod "
                "check in paper/runner.py._startup_gate."
            ),
            "gate-stale": (
                "CLI should refuse on a >24h probe. Check is_fresh in "
                "paper/runner.py._startup_gate."
            ),
            "gate-live": (
                "CLI should refuse when LIVE_TRADING=true. Check _env_bool "
                "in paper/runner.py._check_env."
            ),
            "gate-happy-path": (
                "Smoke-test did not exit 0. Inspect verify_paper_output.txt "
                "for the pipeline error."
            ),
            "gate-events": (
                "Smoke-test ran but wrote too few rows. Check scanner "
                "threshold vs fake WS prices, or flush()/opp-id lookup "
                "in paper/runner.py."
            ),
        }.get(first, f"'{first}' failed -- see transcript above.")
        print(f"  RESULT: FAIL ({len(failures)} of 6 gates failed: "
              f"{', '.join(failures)})")
        print(f"  ACTION: {action}")
        print("=" * 70)
        print(f"HEADLINE: FAIL -- {action}")
        return 10

    print("  RESULT: PASS -- all 6 gates green.")
    print("  Paper CLI refuses unsafe configs and runs pipeline end-to-end.")
    print("=" * 70)
    print("HEADLINE: PASS -- paper CLI gate + pipeline + SIGINT all green.")
    return 0


def main() -> int:
    # Isolated work dir in tmp. Always cleaned up.
    work = Path(tempfile.mkdtemp(prefix="verify_paper_cli_"))
    try:
        return _verify(work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
