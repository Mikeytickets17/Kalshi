#!/usr/bin/env bash
# Validate every .ps1 script in the repo:
#   1. UTF-8 BOM present (Windows PowerShell 5.1 needs this for non-ASCII)
#   2. PowerShell parse check — no syntax errors
#   3. Runtime smoke test of one_click.ps1 with git/python/pip/pytest mocked —
#      catches cwd drift, unhandled exceptions, and path resolution bugs
#
# Run before every commit that touches a .ps1 file.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

fail() { echo "[FAIL] $*"; exit 1; }
ok()   { echo "[OK]   $*"; }

command -v pwsh >/dev/null 2>&1 || fail "pwsh not installed. sudo apt-get install -y powershell"

# ---- 1. BOM + encoding check -------------------------------------------
for f in $(find . -name '*.ps1' -not -path './.venv/*'); do
    # first 3 bytes must be UTF-8 BOM
    bom="$(python3 -c "import sys; print(open(sys.argv[1],'rb').read(3).hex())" "$f")"
    if [ "$bom" != "efbbbf" ]; then
        fail "$f missing UTF-8 BOM (first 3 bytes: $bom). Run: python3 -c \"import sys; d=open(sys.argv[1],'rb').read(); open(sys.argv[1],'wb').write(b'\\xef\\xbb\\xbf'+d if not d.startswith(b'\\xef\\xbb\\xbf') else d)\" '$f'"
    fi
    # warn on any non-ASCII bytes (after BOM) — they commonly break PS 5.1 string parsing
    tail -c +4 "$f" | python3 -c "
import sys
data = sys.stdin.buffer.read()
bad = [(i, b) for i, b in enumerate(data) if b > 127]
if bad:
    print(f'[FAIL] Non-ASCII bytes in $f (will break PS 5.1 parser):')
    for i, b in bad[:10]:
        ctx = data[max(0, i-15):i+15].decode('utf-8', errors='replace')
        print(f'  offset {i} (0x{b:02x}): ...{ctx}...')
    sys.exit(1)
"
    ok "$f encoding"
done

# ---- 1b. Antipattern check -------------------------------------------
# Bugs we've shipped once and must never ship again get a dedicated static
# check. Failures here mean a regression to a known-bad pattern.
for f in $(find . -name '*.ps1' -not -path './.venv/*'); do
    python3 <<PY
import re, sys
src = open("$f").read()
# Strip comments so we don't trip on doc references
no_comments = re.sub(r'(?m)^\s*#.*$', '', src)

# Antipattern: Push-Location inside a try {} whose catch{} does not Pop.
# On Windows PS 5.1 this strands cwd if an external command writes to stderr
# with \$ErrorActionPreference=Stop. The operator hit this exact bug on
# one_click.ps1 during Push #1 setup. Do not reintroduce.
for m in re.finditer(r'try\s*\{([^}]*)\}\s*catch\s*\{([^}]*)\}', no_comments, re.DOTALL):
    try_body, catch_body = m.group(1), m.group(2)
    if 'Push-Location' in try_body and 'Pop-Location' not in catch_body:
        print(f"[FAIL] {('$f')}: Push-Location in try{{}} without matching Pop-Location in catch{{}} "
              f"(or missing try/finally). Use 'git -C <path>' or wrap in try/finally.")
        sys.exit(1)
print(f"[OK]   $f antipatterns")
PY
done

# ---- 2. Parse check via PowerShell AST ---------------------------------
for f in $(find . -name '*.ps1' -not -path './.venv/*'); do
    pwsh -NoProfile -c "
\$errors = \$null
\$null = [System.Management.Automation.Language.Parser]::ParseFile('$f', [ref]\$null, [ref]\$errors)
if (\$errors -and \$errors.Count -gt 0) {
    \$errors | ForEach-Object { Write-Host \"  line \$(\$_.Extent.StartLineNumber):\$(\$_.Extent.StartColumnNumber): \$(\$_.Message)\" }
    exit 1
}
" || fail "$f parse errors"
    ok "$f parse"
done

# ---- 3. Runtime smoke test of one_click.ps1 ----------------------------
# Mock git / python / pip / pytest / notepad / Microsoft.VisualBasic.Interaction
# so we can run the script end-to-end and assert every phase reports Green.
WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# Build a fake repo layout identical to what the script expects
cp one_click.ps1      "$WORKDIR/one_click.ps1"
cp pyproject.toml     "$WORKDIR/pyproject.toml"
cp .env.example       "$WORKDIR/.env.example"
mkdir -p "$WORKDIR/tests" "$WORKDIR/kalshi_arb/probe" "$WORKDIR/config"
touch    "$WORKDIR/kalshi-demo.pem"

# Mock external commands so they succeed silently with $LASTEXITCODE=0
MOCK_BIN="$WORKDIR/mockbin"
mkdir -p "$MOCK_BIN"
for cmd in pip pytest notepad.exe; do
    cat > "$MOCK_BIN/$cmd" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "$MOCK_BIN/$cmd"
done

# Special mock for git: writes to stderr like the real binary (progress lines),
# which is how the Push-Location/try-catch regression was triggered.
cat > "$MOCK_BIN/git" <<'EOF'
#!/usr/bin/env bash
# Match real git: progress on stderr, data on stdout
echo "remote: Enumerating objects: 3, done." >&2
echo "From https://github.com/fake/repo" >&2
echo "Already up to date."
exit 0
EOF
chmod +x "$MOCK_BIN/git"
# Special python mock: echo args so we can verify cwd
cat > "$MOCK_BIN/python" <<'EOF'
#!/usr/bin/env bash
echo "[mock python] pwd=$(pwd) args=$*"
# Write an expected output file when asked to run the probe
if [[ "$*" == *"kalshi_arb.probe.probe"* ]]; then
    mkdir -p config
    cat > config/detected_limits.yaml <<'YAML'
ts_utc: '2026-04-17T00:00:00Z'
environment: demo
ws_subscription: {max_confirmed_tickers: 0, environment: demo}
YAML
fi
exit 0
EOF
chmod +x "$MOCK_BIN/python"

# Also provide Start-Process replacement by stubbing a PS module
cat > "$WORKDIR/stub_gui.ps1" <<'EOF'
# Stub out interactive Windows-only APIs so the script runs on Linux pwsh
function Global:Start-Process { param($a,$b) Write-Host "[stub Start-Process] $a $b" }
# Pretend Microsoft.VisualBasic.Interaction.InputBox returned a fake key id
Add-Type -TypeDefinition @"
namespace Microsoft.VisualBasic {
    public class Interaction {
        public static string InputBox(string p, string t, string d) { return "stub-key-id-for-testing"; }
    }
}
"@ -Language CSharp
EOF

# Run the script with the mocks on PATH and the GUI stubs preloaded
cd "$WORKDIR"
# Linux pwsh can't load Windows-only assemblies (System.Windows.Forms,
# Microsoft.VisualBasic). The smoke test validates every phase up to the
# first GUI call — if the script reaches "[3/7] Creating the .env file..."
# AND the cwd is correct at that point, the bugs we've actually hit
# (em-dash parse errors, Push-Location cwd drift) would have been caught.
SMOKE_OUT=$(PATH="$MOCK_BIN:$PATH" pwsh -NoProfile -c "
. ./stub_gui.ps1
try {
    . ./one_click.ps1
} catch {
    Write-Host \"[smoke] stopped at: \$_\"
}
Write-Host \"[smoke] final cwd: \$(Get-Location)\"
" 2>&1)
echo "$SMOKE_OUT" | grep -q '\[1/7\] Pulling latest code'     || fail "one_click.ps1 smoke: phase 1/7 missing. Output:\n$SMOKE_OUT"
echo "$SMOKE_OUT" | grep -q '\[2/7\] Looking for kalshi-demo' || fail "one_click.ps1 smoke: phase 2/7 missing"
echo "$SMOKE_OUT" | grep -q 'Already in place'               || fail "one_click.ps1 smoke: PEM not detected when present"
echo "$SMOKE_OUT" | grep -q '\[3/7\] Creating the .env file' || fail "one_click.ps1 smoke: phase 3/7 missing (cwd drift regression?)"
echo "$SMOKE_OUT" | grep -q "final cwd: $WORKDIR"            || fail "one_click.ps1 smoke: cwd drifted from \$here. Full output:\n$SMOKE_OUT"
cd "$REPO_ROOT"
ok "one_click.ps1 smoke test (phases 1-3 + cwd stable)"

echo ""
echo "All PowerShell validation checks passed."
