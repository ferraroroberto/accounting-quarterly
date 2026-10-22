r"""The gate's docs-ratchet stage: run the vendored script, map its exit code.

    & .\.venv\Scripts\python.exe -m scripts.docs_ratchet_gate

`scripts/docs_ratchet.py` (vendored, never edited here) exits 0 on a pass, 1 when
a history marker or the README size rose above the baseline, 2 on a bad table or
baseline and 3 when it could not establish the count (git unusable, a file that
is not UTF-8, a fence or comment that never closes). Exit 3 means nothing was
checked, so it fails the gate as its own state and never reads as a pass or a
skip. This wrapper is the one place that mapping lives, so a test can pin it;
`verify-before-ship.ps1` calls the wrapper, never the script.

Exit 0 only when the script exited 0; every other outcome exits 1 with its
reason on the last line.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / "scripts" / "docs_ratchet.py"

# A Markdown line scan over a few dozen files takes under a second; the bound
# only turns a hung child into NOT CONFIRMED instead of a stuck gate.
RUN_TIMEOUT_S = 120

EXIT_PASS, EXIT_NEW, EXIT_CONFIG, EXIT_NOT_CONFIRMED = 0, 1, 2, 3

_FAIL_LABELS = {
    EXIT_NEW: "a history marker or the README size rose above the baseline",
    EXIT_CONFIG: "bad [docs_ratchet] table, baseline or arguments",
    EXIT_NOT_CONFIRMED: "NOT CONFIRMED -- the count could not be established, so nothing was checked",
}


def classify_exit(code: int) -> tuple[bool, str]:
    """`(ok, label)` for one script exit code. Only 0 passes."""
    if code == EXIT_PASS:
        return True, "no new history marker and the README within budget"
    return False, _FAIL_LABELS.get(code, f"unexpected exit {code}")


def run(script: Path = SCRIPT) -> int:
    """Run `script` under this interpreter, echo its output, then the verdict.
    A script that hangs or cannot start is exit 3, NOT CONFIRMED. Returns the
    gate's exit code."""
    try:
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
            errors="replace", stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            timeout=RUN_TIMEOUT_S, check=False,
        )
        code, output = proc.returncode, (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        code, output = EXIT_NOT_CONFIRMED, f"{script.name} did not finish in {RUN_TIMEOUT_S}s\n"
    except OSError as exc:
        code, output = EXIT_NOT_CONFIRMED, f"{script.name} could not start: {exc}\n"
    if output:
        sys.stdout.write(output if output.endswith("\n") else output + "\n")
    ok, label = classify_exit(code)
    print(f"{'OK' if ok else 'FAIL'}: docs ratchet: {label}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(run())
