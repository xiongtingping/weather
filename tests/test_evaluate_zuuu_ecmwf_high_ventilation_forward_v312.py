from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone

import pytest

from scripts import evaluate_zuuu_ecmwf_high_ventilation_forward_v312 as diagnostic


def test_brier_decomposition_identity() -> None:
    baseline = 0.72
    candidate = 0.65
    actual = 1.0
    delta = candidate - baseline
    observed = (candidate - actual) ** 2 - (baseline - actual) ** 2
    expected = 2.0 * (baseline - actual) * delta + delta**2
    assert observed == pytest.approx(expected, abs=1e-15)


def test_forward_audit_rejects_post_outcome_snapshot() -> None:
    amsc_rows = [
        {
            "event_date_local": "2026-07-09",
            "daily_max_label_eligible": True,
            "final_daily_max_c": 40.0,
        }
    ]
    coverage = {
        "2026-07-09": {
            "raw_archive_integrity": {"ok": True},
            "distinct_valid_time_count": 44,
        }
    }
    snapshots = [
        {
            "local_file_acquired_at_utc": datetime(2026, 7, 10, 18, tzinfo=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "local_file_acquired_at_local": "2026-07-11T02:00:00+08:00",
        }
    ]
    result = diagnostic._forward_audit(amsc_rows, coverage, snapshots)
    assert result["forward_scored_day_count"] == 0
    assert result["forward_rows"][0]["exclusion_reason"] == "ecmwf_asof_snapshot_missing"
    assert result["post_outcome_past_days_rejected"] is True


def test_phase_and_probability_bins_have_stable_boundaries() -> None:
    assert diagnostic._phase(5.5) == "00-05"
    assert diagnostic._phase(6.0) == "06-09"
    assert diagnostic._phase(14.0) == "14-17"
    assert diagnostic._probability_bin(0.0) == "0.0-0.2"
    assert diagnostic._probability_bin(0.999) == "0.8-1.0"


def test_embedded_raw_capture_is_preferred_over_filesystem_mtime(tmp_path) -> None:
    response_body = '[{"hourly":{"time":[]}}]'
    capture = {
        "schema": "open_meteo_sichuan_multimodel_raw_capture_v1",
        "requested_as_of_utc": "2026-07-09T12:00:00.000000Z",
        "response_completed_at": "2026-07-09T12:00:01.000000Z",
        "response_sha256": hashlib.sha256(response_body.encode("utf-8")).hexdigest(),
        "response_body_utf8": response_body,
    }
    raw = tmp_path / "capture.json.gz"
    with gzip.open(raw, "wt", encoding="utf-8") as handle:
        json.dump(capture, handle)

    snapshots = diagnostic._ecmwf_snapshot_times(tmp_path)
    assert snapshots[0]["acquisition_time_basis"] == "embedded_immutable_capture"
    assert snapshots[0]["response_body_sha256_verified"] is True
