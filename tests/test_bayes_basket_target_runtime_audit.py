from __future__ import annotations

from pathlib import Path

from scripts.bayes_basket_target_runtime_audit import audit


def test_target_runtime_has_one_worker_owner_and_no_legacy_or_live_route() -> None:
    result = audit(project_root=Path(__file__).parents[1])

    assert result["ok"] is True
    assert result["class_owners"] == ["bayes_basket_target/production_worker.py"]
    assert result["forbidden_imports"] == []
    assert result["forbidden_calls"] == []
    assert result["boundary"] == {
        "authorizes_order": False,
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "writes_shared_live_db": False,
        "network_access": False,
        "read_only_audit": True,
    }

