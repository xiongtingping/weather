from __future__ import annotations

import json
from pathlib import Path

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.official_nwp_capture_scheduler import (
    CANDIDATE_PREFETCH_SCHEMA,
)
from scripts.build_bayes_basket_target_model_arrival_evidence import build


SHA = "a" * 64


def _arrival(provider: str, model: str, station: str) -> dict[str, object]:
    return {
        "arrival_id": f"arrival-{provider}-{station}",
        "provider_id": provider,
        "model_id": model,
        "station_id": station,
        "target_local_date": "2026-07-23",
        "target_timezone": "America/New_York",
        "run_initialization_utc": "2026-07-23T00:00:00Z",
        "valid_time_utc": "2026-07-23T16:00:00Z",
        "published_at_utc": "2026-07-23T02:45:00Z",
        "received_at_utc": "2026-07-23T14:00:00Z",
        "decision_at_utc": "2026-07-23T18:00:00Z",
        "publication_time_source": "provider_http_last_modified",
        "source_url": "https://opendata.dwd.de/weather/model.grib2.bz2",
        "raw_payload_sha256s": [SHA],
        "receipt_sha256": "b" * 64,
        "capture_mode": "prospective",
        "historical_arrival_time_observed": True,
        "aggregator_or_republisher": False,
        "open_meteo": False,
    }


def test_arrival_evidence_aggregates_only_validated_captures(tmp_path: Path) -> None:
    accepted = tmp_path / "accepted.json"
    accepted.write_text(json.dumps({"status": "CAPTURED", "arrival": _arrival("icon_global_official", "ICON", "ZUUU")}))
    blocked = tmp_path / "blocked.json"
    blocked.write_text(json.dumps({"status": "BLOCKED", "receipt": {"status": "BLOCKED", "reason_codes": ["provider_publication_timestamp_missing"]}}))

    result = build(
        capture_paths=[accepted, blocked],
        as_of_utc="2026-07-23T18:00:00Z",
    )

    assert result["audit"]["accepted_count"] == 1
    assert result["audit"]["blocked_count"] == 0
    assert len(result["blocked_captures"]) == 1
    assert result["checks"]["all_arrivals_prospective"] is True
    assert result["checks"]["all_publication_sources_native"] is True
    assert result["checks"]["promotion_allowed"] is False
    assert result["boundary"]["writes_production_db"] is False


def test_arrival_evidence_duplicate_key_conflict_is_not_promoted(tmp_path: Path) -> None:
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps({"status": "CAPTURED", "arrival": _arrival("icon_global_official", "ICON", "ZUUU")}))
    changed = _arrival("icon_global_official", "ICON", "ZUUU")
    changed["receipt_sha256"] = "c" * 64
    second.write_text(json.dumps({"status": "CAPTURED", "arrival": changed}))

    result = build(
        capture_paths=[first, second],
        as_of_utc="2026-07-23T18:00:00Z",
    )

    assert result["audit"]["accepted_count"] == 1
    assert result["audit"]["blocked_count"] == 1
    assert result["checks"]["promotion_allowed"] is False


def test_candidate_receipt_projects_only_hash_bound_native_arrivals(
    tmp_path: Path,
) -> None:
    providers = []
    for provider_id, model_id in (
        ("icon_global_official", "ICON"),
        ("gfs_0p25_official", "GFS"),
    ):
        arrival_identity = _arrival(provider_id, model_id, "ZUUU")
        arrival_identity["arrival_id"] = f"arrival-{provider_id}-ZUUU"
        arrival = {
            **arrival_identity,
            "content_sha256": content_sha256(arrival_identity),
        }
        payload = {"arrival": arrival}
        evidence_path = tmp_path / f"{provider_id}.json"
        evidence_path.write_text(json.dumps(payload), encoding="utf-8")
        binding_identity = {
            "model_id": model_id,
            "provider_id": provider_id,
            "source_class": "native_or_official",
            "station_id": "ZUUU",
            "event_date": "2026-07-23",
            "run_initialization_utc": "2026-07-23T00:00:00Z",
            "published_at_utc": "2026-07-23T02:45:00Z",
            "received_at_utc": "2026-07-23T14:00:00Z",
            "raw_sha256s": [SHA],
            "provider_receipt_sha256": "b" * 64,
            "evidence_payload_sha256": content_sha256(payload),
            "evidence_path": str(evidence_path),
        }
        binding = {
            **binding_identity,
            "binding_sha256": content_sha256(binding_identity),
        }
        providers.append({
            "model_id": model_id,
            "provider_id": provider_id,
            "source_class": "native_or_official",
            "aggregator_or_republisher": False,
            "native_arrival_indexed": False,
            "state": "COMPLETE",
            "evidence_bindings": [binding],
        })
    receipt_identity = {
        "schema": CANDIDATE_PREFETCH_SCHEMA,
        "state": "CAPTURED_CANDIDATE",
        "candidate_only": True,
        "p0_materialization_authorized": False,
        "native_arrival_indexed": False,
        "creates_activation": False,
        "creates_p0": False,
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "America/New_York",
        "run_initialization_utc": "2026-07-23T00:00:00Z",
        "as_of_utc": "2026-07-23T18:00:00Z",
        "providers": providers,
    }
    candidate = tmp_path / "candidate.json"
    candidate.write_text(
        json.dumps({
            **receipt_identity,
            "content_sha256": content_sha256(receipt_identity),
        }),
        encoding="utf-8",
    )
    result = build(
        capture_paths=[],
        candidate_receipt_paths=[candidate],
        as_of_utc="2026-07-23T18:00:00Z",
    )
    assert result["audit"]["accepted_count"] == 2
    assert result["audit"]["blocked_count"] == 0
    assert result["blocked_captures"] == []
    assert {
        row["model_id"] for row in result["arrivals"]
    } == {"GFS", "ICON"}
    assert result["checks"]["promotion_allowed"] is False
    assert result["boundary"]["calls_order_gateway"] is False


def test_candidate_receipt_tamper_is_blocked_without_projection(
    tmp_path: Path,
) -> None:
    identity = {
        "schema": CANDIDATE_PREFETCH_SCHEMA,
        "state": "CAPTURED_CANDIDATE",
        "candidate_only": True,
        "p0_materialization_authorized": False,
        "native_arrival_indexed": False,
        "creates_activation": False,
        "creates_p0": False,
        "station_id": "ZUUU",
        "event_date": "2026-07-23",
        "settlement_timezone": "America/New_York",
        "run_initialization_utc": "2026-07-23T00:00:00Z",
        "as_of_utc": "2026-07-23T18:00:00Z",
        "providers": [],
    }
    candidate = tmp_path / "tampered-candidate.json"
    candidate.write_text(
        json.dumps({
            **identity,
            "content_sha256": "0" * 64,
        }),
        encoding="utf-8",
    )
    result = build(
        capture_paths=[],
        candidate_receipt_paths=[candidate],
        as_of_utc="2026-07-23T18:00:00Z",
    )
    assert result["audit"]["accepted_count"] == 0
    assert len(result["blocked_captures"]) == 1
    assert result["checks"]["promotion_allowed"] is False
