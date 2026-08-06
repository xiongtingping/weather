from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path
import sqlite3

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.surface_observation_contract import (
    CanonicalSurfaceObservationContract,
    CanonicalSurfaceObservationWinnerContract,
    assert_surface_observation_external_evidence,
    build_surface_observation_contract,
    build_surface_observation_winner_contract,
    seal_candidate_terminal,
)


def _observation(valid_at: str, temperature: str, raw_sha: str) -> dict[str, object]:
    reported = float(temperature)
    return {
        "station_id": "KMIA",
        "event_date": "2026-08-03",
        "settlement_timezone": "America/New_York",
        "source_lane": "AWC",
        "source_class": "official_awc",
        "source_priority": 100,
        "provider_id": "NOAA_AWC",
        "report_type": "METAR",
        "measurement_semantics": "AWC_METAR_SPECI_REMARK_TENTHS_CELSIUS",
        "observation_id": f"AWC:{raw_sha}",
        "valid_at_utc": valid_at,
        "source_committed_at_utc": "2026-08-05T16:27:21.000Z",
        "canonical_committed_at_utc": "2026-08-05T16:28:00.000Z",
        "reported_temperature_c": temperature,
        "temperature_resolution_c": "0.1",
        "hard_lower_bound_c": str(reported - 0.05),
        "hard_lower_bound_method": "ROUND_NEAREST_RESOLUTION_LOWER_EDGE",
        "raw_record_sha256": raw_sha,
        "source_evidence_receipt_sha256": "d" * 64,
        "origin_url": "https://aviationweather.gov/api/data/metar",
        "qc_state": "ACCEPTED",
        "correction_suspected": False,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "state": "MATERIALIZED_UNPUBLISHED",
    }


def _database(packet: dict[str, object]) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE canonical_ssot_fact("
        "fact_id TEXT PRIMARY KEY,fact_type TEXT,scope_key TEXT,content_sha256 TEXT,"
        "fact_json TEXT,lineage_json TEXT,ingested_at_utc TEXT)"
    )
    parsed = CanonicalSurfaceObservationContract.from_mapping(packet)
    for fact in parsed.observation_facts:
        conn.execute(
            "INSERT INTO canonical_ssot_fact VALUES(?,?,?,?,?,?,?)",
            (
                f"surface_observation:{fact.content_sha256}",
                "surface_observation",
                fact.scope_key,
                fact.content_sha256,
                json.dumps(fact.raw, sort_keys=True),
                "{}",
                packet["as_of_utc"],
            ),
        )
    return conn


def _terminals(packet: dict[str, object]) -> list[dict[str, object]]:
    facts = CanonicalSurfaceObservationContract.from_mapping(packet).observation_facts
    ordered = sorted(facts, key=lambda item: item.raw["valid_at_utc"])
    maximum = max(ordered, key=lambda item: float(item.raw["reported_temperature_c"]))
    return [
        seal_candidate_terminal({
            "source_lane": "IEM", "source_class": "aggregator_republisher",
            "source_priority": 220, "eligibility_state": "INELIGIBLE",
            "terminal_reason": "COVERAGE_INCOMPLETE", "observation_count": 207,
            "coverage_complete": False, "source_evidence_receipt_sha256": "a" * 64,
            "observation_set_sha256": "1" * 64, "observed_max_c": "28.0",
            "max_valid_at_utc": "2026-08-03T07:00:00.000Z",
            "max_observation_content_sha256": None,
        }),
        seal_candidate_terminal({
            "source_lane": "MADIS", "source_class": "official_republisher",
            "source_priority": 210, "eligibility_state": "DATA_MISSING",
            "terminal_reason": "NO_RETAINED_OBJECT", "observation_count": 0,
            "coverage_complete": False, "source_evidence_receipt_sha256": "b" * 64,
            "observation_set_sha256": content_sha256([]), "observed_max_c": None,
            "max_valid_at_utc": None, "max_observation_content_sha256": None,
        }),
        seal_candidate_terminal({
            "source_lane": "AWC", "source_class": "official_awc",
            "source_priority": 100, "eligibility_state": "ELIGIBLE",
            "terminal_reason": "OFFICIAL_HOUR_COVERAGE_COMPLETE",
            "observation_count": len(ordered), "coverage_complete": True,
            "source_evidence_receipt_sha256": "d" * 64,
            "observation_set_sha256": content_sha256([item.content_sha256 for item in ordered]),
            "observed_max_c": maximum.raw["reported_temperature_c"],
            "max_valid_at_utc": maximum.raw["valid_at_utc"],
            "max_observation_content_sha256": maximum.content_sha256,
        }),
    ]


def test_awc_surface_observations_build_one_priority_winner() -> None:
    packet = build_surface_observation_contract(
        observation_inputs=[
            _observation("2026-08-03T15:53:00Z", "31.7", "a" * 64),
            _observation("2026-08-03T16:53:00Z", "32.8", "b" * 64),
        ],
        as_of_utc="2026-08-05T16:28:00.000Z",
    )
    conn = _database(packet)
    winner = build_surface_observation_winner_contract(
        conn,
        station_id="KMIA",
        event_date="2026-08-03",
        settlement_timezone="America/New_York",
        candidate_terminals=_terminals(packet),
        as_of_utc="2026-08-05T16:29:00.000Z",
    )
    fact = CanonicalSurfaceObservationWinnerContract.from_mapping(winner).winner_facts[0]
    assert fact.raw["selected_source_lane"] == "AWC"
    assert fact.raw["reported_high_c"] == "32.800000000000"
    assert fact.raw["hard_lower_bound_c"] == "32.750000000000"
    assert [item["source_lane"] for item in fact.raw["candidate_terminals"]] == [
        "IEM", "MADIS", "AWC"
    ]


def test_surface_observation_rejects_source_relabel_and_wrong_lower_bound() -> None:
    raw = _observation("2026-08-03T16:53:00Z", "32.8", "b" * 64)
    raw["source_lane"] = "IEM"
    with pytest.raises(ContractError, match="source_lineage_invalid"):
        build_surface_observation_contract(
            observation_inputs=[raw], as_of_utc="2026-08-05T16:28:00.000Z"
        )
    raw = _observation("2026-08-03T16:53:00Z", "32.8", "b" * 64)
    raw["hard_lower_bound_c"] = "32.8"
    with pytest.raises(ContractError, match="hard_lower_bound_mismatch"):
        build_surface_observation_contract(
            observation_inputs=[raw], as_of_utc="2026-08-05T16:28:00.000Z"
        )


def test_winner_rejects_nonmaximum_selected_observation() -> None:
    packet = build_surface_observation_contract(
        observation_inputs=[
            _observation("2026-08-03T15:53:00Z", "31.7", "a" * 64),
            _observation("2026-08-03T16:53:00Z", "32.8", "b" * 64),
        ],
        as_of_utc="2026-08-05T16:28:00.000Z",
    )
    conn = _database(packet)
    terminals = _terminals(packet)
    awc = deepcopy(terminals[-1])
    lower = CanonicalSurfaceObservationContract.from_mapping(packet).observation_facts[0]
    identity = {k: v for k, v in awc.items() if k != "terminal_content_sha256"}
    identity["observed_max_c"] = lower.raw["reported_temperature_c"]
    identity["max_valid_at_utc"] = lower.raw["valid_at_utc"]
    identity["max_observation_content_sha256"] = lower.content_sha256
    terminals[-1] = seal_candidate_terminal(identity)
    with pytest.raises(ContractError, match="source_maximum_mismatch"):
        build_surface_observation_winner_contract(
            conn,
            station_id="KMIA", event_date="2026-08-03",
            settlement_timezone="America/New_York",
            candidate_terminals=terminals,
            as_of_utc="2026-08-05T16:29:00.000Z",
        )


def test_external_awc_evidence_normalizes_equivalent_utc_precision(
    tmp_path: Path,
) -> None:
    raw_text = "METAR KMIA 031653Z 09008KT 10SM CLR 33/24 A2998 RMK T03280244"
    raw_sha = __import__("hashlib").sha256(raw_text.encode("utf-8")).hexdigest()
    receipt_identity = {"schema": "test.awc.receipt.v1", "observation_count": 1}
    receipt = {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }
    (tmp_path / "fetch_receipt.json").write_text(
        json.dumps(receipt), encoding="utf-8"
    )
    raw_path = tmp_path / "raw" / "objects" / raw_sha[:2] / f"{raw_sha}.json.gz"
    raw_path.parent.mkdir(parents=True)
    with gzip.open(raw_path, "wt", encoding="utf-8") as handle:
        json.dump(
            {
                "raw_text": raw_text,
                "station_id": "KMIA",
                "valid_at_utc": "2026-08-03T16:53:00Z",
                "report_type": "METAR",
                "reported_temperature_c": 32.8,
                "source_url": "https://aviationweather.gov/api/data/metar",
            },
            handle,
        )
    observation = _observation(
        "2026-08-03T16:53:00.000Z", "32.8", raw_sha
    )
    observation["source_evidence_receipt_sha256"] = receipt["content_sha256"]
    packet = build_surface_observation_contract(
        observation_inputs=[observation],
        as_of_utc="2026-08-05T16:28:00.000Z",
    )

    assert_surface_observation_external_evidence(
        tmp_path, CanonicalSurfaceObservationContract.from_mapping(packet)
    )
