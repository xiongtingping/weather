from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_rkpk_correction_review_no_live.sh"


def test_cutover_script_is_valid_and_narrowly_scoped() -> None:
    subprocess.run(
        ["/bin/bash", "-n", str(SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
    )
    text = SCRIPT.read_text(encoding="utf-8")

    assert "--confirm-rkpk-reference-only" in text
    assert "apply_reported_trajectory_correction_review_schema.py" in text
    assert "review_reported_trajectory_station_halt.py" in text
    assert "CLEAR_REFERENCE_ONLY" in text
    assert '/bin/launchctl bootout "$SERVICE"' in text
    assert "bootout system/com.weather.market_ws" not in text
    assert "bootout system/com.weather.clob_order_service" not in text
    assert 'market_after" != "$market_before' in text
    assert 'clob_after" != "$clob_before' in text
    assert "reported_trajectory_no_production_acceptance.py" in text


def test_cutover_forbids_full_archive_copy_and_live_authorization() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "cp " not in text
    assert "rsync " not in text
    assert "rm " not in text
    assert '"calls_order_gateway": False' in text
    assert '"live_submit_authorized": False' in text
    assert '"orders_submitted": 0' in text
