from __future__ import annotations

import copy
import json
import sqlite3
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest

from bayes_basket_target.canonical_contract import CanonicalTruthFact
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.deterministic_republisher_oos import (
    build_deterministic_republisher_oos,
)
from bayes_basket_target.republisher_oos_artifact import (
    ARTIFACT_METHOD_VERSION,
    OOS_PROTOCOL_VERSION,
    PRODUCT_REGISTRY,
    build_republisher_oos_artifact_contract,
)
from bayes_basket_target.settlement_time import settlement_day_window
from scripts.apply_canonical_contract_schema import migrate as apply_contract_schema


AS_OF = "2026-08-04T05:16:39Z"
RECONSTRUCTED_AT = "2026-08-03T00:00:00Z"
STATION_TIMEZONES = {
    "CYYZ": "America/Toronto",
    "NZWN": "Pacific/Auckland",
}
SEGMENTS = (
    ("2026-07-01", "2026-07-14"),
    ("2026-07-15", "2026-07-28"),
    ("2026-07-29", "2026-07-31"),
)
PRODUCTS = (
    (
        "AIFS",
        "ecmwf_aifs_single_open_meteo",
        "ecmwf_aifs025_single",
        "ecmwf_aifs025_single",
        "ECMWF",
    ),
    (
        "ICON",
        "icon_global_open_meteo",
        "dwd_icon_global",
        "dwd_icon_global",
        "DWD",
    ),
)


def _date_range(start: str, end: str) -> list[str]:
    first = date.fromisoformat(start)
    last = date.fromisoformat(end)
    return [
        (first + timedelta(days=offset)).isoformat()
        for offset in range((last - first).days + 1)
    ]


def _truth_value(station_id: str, event_date: str) -> float:
    # A non-flat closed-day series makes calibrated OOS strictly better than
    # the train climatology without coupling this test to production evidence.
    offset = (date.fromisoformat(event_date) - date(2026, 7, 1)).days
    base = 20.0 if station_id == "CYYZ" else 10.0
    return base + float((offset * 3) % 7)


def _capture(
    *,
    model_id: str,
    provider_id: str,
    model_parameter: str,
    start: str,
    end: str,
) -> dict[str, object]:
    rows: list[dict[str, object]] = []
    for station_id in sorted(STATION_TIMEZONES):
        raw_sha = content_sha256(
            {
                "station_id": station_id,
                "provider_id": provider_id,
                "start": start,
                "end": end,
            }
        )
        for event_date in _date_range(start, end):
            rows.append(
                {
                    "station_id": station_id,
                    "event_date": event_date,
                    "source_class": "aggregator_republisher",
                    "aggregator_or_republisher": True,
                    "open_meteo": True,
                    "complete": True,
                    "forecast_daily_max_c": _truth_value(station_id, event_date)
                    - 0.25,
                    "raw_message_sha256": raw_sha,
                    "raw_object_ref": (
                        f"retained:test/{provider_id}/{station_id}/{start}_{end}.json"
                    ),
                }
            )
    identity: dict[str, object] = {
        "schema": "bayes_basket_target_historical_aggregator_rebuild_evidence_v1",
        "provider_id": provider_id,
        "model_id": model_id,
        "reconstructed_at_utc": RECONSTRUCTED_AT,
        "station_timezones": dict(STATION_TIMEZONES),
        "source": {
            "source_class": "aggregator_republisher",
            "open_meteo": True,
            "model_parameter": model_parameter,
            "run_cycle": None,
            "run_day_offset": None,
            "single_run_parameter_explicit": False,
        },
        "checks": {
            "native_arrival_index_eligible": False,
            "prospective_strict_asof_eligible": False,
        },
        "curves": {"eligible_model_rows": rows},
    }
    return {**identity, "receipt_sha256": content_sha256(identity)}


def _insert_truth(
    conn: sqlite3.Connection, *, station_id: str, event_date: str
) -> None:
    timezone_name = STATION_TIMEZONES[station_id]
    truth_content_sha256 = content_sha256(
        {"station_id": station_id, "event_date": event_date, "source": "AWC"}
    )
    identity: dict[str, object] = {
        "truth_id": f"awc:{station_id}:{event_date}",
        "station_id": station_id,
        "event_date": event_date,
        "truth_revision_no": 0,
        "truth_value": _truth_value(station_id, event_date),
        "truth_content_sha256": truth_content_sha256,
        "canonical_committed_at_utc": "2026-08-03T12:00:00Z",
        "station_day_end_utc": settlement_day_window(event_date, timezone_name)[
            "target_day_end_utc"
        ],
        "closed_day": True,
        "source_lane": "AWC",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": timezone_name,
    }
    fact = {**identity, "content_sha256": content_sha256(identity)}
    parsed = CanonicalTruthFact.from_mapping(fact)
    lineage = {
        "truth_content_sha256": fact["truth_content_sha256"],
        "source_role": fact["source_role"],
        "authority_scope": fact["authority_scope"],
    }
    conn.execute(
        "INSERT INTO canonical_ssot_fact "
        "(fact_id,fact_type,scope_key,content_sha256,fact_json,lineage_json,ingested_at_utc) "
        "VALUES(?,?,?,?,?,?,?)",
        (
            "truth:" + str(fact["content_sha256"]),
            "truth",
            parsed.scope_key,
            fact["content_sha256"],
            json.dumps(fact, sort_keys=True, separators=(",", ":")),
            json.dumps(lineage, sort_keys=True, separators=(",", ":")),
            "2026-08-03T12:00:00Z",
        ),
    )


def _temporary_v7_ssot(tmp_path: Path) -> sqlite3.Connection:
    database = tmp_path / "republisher_oos_artifact_v7.db"
    database.touch()
    assert apply_contract_schema(database, apply=True)["state"] == "APPLIED"
    conn = sqlite3.connect(database)
    conn.create_function("canonical_ingress_authorized", 0, lambda: 1)
    conn.execute(
        "CREATE TABLE calibration_station_registry "
        "(station_id TEXT NOT NULL,settlement_definition TEXT NOT NULL)"
    )
    for station_id in sorted(STATION_TIMEZONES):
        conn.execute(
            "INSERT INTO calibration_station_registry VALUES(?,?)",
            (station_id, "daily_max_temperature_c"),
        )
        # Leave July 1 and July 30-31 absent: the frozen three-capture cohort
        # must select exactly July 2-29, not silently substitute an extra day.
        for event_date in _date_range("2026-07-02", "2026-07-29"):
            _insert_truth(conn, station_id=station_id, event_date=event_date)
    conn.commit()
    return conn


def _report(
    conn: sqlite3.Connection,
    *,
    model_id: str,
    provider_id: str,
    model_parameter: str,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    captures = [
        _capture(
            model_id=model_id,
            provider_id=provider_id,
            model_parameter=model_parameter,
            start=start,
            end=end,
        )
        for start, end in SEGMENTS
    ]
    report = build_deterministic_republisher_oos(
        captures,
        conn,
        as_of_utc=AS_OF,
    )
    return report, captures


def _rehash_report(report: dict[str, object]) -> None:
    report["content_sha256"] = content_sha256(
        {key: value for key, value in report.items() if key != "content_sha256"}
    )


def _rehash_candidate(candidate: dict[str, object]) -> None:
    candidate["content_sha256"] = content_sha256(
        {
            key: value
            for key, value in candidate.items()
            if key != "content_sha256"
        }
    )


def _assert_candidate_hashes(
    candidate: dict[str, object],
    *,
    model_id: str,
    provider_id: str,
    capture_receipts: set[str],
) -> None:
    rows = candidate["cohort_rows"]
    assert isinstance(rows, list)
    assert len(rows) == 28
    assert [row["event_date"] for row in rows] == _date_range(
        "2026-07-02", "2026-07-29"
    )
    assert [row["partition"] for row in rows] == ["train"] * 20 + [
        "holdout"
    ] * 8
    receipt_counts = Counter(row["source_receipt_sha256"] for row in rows)
    assert set(receipt_counts) == capture_receipts
    assert sorted(receipt_counts.values()) == [1, 13, 14]
    for row in rows:
        case_identity = {
            key: value for key, value in row.items() if key != "case_contract_sha256"
        }
        assert row["case_contract_sha256"] == content_sha256(case_identity)
    assert candidate["train_case_set_sha256"] == content_sha256(
        [row["case_contract_sha256"] for row in rows[:20]]
    )
    assert candidate["holdout_case_set_sha256"] == content_sha256(
        [row["case_contract_sha256"] for row in rows[20:]]
    )
    cohort_scope = {
        "station_id": candidate["station_id"],
        "model_id": model_id,
        "provider_id": provider_id,
        "transport_provider_id": "OPEN_METEO",
        "source_role": "aggregator_republisher",
        "settlement_timezone": candidate["settlement_timezone"],
        "lead_bucket": "d0",
        "run_cycle": None,
        "run_cycle_scope": "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE",
        "season_bin": "JJA",
        "cohort_rows": rows,
    }
    assert candidate["strict_oos_cohort_sha256"] == content_sha256(cohort_scope)
    assert candidate["cohort_capture_receipt_sha256s"] == sorted(capture_receipts)
    assert candidate["run_cycle"] is None
    assert candidate["run_cycle_scope"] == "REPUBLISHER_HISTORICAL_DAY_NO_NATIVE_CYCLE"
    assert candidate["native_arrival_index_eligible"] is False
    assert candidate["p1_eligible"] is False
    assert candidate["formal_p0_eligible"] is False


@pytest.mark.parametrize(
    ("model_id", "provider_id", "model_parameter", "product_id", "family_id"),
    PRODUCTS,
)
def test_retired_legacy_truth_cohort_cannot_recreate_republisher_artifact(
    tmp_path: Path,
    model_id: str,
    provider_id: str,
    model_parameter: str,
    product_id: str,
    family_id: str,
) -> None:
    conn = _temporary_v7_ssot(tmp_path)
    try:
        with pytest.raises(
            ContractError, match="deterministic_republisher_canonical_ssot_v12_required"
        ):
            _report(
                conn,
                model_id=model_id,
                provider_id=provider_id,
                model_parameter=model_parameter,
            )
        assert PRODUCT_REGISTRY[(model_id, provider_id)] == {
            "independence_family_id": family_id,
            "republisher_product_id": product_id,
        }
    finally:
        conn.close()
