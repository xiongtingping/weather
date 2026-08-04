"""AIFS Single 45-station candidate decode regression coverage.

All fixtures are synthetic, local and content-addressed.  No test contacts
ECMWF/Open-Meteo, starts a service, opens SQLite, activates P0, or submits an
order.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Callable

import pytest

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.model_arrival import validate_model_arrival_capture
from bayes_basket_target.station_registry import station_coordinates
from scripts.decode_bayes_basket_target_aifs_station_candidates import (
    AifsCandidateDecodeError,
    PRODUCT_ID,
    PROVIDER_ID,
    build_candidate_evidence,
    load_station_arrivals,
)
from scripts.run_bayes_basket_target_aifs_forward_capture import build_plan


_ROOT = Path(__file__).resolve().parents[1]
_REGISTRY = _ROOT / "data/bayes_basket_target_market_registry_snapshot.json"
_RUN = "2026-07-31T18:00:00Z"
_PUBLISHED = "2026-07-31T23:28:00Z"
_RECEIVED = "2026-08-01T07:34:00Z"
_DECISION = "2026-08-01T07:40:00Z"


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")


def _rebind_capture(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    value["content_sha256"] = content_sha256(
        {key: item for key, item in value.items() if key != "content_sha256"}
    )
    _write_json(path, value)
    return value


def _fixture(
    tmp_path: Path,
) -> tuple[Path, Callable[..., dict[str, Any]], dict[str, Any]]:
    """Create a complete 45-station / 179-arrival / seven-raw capture tree."""

    plan = build_plan(
        event_date="2026-08-01",
        run_initialization_utc=_RUN,
        as_of_utc="2026-08-01T07:35:00Z",
        market_registry_snapshot_path=_REGISTRY,
    )
    raw_dir = tmp_path / "raw"
    receipt_dir = tmp_path / "receipts"
    arrival_dir = tmp_path / "station_arrivals"
    raw_by_valid: dict[str, dict[str, Any]] = {}
    for row in plan["valid_hours"]:
        valid = str(row["valid_time_utc"])
        raw_path = raw_dir / (valid.replace(":", "").replace("-", "") + ".grib2")
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_bytes(("synthetic official AIFS " + valid).encode("utf-8"))
        raw_sha = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        transport_identity = {
            "schema": "bayes_basket_target_native_model_transport_receipt_v1",
            "provider_id": PROVIDER_ID,
            "station_id": "ZUUU",
            "target_local_date": "2026-08-01",
            "run_initialization_utc": _RUN,
            "valid_time_utc": valid,
            "capture_mode": "prospective",
            "raw_receipts": [{
                "raw_object_ref": str(raw_path),
                "raw_sha256": raw_sha,
                "raw_request_url": str(row["grib_url"]),
                "provider_published_at_utc": _PUBLISHED,
                "response_completed_at": _RECEIVED,
                "response_bytes": raw_path.stat().st_size,
                "response_headers": {"content-length": str(raw_path.stat().st_size)},
            }],
        }
        transport = {
            **transport_identity,
            "receipt_content_sha256": content_sha256(transport_identity),
        }
        transport_path = receipt_dir / f"{raw_sha}.json"
        _write_json(transport_path, transport)
        raw_by_valid[valid] = {
            "raw_path": raw_path,
            "raw_sha256": raw_sha,
            "transport_path": transport_path,
            "grib_url": str(row["grib_url"]),
        }

    outputs: list[dict[str, str]] = []
    for station_id, rows in plan["station_valid_hours"].items():
        for row in rows:
            valid = str(row["valid_time_utc"])
            raw = raw_by_valid[valid]
            arrival = validate_model_arrival_capture({
                "arrival_id": hashlib.sha256(
                    f"{station_id}|{valid}".encode("utf-8")
                ).hexdigest()[:24],
                "provider_id": PROVIDER_ID,
                "model_id": "ECMWF",
                "station_id": station_id,
                "target_local_date": "2026-08-01",
                "target_timezone": plan["station_timezones"][station_id],
                "run_initialization_utc": _RUN,
                "valid_time_utc": valid,
                "published_at_utc": _PUBLISHED,
                "received_at_utc": _RECEIVED,
                "decision_at_utc": _DECISION,
                "publication_time_source": "provider_http_last_modified",
                "source_url": raw["grib_url"],
                "raw_payload_sha256s": [raw["raw_sha256"]],
                "receipt_sha256": hashlib.sha256(
                    ("arrival|" + station_id + "|" + valid).encode("utf-8")
                ).hexdigest(),
                "capture_mode": "prospective",
                "historical_arrival_time_observed": True,
                "aggregator_or_republisher": False,
                "open_meteo": False,
            })
            arrival_path = arrival_dir / f"{station_id}_{valid.replace(':', '').replace('-', '')}.json"
            _write_json(arrival_path, {
                "status": "CAPTURED",
                "arrival": arrival,
                "transport_receipt_path": str(raw["transport_path"]),
            })
            outputs.append({"status": "CAPTURED", "path": str(arrival_path)})

    capture_identity = {
        "schema": "bayes_basket_target_aifs_forward_capture_scheduler_v1",
        "state": "CAPTURED_NATIVE_FORWARD_CANDIDATE",
        "provider_id": PROVIDER_ID,
        "product_id": PRODUCT_ID,
        "independence_family_id": "ECMWF",
        "target_local_date": "2026-08-01",
        "station_timezones": plan["station_timezones"],
        "run_initialization_utc": _RUN,
        "execution_decision_at_utc": _DECISION,
        "blocked_count": 0,
        "authority": {
            "source_role": "official_native",
            "native_arrival_index_eligible": True,
            "formal_p0_eligible_without_strict_oos": False,
            "counts_as_independent_family_from_ifs": False,
        },
        "boundary": {
            "writes_target_database": False,
            "writes_production_db": False,
            "calls_order_gateway": False,
            "places_orders": False,
            "live_submit_authorized": False,
            "promotion_allowed": False,
        },
        "fanout": {
            "expected_arrival_count": len(outputs),
            "captured_count": len(outputs),
            "blocked_count": 0,
            "station_count": 45,
            "outputs": outputs,
        },
    }
    capture = {**capture_identity, "content_sha256": content_sha256(capture_identity)}
    capture_path = tmp_path / "capture.json"
    _write_json(capture_path, capture)

    by_raw_path = {
        str(item["raw_path"].resolve()): (valid, item)
        for valid, item in raw_by_valid.items()
    }
    decoder_calls: list[tuple[str, str]] = []

    def decoder(
        raw_path: Path,
        *,
        provider_id: str,
        station_id: str,
        grib_get_binary: str | None,
    ) -> dict[str, Any]:
        assert provider_id == PROVIDER_ID
        assert grib_get_binary is None
        valid, _ = by_raw_path[str(raw_path.resolve())]
        decoder_calls.append((station_id, valid))
        return {
            "provider_id": PROVIDER_ID,
            "model_id": "ECMWF",
            "station_id": station_id,
            "selector": "shortName=2t,typeOfLevel=heightAboveGround,level=2",
            "decoder_id": "test_aifs_local_decoder",
            "coordinate_lineage": station_coordinates(station_id),
            "points": [{
                "grib_run_initialization_utc": _RUN,
                "grib_valid_time_utc": valid,
                "temperature_2m_c": float(len(decoder_calls)),
            }],
        }

    return capture_path, decoder, {
        "capture": capture,
        "outputs": outputs,
        "raw_by_valid": raw_by_valid,
        "decoder_calls": decoder_calls,
    }


def test_aifs_single_45_station_candidate_is_complete_and_never_promoted(
    tmp_path: Path,
) -> None:
    capture_path, decoder, fixture = _fixture(tmp_path)

    evidence = build_candidate_evidence(capture_path, decoder=decoder, workers=1)

    assert evidence["state"] == "OFFICIAL_NATIVE_AIFS_SINGLE_45_STATION_CANDIDATE_COMPLETE"
    assert evidence["checks"] == {
        "canonical_station_count": 45,
        "station_arrival_count": 179,
        "decoded_point_count": 179,
        "complete_curve_count": 45,
        "all_station_curves_complete": True,
        "all_raw_objects_official_native": True,
        "all_station_arrivals_prospective": True,
        "raw_objects_reused_without_download": True,
        "open_meteo_aifs_variants_not_upgraded": True,
        "p0_activation_created": False,
        "writes_target_database": False,
        "writes_production_db": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }
    assert len(fixture["decoder_calls"]) == 179
    assert len(evidence["raw_objects"]) == 7
    assert all(
        hashlib.sha256(Path(row["raw_path"]).read_bytes()).hexdigest()
        == row["raw_message_sha256"]
        for row in evidence["raw_objects"]
    )
    assert {row["source_url"].split("/")[2] for row in evidence["raw_objects"]} == {
        "ecmwf-forecasts.s3.eu-central-1.amazonaws.com"
    }
    assert evidence["authority"] == {
        "source_role": "official_native",
        "candidate_only": True,
        "formal_p0_eligible": False,
        "strict_oos_activation_present": False,
        "counts_as_independent_family_from_ifs": False,
        "native_arrival_index_eligible": True,
        "p1_or_hard_no_eligible": False,
        "deterministic_single_not_ensemble": True,
        "provider_binding_required_before_ecmwf_role_selection": True,
    }
    # The AIFS Global/Europe ensemble variants remain Open-Meteo-republished
    # candidates.  They cannot inherit the official AIFS Single transport.
    assert {
        system: {
            "source_role": row["source_role"],
            "candidate_only": row["candidate_only"],
            "formal_p0_eligible": row["formal_p0_eligible"],
            "native_arrival_index_eligible": row["native_arrival_index_eligible"],
            "p1_or_hard_no_eligible": row["p1_or_hard_no_eligible"],
        }
        for system, row in evidence["aifs_ensemble_variants"].items()
    } == {
        "ECMWF_AIFS_ENS_GLOBAL": {
            "source_role": "aggregator_republisher",
            "candidate_only": True,
            "formal_p0_eligible": False,
            "native_arrival_index_eligible": False,
            "p1_or_hard_no_eligible": False,
        },
        "ECMWF_AIFS_ENS_EUROPE": {
            "source_role": "aggregator_republisher",
            "candidate_only": True,
            "formal_p0_eligible": False,
            "native_arrival_index_eligible": False,
            "p1_or_hard_no_eligible": False,
        },
    }


def test_aifs_candidate_rejects_raw_sha_capture_binding_tamper(tmp_path: Path) -> None:
    capture_path, _decoder, fixture = _fixture(tmp_path)
    raw = next(iter(fixture["raw_by_valid"].values()))
    Path(raw["raw_path"]).write_bytes(b"tampered raw")

    with pytest.raises(AifsCandidateDecodeError, match="raw_object_sha_mismatch"):
        load_station_arrivals(capture_path)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (
            lambda capture, fixture: capture["fanout"].update({
                "outputs": [
                    item
                    for item in capture["fanout"]["outputs"]
                    if "/KATL_" not in item["path"]
                ],
            }),
            "station_valid_scope_incomplete",
        ),
        (
            lambda capture, fixture: capture["fanout"]["outputs"].append(
                dict(fixture["outputs"][0])
            ),
            "station_valid_duplicate",
        ),
    ],
)
def test_aifs_candidate_rejects_missing_or_duplicate_station_arrivals(
    tmp_path: Path,
    mutation: Callable[[dict[str, Any], dict[str, Any]], None],
    error: str,
) -> None:
    capture_path, _decoder, fixture = _fixture(tmp_path)
    capture = json.loads(capture_path.read_text(encoding="utf-8"))
    mutation(capture, fixture)
    capture["fanout"]["expected_arrival_count"] = len(capture["fanout"]["outputs"])
    capture["fanout"]["captured_count"] = len(capture["fanout"]["outputs"])
    capture["content_sha256"] = content_sha256(
        {key: value for key, value in capture.items() if key != "content_sha256"}
    )
    _write_json(capture_path, capture)

    with pytest.raises(AifsCandidateDecodeError, match=error):
        load_station_arrivals(capture_path)


def test_aifs_candidate_rejects_missing_station_transport_lineage(tmp_path: Path) -> None:
    capture_path, _decoder, fixture = _fixture(tmp_path)
    arrival_path = Path(fixture["outputs"][0]["path"])
    arrival = json.loads(arrival_path.read_text(encoding="utf-8"))
    arrival.pop("transport_receipt_path")
    _write_json(arrival_path, arrival)

    with pytest.raises(AifsCandidateDecodeError, match="transport_receipt_missing"):
        load_station_arrivals(capture_path)
