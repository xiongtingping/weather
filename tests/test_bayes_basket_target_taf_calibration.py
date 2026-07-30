from __future__ import annotations

import json
import subprocess
from datetime import date, datetime, timedelta, timezone

import pytest

from bayes_basket_target.contracts import ContractError
from bayes_basket_target.taf_calibration import fit_taf_revision_artifact
from scripts import capture_bayes_basket_target_awc_taf_calibration as awc_capture
from scripts.capture_bayes_basket_target_awc_taf_calibration import (
    _event_window,
    _forecast_features,
    _tx,
    capture,
)


def _samples(count: int = 40) -> list[dict]:
    first = date(2026, 5, 1)
    rows: list[dict] = []
    noise = (-0.20, 0.10, 0.0, 0.15, -0.05)
    for index in range(count):
        day = first + timedelta(days=index)
        cloud = (index % 5) / 4.0
        precip = 1.0 if index % 7 == 0 else 0.0
        wind = 3.0 + index % 8
        tx = 25.0 + (index % 9) * 0.4
        truth = tx + 0.8 - 0.9 * cloud - 0.6 * precip + 0.15 * wind / 10.0 + noise[index % len(noise)]
        rows.append({
            "sample_id": f"sample-{index:03d}",
            "station_id": "RKPK",
            "event_date": day.isoformat(),
            "settlement_timezone": "Asia/Seoul",
            "season_bin": "summer",
            "lead_bucket": "d0",
            "run_cycle": "18Z",
            "settlement_definition": "official_daily_tmax_c",
            "nwp_combination_artifact_sha256": "9" * 64,
            "nwp_combination_decision_at_utc": (
                f"{(day - timedelta(days=1)).isoformat()}T10:55:00Z"
            ),
            "nwp_combination_prediction_oos": True,
            "taf_issued_at_utc": f"{(day - timedelta(days=1)).isoformat()}T11:00:00Z",
            "taf_available_at_utc": f"{(day - timedelta(days=1)).isoformat()}T11:06:00Z",
            "truth_closed_at_utc": f"{day.isoformat()}T15:00:00Z",
            "tx_c": tx,
            "truth_tmax_c": truth,
            "cloud_cover_fraction": cloud,
            "precipitation_probability": precip,
            "wind_speed_mps": wind,
            "nwp_combined_tmax_c": (
                tx + 0.4 + noise[(index + 2) % len(noise)]
            ),
        })
    return rows


def test_awc_taf_fetch_bypasses_only_aviationweather_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[str] = []

    def fake_run(command, **_kwargs):
        captured.extend(command)
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps([]).encode(),
            stderr=b"",
        )

    monkeypatch.setattr(awc_capture.subprocess, "run", fake_run)

    rows, _sha, size = awc_capture._fetch_json(
        "https://aviationweather.gov/api/data/taf?ids=RKPK",
        timeout_sec=8,
    )

    assert rows == []
    assert size == 2
    assert captured[captured.index("--noproxy") + 1] == (
        "aviationweather.gov"
    )


def test_fit_taf_revision_artifact_uses_strict_holdout_and_emits_active_artifact() -> None:
    artifact, scorecard = fit_taf_revision_artifact(
        _samples(),
        artifact_id="taf-rkpk-summer-d0-18z-v1",
        activation_id="taf-rkpk-summer-d0-18z-activation-v1",
        season_bin="summer",
        lead_bucket="d0",
        run_cycle="18Z",
        settlement_definition="official_daily_tmax_c",
        valid_from_utc="2026-06-10T15:01:00Z",
        valid_until_utc="2026-09-01T00:00:00Z",
    )
    assert artifact.station_id == "RKPK"
    assert artifact.sample_n == 40
    assert artifact.effective_sample_n <= 40
    assert artifact.activated is True
    assert artifact.nwp_combination_artifact_sha256 == "9" * 64
    assert artifact.taf_nwp_delta_coefficient != 1.0
    assert artifact.joint_physical_oos_validated is True
    assert 0.2 <= artifact.correlation_discount <= 1.0
    assert artifact.max_correction_strength <= artifact.max_confirmation_strength
    assert scorecard["train_end_date"] < scorecard["holdout_start_date"]
    assert scorecard["oos_gate"]["passed"] is True
    assert scorecard["joint_physical_feature_oos_gate"]["passed"] is True
    assert (
        scorecard["calibrated_oos_metrics"]["nll"]
        <= scorecard["nwp_baseline_oos_metrics"]["nll"]
    )


def test_fit_taf_revision_artifact_rejects_too_few_samples() -> None:
    with pytest.raises(ContractError, match="sample_n_below_min"):
        fit_taf_revision_artifact(
            _samples(23),
            artifact_id="too-small",
            activation_id="too-small-activation",
            season_bin="summer",
            lead_bucket="d0",
            run_cycle="18Z",
            settlement_definition="official_daily_tmax_c",
            valid_from_utc="2026-06-10T15:01:00Z",
            valid_until_utc="2026-09-01T00:00:00Z",
        )


def test_fit_taf_revision_artifact_rejects_training_time_leakage() -> None:
    with pytest.raises(ContractError, match="valid_from_not_after_training"):
        fit_taf_revision_artifact(
            _samples(),
            artifact_id="leaky",
            activation_id="leaky-activation",
            season_bin="summer",
            lead_bucket="d0",
            run_cycle="18Z",
            settlement_definition="official_daily_tmax_c",
            valid_from_utc="2026-06-09T15:00:00Z",
            valid_until_utc="2026-09-01T00:00:00Z",
        )


def test_fit_taf_revision_artifact_rejects_mixed_station_or_timezone_scope() -> None:
    rows = _samples()
    rows[-1] = {**rows[-1], "station_id": "RKSI"}
    with pytest.raises(ContractError, match="scope_mixed"):
        fit_taf_revision_artifact(
            rows,
            artifact_id="mixed-scope",
            activation_id="mixed-scope-activation",
            season_bin="summer",
            lead_bucket="d0",
            run_cycle="18Z",
            settlement_definition="official_daily_tmax_c",
            valid_from_utc="2026-06-10T15:01:00Z",
            valid_until_utc="2026-09-01T00:00:00Z",
        )


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("season_bin", "winter"),
        ("lead_bucket", "d1"),
        ("run_cycle", "00Z"),
        ("settlement_definition", "wrong_daily_target"),
        ("nwp_combination_artifact_sha256", "8" * 64),
    ),
)
def test_fit_taf_revision_artifact_rejects_mixed_sample_scope(
    field: str,
    value: str,
) -> None:
    rows = _samples()
    rows[-1] = {**rows[-1], field: value}
    with pytest.raises(ContractError, match="scope_mixed"):
        fit_taf_revision_artifact(
            rows,
            artifact_id="mixed-sample-scope",
            activation_id="mixed-sample-scope-activation",
            season_bin="summer",
            lead_bucket="d0",
            run_cycle="18Z",
            settlement_definition="official_daily_tmax_c",
            valid_from_utc="2026-06-10T15:01:00Z",
            valid_until_utc="2026-09-01T00:00:00Z",
        )


def test_fit_taf_revision_artifact_rejects_declared_scope_mismatch() -> None:
    with pytest.raises(
        ContractError,
        match="declared_scope_mismatch",
    ):
        fit_taf_revision_artifact(
            _samples(),
            artifact_id="declared-scope-mismatch",
            activation_id="declared-scope-mismatch-activation",
            season_bin="summer",
            lead_bucket="d1",
            run_cycle="18Z",
            settlement_definition="official_daily_tmax_c",
            valid_from_utc="2026-06-10T15:01:00Z",
            valid_until_utc="2026-09-01T00:00:00Z",
        )


def test_fit_taf_revision_artifact_rejects_oos_regression() -> None:
    rows = _samples()
    split = 32
    for index, row in enumerate(rows):
        row["truth_tmax_c"] = row["tx_c"] + (3.0 if index < split else -3.0)
    with pytest.raises(ContractError, match="oos_gate_failed"):
        fit_taf_revision_artifact(
            rows,
            artifact_id="regressed",
            activation_id="regressed-activation",
            season_bin="summer",
            lead_bucket="d0",
            run_cycle="18Z",
            settlement_definition="official_daily_tmax_c",
            valid_from_utc="2026-06-10T15:01:00Z",
            valid_until_utc="2026-09-01T00:00:00Z",
        )


def test_fit_taf_revision_artifact_rejects_nwp_decision_after_taf_arrival() -> None:
    rows = _samples()
    rows[0]["nwp_combination_decision_at_utc"] = rows[0]["truth_closed_at_utc"]
    with pytest.raises(ContractError, match="nwp_combination_after_taf_arrival"):
        fit_taf_revision_artifact(
            rows,
            artifact_id="late-nwp",
            activation_id="late-nwp-activation",
            season_bin="summer",
            lead_bucket="d0",
            run_cycle="18Z",
            settlement_definition="official_daily_tmax_c",
            valid_from_utc="2026-06-10T15:01:00Z",
            valid_until_utc="2026-09-01T00:00:00Z",
        )


def test_fit_taf_revision_artifact_rejects_non_oos_nwp_prediction() -> None:
    rows = _samples()
    rows[0]["nwp_combination_prediction_oos"] = False
    with pytest.raises(ContractError, match="nwp_combination_oos_required"):
        fit_taf_revision_artifact(
            rows,
            artifact_id="in-sample-nwp",
            activation_id="in-sample-nwp-activation",
            season_bin="summer",
            lead_bucket="d0",
            run_cycle="18Z",
            settlement_definition="official_daily_tmax_c",
            valid_from_utc="2026-06-10T15:01:00Z",
            valid_until_utc="2026-09-01T00:00:00Z",
        )


def test_awc_capture_resume_reuses_completed_day_and_retries_only_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = {
        "schema": awc_capture.SCHEMA,
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
        "start_date": "2026-07-01",
        "end_date": "2026-07-02",
        "samples": [{"event_date": "2026-07-01", "sample_id": "retained-day"}],
        "excluded_samples": [],
        "blockers": [{"event_date": "2026-07-02", "reason": "transport"}],
        "resource_bounds": {"actual_response_bytes": 17},
    }
    peak_start = datetime(2026, 7, 1, 21, tzinfo=timezone.utc)
    peak_end = datetime(2026, 7, 2, 6, tzinfo=timezone.utc)
    taf_rows = [{
        "icaoId": "RKPK",
        "issueTime": "2026-07-01T11:00:00Z",
        "dbPopTime": "2026-07-01T11:05:00Z",
        "rawTAF": "TAF RKPK 011100Z 0112/0218 TX31/0206Z",
        "fcsts": [{
            "timeFrom": int(peak_start.timestamp()),
            "timeTo": int(peak_end.timestamp()),
            "wspd": 8,
            "probability": 0,
            "wxString": "",
            "clouds": [{"cover": "SCT", "base": 3000}],
        }],
    }]
    metar_rows = [{
        "icaoId": "RKPK",
        "obsTime": int(datetime(2026, 7, 2, 6, tzinfo=timezone.utc).timestamp()),
        "receiptTime": "2026-07-02T06:01:00Z",
        "metarType": "METAR",
        "temp": 31.0,
        "rawOb": "RKPK 020600Z 00000KT CAVOK 31/20 Q1008",
    }]
    calls: list[str] = []

    def fake_fetch(url: str, *, timeout_sec: int) -> tuple[list[dict], str, int]:
        calls.append(url)
        assert timeout_sec == 5
        return (
            (taf_rows, "a" * 64, 101)
            if "/taf?" in url
            else (metar_rows, "b" * 64, 103)
        )

    monkeypatch.setattr(awc_capture, "_fetch_json", fake_fetch)
    monkeypatch.setattr(awc_capture, "assert_process_memory_budget", lambda: None)
    result = capture(
        station_id="RKPK",
        timezone_name="Asia/Seoul",
        start_date="2026-07-01",
        end_date="2026-07-02",
        timeout_sec=5,
        request_interval_sec=0.0,
        existing=existing,
    )
    assert len(calls) == 2
    assert [row["event_date"] for row in result["samples"]] == [
        "2026-07-01",
        "2026-07-02",
    ]
    assert result["blockers"] == []
    assert result["resource_bounds"]["actual_response_bytes"] == 221


def test_awc_capture_resume_rejects_scope_drift() -> None:
    with pytest.raises(ContractError, match="resume_scope_mismatch"):
        capture(
            station_id="RKPK",
            timezone_name="Asia/Seoul",
            start_date="2026-07-01",
            end_date="2026-07-02",
            timeout_sec=5,
            request_interval_sec=0.0,
            existing={
                "schema": awc_capture.SCHEMA,
                "station_id": "RKSI",
                "settlement_timezone": "Asia/Seoul",
                "start_date": "2026-07-01",
                "end_date": "2026-07-02",
            },
        )


def test_awc_capture_resume_allows_monotonic_end_date_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = {
        "schema": awc_capture.SCHEMA,
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
        "start_date": "2026-07-01",
        "end_date": "2026-07-01",
        "samples": [{
            "event_date": "2026-07-01",
            "station_id": "RKPK",
        }],
        "excluded_samples": [],
        "resource_bounds": {"actual_response_bytes": 0},
    }
    peak_start = datetime(2026, 7, 1, 21, tzinfo=timezone.utc)
    peak_end = datetime(2026, 7, 2, 6, tzinfo=timezone.utc)
    responses = iter((
        ([{
            "icaoId": "RKPK",
            "issueTime": "2026-07-01T11:00:00Z",
            "dbPopTime": "2026-07-01T11:05:00Z",
            "rawTAF": "TAF RKPK 011100Z 0112/0218 TX31/0206Z",
            "fcsts": [{
                "timeFrom": int(peak_start.timestamp()),
                "timeTo": int(peak_end.timestamp()),
                "wspd": 8,
                "probability": 0,
                "wxString": "",
                "clouds": [{"cover": "SCT", "base": 3000}],
            }],
        }], "a" * 64, 101),
        ([{
            "icaoId": "RKPK",
            "obsTime": int(peak_end.timestamp()),
            "receiptTime": "2026-07-02T06:01:00Z",
            "metarType": "METAR",
            "temp": 31.0,
            "rawOb": "RKPK 020600Z 00000KT CAVOK 31/20 Q1008",
        }], "b" * 64, 103),
    ))
    monkeypatch.setattr(
        awc_capture,
        "_fetch_json",
        lambda _url, *, timeout_sec: next(responses),
    )
    monkeypatch.setattr(awc_capture, "assert_process_memory_budget", lambda: None)
    result = capture(
        station_id="RKPK",
        timezone_name="Asia/Seoul",
        start_date="2026-07-01",
        end_date="2026-07-02",
        timeout_sec=5,
        request_interval_sec=0.0,
        existing=existing,
    )
    assert result["end_date"] == "2026-07-02"
    assert [row["event_date"] for row in result["samples"]] == [
        "2026-07-01",
        "2026-07-02",
    ]


def test_awc_tx_parser_selects_tx_inside_settlement_window() -> None:
    start, end = _event_window("2026-07-27", "Asia/Seoul")
    tx_c, valid_at = _tx(
        "TAF RKPK 261100Z 2612/2718 TX35/2706Z",
        issue_time=datetime(2026, 7, 26, 11, tzinfo=timezone.utc),
        start=start,
        end=end,
    )
    assert tx_c == 35.0
    assert valid_at == "2026-07-27T06:00:00Z"


def test_awc_physical_features_are_joint_and_bounded() -> None:
    start = int(datetime(2026, 7, 26, 21, tzinfo=timezone.utc).timestamp())
    end = int(datetime(2026, 7, 27, 6, tzinfo=timezone.utc).timestamp())
    cloud, precip, wind = _forecast_features(
        {
            "fcsts": [{
                "timeFrom": start,
                "timeTo": end,
                "wspd": 10,
                "probability": 40,
                "wxString": "-RA",
                "clouds": [{"cover": "BKN", "base": 3000}],
            }]
        },
        event_date="2026-07-27",
        timezone_name="Asia/Seoul",
    )
    assert cloud == 0.75
    assert precip == 0.4
    assert 5.14 < wind < 5.15
