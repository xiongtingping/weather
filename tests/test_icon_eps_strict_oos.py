from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.icon_eps_strict_oos import (
    MIN_HOLDOUT_DAYS,
    MIN_TRAIN_DAYS,
    build_icon_eps_strict_oos_report,
)
from bayes_basket_target.icon_eps_training_facts import TRAINING_BATCH_SCHEMA, TRAINING_FACT_SCHEMA
from bayes_basket_target.fleet import expected_station_count


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _fact(index: int, *, arrival_index: bool = True) -> dict[str, object]:
    day = date(2026, 6, 1) + timedelta(days=index)
    event_date = day.isoformat()
    run = datetime.combine(day - timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).replace(hour=18)
    run_text = run.isoformat().replace("+00:00", "Z")
    values = [20.0 + index / 10.0 + member / 100.0 for member in range(1, 41)]
    members = [
        {
            "member_id": f"member_{member:02d}",
            "member_role": "PERTURBED",
            "raw_tmax_c": value,
            "curve_sha256": _sha(f"curve-{index}-{member}"),
            "component_cohort_sha256": [_sha(f"cohort-{index}-{member}")],
        }
        for member, value in enumerate(values, 1)
    ]
    identity = {
        "schema": TRAINING_FACT_SCHEMA,
        "provider_id": "icon_global_eps_official",
        "model_id": "ICON",
        "ensemble_system_id": "DWD_ICON_GLOBAL_EPS",
        "independence_family_id": "DWD_ICON_EPS",
        "station_id": "KATL",
        "event_date": event_date,
        "settlement_timezone": "America/New_York",
        "run_initialization_utc": run_text,
        "run_cycle": "18Z",
        "member_count": 40,
        "member_daily_max_c": members,
        "member_set_sha256": _sha(f"member-set-{index}"),
        "daily_curve_capture_sha256": _sha(f"capture-{index}"),
        "daily_archive_content_sha256": _sha("archive"),
        "forecast_available_at_utc": (run + timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "raw_leads": [
            {
                "valid_time_utc": (run + timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
                "forecast_hour": 12,
                "raw_sha256": _sha(f"raw-{index}"),
                "cohort_content_sha256": _sha(f"cohort-{index}"),
            }
        ],
        "official_tmax_c": 20.0 + index / 10.0 + 0.4,
        "truth_id": f"truth-{index}",
        "truth_content_sha256": _sha(f"truth-{index}"),
        "truth_canonical_committed_at_utc": (datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=5)).isoformat().replace("+00:00", "Z"),
        "settlement_closed_at_utc": (datetime.combine(day + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc) + timedelta(hours=4)).isoformat().replace("+00:00", "Z"),
        "source_lineage": {
            "forecast_source_role": "official_native",
            "forecast_transport_provider_id": "DWD",
            "forecast_raw_member_fanout": False,
            "official_grid_uuid": "ae487d14fe2e11e4af85e50a2a56a360",
            "official_grid_cell_count": 737280,
            "native_arrival_index_eligible": arrival_index,
            "arrival_lineage_status": "NATIVE_INDEX_VERIFIED" if arrival_index else "INDEX_PENDING",
            "native_arrival_index_evidence_sha256": _sha("arrival-index") if arrival_index else None,
            "truth_source_lane": "AWC",
            "truth_authority_scope": "FORMAL_NATIVE_ELIGIBLE",
            "truth_closed_day_required": True,
        },
        "boundary": {
            "writes_target_database": False,
            "writes_production_database": False,
            "downloads_raw": False,
            "creates_formal_provider_binding": False,
            "activates_p0": False,
            "formal_p0_eligible": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    digest = content_sha256(identity)
    return {
        **identity,
        "training_fact_id": "icon-eps-" + digest[:32],
        "content_sha256": digest,
    }


def _batch(facts: list[dict[str, object]], unresolved: list[dict[str, object]] | None = None) -> dict[str, object]:
    identity = {
        "schema": TRAINING_BATCH_SCHEMA,
        "state": "ACCUMULATING_STRICT_OOS",
        "as_of_utc": "2026-08-01T00:00:00Z",
        "provider_id": "icon_global_eps_official",
        "model_id": "ICON",
        "ensemble_system_id": "DWD_ICON_GLOBAL_EPS",
        "independence_family_id": "DWD_ICON_EPS",
        "daily_archive_content_sha256": _sha("archive"),
        "run_initialization_utc": "2026-05-31T18:00:00Z",
        "event_date": "2026-06-01",
        "station_count": 1,
        "fact_count": len(facts),
        "facts": facts,
        "unresolved_station_days": unresolved or [],
        "strict_oos_state": "PENDING_ROLLING_STRICT_OOS",
        "formal_p0_eligible": False,
        "boundary": {
            "writes_target_database": False,
            "writes_production_database": False,
            "downloads_raw": False,
            "creates_formal_provider_binding": False,
            "activates_p0": False,
            "calls_order_gateway": False,
            "orders_submitted": 0,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_native_arrival_index_and_awc_truth_form_strict_oos_candidate() -> None:
    # The first holdout's native receipt precedes the immediately preceding
    # day's AWC truth commit, so one extra fact is needed for 20 leak-free
    # training days (29 total for an 8-day holdout).
    facts = [_fact(index) for index in range(MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS + 1)]
    result = build_icon_eps_strict_oos_report(
        [_batch(facts)], as_of_utc="2026-08-02T00:00:00Z"
    )

    assert result["candidate_count"] == 1
    candidate = result["candidates"][0]
    assert candidate["state"] == "STRICT_OOS_CANDIDATE_NOT_BINDING"
    assert candidate["formal_p0_eligible"] is False
    assert len(candidate["predictions"]) == MIN_HOLDOUT_DAYS
    assert result["boundary"]["orders_submitted"] == 0


def test_provider_receipts_without_native_arrival_index_stay_blocked() -> None:
    facts = [_fact(index, arrival_index=False) for index in range(MIN_TRAIN_DAYS + MIN_HOLDOUT_DAYS + 1)]
    result = build_icon_eps_strict_oos_report(
        [_batch(facts)], as_of_utc="2026-08-02T00:00:00Z"
    )

    assert result["candidate_count"] == 0
    readiness = [row for row in result["readiness"] if row["station_id"] == "KATL"]
    assert len(readiness) == 1
    assert "arrival lineage 缺失" in readiness[0]["blockers"]
    assert readiness[0]["arrival_lineage_case_count"] == 0
    assert result["formal_p0_eligible"] is False


def test_icon_truth_before_station_close_cannot_enter_oos() -> None:
    fact = _fact(0)
    fact["truth_canonical_committed_at_utc"] = "2026-06-01T01:00:00Z"
    identity = {
        key: value
        for key, value in fact.items()
        if key not in {"training_fact_id", "content_sha256"}
    }
    fact["content_sha256"] = content_sha256(identity)
    fact["training_fact_id"] = "icon-eps-" + fact["content_sha256"][:32]

    with pytest.raises(ContractError, match="truth_before_settlement_close"):
        build_icon_eps_strict_oos_report(
            [_batch([fact])], as_of_utc="2026-08-02T00:00:00Z"
        )


def test_zero_fact_batch_keeps_canonical_station_gap_rows() -> None:
    result = build_icon_eps_strict_oos_report(
        [_batch([], unresolved=[{"station_id": "KATL", "reason": "official_awc_closed_truth_missing_as_of"}])],
        as_of_utc="2026-08-02T00:00:00Z",
    )

    assert result["candidate_count"] == 0
    assert len(result["readiness"]) == expected_station_count()
    katl = next(row for row in result["readiness"] if row["station_id"] == "KATL")
    assert katl["state"] == "PENDING_CLOSED_AWC_TRUTH"
    assert "truth 缺失" in katl["blockers"]
