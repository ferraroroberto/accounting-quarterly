#Requires -Version 5.1
<#
.SYNOPSIS
    Pre-ship verification gate for accounting-quarterly. One pass/fail pipeline.

.DESCRIPTION
    Stages, fail-fast:
      1. byte-compile  - every .py under app/ src/ scripts/ tests/ parses
      2. docs ratchet  - README.md within 40 KiB and no new history marker (issue
                         reference, dated or changelog heading) in README.md or
                         docs/**/*.md beyond .docs-ratchet-baseline.json. Whole
                         tree, never routed; exit 3 (count not established) is
                         NOT CONFIRMED and fails, never a pass or a skip.
      3. pytest        - the unit suite

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
# The wrapper maps the script's exit 3 to NOT CONFIRMED (tests/test_docs_ratchet_gate.py
# pins it); call it, never scripts/docs_ratchet.py directly.
Invoke-Stage "docs ratchet"  { & $py -m scripts.docs_ratchet_gate }
Invoke-Stage "pytest"        { & $py -m pytest }

Write-Host ""
Write-Host "[PASS] all checks green - safe to ship." -ForegroundColor Green
