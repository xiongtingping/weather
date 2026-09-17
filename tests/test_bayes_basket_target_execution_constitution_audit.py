from __future__ import annotations

from pathlib import Path

from scripts.bayes_basket_target_execution_constitution_audit import audit


ROOT = Path(__file__).resolve().parents[1]


def _compliant_runtime() -> str:
    return '''
"""
This explanation mentions 4→3→2, equal_share, and all-leg FOK only as
historical semantics that the target constitution rejects.
"""

# A test-like comment: equal_share and immediate_stage_all_legs_fok_required
# are not executable semantics and must not be scanned.
class ExecutionCandidate:
    TAKER_FOK = "TAKER_FOK"


def decide(target_shares, delta_shares, filled_shares, unknown_fill):
    target_inventory = target_shares
    inventory_gap = delta_shares
    maker_post_only = "MAKER_POST_ONLY"
    cancel_acknowledged = True
    if unknown_fill:
        return {"state": "reconcile_required"}
    if cancel_acknowledged:
        execution_candidate = ExecutionCandidate.TAKER_FOK
        single_bucket_fok = "single_bucket_fok"
        recompute_after_fill = filled_shares
        return {
            "mode": execution_candidate,
            "contract": single_bucket_fok,
            "recompute": recompute_after_fill,
            "live_authorized": False,
            "live_submit_authorized": False,
            "execution_mode": "production_no_live",
        }
    return {"mode": maker_post_only, "live_submit_authorized": False}
'''


def test_active_contract_passes_and_docstrings_comments_are_ignored(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.py"
    runtime.write_text(_compliant_runtime(), encoding="utf-8")

    result = audit(project_root=tmp_path, scope_files=["runtime.py"])

    assert result["ok"] is True
    assert result["scope"]["included_files"] == ["runtime.py"]
    assert result["violations"] == []
    assert result["checks"] == {
        "target_inventory_contract_present": True,
        "post_only_maker_contract_present": True,
        "confirmed_cancel_precedes_fok_contract_present": True,
        "single_bucket_fok_contract_present": True,
        "recompute_after_fill_contract_present": True,
        "unknown_or_unconfirmed_cancel_reconcile_contract_present": True,
        "production_no_live_live_authorized_false": True,
        "runtime_sources_parsed": True,
        "forbidden_fixed_stage_sequence_absent": True,
        "forbidden_equal_share_semantics_absent": True,
        "forbidden_all_legs_fok_semantics_absent": True,
        "scope_resource_bounds_ok": True,
    }


def test_active_legacy_tokens_are_detected_but_test_counterexample_is_outside_scope(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime.py"
    runtime.write_text(
        "STAGED_COUNTS = (4, 3, 2)\n"
        "MODE = 'equal_share'\n"
        "REASON = 'immediate_stage_all_legs_fok_required'\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_counterexample.py").write_text(
        "MODE = 'equal_share'\nSTAGES = (4, 3, 2)\n",
        encoding="utf-8",
    )

    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    (docs_dir / "ADR.md").write_text(
        "The rejected 4→3→2 and all-leg FOK semantics are documented here.\n",
        encoding="utf-8",
    )

    result = audit(
        project_root=tmp_path,
        scope_files=["runtime.py", "tests/test_counterexample.py", "docs/ADR.md"],
    )

    assert result["ok"] is False
    assert result["scope"]["included_files"] == ["runtime.py"]
    codes = {item["code"] for item in result["violations"]}
    assert "forbidden_fixed_stage_sequence" in codes
    assert "forbidden_equal_share_semantics" in codes
    assert "forbidden_all_legs_fok_semantics" in codes
    assert all("tests/" not in item["file"] for item in result["violations"])


def test_fok_router_with_cancel_flag_after_fok_does_not_satisfy_precedence_gate(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime.py"
    runtime.write_text(
        "class ExecutionCandidate:\n"
        "    TAKER_FOK = 'TAKER_FOK'\n\n"
        "def route(execution_candidate):\n"
        "    selected = ExecutionCandidate.TAKER_FOK\n"
        "    cancel_acknowledged = True\n"
        "    return {'execution': selected, 'cancel': cancel_acknowledged}\n",
        encoding="utf-8",
    )

    result = audit(project_root=tmp_path, scope_files=["runtime.py"])

    assert result["ok"] is False
    assert result["checks"]["confirmed_cancel_precedes_fok_contract_present"] is False
    assert any(
        item["code"] == "fok_without_confirmed_cancel_gate"
        for item in result["violations"]
    )


def test_scope_resource_bound_fails_closed_before_large_file_is_parsed(tmp_path: Path) -> None:
    runtime = tmp_path / "runtime.py"
    runtime.write_text("# bounded probe\n" + ("x = 1\n" * 100), encoding="utf-8")

    result = audit(
        project_root=tmp_path,
        scope_files=["runtime.py"],
        max_file_bytes=32,
        max_total_bytes=128,
    )

    assert result["ok"] is False
    assert result["checks"]["scope_resource_bounds_ok"] is False
    assert any(
        item["code"] == "resource_file_bytes_exceeded"
        for item in result["scope_resource_hits"]
    )
