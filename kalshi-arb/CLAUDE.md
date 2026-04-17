# kalshi-arb — session guidance

## ABSOLUTE RULE: TEST WINDOWS SCRIPTS BEFORE PUSHING

The operator is on Windows and runs `.bat` / `.ps1` files by double-clicking.
They do not debug. Every broken script costs them a full round-trip.

**Before committing any change to a `.ps1` or `.bat` file, run:**

```bash
bash scripts/validate_powershell.sh
```

The script enforces:

1. **UTF-8 BOM** on every `.ps1`. Windows PowerShell 5.1 reads non-BOM files
   as Windows-1252, which corrupts any non-ASCII byte and poisons the quote
   parser. The first historical failure — `one_click.ps1:176 "missing
   terminator"` — was this exact bug, caused by em-dashes in comments.
2. **No non-ASCII bytes** anywhere in the file body. Use `--`, not `—`. Use
   `-`, not `–`. Use `'`, not `'`. Use `"`, not `"`.
3. **PowerShell AST parse** passes (`[Parser]::ParseFile` with zero errors).
4. **Runtime smoke test** with `git`, `python`, `pip`, `pytest`, `notepad.exe`
   all mocked. Verifies:
   - Script reaches phases 1 → 2 → 3
   - Cwd stays at `$here` through the git-pull step (the second historical
     failure was `Push-Location ..` + `try/catch` skipping `Pop-Location`,
     leaving pip install running from the wrong folder)

If the validation script fails, DO NOT push. Fix the script and rerun.

## When you modify a `.ps1`, you must:

1. Edit the file.
2. Run `python3 -c "import sys; ..."` or equivalent to re-add the UTF-8 BOM
   (the Edit tool may strip it).
3. Run `bash scripts/validate_powershell.sh` and confirm "All PowerShell
   validation checks passed."
4. Only then `git commit` and `git push`.

## Environment conventions

- The operator's Windows path: `C:\Users\mikey\Kalshi\kalshi-arb\`
- PowerShell 5.1 is the default (comes with Windows). It is picky about
  encoding and string parsing. Do not assume PowerShell 7 features.
- VS Code terminal defaults to PowerShell 5.1 on Windows.
- `Set-Location` and `Push-Location`/`Pop-Location` inside a `try/catch`
  block are DANGEROUS. The `catch` can skip the pop and strand cwd.
  Prefer `git -C <path>` over changing directories.

## Testing

- `python -m pytest tests/` — unit tests for the Python code.
- `bash scripts/validate_powershell.sh` — validates `.bat` / `.ps1` scripts.
- The operator's Kalshi demo key is IP-allowlisted to their home IP, so
  probe cannot run from the Claude sandbox. Only tests + script validation
  run locally. Actual probe runs happen on the operator's machine.
