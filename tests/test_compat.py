"""Runs check-compat.py as a test — CI will catch upstream drift."""
import subprocess, sys, pathlib

REPO = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO / "upstream" / "scripts" / "check-compat.py"


def test_constants_match_upstream():
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True, text=True, cwd=str(REPO),
    )
    assert result.returncode == 0, (
        "Upstream compatibility check failed:\n" + result.stdout + result.stderr
    )
