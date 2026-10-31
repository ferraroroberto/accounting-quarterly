#Requires -Version 5.1
<#
.SYNOPSIS
    Pre-ship verification gate for accounting-quarterly. One pass/fail pipeline.

.DESCRIPTION
    Stages, fail-fast:
      1. byte-compile  - every .py under app/ src/ scripts/ tests/ parses
      2. static ratchet - a duplicate block (jscpd, Python + JS), an unused import or
                         variable (ruff F401/F841) or a curated ruff bug/security hit
                         beyond .static-ratchet-baseline.json fails. Needs Node (npx)
                         and ruff in .venv. Whole tree, never routed; exit 3 (a
                         detector could not run) is NOT CONFIRMED and fails, never a
                         pass.
      3. comment ratchet - a #NNN issue reference in a product-code comment (py, js,
                         css, ps1; tests and vendored files left out) that raises a
                         file's count above .comment-ratchet-baseline.json fails.
                         Whole tree, never routed; exit 3 (a file could not be
                         scanned) is NOT CONFIRMED and fails, never a pass.
      3b. docstring ratchet - repo-local (not vendored): a #NNN issue reference
                         inside a Python module/class/function docstring -- the
                         vendored comment ratchet's documented blind spot -- that
                         raises a file's count above .docstring-ratchet-baseline.json
                         fails. Tracked .py files, tests and vendored files left out;
                         exit 3 (git unusable, a file unreadable or unparseable) is
                         NOT CONFIRMED and fails, never a pass.
      4. conventions ratchet - a file whose count of no-window (a subprocess spawn
                         without creationflags), logging-not-print (print() in app/
                         or src/ library code) or no-drive-path (a hardcoded drive
                         path) hits rises above .conventions-ratchet-baseline.json
                         fails. Whole tree, never routed; exit 3 (a file could not
                         be parsed) is NOT CONFIRMED and fails, never a pass.
      5. docs ratchet  - README.md within 40 KiB and no new history marker (issue
                         reference, dated or changelog heading) in README.md or
                         docs/**/*.md beyond .docs-ratchet-baseline.json. Whole
                         tree, never routed; exit 3 (count not established) is
                         NOT CONFIRMED and fails, never a pass or a skip.
      6. structure ratchet - a file past 1,000 lines, a function past 100, a
                         directory past 40 modules or a src/ module importing app/
                         beyond .structure-ratchet-baseline.json, or a root folder
                         outside the declared shape, fails. Whole tree, never
                         routed; exit 3 (not confirmed) fails, never a pass.
      7. instruction ratchet - a rule hit in CLAUDE.md, AGENTS.md, .claude/rules or
                         a SKILL.md (or a file passing its size cap) beyond
                         .instruction-ratchet-baseline.json fails. Whole tree,
                         never routed; exit 3 (not confirmed) fails, never a pass.
      8. doc-refs ratchet - a repo path, python -m module, relative link or loopback
                         port named in README.md, CLAUDE.md or docs/**/*.md that
                         does not exist, beyond .doc-refs-ratchet-baseline.json,
                         fails. Whole tree, never routed; exit 3 (not confirmed)
                         fails, never a pass.
      9. dead-code ratchet - a new Python function, class or method that nothing in
                         the repo refers to, beyond .dead-code-ratchet-baseline.json,
                         fails. Whole tree, never routed; exit 3 (not confirmed)
                         fails, never a pass.
      10. pytest       - the unit suite

    Anchors to the repo root, so run it from anywhere:
        & .\scripts\verify-before-ship.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")

$py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[FAIL] .venv not found at $py" -ForegroundColor Red
    Write-Host "       Create it: py -m venv .venv; then $py -m pip install -r requirements.txt" -ForegroundColor Red
    exit 1
}

# Piped/redirected stdout makes Python fall back to cp1252 on Windows and the
# emoji log markers then throw UnicodeEncodeError.
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"

. (Join-Path $PSScriptRoot "box_lock_gate.ps1"); Enter-BoxLockGate

function Invoke-Stage {
    param([string]$Name, [scriptblock]$Body)
    Write-Host ""
    Write-Host ">> $Name" -ForegroundColor Cyan
    & $Body
    if ($LASTEXITCODE -ne 0) {
        Write-Host "[FAIL] $Name (exit $LASTEXITCODE)" -ForegroundColor Red
        exit 1
    }
    Write-Host "[PASS] $Name" -ForegroundColor Green
}

Invoke-Stage "byte-compile"  { & $py -m compileall -q app src scripts tests }
# Not path-routed: duplication needs the whole tree. Exit 3 (npx or ruff unusable)
# fails here the same way.
Invoke-Stage "static ratchet" { & $py "scripts/static_ratchet.py" }
# Not path-routed: a count per file needs the whole tree. Exit 3 fails here the same way.
Invoke-Stage "comment ratchet" { & $py "scripts/comment_ratchet.py" }
# Repo-local script (not vendored): covers the vendored comment ratchet's documented
# docstring blind spot. Not path-routed; exit 3 fails here the same way.
Invoke-Stage "docstring ratchet" { & $py "scripts/docstring_ratchet.py" }
# Not path-routed: a rule added to `rules` must see the whole tree. Exit 3 fails here too.
Invoke-Stage "conventions ratchet" { & $py "scripts/conventions_ratchet.py" }
# Not path-routed: any README or docs change can add a history marker. Exit 3 fails here too.
Invoke-Stage "docs ratchet"  { & $py "scripts/docs_ratchet.py" }
# Invoke-Stage fails on any non-zero exit, so the script's exit 3 (git unusable, an
# unparseable file, an unknown shape: nothing was checked) is a failure, not a pass.
Invoke-Stage "structure ratchet" { & $py "scripts/structure_ratchet.py" }
# Not path-routed: the reader of one file depends on another (AGENTS.md), and a skill's
# reference docs live outside its folder. Exit 3 fails here the same way.
Invoke-Stage "instruction ratchet" { & $py "scripts/instruction_ratchet.py" }
# Not path-routed: a code change can strand a doc reference without touching the doc.
Invoke-Stage "doc-refs ratchet" { & $py "scripts/doc_refs_ratchet.py" }
# Not path-routed: deleting the last caller in one file strands a definition in another.
Invoke-Stage "dead-code ratchet" { & $py "scripts/dead_code_ratchet.py" }
Invoke-Stage "pytest"        { & $py -m pytest }

Write-Host ""
Write-Host "[PASS] all checks green - safe to ship." -ForegroundColor Green
