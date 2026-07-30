from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (
    ROOT
    / "scripts/cutover_bayes_basket_probability_evidence_no_live.sh"
)


def test_probability_evidence_cutover_is_syntax_valid_target_only_no_live() -> None:
    subprocess.run(
        ["/bin/bash", "-n", str(SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
    )
    text = SCRIPT.read_text(encoding="utf-8")

    assert "--confirm-probability-evidence-production-no-live" in text
    assert "apply_bayes_basket_probability_evidence_schema.py" in text
    assert '/bin/launchctl bootout "$TARGET_SERVICE"' in text
    assert 'bootout "system/$MARKET_LABEL"' not in text
    assert 'bootout "system/$ORDER_LABEL"' not in text
    assert 'market_pid_after" != "$market_pid_before' in text
    assert 'order_pid_after" != "$order_pid_before' in text
    assert "reported_trajectory_no_production_acceptance.py" in text


def test_probability_evidence_cutover_has_resource_and_zero_order_gates() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    assert "free < 1 << 30" in text
    assert "db_bytes > 256 << 20" in text
    assert "wal_bytes > 64 << 20" in text
    stop_index = text.index('/bin/launchctl bootout "$TARGET_SERVICE"')
    wal_gate_index = text.index(
        "wal_bytes > 64 << 20",
        stop_index,
    )
    assert wal_gate_index > stop_index
    assert "wal_bytes_before_stop" in text
    assert "wal_bytes_after_stop" in text
    assert "PRAGMA quick_check" in text
    assert "basket_target_order_intents" in text
    assert "basket_target_order_events" in text
    assert "basket_target_order_reconciliations" in text
    assert '"calls_order_gateway": False' in text
    assert '"live_submit_authorized": False' in text
    assert '"orders_submitted": 0' in text
    assert "live_pause_retained" in text
    assert "cp " not in text
    assert "rsync " not in text
    assert "rm " not in text
