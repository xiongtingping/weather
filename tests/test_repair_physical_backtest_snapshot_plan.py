from scripts.repair_physical_backtest_snapshot_plan import (
    _merge_plan_rows,
    _update_plan_doc,
)


def test_merge_plan_rows_replaces_same_slug_date() -> None:
    existing = [
        {"slug": "a", "event_date": "2026-06-08", "feature_digest": "old"},
        {"slug": "b", "event_date": "2026-06-08", "feature_digest": "keep"},
    ]
    new_rows = [
        {"slug": "a", "event_date": "2026-06-08", "feature_digest": "new"},
        {"slug": "c", "event_date": "2026-06-09", "feature_digest": "add"},
    ]

    merged = _merge_plan_rows(existing, new_rows)

    assert [(row["slug"], row["feature_digest"]) for row in merged] == [
        ("a", "new"),
        ("b", "keep"),
        ("c", "add"),
    ]


def test_update_plan_doc_keeps_repair_read_only() -> None:
    plan_doc = {
        "candidate_projection": {
            "physical_backtest_apply_plan": {
                "read_only": True,
                "writes_performed": False,
                "rows": [],
            }
        }
    }
    rows = [
        {"slug": f"s-{idx}", "event_date": "2026-06-08", "snapshot_payload": {"rows": [idx]}}
        for idx in range(101)
    ]

    out = _update_plan_doc(
        plan_doc,
        merged_rows=rows,
        added_rows=rows[:3],
        repair_meta={"generated_at": "2026-06-11T00:00:00+00:00"},
    )

    plan = out["candidate_projection"]["physical_backtest_apply_plan"]
    repair = out["candidate_projection"]["physical_backtest_apply_plan_repair"]
    assert plan["ok"] is True
    assert plan["read_only"] is True
    assert plan["writes_performed"] is False
    assert plan["apply_allowed"] is False
    assert plan["ready_row_count"] == 101
    assert plan["repaired_added_row_count"] == 3
    assert repair["read_only"] is True
    assert repair["writes_performed"] is False
