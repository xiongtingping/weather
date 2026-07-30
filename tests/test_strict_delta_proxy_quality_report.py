from __future__ import annotations

from scripts.build_strict_delta_proxy_quality_report import build_report


def test_proxy_quality_is_disjoint_but_never_promotable() -> None:
    report = build_report()
    assert report["status"] == "NO_LIVE_REPLAY_CANDIDATE"
    assert report["promotion_permitted"] is False
    assert report["formal_oos_artifact_permitted"] is False
    split = report["fixed_event_split"]
    assert split["event_level_disjoint"] is True
    assert split["event_overlap_count"] == 0
    assert split["train_window"]["row_count"] == 2157
    assert split["holdout_window"]["row_count"] == 1237
    assert "official CLOB WS multi-level snapshot at each decision" in (
        report["missing_formal_decision_point_evidence"]
    )
    assert report["boundary"]["orders_submitted"] == 0
    assert report["boundary"]["resource_bounded"] is True
