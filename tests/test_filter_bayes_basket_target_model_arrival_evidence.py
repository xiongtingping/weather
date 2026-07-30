from __future__ import annotations

import json
from pathlib import Path

from bayes_basket_target.contracts import content_sha256
from scripts.filter_bayes_basket_target_model_arrival_evidence import build


def _arrival(station_id: str) -> dict[str, object]:
    return {
        "schema": "bayes_basket_target_prospective_model_arrival_v1",
        "arrival_id": f"arrival-{station_id}",
        "provider_id": "ecmwf_ifs025_official",
        "model_id": "ECMWF",
        "station_id": station_id,
        "target_local_date": "2026-07-24",
        "target_timezone": "America/New_York",
        "run_initialization_utc": "2026-07-24T00:00:00Z",
        "valid_time_utc": "2026-07-24T06:00:00Z",
        "published_at_utc": "2026-07-24T03:00:00Z",
        "received_at_utc": "2026-07-24T04:00:00Z",
        "decision_at_utc": "2026-07-25T03:00:00Z",
        "publication_time_source": "provider_http_last_modified",
        "source_url": "https://example.test/model.grib2",
        "raw_payload_sha256s": ["a" * 64],
        "receipt_sha256": "b" * 64,
        "capture_mode": "prospective",
        "historical_arrival_time_observed": True,
        "aggregator_or_republisher": False,
        "open_meteo": False,
        "production_authority": False,
        "live_order_authorized": False,
    }


def test_projection_filters_non_awc_rows_and_keeps_native_boundary(tmp_path: Path) -> None:
    payload: dict[str, object] = {
        "schema": "bayes_basket_target_model_arrival_evidence_v1",
        "as_of_utc": "2026-07-25T03:00:00Z",
        "arrivals": [_arrival("AWC1"), _arrival("ZBAA")],
        "blocked_captures": [],
        "input_artifacts": [],
        "audit": {},
        "checks": {},
        "boundary": {},
    }
    payload["evidence_sha256"] = content_sha256(payload)
    payload["receipt_sha256"] = content_sha256({**payload, "evidence_sha256": payload["evidence_sha256"]})
    source = tmp_path / "source.json"
    source.write_text(json.dumps(payload), encoding="utf-8")

    result = build(
        input_paths=[source],
        station_ids=["awc1"],
        as_of_utc="2026-07-25T03:00:00Z",
    )

    assert result["checks"]["all_rows_in_explicit_awc_scope"] is True
    assert result["audit"]["accepted_count"] == 1
    assert result["projection_audit"]["excluded_row_count"] == 1
    assert result["projection_audit"]["excluded_station_ids"] == ["ZBAA"]
    assert result["checks"]["promotion_allowed"] is False
