#!/usr/bin/env bash
# kalshi-arb dashboard -- one-click launcher (macOS / Linux).
# Sister of start_dashboard.bat; same behavior.

set -u
cd "$(dirname "$0")"

echo
echo "=================================================================="
echo "  Starting kalshi-arb dashboard launcher"
echo "  Wait for the DASHBOARD URL banner (about 10 seconds)."
echo "=================================================================="
echo

python3 -m kalshi_arb.dashboard.launcher
exit $?
