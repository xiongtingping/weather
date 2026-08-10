from __future__ import annotations

from datetime import date, timedelta
import sqlite3

import pytest

from bayes_basket_target import ContractError
from bayes_basket_target import rolling_native_taf_oos
from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.taf_calibration import MIN_TAF_CALIBRATION_SAMPLE_N
from bayes_basket_target.taf_nwp_transport_oos import MIN_TRANSPORT_SAMPLE_N
from bayes_basket_target.rolling_native_taf_oos import (
    EXACT_JOIN_SCHEMA,
    SOURCE_CLASS,
    build_rolling_origin_native_oos,
    build_rolling_taf_exact_joins,
    plan_non_overlapping_rolling_blocks,
    split_rolling_taf_exact_join,
)


def _days(count: int) -> list[str]:
    start = date(2026, 6, 1)
    return [(start + timedelta(days=index)).isoformat() for index in range(count)]


def test_four_rolling_blocks_require_52_unique_common_days() -> None:
    """One series must carry both windows an artifact needs, so it takes four.

    Three blocks cap the series at 24 samples, and the fit window plus the
    strictly later window it is gated on cannot share that.
    """

    plan = plan_non_overlapping_rolling_blocks(_days(52))

    assert plan["state"] == "READY"
    assert plan["block_count"] == 4
    assert [block["train_case_count"] for block in plan["blocks"]] == [20, 28, 36, 44]
    holdouts = [day for block in plan["blocks"] for day in block["holdout_dates"]]
    assert len(holdouts) == 32
    assert len(set(holdouts)) == 32
    assert plan["blocks"][0]["holdout_dates"] == _days(52)[20:28]
    assert plan["blocks"][3]["holdout_dates"] == _days(52)[44:52]


def test_29_days_remain_fail_closed_and_report_the_gap() -> None:
    plan = plan_non_overlapping_rolling_blocks(_days(29))

    assert plan["state"] == "ACCUMULATING"
    assert plan["block_count"] == 1
    assert plan["missing_common_case_count"] == 23


def test_rolling_plan_rejects_duplicate_or_weakened_window() -> None:
    with pytest.raises(ContractError, match="event_dates_not_unique"):
        plan_non_overlapping_rolling_blocks(_days(51) + [_days(1)[0]])
    with pytest.raises(ContractError, match="window_contract_invalid"):
        plan_non_overlapping_rolling_blocks(_days(52), min_train_days=19)


def _series_join(day_count: int) -> dict[str, object]:
    """One rolling series with a two-day decision lead, as d2 P0 produces."""

    samples = []
    for index in range(day_count):
        day = date(2026, 6, 1) + timedelta(days=index)
        truth_c = 20.0 + 0.5 * (index % 5)
        samples.append({
            "sample_id": f"rolling-TEST-{day.isoformat()}",
            "station_id": "TEST",
            "event_date": day.isoformat(),
            "settlement_timezone": "Etc/UTC",
            "calibration_scope": {
                "season_bin": "JJA",
                "lead_bucket": "d2",
                "run_cycle": "18Z",
                "settlement_definition": "official_daily_tmax_c",
            },
            "native_p0": {
                "probability_pathway": "CALIBRATED_NATIVE_MEMBERS_CROSS_ENSEMBLE",
                "prediction_oos": True,
                "trained_through_event_date": "2026-05-20",
                "decision_at_utc": (
                    day - timedelta(days=2)
                ).isoformat() + "T18:00:00Z",
                # A flat 20.5C forecast every day: the baseline the TAF
                # increment has to beat is deliberately uninformative.
                "probabilities": {"20": 0.5, "21": 0.5},
                "bucket_centers_c": {"20": 20.0, "21": 21.0},
                "prediction_content_sha256": content_sha256({"day": day.isoformat()}),
            },
            "taf": {
                "available_at_utc": (
                    day - timedelta(days=1)
                ).isoformat() + "T06:00:00Z",
                "tx_c": truth_c - 0.4,
                "cloud_cover_fraction": 0.2,
                "precipitation_probability": 0.1,
                "wind_speed_mps": 3.0,
                "source_class": "official_date_addressed_historical_replay",
                "truth_label_authority": "FORMAL_STATION_LANE_TRUTH",
                "reference_only": False,
            },
            "truth": {
                "tmax_c": truth_c,
                "canonical_truth_id": f"truth-TEST-{day.isoformat()}",
                "source_lane": "AWC",
                "authority_scope": "FORMAL_NATIVE_ELIGIBLE",
            },
            "taf_lineage": {
                "capture_content_sha256": "c" * 64,
                "truth_label_authority": "FORMAL_STATION_LANE_TRUTH",
                "reference_only": False,
                "enters_realtime_truth": False,
                "enters_p1": False,
                "enters_hard_no": False,
            },
            "settlement_window": {
                "end_utc": (day + timedelta(days=1)).isoformat() + "T00:00:00Z",
            },
        })
    identity = {
        "schema": EXACT_JOIN_SCHEMA,
        "source_class": SOURCE_CLASS,
        "station_id": "TEST",
        "season_bin": "JJA",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "settlement_definition": "official_daily_tmax_c",
        "settlement_timezone": "Etc/UTC",
        "nwp_crossfit_prediction_artifact_sha256": "d" * 64,
        "sample_count": len(samples),
        "samples": samples,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_split_hands_the_gate_a_window_the_fit_never_saw() -> None:
    split = split_rolling_taf_exact_join(_series_join(56))

    assert split["state"] == "READY"
    assert split["fit_sample_count"] >= MIN_TAF_CALIBRATION_SAMPLE_N
    assert split["transport_sample_count"] >= MIN_TRANSPORT_SAMPLE_N
    # The days between the windows are decided before the fit's truth closes,
    # so they belong to neither half.
    assert split["dropped_boundary_sample_count"] == 2
    fit_days = {row["event_date"] for row in split["fit"]["samples"]}
    transport_days = {row["event_date"] for row in split["transport"]["samples"]}
    assert not fit_days & transport_days
    boundary = split["strict_boundary_utc"]
    assert all(
        row["native_p0"]["decision_at_utc"] > boundary
        for row in split["transport"]["samples"]
    )
    # Both halves quote the series digest, which is what lets the artifact
    # fitted from one be replayed against the other at all.
    assert (
        split["fit"]["nwp_crossfit_prediction_artifact_sha256"]
        == split["transport"]["nwp_crossfit_prediction_artifact_sha256"]
        == "d" * 64
    )
    for half in ("fit", "transport"):
        document = split[half]
        identity = {
            key: value
            for key, value in document.items()
            if key != "content_sha256"
        }
        assert content_sha256(identity) == document["content_sha256"]
        assert document["sample_count"] == len(document["samples"])


def test_artifact_fitted_on_one_half_can_be_gated_on_the_other() -> None:
    """The two conditions the native gate applies, which no single join can meet.

    Fitting and qualifying on one document is impossible by construction: the
    artifact is stamped ``trained_through_utc`` at its last truth close, which
    is after every decision it saw, and it carries the crossfit digest of the
    dataset it was fitted from, so substituting a freshly built join fails the
    other way.  Splitting one series is what satisfies both at once.
    """

    from bayes_basket_target.taf_calibration import fit_taf_revision_artifact
    from scripts.fit_bayes_basket_target_taf_revision import _native_samples

    split = split_rolling_taf_exact_join(_series_join(56))
    artifact, _scorecard = fit_taf_revision_artifact(
        _native_samples(split["fit"], station_id="TEST"),
        artifact_id="taf-test-split",
        activation_id="taf-test-split-activation",
        season_bin="JJA",
        lead_bucket="d2",
        run_cycle="18Z",
        settlement_definition="official_daily_tmax_c",
        valid_from_utc="2026-08-01T00:00:00Z",
        valid_until_utc="2026-08-31T00:00:00Z",
    )

    transport = split["transport"]
    assert (
        artifact.nwp_combination_artifact_sha256
        == transport["nwp_crossfit_prediction_artifact_sha256"]
    )
    assert artifact.trained_through_utc == split["strict_boundary_utc"]
    assert all(
        artifact.trained_through_utc < row["native_p0"]["decision_at_utc"]
        for row in transport["samples"]
    )
    # And the fit half is exactly the window that is now off limits to the gate.
    assert all(
        row["native_p0"]["decision_at_utc"] < artifact.trained_through_utc
        for row in split["fit"]["samples"]
    )


def test_split_stays_accumulating_at_one_windows_worth_of_evidence() -> None:
    # Below one fit window plus one gate window there is nothing to cut.
    split = split_rolling_taf_exact_join(_series_join(16))

    assert split["state"] == "ACCUMULATING"
    assert split["fit"] is None and split["transport"] is None
    assert split["required_sample_count"] == (
        MIN_TAF_CALIBRATION_SAMPLE_N + MIN_TRANSPORT_SAMPLE_N
    )


def test_split_refuses_a_caller_supplied_minimum_below_either_gate() -> None:
    with pytest.raises(ContractError, match="split_minimum_below_gate"):
        split_rolling_taf_exact_join(_series_join(56), min_fit_sample_n=4)
    with pytest.raises(ContractError, match="split_minimum_below_gate"):
        split_rolling_taf_exact_join(_series_join(56), min_transport_sample_n=2)


def _cases(system_id: str) -> list[dict[str, object]]:
    rows = []
    for index, event_date in enumerate(_days(52)):
        day = date.fromisoformat(event_date)
        actual = 20.0 + float(index % 4)
        rows.append({
            "station_id": "TEST",
            "event_date": event_date,
            "aggregator_snapshot_at_utc": (
                day - timedelta(hours=6)
            ).isoformat() + "T00:00:00Z",
            "values": [actual - 0.2, actual, actual + 0.2],
            "member_set_sha256": ("a" if system_id == "ECMWF_TEST" else "b") * 64,
            "holdout_eligible": True,
            "transport_provider_id": "ECMWF_OPEN_DATA_S3" if system_id == "ECMWF_TEST" else "NOAA_GEFS_PDS_S3",
            "actual_tmax_c": actual,
            "settlement_definition": "official_daily_tmax_c",
            "timezone_name": "Etc/UTC",
        })
    return rows


def test_rolling_builder_emits_four_disjoint_20x8_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = {
        ("ECMWF_TEST", "JJA", "d2", "18Z"): _cases("ECMWF_TEST"),
        ("NOAA_TEST", "JJA", "d2", "18Z"): _cases("NOAA_TEST"),
    }
    systems = {
        "ECMWF_TEST": {"independence_family_id": "ECMWF"},
        "NOAA_TEST": {"independence_family_id": "NOAA_GEFS"},
    }
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "native_ensemble_foundation_schema_status",
        lambda _conn: {"ok": True},
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_load_cases",
        lambda _conn, **_kwargs: cases,
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_load_systems",
        lambda _conn: systems,
    )

    report = build_rolling_origin_native_oos(
        object(),
        station_id="TEST",
        as_of_utc="2026-08-01T00:00:00Z",
    )

    assert report["state"] == "READY"
    assert len(report["candidates"]) == 4
    assert [
        candidate["rolling_origin_block"]["block_index"]
        for candidate in report["candidates"]
    ] == [0, 1, 2, 3]
    assert all(candidate["state"] == "PRODUCTION_NO_LIVE_OOS_PASS" for candidate in report["candidates"])
    prediction_days = [
        prediction["event_date"]
        for candidate in report["candidates"]
        for prediction in candidate["predictions"]
    ]
    assert len(prediction_days) == 32
    assert len(set(prediction_days)) == 32


def test_rolling_exact_join_combines_all_blocks_without_promoting_awc(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = {
        ("ECMWF_TEST", "JJA", "d2", "18Z"): _cases("ECMWF_TEST"),
        ("NOAA_TEST", "JJA", "d2", "18Z"): _cases("NOAA_TEST"),
    }
    systems = {
        "ECMWF_TEST": {"independence_family_id": "ECMWF"},
        "NOAA_TEST": {"independence_family_id": "NOAA_GEFS"},
    }
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "native_ensemble_foundation_schema_status",
        lambda _conn: {"ok": True},
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_load_cases",
        lambda _conn, **_kwargs: cases,
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_load_systems",
        lambda _conn: systems,
    )
    report = build_rolling_origin_native_oos(
        object(),
        station_id="TEST",
        as_of_utc="2026-08-01T00:00:00Z",
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_bucket_intervals_for_candidate",
        lambda _conn, _candidate: {
            "19°C or below": (None, 19.5),
            "20°C": (19.5, 20.5),
            "21°C": (20.5, 21.5),
            "22°C or higher": (21.5, None),
        },
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_taf_truth_for_prediction",
        lambda _conn, **kwargs: (
            (22.0, "2026-08-01T00:00:00Z", "truth-test", "AWC", "FORMAL_NATIVE_ELIGIBLE"),
            ("2026-06-30T12:00:00Z", 22.0, 0.25, 0.1, 4.0, "c" * 64, "official_date_addressed_historical_replay", "FORMAL_STATION_LANE_TRUTH"),
            "2026-06-30T23:59:59Z",
        ),
    )

    joins = build_rolling_taf_exact_joins(object(), report)

    assert len(joins) == 1
    assert joins[0]["sample_count"] == 32
    assert {
        key: joins[0][key]
        for key in (
            "station_id", "season_bin", "lead_bucket", "run_cycle",
            "settlement_definition", "settlement_timezone",
        )
    } == {
        "station_id": "TEST",
        "season_bin": "JJA",
        "lead_bucket": "d2",
        "run_cycle": "18Z",
        "settlement_definition": "official_daily_tmax_c",
        "settlement_timezone": "Etc/UTC",
    }
    assert len({row["event_date"] for row in joins[0]["samples"]}) == 32
    assert all(row["taf_lineage"]["enters_p1"] is False for row in joins[0]["samples"])
    assert all(row["taf_lineage"]["enters_hard_no"] is False for row in joins[0]["samples"])


def test_a_series_with_no_bulletin_after_its_decision_does_not_abort_the_station(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Lead buckets differ in whether a bulletin can arrive after the decision.

    A run issued once the local day is already under way is decided after that
    day's bulletins have gone out, so its series can never be joined.  That is
    a property of the bucket, not of the station, and it used to abort every
    other bucket at the same station on the first missing day.
    """

    cases = {
        ("ECMWF_TEST", "JJA", "d2", "18Z"): _cases("ECMWF_TEST"),
        ("NOAA_TEST", "JJA", "d2", "18Z"): _cases("NOAA_TEST"),
    }
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "native_ensemble_foundation_schema_status",
        lambda _conn: {"ok": True},
    )
    monkeypatch.setattr(
        rolling_native_taf_oos, "_load_cases", lambda _conn, **_kwargs: cases
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_load_systems",
        lambda _conn: {
            "ECMWF_TEST": {"independence_family_id": "ECMWF"},
            "NOAA_TEST": {"independence_family_id": "NOAA_GEFS"},
        },
    )
    report = build_rolling_origin_native_oos(
        object(), station_id="TEST", as_of_utc="2026-08-01T00:00:00Z"
    )
    monkeypatch.setattr(
        rolling_native_taf_oos,
        "_bucket_intervals_for_candidate",
        lambda _conn, _candidate: {"20°C": (None, 20.5), "21°C": (20.5, None)},
    )

    def _no_bulletin(_conn, **_kwargs):
        raise ContractError("rolling_native_taf_exact_taf_missing")

    monkeypatch.setattr(
        rolling_native_taf_oos, "_taf_truth_for_prediction", _no_bulletin
    )
    skipped: list[dict] = []

    joins = build_rolling_taf_exact_joins(
        object(), report, skipped_scopes=skipped
    )

    assert joins == []
    assert len(skipped) == 1
    assert skipped[0]["reason"] == "rolling_native_taf_exact_taf_missing"
    assert skipped[0]["lead_bucket"] == "d2"

    # A violated contract is corruption, not a series waiting on evidence, and
    # still aborts.
    def _leaking(_conn, **_kwargs):
        raise ContractError("rolling_native_taf_prediction_leakage")

    monkeypatch.setattr(
        rolling_native_taf_oos, "_taf_truth_for_prediction", _leaking
    )
    with pytest.raises(ContractError, match="prediction_leakage"):
        build_rolling_taf_exact_joins(object(), report)


def test_historical_taf_join_uses_content_tiebreak_without_forward_only_columns() -> None:
    """Historical TAF facts lack forward-only revision and availability flags."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth("
        "station_id TEXT,local_date TEXT,settlement_definition TEXT,"
        "qc_status TEXT,authority_scope TEXT,revision_no INTEGER,"
        "official_tmax_c REAL,canonical_committed_at_utc TEXT,"
        "truth_id TEXT,source_lane TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_historical_taf_capture("
        "station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
        "tx_c REAL,authority_scope TEXT,"
        "truth_label_authority TEXT,provider_available_at_utc TEXT,"
        "cloud_cover_fraction REAL,precipitation_probability REAL,"
        "wind_speed_mps REAL,content_sha256 TEXT,source_class TEXT)"
    )
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "TEST", "2026-06-21", "official_daily_tmax_c", "ACCEPTED",
            "FORMAL_NATIVE_ELIGIBLE", 1, 22.0, "2026-06-22T01:00:00Z",
            "truth-test", "AWC",
        ),
    )
    common = (
        "TEST", "2026-06-21", "Etc/UTC", 22.5,
        "CALIBRATION_TRAIN_ONLY", "FORMAL_STATION_LANE_TRUTH",
        "2026-06-20T18:00:00Z", 0.25, 0.1, 4.0,
    )
    conn.execute(
        "INSERT INTO calibration_historical_taf_capture VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (*common, "a" * 64, "official_date_addressed_historical_replay"),
    )
    # A retired AMSC/temporary row sorts before the formal row by content
    # hash.  The exact join must nevertheless exclude it entirely.
    conn.execute(
        "INSERT INTO calibration_historical_taf_capture VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            *common[:5],
            "TEMPORARY_AWC_CALIBRATION_LABEL",
            *common[6:],
            "0" * 64,
            "official_date_addressed_historical_replay",
        ),
    )
    conn.execute(
        "INSERT INTO calibration_historical_taf_capture VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (*common, "b" * 64, "official_date_addressed_historical_replay"),
    )

    truth, taf, _day_end = rolling_native_taf_oos._taf_truth_for_prediction(
        conn,
        station_id="TEST",
        event_date="2026-06-21",
        settlement_timezone="Etc/UTC",
        settlement_definition="official_daily_tmax_c",
        decision_at_utc="2026-06-20T12:00:00Z",
    )

    assert truth[2] == "truth-test"
    assert taf[5] == "a" * 64


def test_historical_taf_join_rejects_amsc_truth_and_temporary_awc_label() -> None:
    """Formal-looking retired lineage must not cross the AWC/OOS boundary."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth("
        "station_id TEXT,local_date TEXT,settlement_definition TEXT,"
        "qc_status TEXT,authority_scope TEXT,revision_no INTEGER,"
        "official_tmax_c REAL,canonical_committed_at_utc TEXT,"
        "truth_id TEXT,source_lane TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_historical_taf_capture("
        "station_id TEXT,event_date TEXT,settlement_timezone TEXT,"
        "tx_c REAL,authority_scope TEXT,"
        "truth_label_authority TEXT,provider_available_at_utc TEXT,"
        "cloud_cover_fraction REAL,precipitation_probability REAL,"
        "wind_speed_mps REAL,content_sha256 TEXT,source_class TEXT)"
    )
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "TEST", "2026-06-21", "official_daily_tmax_c", "ACCEPTED",
            "FORMAL_NATIVE_ELIGIBLE", 1, 22.0, "2026-06-22T01:00:00Z",
            "amsc-truth", "AMSC",
        ),
    )
    conn.execute(
        "INSERT INTO calibration_historical_taf_capture VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            "TEST", "2026-06-21", "Etc/UTC", 22.5,
            "CALIBRATION_TRAIN_ONLY", "TEMPORARY_AWC_CALIBRATION_LABEL",
            "2026-06-20T18:00:00Z", 0.25, 0.1, 4.0, "a" * 64,
            "official_date_addressed_historical_replay",
        ),
    )

    with pytest.raises(ContractError, match="rolling_native_taf_canonical_truth_missing"):
        rolling_native_taf_oos._taf_truth_for_prediction(
            conn,
            station_id="TEST",
            event_date="2026-06-21",
            settlement_timezone="Etc/UTC",
            settlement_definition="official_daily_tmax_c",
            decision_at_utc="2026-06-20T12:00:00Z",
        )

    conn.execute("DELETE FROM calibration_station_daily_high_truth")
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            "TEST", "2026-06-21", "official_daily_tmax_c", "ACCEPTED",
            "FORMAL_NATIVE_ELIGIBLE", 1, 22.0, "2026-06-22T01:00:00Z",
            "awc-truth", "AWC",
        ),
    )

    with pytest.raises(ContractError, match="rolling_native_taf_exact_taf_missing"):
        rolling_native_taf_oos._taf_truth_for_prediction(
            conn,
            station_id="TEST",
            event_date="2026-06-21",
            settlement_timezone="Etc/UTC",
            settlement_definition="official_daily_tmax_c",
            decision_at_utc="2026-06-20T12:00:00Z",
        )


def test_historical_bucket_join_uses_bucket_schema_without_member_run_cycle() -> None:
    """Bucket facts omit the cycle kept by their associated member-run facts."""

    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_historical_member_bucket("
        "ensemble_system_id TEXT,station_id TEXT,target_local_date TEXT,"
        "archive_lead_bucket TEXT,archive_query_at_utc TEXT,"
        "bucket_intervals_json TEXT)"
    )
    conn.execute(
        "INSERT INTO calibration_native_ensemble_historical_member_bucket "
        "VALUES(?,?,?,?,?,?)",
        (
            "ECMWF_TEST", "TEST", "2026-06-21", "d2",
            "2026-06-19T18:00:00Z",
            '{"19°C or below":[null,19.5],"20°C":[19.5,20.5]}',
        ),
    )

    intervals = rolling_native_taf_oos._bucket_intervals_for_candidate(
        conn,
        {
            "selected_system_ids": ["ECMWF_TEST", "NOAA_TEST"],
            "station_id": "TEST",
            "lead_bucket": "d2",
            "run_cycle_by_system": {
                "ECMWF_TEST": "18Z",
                "NOAA_TEST": "18Z",
            },
            "predictions": [{"event_date": "2026-06-21"}],
        },
    )

    assert intervals == {"19°C or below": (None, 19.5), "20°C": (19.5, 20.5)}
