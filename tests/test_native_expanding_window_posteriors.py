"""Historical root posteriors must be strictly as-of and training-only.

The trajectory fit scores folds under ``root posterior x likelihood``, and no
committed P0 exists before 2026-08-02.  The reconstruction is honest only if
every number derives from information that predated the day: parameters fitted
on settled days strictly before it, members from runs issued before it, and an
authority label that can never be resolved as P0.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path


def _load():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "build_native_expanding_window_posteriors",
        root / "scripts" / "build_native_expanding_window_posteriors.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


emitter = _load()


def _seed(days: int) -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE calibration_station_daily_high_truth("
        "station_id TEXT, local_date TEXT, official_tmax_c REAL,"
        "qc_status TEXT)"
    )
    conn.execute(
        "CREATE TABLE calibration_native_ensemble_historical_member_run("
        "station_id TEXT, ensemble_system_id TEXT, target_local_date TEXT,"
        "archive_lead_bucket TEXT, transport_provider_id TEXT,"
        "raw_tmax_c REAL)"
    )
    conn.execute(
        "CREATE TABLE basket_target_market_registry("
        "station_id TEXT, event_date TEXT, settlement_unit TEXT,"
        "outcome TEXT, registry_json TEXT, source_eligible INTEGER)"
    )
    for index in range(days):
        day = f"2026-07-{index + 1:02d}"
        conn.execute(
            "INSERT INTO calibration_station_daily_high_truth VALUES"
            "('ZUUU',?,?,'ACCEPTED')",
            (day, 30.0 + (index % 3)),
        )
        for system in emitter.SYSTEMS:
            for member in range(5):
                conn.execute(
                    "INSERT INTO calibration_native_ensemble_historical_member_run "
                    "VALUES('ZUUU',?,?,'d1','OFFICIAL',?)",
                    (system, day, 29.0 + (index % 3) + member * 0.4),
                )
        for bucket, payload in (
            ("30°C or below", '{"lower": null, "upper": 30}'),
            ("31°C", '{"lower": 31, "upper": 31}'),
            ("32°C or higher", '{"lower": 32, "upper": null}'),
        ):
            conn.execute(
                "INSERT INTO basket_target_market_registry VALUES"
                "('ZUUU',?,'C',?,?,1)",
                (day, bucket, payload),
            )
    return conn


def test_first_posterior_waits_for_the_training_minimum() -> None:
    conn = _seed(25)

    result = emitter.build_station_posteriors(
        conn, station_id="ZUUU", lead_bucket="d1", min_train_days=20
    )

    days = [row["event_date"] for row in result["posteriors"]]
    # Days 1..20 lack twenty strictly-prior settled days; 21..25 qualify.
    assert days == [f"2026-07-{index:02d}" for index in range(21, 26)]
    assert result["skipped"]["insufficient_train_days"] == 20


def test_posteriors_are_normalised_and_labelled_training_only() -> None:
    conn = _seed(23)

    result = emitter.build_station_posteriors(
        conn, station_id="ZUUU", lead_bucket="d1", min_train_days=20
    )

    assert result["authority_scope"] == "TRAINING_ONLY_NEVER_P0"
    assert result["evidence_class"] == "HISTORICAL_EXPANDING_WINDOW_V1"
    for row in result["posteriors"]:
        total = sum(row["root_probabilities"].values())
        assert abs(total - 1.0) < 1e-9
        assert set(row["root_probabilities"]) == set(row["bucket_centers_c"])
        assert row["train_day_count"] >= 20
        # The curve combiner mirrors production's bias-corrected shape.
        assert set(row["system_bias_c"]) == set(emitter.SYSTEMS)


def test_open_meteo_rows_never_enter_the_reconstruction() -> None:
    # Byte-identical with and without the republisher row: the only complete
    # proof of exclusion, since any distribution-shaped assertion can be
    # satisfied by the official members alone.
    baseline = emitter.build_station_posteriors(
        _seed(22), station_id="ZUUU", lead_bucket="d1", min_train_days=20
    )
    contaminated = _seed(22)
    contaminated.execute(
        "INSERT INTO calibration_native_ensemble_historical_member_run "
        "VALUES('ZUUU',?, '2026-07-21','d1','OPEN_METEO', 99.0)",
        (emitter.SYSTEMS[0],),
    )

    result = emitter.build_station_posteriors(
        contaminated, station_id="ZUUU", lead_bucket="d1", min_train_days=20
    )

    assert result["content_sha256"] == baseline["content_sha256"]
