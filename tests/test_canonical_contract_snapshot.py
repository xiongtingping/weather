from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bayes_basket_target.canonical_contract import CanonicalContract
from bayes_basket_target.canonical_contract_snapshot import (
    CanonicalContractSnapshotError,
    build_canonical_contract_snapshot,
    rebind_truth_gap_evidence,
)
from bayes_basket_target.canonical_activation_release import (
    load_db_projection,
    load_truth_gap_evidence,
)
from bayes_basket_target.canonical_ssot_v11_migration import (
    TARGET_TABLE_DDL,
    TARGET_TRIGGER_DDL,
)
from bayes_basket_target.contracts import content_sha256


def _truth() -> dict[str, object]:
    identity: dict[str, object] = {
        "truth_id": "truth-ZUUU-2026-07-23-r1",
        "truth_revision_no": 1,
        "truth_value": 31.0,
        "truth_content_sha256": "a" * 64,
        "canonical_committed_at_utc": "2026-07-24T18:00:00Z",
        "station_day_end_utc": "2026-07-24T16:00:00Z",
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "Asia/Shanghai",
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _member() -> dict[str, object]:
    identity: dict[str, object] = {
        "station_id": "ZUUU",
        "ensemble_system_id": "NOAA_GEFS_05",
        "model_family": "global_ensemble",
        "provider_id": "NOAA",
        "source_class": "native_official",
        "event_date": "2026-07-23",
        "run_init_utc": "2026-07-22T18:00:00Z",
        "lead_bucket": "d1",
        "run_cycle": "18Z",
        "member_id": "c00",
        "member_role": "CONTROL",
        "extraction_version": "gefs.tmax.v1",
        "settlement_timezone": "Asia/Shanghai",
        "forecast_value": 31.5,
        "raw_receipt_set_sha256": "b" * 64,
        "source_member_fact_sha": "c" * 64,
        "archive_query_at_known": True,
        "archive_query_at_utc": "2026-07-22T21:05:00Z",
        "oos_role": "historical_strict_oos",
        "closed_day": True,
        "strict_asof": True,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _database(
    tmp_path: Path,
    *,
    v11: bool = True,
    identity_mismatch: bool = False,
    include_member: bool = False,
) -> Path:
    path = tmp_path / "snapshot.db"
    with sqlite3.connect(path) as conn:
        conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
        conn.execute(TARGET_TABLE_DDL if v11 else TARGET_TABLE_DDL.replace(",\'market_bucket_bridge\'", ""))
        for ddl in TARGET_TRIGGER_DDL:
            conn.execute(ddl)
        truth = _truth()
        digest = str(truth["content_sha256"])
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            (
                f"truth:{'b' * 64 if identity_mismatch else digest}",
                "truth",
                "ZUUU|2026-07-23|r1",
                digest,
                json.dumps(truth, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                "{}",
                "2026-08-04T09:00:00Z",
            ),
        )
        if include_member:
            member = _member()
            member_digest = str(member["content_sha256"])
            conn.execute(
                "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
                (
                    f"ensemble_member:{member_digest}",
                    "ensemble_member",
                    "ZUUU|NOAA_GEFS_05|2026-07-23|d1|18Z|c00|gefs.tmax.v1",
                    member_digest,
                    json.dumps(member, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                    "{}",
                    "2026-08-04T09:00:00Z",
                ),
            )
    return path


def test_builds_new_v11_contract_with_all_arrays_explicit(tmp_path: Path) -> None:
    contract, receipt = build_canonical_contract_snapshot(
        _database(tmp_path), as_of_utc="2026-08-04T10:00:00Z"
    )
    parsed = CanonicalContract.from_mapping(contract)
    assert len(parsed.truth_facts) == 1
    assert parsed.ensemble_member_facts == ()
    assert parsed.native_arrival_facts == ()
    assert parsed.ensemble_member_receipt_manifest_facts == ()
    assert parsed.ensemble_member_arrival_binding_facts == ()
    assert receipt["state"] == "READY"
    assert receipt["selected_fact_count"] == 1
    assert receipt["writes_database"] is False


def test_rejects_non_v11_instead_of_adapting(tmp_path: Path) -> None:
    with pytest.raises(
        CanonicalContractSnapshotError,
        match="canonical_contract_snapshot_requires_v11_v12_v13_or_v14",
    ):
        build_canonical_contract_snapshot(
            _database(tmp_path, v11=False),
            as_of_utc="2026-08-04T10:00:00Z",
        )


def test_rejects_persisted_identity_mismatch(tmp_path: Path) -> None:
    path = _database(tmp_path, identity_mismatch=True)
    with pytest.raises(
        CanonicalContractSnapshotError,
        match="canonical_contract_snapshot_fact_identity_mismatch",
    ):
        build_canonical_contract_snapshot(
            path, as_of_utc="2026-08-04T10:00:00Z"
        )


def test_release_projection_exactly_rechecks_ensemble_arrays(tmp_path: Path) -> None:
    path = _database(tmp_path, include_member=True)
    contract, _ = build_canonical_contract_snapshot(
        path, as_of_utc="2026-08-04T10:00:00Z"
    )
    parsed = CanonicalContract.from_mapping(contract)
    _, projection = load_db_projection(
        path,
        as_of_utc="2026-08-04T10:00:00Z",
        contracts=[parsed],
    )
    assert projection["row_count"] == 2
    assert {row["fact_type"] for row in projection["rows"]} == {
        "truth",
        "ensemble_member",
    }


def _truth_gap_receipt(path: Path) -> tuple[Path, list[str]]:
    station_ids = sorted(["ZUUU", *(f"X{index:03d}" for index in range(44))])
    stations = []
    for station_id in station_ids:
        ready = ["2026-07-23"] if station_id == "ZUUU" else []
        blocked = [] if ready else ["2026-07-23"]
        stations.append(
            {
                "station_id": station_id,
                "truth_ready_dates": ready,
                "truth_blocked_dates": blocked,
                "blockers": ["OOS_SAMPLE_SHORT"],
                "missing_date_diagnostics": (
                    [] if ready else [{"event_date": "2026-07-23"}]
                ),
                "truth_fact_count": len(ready),
            }
        )
    identity = {
        "schema": "bayes_basket_target_historical_awc_truth_contract_build_v1",
        "as_of_utc": "2026-08-04T09:00:00Z",
        "builder_id": "old.truth.gap.v1",
        "canonical_contract_sha256": "f" * 64,
        "station_count": 45,
        "truth_fact_count": 1,
        "complete_station_count": 0,
        "blocked_station_count": 44,
        "stations": stations,
        "production_no_live": True,
        "writes_target_database": False,
        "writes_production_database": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    value = {**identity, "content_sha256": content_sha256(identity)}
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return path, station_ids


def test_truth_gap_rebind_requires_exact_contract_truth_scope(tmp_path: Path) -> None:
    contract, _ = build_canonical_contract_snapshot(
        _database(tmp_path, include_member=True),
        as_of_utc="2026-08-04T10:00:00Z",
    )
    source_path, station_ids = _truth_gap_receipt(tmp_path / "truth-gap.json")
    rebound = rebind_truth_gap_evidence(source_path, contract)
    rebound_path = tmp_path / "rebound.json"
    rebound_path.write_text(json.dumps(rebound, sort_keys=True), encoding="utf-8")
    loaded = load_truth_gap_evidence(
        rebound_path,
        release_as_of_utc="2026-08-04T10:00:00Z",
        canonical_station_ids=station_ids,
        contract_evidence=[{"content_sha256": contract["content_sha256"]}],
    )
    assert loaded["source_evidence"]["canonical_contract_content_sha256"] == contract[
        "content_sha256"
    ]
    tampered = json.loads(json.dumps(contract))
    tampered["truth_facts"] = []
    identity = {key: value for key, value in tampered.items() if key != "content_sha256"}
    tampered["content_sha256"] = content_sha256(identity)
    with pytest.raises(
        CanonicalContractSnapshotError,
        match="truth_gap_rebind_truth_scope_mismatch",
    ):
        rebind_truth_gap_evidence(source_path, tampered)
