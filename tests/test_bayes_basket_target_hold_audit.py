from __future__ import annotations

from pathlib import Path

from bayes_basket_target.hold_audit import audit_basket_hold_closure


def test_basket_hold_closure_is_verified() -> None:
    receipt = audit_basket_hold_closure(project_root=Path(__file__).resolve().parents[1])
    assert receipt["hold_closure_verified"] is True
    assert receipt["hold_policy"] == {
        "hold_to_settlement": True,
        "merge": False,
        "selective_rebalance": False,
        "early_sell": False,
    }
    assert receipt["boundary"]["writes_production_db"] is False


def test_hold_audit_detects_executable_exit_call(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("def run():\n    return sell_position('token')\n", encoding="utf-8")
    receipt = audit_basket_hold_closure(project_root=tmp_path, roots=[bad])
    assert receipt["hold_closure_verified"] is False
    assert receipt["forbidden_executable_call_hits"][0]["call"] == "sell_position"
