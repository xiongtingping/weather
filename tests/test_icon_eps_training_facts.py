from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import sqlite3

import pytest

from bayes_basket_target.fleet import expected_station_count
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.icon_eps_daily_curve import build_icon_eps_daily_archive
from bayes_basket_target.icon_eps_training_facts import (
    TRAINING_BATCH_SCHEMA,
    build_icon_eps_training_facts,
)
from bayes_basket_target.station_registry import coordinate_registry_manifest
from scripts.build_icon_eps_training_facts import read_ssot_awc_closed_truths


STATIONS = tuple(coordinate_registry_manifest()["station_ids"])
INDICES = {station: index * 100 for index, station in enumerate(STATIONS)}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _cohort(lead: int) -> dict[str, object]:
    run = datetime(2026, 8, 1, tzinfo=timezone.utc)
    valid = run + timedelta(hours=lead)
    raw_sha = _sha(f"raw-{lead}")
    identity = {
        "schema": "bayes_basket_target_icon_global_eps_station_member_cohort_v1",
        "state": "OFFICIAL_40_MEMBER_45_STATION_DECODED_CANDIDATE_ONLY",
        "provider_id": "icon_global_eps_official",
        "product_id": "DWD_ICON_GLOBAL_EPS",
        "independence_family_id": "DWD_ICON_EPS",
        "run_initialization_utc": "2026-08-01T00:00:00Z",
        "provider_published_at_utc": "2026-08-01T02:00:00Z",
        "response_completed_at_utc": "2026-08-01T03:00:00Z",
        "forecast_hour": lead,
        "valid_time_utc": valid.isoformat().replace("+00:00", "Z"),
        "raw_sha256": raw_sha,
        "member_count": 40,
        "station_count": expected_station_count(),
        "member_record_count": 1_800,
        "lineage": {
            "source_role": "official_native",
            "transport_provider_id": "DWD",
            "official_grid": {
                "source_grid_uuid": "ae487d14fe2e11e4af85e50a2a56a360",
                "source_grid_cell_count": 737_280,
                "station_nearest_cell_indices": INDICES,
            },
        },
        "member_station_records": [
            {
                "station_id": station,
                "member_id": member,
                "temperature_2m_c": 15.0 + lead / 10.0 + member / 100.0,
                "raw_sha256": raw_sha,
            }
            for member in range(1, 41)
            for station in sorted(STATIONS)
        ],
        "formal_p0_eligible": False,
        "hard_no_eligible": False,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _archive() -> dict[str, object]:
    cohorts = [_cohort(lead) for lead in range(0, 24, 3)]
    archive = build_icon_eps_daily_archive(
        cohorts=cohorts,
        event_date="2026-08-01",
        station_timezones={station: "UTC" for station in STATIONS},
        built_at_utc="2026-08-01T04:00:00Z",
    )
    identity = {
        **archive,
        "lead_results": [
            {
                "forecast_hour": cohort["forecast_hour"],
                "valid_time_utc": cohort["valid_time_utc"],
                "raw_sha256": cohort["raw_sha256"],
                "cohort_content_sha256": cohort["content_sha256"],
            }
            for cohort in cohorts
        ],
    }
    identity.pop("content_sha256", None)
    return {**identity, "content_sha256": content_sha256(identity)}


def _truths() -> list[dict[str, object]]:
    return [
        {
            "truth_id": f"truth-{station}",
            "station_id": station,
            "local_date": "2026-08-01",
            "settlement_timezone": "UTC",
            "official_tmax_c": 23.5,
            "source_lane": "AWC",
            "settlement_definition": "official_daily_tmax_c",
            "canonical_committed_at_utc": "2026-08-02T00:01:00Z",
            "qc_status": "ACCEPTED",
            "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
            "content_sha256": _sha(f"truth-payload-{station}"),
        }
        for station in STATIONS
    ]


def test_builds_45_closed_awc_facts_with_member_raw_and_grid_lineage() -> None:
    batch = build_icon_eps_training_facts(
        _archive(), _truths(), as_of_utc="2026-08-02T01:00:00Z"
    )

    assert batch["schema"] == TRAINING_BATCH_SCHEMA
    assert batch["state"] == "ACCUMULATING_STRICT_OOS"
    assert batch["fact_count"] == expected_station_count()
    assert batch["unresolved_station_days"] == []
    assert batch["formal_p0_eligible"] is False
    fact = batch["facts"][0]
    assert len(fact["member_daily_max_c"]) == 40
    assert len(fact["raw_leads"]) == 8
    assert {lead["raw_sha256"] for lead in fact["raw_leads"]}
    assert fact["source_lineage"]["official_grid_uuid"] == (
        "ae487d14fe2e11e4af85e50a2a56a360"
    )
    assert fact["source_lineage"]["truth_source_lane"] == "AWC"
    assert fact["boundary"]["writes_target_database"] is False
    assert batch["content_sha256"] == content_sha256({
        key: value for key, value in batch.items() if key != "content_sha256"
    })


def test_indexed_daily_archive_carries_immutable_arrival_pointer_to_facts() -> None:
    archive = _archive()
    arrival_index_sha = _sha("immutable-dwd-native-arrival-index")
    archive["native_arrival_index_eligible"] = True
    archive["source_authority"] = {
        **archive["source_authority"],
        "native_arrival_index_eligible": True,
        "arrival_lineage_status": "IMMUTABLE_OFFICIAL_NATIVE_ARRIVAL_INDEX_VERIFIED",
        "native_arrival_index_evidence_sha256": arrival_index_sha,
    }
    archive["native_arrival_index"] = {
        "schema": "dwd_icon_global_eps_immutable_native_arrival_index_v1",
        "content_sha256": arrival_index_sha,
        "entry_count": 8,
        "run_initialization_utc": "2026-08-01T00:00:00Z",
    }
    identity = {key: value for key, value in archive.items() if key != "content_sha256"}
    archive["content_sha256"] = content_sha256(identity)

    batch = build_icon_eps_training_facts(
        archive, _truths(), as_of_utc="2026-08-02T01:00:00Z"
    )

    assert batch["fact_count"] == expected_station_count()
    lineage = batch["facts"][0]["source_lineage"]
    assert lineage["native_arrival_index_eligible"] is True
    assert lineage["native_arrival_index_evidence_sha256"] == arrival_index_sha


def test_open_day_returns_zero_facts_without_reading_a_future_truth() -> None:
    batch = build_icon_eps_training_facts(
        _archive(), [], as_of_utc="2026-08-01T12:00:00Z"
    )

    assert batch["state"] == "PENDING_CLOSED_AWC_TRUTH"
    assert batch["fact_count"] == 0
    assert len(batch["unresolved_station_days"]) == expected_station_count()
    assert {row["reason"] for row in batch["unresolved_station_days"]} == {
        "settlement_day_open_as_of"
    }


def test_missing_closed_awc_truth_returns_zero_facts_fail_closed() -> None:
    batch = build_icon_eps_training_facts(
        _archive(), [], as_of_utc="2026-08-02T01:00:00Z"
    )

    assert batch["state"] == "PENDING_CLOSED_AWC_TRUTH"
    assert batch["fact_count"] == 0
    assert {row["reason"] for row in batch["unresolved_station_days"]} == {
        "official_awc_closed_truth_missing_as_of"
    }


def test_rejects_non_awc_truth_instead_of_relabeling_it() -> None:
    truths = _truths()
    truths[0]["source_lane"] = "AMSC"
    with pytest.raises(ContractError, match="truth_authority_invalid"):
        build_icon_eps_training_facts(
            _archive(), truths, as_of_utc="2026-08-02T01:00:00Z"
        )


def test_rejects_daily_archive_when_member_lead_cannot_bind_to_raw_hash() -> None:
    archive = _archive()
    archive["lead_results"][0]["cohort_content_sha256"] = _sha("wrong-cohort")
    identity = {key: value for key, value in archive.items() if key != "content_sha256"}
    archive["content_sha256"] = content_sha256(identity)

    with pytest.raises(ContractError, match="lead_lineage_invalid"):
        build_icon_eps_training_facts(
            archive, _truths(), as_of_utc="2026-08-02T01:00:00Z"
        )


def test_read_ssot_truths_uses_latest_formal_awc_revision_as_of(tmp_path) -> None:
    db = tmp_path / "truth.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE calibration_station_daily_high_truth("
            "truth_id TEXT,station_id TEXT,local_date TEXT,official_tmax_c REAL,"
            "source_lane TEXT,settlement_definition TEXT,"
            "canonical_committed_at_utc TEXT,qc_status TEXT,authority_scope TEXT,"
            "content_sha256 TEXT,revision_no INTEGER)"
        )
        conn.executemany(
            "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    "old", "KATL", "2026-08-01", 29.0, "AWC",
                    "official_daily_tmax_c", "2026-08-02T01:00:00Z", "ACCEPTED",
                    "FORMAL_NATIVE_ELIGIBLE", _sha("old"), 0,
                ),
                (
                    "new", "KATL", "2026-08-01", 30.0, "AWC",
                    "official_daily_tmax_c", "2026-08-02T02:00:00Z", "ACCEPTED",
                    "FORMAL_NATIVE_ELIGIBLE", _sha("new"), 1,
                ),
                (
                    "temporary", "KATL", "2026-08-01", 31.0, "AWC",
                    "official_daily_tmax_c", "2026-08-02T03:00:00Z", "ACCEPTED",
                    "NO_LIVE_REPLAY_CANDIDATE", _sha("temporary"), 2,
                ),
            ],
        )
    truths = read_ssot_awc_closed_truths(
        db,
        event_date="2026-08-01",
        station_timezones={"KATL": "America/New_York"},
        as_of_utc="2026-08-02T02:30:00Z",
    )

    assert len(truths) == 1
    assert truths[0]["truth_id"] == "new"
    assert truths[0]["official_tmax_c"] == 30.0
    assert truths[0]["settlement_timezone"] == "America/New_York"
