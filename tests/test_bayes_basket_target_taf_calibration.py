from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from datetime import date, datetime, timedelta, timezone

import pytest

from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.taf_calibration import (
    MIN_TAF_CALIBRATION_SAMPLE_N,
    fit_taf_revision_artifact,
)
from scripts import capture_bayes_basket_target_awc_taf_calibration as awc_capture
from scripts import backfill_awc_taf_calibration_multistation as awc_multistation
from scripts import fit_bayes_basket_target_taf_revision as taf_fit_cli
from scripts.capture_bayes_basket_target_awc_taf_calibration import (
    _event_window,
    _forecast_features,
    _tx,
    capture,
)


AWC_TAF_AUTHORITY = awc_capture._truth_authority_for_source_lane("AWC")


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


def test_awc_taf_fetch_retries_only_transient_curl_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    responses = iter((
        subprocess.CompletedProcess(
            [], 6, stdout=b"", stderr=b"Could not resolve host\n"
        ),
        subprocess.CompletedProcess(
            [], 28, stdout=b"", stderr=b"Operation timed out\n"
        ),
        subprocess.CompletedProcess([], 0, stdout=b"[]", stderr=b""),
    ))
    calls: list[list[str]] = []
    sleeps: list[float] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        return next(responses)

    monkeypatch.setattr(awc_capture.subprocess, "run", fake_run)
    monkeypatch.setattr(awc_capture.time, "sleep", sleeps.append)

    rows, _sha, size = awc_capture._fetch_json(
        "https://aviationweather.gov/api/data/taf?ids=RKPK",
        timeout_sec=8,
    )

    assert rows == []
    assert size == 2
    assert len(calls) == awc_capture.MAX_FETCH_ATTEMPTS
    assert sleeps == [0.25, 0.5]


def test_awc_taf_fetch_keeps_nontransport_errors_single_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            command, 22, stdout=b"", stderr=b"The requested URL returned error: 400\n"
        )

    monkeypatch.setattr(awc_capture.subprocess, "run", fake_run)
    monkeypatch.setattr(awc_capture.time, "sleep", sleeps.append)

    with pytest.raises(
        ContractError,
        match="awc_taf_calibration_curl_failed_rc_22:.*400",
    ):
        awc_capture._fetch_json(
            "https://aviationweather.gov/api/data/taf?ids=RKPK",
            timeout_sec=8,
        )

    assert calls == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [429, 504])
def test_awc_taf_fetch_retries_rc22_for_explicit_rate_limit_or_gateway_status(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    responses = iter((
        subprocess.CompletedProcess(
            [], 22,
            stdout=b"{\"error\":\"temporary\"}",
            stderr=f"curl: (22) The requested URL returned error: {status}\n".encode(),
        ),
        subprocess.CompletedProcess([], 0, stdout=b"[]", stderr=b""),
    ))
    calls = 0
    sleeps: list[float] = []

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        return next(responses)

    monkeypatch.setattr(awc_capture.subprocess, "run", fake_run)
    monkeypatch.setattr(awc_capture.time, "sleep", sleeps.append)

    rows, _sha, size = awc_capture._fetch_json(
        "https://aviationweather.gov/api/data/taf?ids=RKPK",
        timeout_sec=8,
    )

    assert rows == []
    assert size == 2
    assert calls == 2
    assert sleeps == [0.25]


def test_awc_taf_fetch_keeps_parse_errors_single_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(command, 0, stdout=b"{", stderr=b"")

    monkeypatch.setattr(awc_capture.subprocess, "run", fake_run)
    monkeypatch.setattr(awc_capture.time, "sleep", sleeps.append)

    with pytest.raises(json.JSONDecodeError):
        awc_capture._fetch_json(
            "https://aviationweather.gov/api/data/taf?ids=RKPK",
            timeout_sec=8,
        )

    assert calls == 1
    assert sleeps == []


def test_awc_taf_fetch_preserves_final_transient_error_after_attempt_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0
    sleeps: list[float] = []

    def fake_run(command, **_kwargs):
        nonlocal calls
        calls += 1
        return subprocess.CompletedProcess(
            command, 7, stdout=b"", stderr=b"Failed to connect\n"
        )

    monkeypatch.setattr(awc_capture.subprocess, "run", fake_run)
    monkeypatch.setattr(awc_capture.time, "sleep", sleeps.append)

    with pytest.raises(
        ContractError,
        match="awc_taf_calibration_curl_failed_rc_7:Failed to connect",
    ):
        awc_capture._fetch_json(
            "https://aviationweather.gov/api/data/taf?ids=RKPK",
            timeout_sec=8,
        )

    assert calls == awc_capture.MAX_FETCH_ATTEMPTS
    assert sleeps == [0.25, 0.5]


def test_awc_capture_rejects_retired_amsc_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start, end = _event_window("2026-07-01", "Asia/Shanghai")
    responses = iter((
        ([{
            "icaoId": "ZUUU",
            "issueTime": "2026-06-30T11:00:00Z",
            "dbPopTime": "2026-06-30T11:05:00Z",
            "rawTAF": "TAF ZUUU 301100Z 3012/0118 TX31/0106Z",
            "fcsts": [{
                "timeFrom": int(start.timestamp()),
                "timeTo": int(end.timestamp()),
                "wspd": 8,
                "probability": 0,
                "wxString": "",
                "clouds": [{"cover": "SCT", "base": 3000}],
            }],
        }], "a" * 64, 101),
        ([{
            "icaoId": "ZUUU",
            "obsTime": int(datetime(2026, 7, 1, 6, tzinfo=timezone.utc).timestamp()),
            "receiptTime": "2026-07-01T06:01:00Z",
            "metarType": "METAR",
            "temp": 31.0,
            "rawOb": "ZUUU 010600Z 00000KT CAVOK 31/20 Q1008",
        }], "b" * 64, 103),
    ))
    monkeypatch.setattr(
        awc_capture,
        "_fetch_json",
        lambda _url, *, timeout_sec: next(responses),
    )
    monkeypatch.setattr(awc_capture, "assert_process_memory_budget", lambda: None)

    with pytest.raises(ContractError, match="source_lane_invalid"):
        capture(
            station_id="ZUUU",
            timezone_name="Asia/Shanghai",
            start_date="2026-07-01",
            end_date="2026-07-01",
            timeout_sec=5,
            request_interval_sec=0.0,
            source_lane="AMSC",
        )


def test_awc_capture_anchor_offset_stays_inside_the_settlement_day() -> None:
    """An anchor at or past the day's close would replay with hindsight.

    The offset exists so several anchors can cover the several decisions made
    against one day, not so a capture can reach the moment its own truth
    became observable.
    """

    for offset in (0, 24, -1):
        with pytest.raises(ContractError, match="decision_anchor_offset_invalid"):
            capture(
                station_id="RKPK",
                timezone_name="Asia/Seoul",
                start_date="2026-07-01",
                end_date="2026-07-01",
                timeout_sec=5,
                request_interval_sec=0.0,
                decision_anchor_offset_hours=offset,
            )


def test_multistation_backfill_rejects_an_out_of_day_anchor_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "backfill",
            "--start-date", "2026-07-01",
            "--end-date", "2026-07-02",
            "--output-dir", "/tmp/awc-anchor-scope",
            "--decision-anchor-hour", "1",
            "--decision-anchor-hour", "24",
        ],
    )
    with pytest.raises(RuntimeError, match="decision_anchor_scope_invalid"):
        awc_multistation.main()


def test_multistation_scope_prefers_awc_over_retired_amsc() -> None:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE basket_target_market_registry("
        "station_id TEXT, settlement_timezone TEXT, source_lane TEXT)"
    )
    conn.executemany(
        "INSERT INTO basket_target_market_registry VALUES(?,?,?)",
        [
            ("ZUUU", "Asia/Shanghai", "amsc"),
            ("ZUUU", "Asia/Shanghai", "amsc"),
            ("ZUUU", "Asia/Shanghai", "awc"),
        ],
    )
    assert awc_multistation._station_scopes(conn, {"ZUUU"}) == {
        "ZUUU": ("Asia/Shanghai", "AWC")
    }


def test_awc_capture_rejects_unlabelled_legacy_resume() -> None:
    with pytest.raises(ContractError, match="resume_authority_mismatch"):
        capture(
            station_id="RKPK",
            timezone_name="Asia/Seoul",
            start_date="2026-07-01",
            end_date="2026-07-01",
            timeout_sec=5,
            request_interval_sec=0.0,
            existing={
                "schema": awc_capture.SCHEMA,
                "station_id": "RKPK",
                "settlement_timezone": "Asia/Seoul",
                "start_date": "2026-07-01",
                "end_date": "2026-07-01",
            },
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


def _native_exact_join_fixture() -> dict:
    sample = {
        "station_id": "RKPK",
        "event_date": "2026-05-31",
        "settlement_timezone": "Asia/Seoul",
        "calibration_scope": {
            "season_bin": "summer",
            "lead_bucket": "d0",
            "run_cycle": "18Z",
            "settlement_definition": "official_daily_tmax_c",
        },
        "native_p0": {
            "prediction_oos": True,
            "trained_through_event_date": "2026-05-30",
            "decision_at_utc": "2026-05-31T10:00:00Z",
            "probabilities": {"20°C": 0.25, "22°C": 0.75},
            "bucket_centers_c": {"20°C": 20.0, "22°C": 22.0},
            "prediction_content_sha256": "b" * 64,
        },
        "taf": {
            "available_at_utc": "2026-05-31T11:00:00Z",
            "tx_c": 21.0,
            "cloud_cover_fraction": 0.25,
            "precipitation_probability": 0.1,
            "wind_speed_mps": 4.0,
            "truth_label_authority": (
                taf_fit_cli.FORMAL_STATION_LANE_TRUTH
            ),
            "reference_only": False,
        },
        "truth": {
            "tmax_c": 21.0,
            "canonical_truth_id": "truth-rkpk-20260531",
            "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
            "source_lane": "AWC",
        },
        "taf_lineage": {
            "truth_label_authority": (
                taf_fit_cli.FORMAL_STATION_LANE_TRUTH
            ),
            "reference_only": False,
            "enters_realtime_truth": False,
            "enters_p1": False,
            "enters_hard_no": False,
        },
        "settlement_window": {"end_utc": "2026-06-01T00:00:00Z"},
    }
    identity = {
        "schema": taf_fit_cli.TAF_NATIVE_EXACT_JOIN_SCHEMA,
        "source_class": "strict_oos_native_p0_taf_truth_exact_join",
        "nwp_crossfit_prediction_artifact_sha256": "a" * 64,
        "sample_count": 1,
        "samples": [sample],
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_taf_fit_cli_consumes_raw_native_exact_join_mean_and_crossfit_hash() -> None:
    rows = taf_fit_cli._resolve_samples(
        _native_exact_join_fixture(),
        station_id=None,
    )
    assert rows[0]["nwp_combined_tmax_c"] == pytest.approx(21.5)
    assert rows[0]["nwp_combination_artifact_sha256"] == "a" * 64


def test_taf_fit_cli_rejects_temporary_awc_exact_join_lineage() -> None:
    dataset = _native_exact_join_fixture()
    dataset["samples"][0]["taf"]["truth_label_authority"] = (
        "TEMPORARY_AWC_CALIBRATION_LABEL"
    )
    dataset["samples"][0]["taf"]["reference_only"] = True
    dataset["samples"][0]["taf_lineage"]["truth_label_authority"] = (
        "TEMPORARY_AWC_CALIBRATION_LABEL"
    )
    dataset["samples"][0]["taf_lineage"]["reference_only"] = True
    identity = {
        key: value for key, value in dataset.items() if key != "content_sha256"
    }
    dataset["content_sha256"] = content_sha256(identity)
    with pytest.raises(ContractError, match="formal_awc_label_required"):
        taf_fit_cli._resolve_samples(dataset, station_id=None)


def test_taf_fit_cli_selects_one_station_from_pipeline_wrapper() -> None:
    exact_join = _native_exact_join_fixture()
    identity = {
        "schema": taf_fit_cli.HISTORICAL_TAF_P0M_PIPELINE_SCHEMA,
        "state": "VALIDATED_NOT_COMMITTED",
        "results": [{"station_id": "RKPK", "exact_join": exact_join}],
    }
    wrapper = {**identity, "content_sha256": content_sha256(identity)}
    rows = taf_fit_cli._resolve_samples(wrapper, station_id="RKPK")
    assert len(rows) == 1
    assert rows[0]["station_id"] == "RKPK"


def test_taf_fit_cli_rejects_ambiguous_pipeline_station_selection() -> None:
    exact_join = _native_exact_join_fixture()
    identity = {
        "schema": taf_fit_cli.HISTORICAL_TAF_P0M_PIPELINE_SCHEMA,
        "state": "VALIDATED_NOT_COMMITTED",
        "results": [
            {"station_id": "RKPK", "exact_join": exact_join},
            {"station_id": "ZUUU", "exact_join": exact_join},
        ],
    }
    wrapper = {**identity, "content_sha256": content_sha256(identity)}
    with pytest.raises(
        ContractError,
        match="station_selection_ambiguous",
    ):
        taf_fit_cli._resolve_samples(wrapper, station_id=None)


def test_fit_taf_revision_artifact_rejects_too_few_samples() -> None:
    # One below whatever the gate currently is, so the test tracks the gate
    # instead of pinning a number that has to be edited alongside it.
    with pytest.raises(ContractError, match="sample_n_below_min"):
        fit_taf_revision_artifact(
            _samples(MIN_TAF_CALIBRATION_SAMPLE_N - 1),
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
        "source_lane": "AWC",
        "truth_label_authority": awc_capture.FORMAL_STATION_LANE_TRUTH,
        "truth_authority": AWC_TAF_AUTHORITY,
        "start_date": "2026-07-01",
        "end_date": "2026-07-02",
        "samples": [{
            "event_date": "2026-07-01",
            "sample_id": "retained-day",
            "source_lane": "AWC",
            "truth_label_authority": awc_capture.FORMAL_STATION_LANE_TRUTH,
            "truth_authority": AWC_TAF_AUTHORITY,
        }],
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


def test_awc_taf_row_selects_latest_bulletin_available_at_anchor() -> None:
    """A boundary bulletin must not hide the preceding strict-as-of row."""

    start, end = _event_window("2026-07-05", "America/Sao_Paulo")
    anchor = start + timedelta(hours=1)
    rows = [
        {
            "icaoId": "SBGR",
            "issueTime": "2026-07-04T22:00:00Z",
            "dbPopTime": "2026-07-04T22:12:00Z",
            "rawTAF": "TAF SBGR 042200Z 0500/0606 15008KT TX22/0517Z",
            "fcsts": [{
                "timeFrom": int(start.timestamp()),
                "timeTo": int(end.timestamp()),
                "wspd": 8,
                "probability": 0,
                "wxString": "",
                "clouds": [{"cover": "BKN", "base": 1000}],
            }],
        },
        {
            "icaoId": "SBGR",
            "issueTime": "2026-07-05T04:00:00Z",
            "dbPopTime": "2026-07-05T04:01:20Z",
            "rawTAF": "TAF SBGR 050400Z 0506/0612 11004KT TX20/0517Z",
            "fcsts": [{
                "timeFrom": int(start.timestamp()),
                "timeTo": int(end.timestamp()),
                "wspd": 4,
                "probability": 0,
                "wxString": "",
                "clouds": [{"cover": "OVC", "base": 800}],
            }],
        },
    ]
    taf, _raw = awc_capture._taf_row(
        rows,
        station_id="SBGR",
        start=start,
        end=end,
        event_date="2026-07-05",
        timezone_name="America/Sao_Paulo",
        decision_anchor=anchor,
    )
    assert taf["issue_time_utc"] == "2026-07-04T22:00:00Z"
    assert taf["available_time_utc"] == "2026-07-04T22:12:00Z"


def test_awc_capture_retries_prior_anchor_exclusion_on_resume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start, end = _event_window("2026-07-01", "Asia/Seoul")
    taf_rows = [{
        "icaoId": "RKPK",
        "issueTime": "2026-06-30T11:00:00Z",
        "dbPopTime": "2026-06-30T11:05:00Z",
        "rawTAF": "TAF RKPK 301100Z 3012/0118 TX31/0106Z",
        "fcsts": [{
            "timeFrom": int(start.timestamp()),
            "timeTo": int(end.timestamp()),
            "wspd": 8,
            "probability": 0,
            "wxString": "",
            "clouds": [{"cover": "SCT", "base": 3000}],
        }],
    }]
    metar_rows = [{
        "icaoId": "RKPK",
        "obsTime": int(datetime(2026, 7, 1, 6, tzinfo=timezone.utc).timestamp()),
        "receiptTime": "2026-07-01T06:01:00Z",
        "metarType": "METAR",
        "temp": 31.0,
        "rawOb": "RKPK 010600Z 00000KT CAVOK 31/20 Q1008",
    }]
    responses = iter(((taf_rows, "a" * 64, 101), (metar_rows, "b" * 64, 103)))
    monkeypatch.setattr(
        awc_capture,
        "_fetch_json",
        lambda _url, *, timeout_sec: next(responses),
    )
    monkeypatch.setattr(awc_capture, "assert_process_memory_budget", lambda: None)
    existing = {
        "schema": awc_capture.SCHEMA,
        "station_id": "RKPK",
        "settlement_timezone": "Asia/Seoul",
        "source_lane": "AWC",
        "truth_label_authority": awc_capture.FORMAL_STATION_LANE_TRUTH,
        "truth_authority": AWC_TAF_AUTHORITY,
        "start_date": "2026-07-01",
        "end_date": "2026-07-01",
        "samples": [],
        "truth_only_samples": [],
        "truth_only_sample_count": 0,
        "excluded_samples": [{
            "event_date": "2026-07-01",
            "reason": "ContractError:awc_taf_historical_available_after_decision_anchor",
            "classification": "official_taf_not_available_at_decision_anchor",
        }],
        "blockers": [],
        "resource_bounds": {"actual_response_bytes": 0},
    }
    result = awc_capture.capture(
        station_id="RKPK",
        timezone_name="Asia/Seoul",
        start_date="2026-07-01",
        end_date="2026-07-01",
        timeout_sec=5,
        request_interval_sec=0.0,
        existing=existing,
    )
    assert result["sample_count"] == 1
    assert result["excluded_samples"] == []


def test_awc_capture_preserves_truth_only_when_official_taf_has_no_tx(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing TAF TX cannot discard independent closed AWC truth."""

    start, _end = _event_window("2026-07-31", "America/Chicago")
    taf_rows = [{
        "icaoId": "KDAL",
        "issueTime": "2026-07-30T11:00:00Z",
        "dbPopTime": "2026-07-30T11:05:00Z",
        # Deliberately no TX group: this must never produce a TAF/P0-M row.
        "rawTAF": "TAF KDAL 301100Z 3012/0118 18010KT P6SM SCT030",
    }]
    metar_rows = [{
        "icaoId": "KDAL",
        "obsTime": int((start + timedelta(hours=12)).timestamp()),
        "receiptTime": "2026-07-31T17:01:00Z",
        "metarType": "METAR",
        "temp": 38.0,
        "rawOb": (
            "METAR KDAL 311700Z COR 18010KT 10SM SCT030 38/20 A2990"
        ),
    }]
    responses = iter(((taf_rows, "a" * 64, 101), (metar_rows, "b" * 64, 103)))
    monkeypatch.setattr(
        awc_capture,
        "_fetch_json",
        lambda _url, *, timeout_sec: next(responses),
    )
    monkeypatch.setattr(awc_capture, "assert_process_memory_budget", lambda: None)

    result = capture(
        station_id="KDAL",
        timezone_name="America/Chicago",
        start_date="2026-07-31",
        end_date="2026-07-31",
        timeout_sec=5,
        request_interval_sec=0.0,
    )

    assert result["samples"] == []
    assert result["sample_count"] == 0
    assert result["excluded_samples"] == [{
        "event_date": "2026-07-31",
        "reason": "ContractError:awc_taf_tx_for_settlement_day_missing",
        "classification": "official_taf_structural_missing_field",
    }]
    assert result["truth_only_sample_count"] == 1
    truth_only = result["truth_only_samples"][0]
    assert truth_only["taf_p0m_eligible"] is False
    assert "taf" not in truth_only
    assert truth_only["taf_observation"]["raw_taf"] == taf_rows[0]["rawTAF"]
    assert truth_only["taf_observation"]["tx_c"] is None
    assert truth_only["taf_observation"]["tx_available"] is False
    assert truth_only["taf_observation"]["taf_response_sha256"] == "a" * 64
    assert truth_only["receipts"]["taf_response_sha256"] == "a" * 64
    assert truth_only["truth"]["tmax_c"] == 38.0
    assert truth_only["truth"]["observations"][0]["raw_sha256"] == (
        hashlib.sha256(
            metar_rows[0]["rawOb"].encode("utf-8")
        ).hexdigest()
    )
    assert truth_only["truth_recovery"] == {
        "historical_replay_only": True,
        "automatic_ssot_commit_eligible": False,
        "requires_authoritative_review": True,
        "requires_manual_correction_review": True,
    }
    assert result["boundary"]["truth_only_enters_p0m"] is False
    assert result["boundary"]["truth_only_writes_production_db"] is False

    # A captured truth-only day is complete on resume; never re-fetch it.
    monkeypatch.setattr(
        awc_capture,
        "_fetch_json",
        lambda *_args, **_kwargs: pytest.fail("truth-only day must not refetch"),
    )
    resumed = capture(
        station_id="KDAL",
        timezone_name="America/Chicago",
        start_date="2026-07-31",
        end_date="2026-07-31",
        timeout_sec=5,
        request_interval_sec=0.0,
        existing=result,
    )
    assert resumed["truth_only_samples"] == result["truth_only_samples"]
    assert resumed["excluded_samples"] == result["excluded_samples"]


def test_awc_capture_excludes_provider_arrival_before_taf_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    start, end = _event_window("2026-07-01", "Asia/Seoul")
    responses = iter((
        ([{
            "icaoId": "RKSI",
            "issueTime": "2026-06-30T11:05:00Z",
            "dbPopTime": "2026-06-30T11:00:00Z",
            "rawTAF": "TAF RKSI 301100Z 3012/0118 TX30/0106Z",
            "fcsts": [{
                "timeFrom": int(start.timestamp()),
                "timeTo": int(end.timestamp()),
                "wspd": 8,
                "probability": 0,
                "wxString": "",
                "clouds": [{"cover": "SCT", "base": 3000}],
            }],
        }], "a" * 64, 101),
        ([{
            "icaoId": "RKSI",
            "obsTime": int(datetime(2026, 7, 1, 6, tzinfo=timezone.utc).timestamp()),
            "receiptTime": "2026-07-01T06:01:00Z",
            "metarType": "METAR",
            "temp": 30.0,
            "rawOb": "METAR RKSI 010600Z 00000KT CAVOK 30/20 Q1008",
        }], "b" * 64, 103),
    ))
    monkeypatch.setattr(
        awc_capture,
        "_fetch_json",
        lambda _url, *, timeout_sec: next(responses),
    )
    monkeypatch.setattr(awc_capture, "assert_process_memory_budget", lambda: None)

    result = capture(
        station_id="RKSI",
        timezone_name="Asia/Seoul",
        start_date="2026-07-01",
        end_date="2026-07-01",
        timeout_sec=5,
        request_interval_sec=0.0,
    )

    assert result["samples"] == []
    assert result["blockers"] == []
    assert result["excluded_samples"] == [{
        "event_date": "2026-07-01",
        "reason": (
            "ContractError:"
            "awc_taf_historical_available_before_issue"
        ),
        "classification": "official_taf_arrival_timestamp_invalid",
    }]


def test_awc_capture_resume_quarantines_legacy_invalid_arrival(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        awc_capture,
        "_fetch_json",
        lambda *_args, **_kwargs: pytest.fail("invalid legacy day must not refetch"),
    )
    monkeypatch.setattr(awc_capture, "assert_process_memory_budget", lambda: None)
    existing = {
        "schema": awc_capture.SCHEMA,
        "station_id": "RKSI",
        "settlement_timezone": "Asia/Seoul",
        "source_lane": "AWC",
        "truth_label_authority": awc_capture.FORMAL_STATION_LANE_TRUTH,
        "truth_authority": AWC_TAF_AUTHORITY,
        "start_date": "2026-07-01",
        "end_date": "2026-07-01",
        "samples": [{
            "event_date": "2026-07-01",
            "source_lane": "AWC",
            "truth_label_authority": awc_capture.FORMAL_STATION_LANE_TRUTH,
            "truth_authority": AWC_TAF_AUTHORITY,
            "taf": {
                "issue_time_utc": "2026-06-30T11:05:00Z",
                "available_time_utc": "2026-06-30T11:00:00Z",
            },
        }],
        "excluded_samples": [],
        "blockers": [],
        "resource_bounds": {"actual_response_bytes": 0},
    }

    result = capture(
        station_id="RKSI",
        timezone_name="Asia/Seoul",
        start_date="2026-07-01",
        end_date="2026-07-01",
        timeout_sec=5,
        request_interval_sec=0.0,
        existing=existing,
    )

    assert result["samples"] == []
    assert result["blockers"] == []
    assert result["excluded_samples"] == [{
        "event_date": "2026-07-01",
        "reason": (
            "ContractError:"
            "awc_taf_historical_available_before_issue"
        ),
        "classification": "official_taf_arrival_timestamp_invalid",
    }]


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
        "source_lane": "AWC",
        "truth_label_authority": awc_capture.FORMAL_STATION_LANE_TRUTH,
        "truth_authority": AWC_TAF_AUTHORITY,
        "start_date": "2026-07-01",
        "end_date": "2026-07-01",
        "samples": [{
            "event_date": "2026-07-01",
            "station_id": "RKPK",
            "source_lane": "AWC",
            "truth_label_authority": awc_capture.FORMAL_STATION_LANE_TRUTH,
            "truth_authority": AWC_TAF_AUTHORITY,
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
