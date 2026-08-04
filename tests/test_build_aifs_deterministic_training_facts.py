from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from scripts.build_aifs_deterministic_training_facts import (
    DECODE_SCHEMA,
    build_training_facts_receipt,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.native_arrival_decode import arrival_wrapper_lineage
from bayes_basket_target.model_arrival import validate_model_arrival_capture
from tests.test_aifs_deterministic_oos import _case


def _write_json(path: Path, value: object) -> str:
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _truth_db(
    path: Path,
    truth: dict[str, object] | list[dict[str, object]],
) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(
            """
            CREATE TABLE calibration_station_daily_high_truth (
                truth_id TEXT NOT NULL,
                station_id TEXT NOT NULL,
                local_date TEXT NOT NULL,
                official_tmax_c REAL NOT NULL,
                source_lane TEXT NOT NULL,
                settlement_definition TEXT NOT NULL,
                revision_no INTEGER NOT NULL,
                canonical_committed_at_utc TEXT NOT NULL,
                timestamps_complete INTEGER NOT NULL,
                qc_status TEXT NOT NULL,
                authority_scope TEXT NOT NULL,
                content_sha256 TEXT NOT NULL
            )
            """
        )
        for index, item in enumerate(
            truth if isinstance(truth, list) else [truth], start=1
        ):
            connection.execute(
                """
                INSERT INTO calibration_station_daily_high_truth VALUES (
                    ?,?,?,?,?,?,0,?,1,'ACCEPTED',
                    'FORMAL_NATIVE_ELIGIBLE',?
                )
                """,
                (
                    f"truth-{index}",
                    item["station_id"],
                    item["event_date"],
                    item["official_tmax_c"],
                    "AWC",
                    "official_daily_tmax_c",
                    item["canonical_committed_at_utc"],
                    item["content_sha256"],
                ),
            )


def _decode_audit(
    tmp_path: Path,
    *,
    curve: dict[str, object] | list[dict[str, object]],
    arrivals: list[dict[str, object]],
) -> Path:
    curves = curve if isinstance(curve, list) else [curve]
    artifacts: list[dict[str, object]] = []
    for index, arrival in enumerate(arrivals):
        raw_path = tmp_path / f"raw_{index}.grib2"
        raw_path.write_bytes(f"retained-aifs-raw-{index}".encode())
        raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        receipt_path = tmp_path / f"transport_receipt_{index}.json"
        receipt_sha = _write_json(receipt_path, {
            "raw_receipts": [{
                "raw_object_ref": str(raw_path),
                "raw_sha256": raw_sha,
            }],
        })
        arrival = validate_model_arrival_capture({
            **arrival,
            "raw_payload_sha256s": [raw_sha],
            "receipt_sha256": receipt_sha,
        })
        arrivals[index] = arrival
        for current_curve in curves:
            for point in current_curve["points"]:
                if (
                    current_curve["station_id"] == arrival["station_id"]
                    and point["valid_time_utc"] == arrival["valid_time_utc"]
                ):
                    point["raw_message_sha256"] = raw_sha
                    point["receipt_sha256"] = receipt_sha
        path = tmp_path / f"arrival_{index}.json"
        wrapper = {
            "status": "CAPTURED",
            "arrival": arrival,
            "transport_receipt_path": str(receipt_path),
        }
        wrapper_sha = _write_json(path, wrapper)
        artifacts.append({
            "path": str(path),
            **arrival_wrapper_lineage(
                wrapper,
                wrapper_sha256=wrapper_sha,
                arrival=arrival,
            ),
        })
    for current_curve in curves:
        curve_identity = {
            key: value for key, value in current_curve.items()
            if key not in {"schema", "state", "forecast_daily_max_c", "curve_sha256"}
        }
        current_curve["curve_sha256"] = content_sha256(curve_identity)
    identity = {
        "schema": DECODE_SCHEMA,
        "curves": {"curves": curves},
        "input_artifacts": artifacts,
    }
    evidence_sha = content_sha256(identity)
    audit = {
        **identity,
        "evidence_sha256": evidence_sha,
        "receipt_sha256": content_sha256({
            **identity,
            "evidence_sha256": evidence_sha,
        }),
    }
    path = tmp_path / "aifs_decode.json"
    _write_json(path, audit)
    return path


def test_builds_content_addressed_fact_from_closed_awc_truth(tmp_path: Path) -> None:
    curve, arrivals, truth = _case(0)
    audit = _decode_audit(tmp_path, curve=curve, arrivals=arrivals)
    database = tmp_path / "ssot.db"
    _truth_db(database, truth)

    receipt = build_training_facts_receipt(
        aifs_decode_path=audit,
        database=database,
        as_of_utc="2026-07-31T00:00:00Z",
        station_id="katl",
    )

    assert receipt["state"] == "ACCUMULATING_STRICT_OOS"
    assert receipt["fact_count"] == 1
    assert receipt["training_batch"]["fact_count"] == 1
    assert receipt["training_batch_content_sha256"] == receipt["training_batch"]["content_sha256"]
    assert receipt["boundary"]["writes_production_database"] is False
    assert receipt["boundary"]["calls_order_gateway"] is False
    assert receipt["training_batch"]["independence_family_id"] == "ECMWF"
    fact = receipt["training_batch"]["facts"][0]
    assert fact["source_authority"]["same_independence_family_as_ifs"] is True
    assert fact["source_authority"]["counts_as_distinct_independence_family"] is False


def test_open_station_local_day_is_a_successful_zero_fact_noop(tmp_path: Path) -> None:
    curve, arrivals, truth = _case(0)
    # The SSOT can already have an AWC row, but it cannot be labelled until
    # the station-local day has actually closed at the supplied as-of clock.
    truth["canonical_committed_at_utc"] = "2026-06-01T11:00:00Z"
    audit = _decode_audit(tmp_path, curve=curve, arrivals=arrivals)
    database = tmp_path / "ssot.db"
    _truth_db(database, truth)

    receipt = build_training_facts_receipt(
        aifs_decode_path=audit,
        database=database,
        as_of_utc="2026-06-01T12:00:00Z",
    )

    assert receipt["state"] == "NO_ELIGIBLE_CLOSED_AWC_TRUTH_NOOP"
    assert receipt["fact_count"] == 0
    assert receipt["skipped_reason_counts"] == {"station_local_day_not_closed": 1}
    assert receipt["readiness"] == {
        "curve_count": 1,
        "open_station_count": 1,
        "closed_truth_missing_station_count": 0,
        "closed_truth_missing_station_ids": [],
        "closed_truth_available_station_count": 0,
        "closed_curve_points_insufficient_station_count": 0,
        "closed_curve_points_insufficient_station_ids": [],
        "next_settlement_close_utc": "2026-06-02T04:00:00Z",
        "next_settlement_close_station_ids": ["KATL"],
        "next_action": "WAIT_FOR_SETTLEMENT_CLOSE",
        "next_action_not_before_utc": "2026-06-02T04:00:00Z",
        "downloads_required": False,
        "reuses_retained_official_aifs_raw": True,
    }
    assert "training_batch" not in receipt


def test_closed_curve_without_awc_truth_is_a_successful_zero_fact_noop(tmp_path: Path) -> None:
    curve, arrivals, truth = _case(0)
    # Keep the table structurally valid but place its truth outside the AIFS
    # curve's station-local date: a missing label must not become a partial
    # training fact simply because the forecast day is already closed.
    truth["event_date"] = "2026-06-02"
    audit = _decode_audit(tmp_path, curve=curve, arrivals=arrivals)
    database = tmp_path / "ssot.db"
    _truth_db(database, truth)

    receipt = build_training_facts_receipt(
        aifs_decode_path=audit,
        database=database,
        as_of_utc="2026-07-31T00:00:00Z",
    )

    assert receipt["state"] == "NO_ELIGIBLE_CLOSED_AWC_TRUTH_NOOP"
    assert receipt["fact_count"] == 0
    assert receipt["skipped_reason_counts"] == {"closed_awc_truth_missing": 1}
    assert receipt["readiness"]["next_action"] == "WAIT_FOR_ACCEPTED_AWC_TRUTH"
    assert receipt["readiness"]["closed_truth_missing_station_ids"] == ["KATL"]
    assert receipt["readiness"]["downloads_required"] is False


def test_short_closed_curve_does_not_poison_other_station_fact(tmp_path: Path) -> None:
    short_curve, short_arrivals, short_truth = _case(0)
    short_curve["station_id"] = "KATL"
    short_curve["points"] = short_curve["points"][:2]
    short_arrivals = short_arrivals[:2]
    good_curve, good_arrivals, good_truth = _case(1)
    good_curve["station_id"] = "RJTT"
    for point in good_curve["points"]:
        point["station_id"] = "RJTT"
    good_truth["station_id"] = "RJTT"
    for arrival in good_arrivals:
        arrival["station_id"] = "RJTT"
        arrival.pop("content_sha256", None)
    audit = _decode_audit(
        tmp_path,
        curve=[short_curve, good_curve],
        arrivals=[*short_arrivals, *good_arrivals],
    )
    database = tmp_path / "ssot.db"
    _truth_db(database, [short_truth, good_truth])

    receipt = build_training_facts_receipt(
        aifs_decode_path=audit,
        database=database,
        as_of_utc="2026-07-31T00:00:00Z",
    )

    assert receipt["fact_count"] == 1
    assert receipt["training_batch"]["facts"][0]["station_id"] == "RJTT"
    assert receipt["skipped_reason_counts"] == {
        "closed_aifs_curve_points_insufficient": 1,
    }
    assert {
        row["station_id"]: row["closed_day_outcome"]
        for row in receipt["station_inventory"]
    } == {
        "KATL": "CLOSED_AIFS_CURVE_POINTS_INSUFFICIENT",
        "RJTT": "CLOSED_AWC_FACT_ELIGIBLE",
    }


def test_complete_three_point_aifs_curve_is_legal_at_native_six_hour_cadence(
    tmp_path: Path,
) -> None:
    """NZWN-style timezone alignment must not be blocked by a generic 4th point."""

    curve, arrivals, truth = _case(0)
    curve["station_id"] = "NZWN"
    curve["target_timezone"] = "Pacific/Auckland"
    curve["points"] = curve["points"][:3]
    curve["forecast_daily_max_c"] = max(
        point["temperature_2m_c"] for point in curve["points"]
    )
    valid_times = [
        "2026-05-31T18:00:00Z",
        "2026-06-01T00:00:00Z",
        "2026-06-01T06:00:00Z",
    ]
    for point, valid_time in zip(curve["points"], valid_times):
        point["station_id"] = "NZWN"
        point["valid_time_utc"] = valid_time
    arrivals = arrivals[:3]
    for arrival, valid_time in zip(arrivals, valid_times):
        arrival["station_id"] = "NZWN"
        arrival["target_timezone"] = "Pacific/Auckland"
        arrival["valid_time_utc"] = valid_time
        arrival.pop("content_sha256", None)
    truth["station_id"] = "NZWN"
    truth["station_timezone"] = "Pacific/Auckland"
    audit = _decode_audit(tmp_path, curve=curve, arrivals=arrivals)
    database = tmp_path / "ssot.db"
    _truth_db(database, truth)

    receipt = build_training_facts_receipt(
        aifs_decode_path=audit,
        database=database,
        as_of_utc="2026-07-31T00:00:00Z",
    )

    assert receipt["fact_count"] == 1
    assert receipt["training_batch"]["facts"][0]["station_id"] == "NZWN"


def test_closed_day_builder_accepts_only_formal_consumption_wrapper_lifecycle_change(
    tmp_path: Path,
) -> None:
    curve, arrivals, truth = _case(0)
    audit = _decode_audit(tmp_path, curve=curve, arrivals=arrivals)
    database = tmp_path / "ssot.db"
    _truth_db(database, truth)

    arrival_path = tmp_path / "arrival_0.json"
    wrapper = json.loads(arrival_path.read_text(encoding="utf-8"))
    wrapper["formal_consumed_at_utc"] = "2026-06-02T00:00:00Z"
    _write_json(arrival_path, wrapper)

    receipt = build_training_facts_receipt(
        aifs_decode_path=audit,
        database=database,
        as_of_utc="2026-07-31T00:00:00Z",
    )

    assert receipt["fact_count"] == 1
    lineage = receipt["inputs"]["eligible_arrival_lineage"]
    assert any(
        item["wrapper_capture_sha256"] != item["wrapper_current_sha256"]
        and item["formal_consumed_at_utc"] == "2026-06-02T00:00:00Z"
        for item in lineage
    )


def test_closed_day_builder_requires_redecode_for_legacy_wrapper_only_audit(
    tmp_path: Path,
) -> None:
    curve, arrivals, truth = _case(0)
    audit = _decode_audit(tmp_path, curve=curve, arrivals=arrivals)
    database = tmp_path / "ssot.db"
    _truth_db(database, truth)
    payload = json.loads(audit.read_text(encoding="utf-8"))
    payload["input_artifacts"] = [
        {
            "path": item["path"],
            "sha256": item["sha256"],
        }
        for item in payload["input_artifacts"]
    ]
    identity = {
        key: value
        for key, value in payload.items()
        if key not in {"evidence_sha256", "receipt_sha256"}
    }
    payload["evidence_sha256"] = content_sha256(identity)
    payload["receipt_sha256"] = content_sha256({
        **identity,
        "evidence_sha256": payload["evidence_sha256"],
    })
    _write_json(audit, payload)

    with pytest.raises(
        ContractError,
        match="arrival_artifact_redecode_required",
    ):
        build_training_facts_receipt(
            aifs_decode_path=audit,
            database=database,
            as_of_utc="2026-07-31T00:00:00Z",
        )


def test_closed_day_builder_rejects_immutable_wrapper_mutation(
    tmp_path: Path,
) -> None:
    curve, arrivals, truth = _case(0)
    audit = _decode_audit(tmp_path, curve=curve, arrivals=arrivals)
    database = tmp_path / "ssot.db"
    _truth_db(database, truth)
    arrival_path = tmp_path / "arrival_0.json"
    wrapper = json.loads(arrival_path.read_text(encoding="utf-8"))
    wrapper["boundary"] = {"forged": True}
    _write_json(arrival_path, wrapper)

    with pytest.raises(
        ContractError,
        match="arrival_wrapper_immutable_sha_mismatch",
    ):
        build_training_facts_receipt(
            aifs_decode_path=audit,
            database=database,
            as_of_utc="2026-07-31T00:00:00Z",
        )
