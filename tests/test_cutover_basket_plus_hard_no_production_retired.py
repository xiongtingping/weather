from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_basket_plus_hard_no_production.py"


def test_live_basket_cutover_is_permanently_retired() -> None:
    source = SCRIPT.read_text(encoding="utf-8").lower()
    assert "retired" in source
    assert "production_no_live" in source
    assert "targetctl.sh" in source
    for forbidden in (
        "launchctl",
        "sudo",
        "sqlite",
        "gateway_ready",
        "live_submit_authorized",
        "pause.unlink",
        "os.kill",
    ):
        assert forbidden not in source
    completed = subprocess.run(
        [sys.executable, str(SCRIPT), "--confirm-live-basket-plus-hard-no"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 78
    assert "retired" in completed.stderr
