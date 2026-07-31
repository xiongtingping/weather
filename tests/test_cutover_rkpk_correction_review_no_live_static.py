import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/cutover_rkpk_correction_review_no_live.sh"


def test_cutover_script_is_retired_before_any_state_change() -> None:
    subprocess.run(
        ["/bin/bash", "-n", str(SCRIPT)],
        check=True,
        capture_output=True,
        text=True,
    )
    text = SCRIPT.read_text(encoding="utf-8")

    assert "exit 78" in text
    assert "cutover_bayes_basket_target_probability_evidence_no_live.sh" in text
    assert "launchctl" not in text
    assert "sudo" not in text
    assert "/Library/LaunchDaemons" not in text


def test_cutover_has_no_old_rkpk_or_database_mutation_logic() -> None:
    text = SCRIPT.read_text(encoding="utf-8")

    for forbidden in (
        "apply_reported_trajectory_correction_review_schema.py",
        "review_reported_trajectory_station_halt.py",
        "CLEAR_REFERENCE_ONLY",
        "sqlite",
        "bootout",
        "bootstrap",
        "kickstart",
    ):
        assert forbidden not in text
