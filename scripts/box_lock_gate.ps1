#Requires -Version 5.1
<#
.SYNOPSIS
    Holds fleet-config's shared box load lock for the whole of a gate run.

.DESCRIPTION
    Vendor-verbatim: copy byte-for-byte into a repo's scripts/ and, in the gate
    script, after its own `Set-Location` and before its first stage, add:

        . (Join-Path $PSScriptRoot "box_lock_gate.ps1"); Enter-BoxLockGate

    A CPU-burn proof saturates the shared box and starves a browser stage into
    timeouts, so gates take ~/.claude/skills/_lib/box_lock.py in shared mode
    (many gates together) and a burn takes it exclusively. Enter-BoxLockGate
    re-runs the gate script under `box_lock.py run --mode shared` and exits
    with its code; the re-run sees VERIFY_BOX_LOCK_HELD=1 and returns at once,
    so the gate falls through to its stages.

    It is a function, not top-level code in this file, because `exit` in a
    dot-sourced file ends only that file, while `exit` in a function ends the
    calling gate script.

    Exit 3 is the helper's "lock not taken in time" code. A gate that itself
    exits 3 would be indistinguishable from it, so a gate wrapped by this file
    must exit only 0 or 1.

    The lock is advisory and machine-local, and a gate must still run where
    fleet-config is absent. A missing helper, or one that cannot be launched,
    prints a `box load lock NOT CONFIRMED` warning and the gate runs unlocked;
    it is never skipped silently and never a hang (the helper's wait is
    bounded). The gate's own result is unaffected.

    The gate's bound parameters are forwarded to the re-run when they are
    switches or scalars. Any other parameter type cannot be forwarded
    faithfully, so the gate runs unlocked with the same warning rather than
    run with different arguments.
#>

function Get-BoxLockPython {
    $venvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
    if (Test-Path $venvPython) { return $venvPython }
    $onPath = Get-Command python -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($onPath) { return $onPath.Source }
    return $null
}

function Get-BoxLockForwardedArgs {
    param([hashtable]$Bound)
    # $null = a bound parameter that cannot be forwarded; an empty array = none.
    $forwarded = @()
    foreach ($name in $Bound.Keys) {
        $value = $Bound[$name]
        if ($value -is [System.Management.Automation.SwitchParameter]) {
            if ($value.IsPresent) { $forwarded += "-$name" }
        } elseif ($value -is [string] -or $value -is [ValueType]) {
            $forwarded += "-$name"
            $forwarded += [string]$value
        } else {
            return $null
        }
    }
    return , $forwarded
}

function Enter-BoxLockGate {
    if ($env:VERIFY_BOX_LOCK_HELD -eq "1") { return }

    # The caller's script path and parameters: this function runs in a child
    # scope of the gate script, so the gate's own variables are one scope up.
    $gate = $MyInvocation.PSCommandPath
    $bound = (Get-Variable PSBoundParameters -Scope 1).Value
    $helper = Join-Path $env:USERPROFILE ".claude\skills\_lib\box_lock.py"
    $python = Get-BoxLockPython
    $forwarded = Get-BoxLockForwardedArgs -Bound $bound

    $reason = $null
    if (-not $gate) {
        $reason = "the gate script path is unknown"
    } elseif (-not (Test-Path $helper)) {
        $reason = "box_lock.py not found at $helper"
    } elseif (-not $python) {
        $reason = "no python interpreter to run box_lock.py"
    } elseif ($null -eq $forwarded) {
        $reason = "a gate parameter cannot be forwarded to the locked re-run"
    }
    if ($reason) {
        Write-Host "[WARN] box load lock NOT CONFIRMED - $reason - running the gate WITHOUT the box load lock" -ForegroundColor Yellow
        return
    }

    $repo = (Split-Path -Leaf (Get-Location)) -replace '-wt-\d+$', ''
    $env:VERIFY_BOX_LOCK_HELD = "1"
    try {
        & $python $helper run --mode shared --repo $repo -- (Get-Process -Id $PID).Path -NoProfile -File $gate @forwarded
        $lockExit = $LASTEXITCODE
    } finally {
        Remove-Item Env:VERIFY_BOX_LOCK_HELD -ErrorAction SilentlyContinue
    }
    if ($lockExit -eq 3) {
        Write-Host "[FAIL] box load lock not taken in time (a CPU burn holds it) - the gate did not run" -ForegroundColor Red
    }
    exit $lockExit
}
