from __future__ import annotations

import json
from pathlib import Path

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from scripts.run_bayes_basket_target_45station_explicit_18z_oos import _truth_rows


def _dataset(*, source_role: str) -> dict:
    observation = {
        "source_lane": "AWC",
        "source_role": source_role,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "settlement_timezone": "Asia/Seoul",
        "evidence_role": "FORMAL",
        "reference_only": False,
        "report_type": "METAR",
        "valid_at_utc": "2026-07-01T00:00:00Z",
        "reported_temperature_c": 25.0,
        "raw_sha256": "a" * 64,
    }
    sample_identity = {
        "station_id": "RKPK",
        "event_date": "2026-07-01",
        "settlement_window": {
            "start_utc": "2026-06-30T15:00:00Z",
            "end_utc": "2026-07-01T15:00:00Z",
        },
        "settlement_timezone": "Asia/Seoul",
        "truth": {
            "source_lane": "AWC",
            "source_role": source_role,
            "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
            "tmax_c": 25.0,
            "observations": [observation],
        },
    }
    sample = {
        "sample_id": "truth-" + content_sha256(sample_identity)[:32],
        **sample_identity,
    }
    identity = {
        "schema": "bayes_basket_station_calibration_dataset_v1",
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
        "source_lane": "AWC",
        "source_role": source_role,
        "authority_scope": "PRODUCTION_NO_LIVE_ONLY",
        "label_authority": "FORMAL_STATION_LANE_TRUTH",
        "samples": [sample],
        "checks": {
            "input_evidence_content_hashes_verified": True,
            "settlement_timezone_exact_match": True,
            "reference_only_excluded": True,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def test_explicit_oos_accepts_only_exact_historical_awc_lineage(tmp_path: Path) -> None:
    valid = tmp_path / "valid.json"
    _write(valid, _dataset(source_role="AWC_HISTORICAL_REPLAY_INGRESS"))
    rows, authority = _truth_rows(valid, station="RKPK", timezone_name="Asia/Seoul")
    assert authority == "FORMAL_STATION_LANE_TRUTH"
    assert set(rows) == {"2026-07-01"}

    forged = tmp_path / "forged.json"
    _write(forged, _dataset(source_role="AMSC_RETIRED_HISTORY_ONLY"))
    with pytest.raises(ContractError, match="explicit_oos_truth_scope_invalid"):
        _truth_rows(forged, station="RKPK", timezone_name="Asia/Seoul")
