from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPLACEMENT = (
    "scripts/cutover_bayes_basket_target_probability_evidence_no_live.sh"
)
TARGETCTL = "scripts/targetctl.sh"
SCRIPTS = (
    "cutover_rkpk_jit_ws_no_live.sh",
    "cutover_rkpk_preliminary_ws_decision_no_live.sh",
    "cutover_rkpk_vertical_slice_v4_no_live.sh",
    "reload_p0_economic_identity_system_no_live.sh",
    "reload_probability_consumer_authority_system_no_live.sh",
)
FORBIDDEN = (
    "system/",
    "LaunchDaemon",
    "sudo",
    "launchctl",
    "sqlite",
    "python",
    "touch",
    "write",
)


def test_all_rkpk_and_system_reload_entries_are_gone() -> None:
    """The five entries were stubbed to ``exit 78`` when they retired and
    deleted outright with the July-August legacy tree (2026-09-17); absent is
    the strongest form of retired, and none may come back."""

    assert [name for name in SCRIPTS if (ROOT / "scripts" / name).exists()] == []
