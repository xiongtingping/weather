from __future__ import annotations

import copy
import sqlite3

import pytest

from bayes_basket_target.calibration_ingress import (
    FORMAL_NATIVE,
    NO_LIVE_REPLAY,
    ingest_calibration_base_facts,
)
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.ssot import (
    DDL,
    _upgrade_empty_calibration_model_pool_schema,
    apply_production_ssot_schema,
)


def _hashed(row: dict) -> dict:
    return {**row, "content_sha256": content_sha256(row)}


def _packet() -> dict:
    station = _hashed({
        "station_id": "KAAA", "icao_code": "KAAA", "city_label": "A",
        "timezone_name": "America/New_York", "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "registry_version": "bayes_basket_target_calibration_evidence_v1",
        "source_role": "CANONICAL_AMSC_AWC_INGRESS",
    })
    truth = _hashed({
        "truth_id": "truth-1", "station_id": "KAAA", "local_date": "2026-07-20",
        "target_timezone": "America/New_York",
        "official_tmax_c": 30.0, "source_lane": "AWC",
        "source_role": "CANONICAL_AMSC_AWC_INGRESS",
        "settlement_definition": "official_daily_tmax_c", "revision_no": 0,
        "published_at_utc": None, "received_at_utc": "2026-07-21T04:05:00Z",
        "canonical_committed_at_utc": "2026-07-21T04:06:00Z",
        "timestamps_complete": True, "qc_status": "ACCEPTED",
        "source_payload_sha256": "a" * 64, "observation_ids": ["obs-1"],
    })
    models = []
    residuals = []
    for model in ("ECMWF", "GFS", "ICON", "GEM", "CMA"):
        aggregator = model in {"ICON", "GEM", "CMA"}
        model_row = _hashed({
            "model_run_id": f"run-{model}", "model_id": model,
            "station_id": "KAAA", "target_local_date": "2026-07-20",
            "target_timezone": "America/New_York",
            "run_init_utc": "2026-07-19T00:00:00Z", "maturity_key": "D2",
            "published_at_utc": None if aggregator else "2026-07-19T06:00:00Z",
            "received_at_utc": None if aggregator else "2026-07-19T06:01:00Z",
            "timestamps_complete": not aggregator, "lead_bucket": "d2",
            "run_cycle": "00Z", "raw_tmax_c": 29.0,
            "settlement_definition": "official_daily_tmax_c",
            "provider_id": {
                "ECMWF": "ecmwf_ifs025_official",
                "GFS": "gfs_0p25_official",
                "ICON": "icon_global_open_meteo",
                "GEM": "gem_global_open_meteo",
                "CMA": "cma_grapes_global_open_meteo",
            }[model],
            "provider_role": {
                "ECMWF": "ecmwf_ifs025_official",
                "GFS": "gfs_0p25_official",
                "ICON": "icon_global_official",
                "GEM": "gem_global_open_meteo",
                "CMA": "cma_grapes_global_open_meteo",
            }[model],
            "source_class": "aggregator_republisher" if aggregator else "native_or_official_cloud_replica",
            "aggregator_or_republisher": aggregator, "open_meteo": aggregator,
            "source_url": "https://example.invalid", "raw_object_ref": "/bounded/raw",
            "source_object_sha256": {
                "ECMWF": "b" * 64,
                "GFS": "c" * 64,
                "ICON": "d" * 64,
                "GEM": "e" * 64,
                "CMA": "1" * 64,
            }[model],
            "asof_status": "HISTORICAL_REPLAY_ONLY" if aggregator else "PROSPECTIVE_STRICT_ASOF",
            "capture_modes": ["historical_rebuild"],
            "historical_replay_eligible": True,
            "prospective_strict_asof_eligible": not aggregator,
        })
        models.append(model_row)
        residuals.append(_hashed({
            "residual_id": f"res-{model}", "truth_id": "truth-1",
            "model_run_id": f"run-{model}", "station_id": "KAAA",
            "model_id": model, "season_bin": "summer", "lead_bucket": "d2",
            "run_cycle": "00Z", "maturity_key": "D2",
            "e_y_minus_f_raw_c": 1.0, "paired_asof_utc": "2026-07-19T00:00:00Z",
            "qc_status": "ACCEPTED", "historical_replay_eligible": True,
            "prospective_strict_asof_eligible": not aggregator,
            "pair_sha256": "f" * 64,
        }))
    return {
        "schema": "bayes_basket_target_calibration_evidence_v1",
        "as_of_utc": "2026-07-22T12:00:00Z",
        "station_registry": [station], "truth_evidence": [truth],
        "calibration_model_rows": models, "residual_evidence": residuals,
        "temporal_boundary": {"closed_settlement_event_days_only": True},
        "checks": {"truth_source_canonical_ingress": True},
        "boundary": {
            "calls_order_gateway": False, "places_orders": False,
            "live_order_authorized": False,
        },
    }


def test_candidate_and_native_base_rows_share_ssot_but_keep_authority_separate() -> None:
    packet = _packet()
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        preview = ingest_calibration_base_facts(conn, packet)
        assert preview["state"] == "VALIDATED_NO_WRITE"
        assert conn.execute("SELECT COUNT(*) FROM calibration_historical_asof_model_run").fetchone()[0] == 0

        result = ingest_calibration_base_facts(conn, packet, commit=True)
        assert result["counts"] == {"stations": 1, "truths": 1, "model_runs": 5, "residuals": 5}
        authorities = dict(conn.execute(
            "SELECT model_id,authority_scope FROM calibration_historical_asof_model_run"
        ).fetchall())
        assert authorities == {
            "ECMWF": FORMAL_NATIVE,
            "GFS": FORMAL_NATIVE,
            "ICON": NO_LIVE_REPLAY,
            "GEM": NO_LIVE_REPLAY,
            "CMA": NO_LIVE_REPLAY,
        }
        assert ingest_calibration_base_facts(conn, packet, commit=True)["counts"] == {
            "stations": 0, "truths": 0, "model_runs": 0, "residuals": 0,
        }


def test_empty_three_model_tables_upgrade_to_five_model_constraints() -> None:
    legacy_ddl = DDL.replace(
        "('ECMWF','GFS','ICON','GEM','CMA')",
        "('ECMWF','GFS','ICON')",
    )
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(legacy_ddl)
        result = _upgrade_empty_calibration_model_pool_schema(conn)
        assert result["state"] == "rebuilt_empty_tables"
        sql = conn.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE name='calibration_historical_asof_model_run'"
        ).fetchone()[0]
        assert "'GEM'" in sql and "'CMA'" in sql


def test_populated_three_model_tables_require_reviewed_migration() -> None:
    legacy_ddl = DDL.replace(
        "('ECMWF','GFS','ICON','GEM','CMA')",
        "('ECMWF','GFS','ICON')",
    )
    with sqlite3.connect(":memory:") as conn:
        conn.executescript(legacy_ddl)
        conn.execute(
            "INSERT INTO calibration_model_artifact VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "cal-legacy", "KAAA", "ECMWF", "summer", "d2", "00Z",
                "official_daily_tmax_c", "2026-07-01T00:00:00Z", 20,
                20.0, 0.0, 1.0, "{}", "2026-07-01T00:00:00Z",
                "2026-08-01T00:00:00Z", "a" * 64,
            ),
        )
        with pytest.raises(
            RuntimeError,
            match="requires_reviewed_migration",
        ):
            _upgrade_empty_calibration_model_pool_schema(conn)


def test_bad_content_address_rolls_back_all_rows() -> None:
    packet = _packet()
    packet["calibration_model_rows"][1]["raw_tmax_c"] = 31.0
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(ContractError, match="content_sha256_mismatch"):
            ingest_calibration_base_facts(conn, packet, commit=True)
        assert conn.execute("SELECT COUNT(*) FROM calibration_station_registry").fetchone()[0] == 0


def test_future_et_truth_is_rejected() -> None:
    packet = copy.deepcopy(_packet())
    row = packet["truth_evidence"][0]
    row["local_date"] = "2026-07-23"
    row["content_sha256"] = content_sha256({key: value for key, value in row.items() if key != "content_sha256"})
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(ContractError, match="future_event_day_not_allowed"):
            ingest_calibration_base_facts(conn, packet, commit=True)


def test_rkpk_truth_and_models_close_on_registry_kst_not_et() -> None:
    packet = copy.deepcopy(_packet())
    station = packet["station_registry"][0]
    station["station_id"] = station["icao_code"] = "RKPK"
    station["timezone_name"] = "Asia/Seoul"
    station["content_sha256"] = content_sha256({
        key: value for key, value in station.items() if key != "content_sha256"
    })
    truth = packet["truth_evidence"][0]
    truth["station_id"] = "RKPK"
    truth["local_date"] = "2026-07-27"
    truth["target_timezone"] = "Asia/Seoul"
    truth["content_sha256"] = content_sha256({
        key: value for key, value in truth.items() if key != "content_sha256"
    })
    for model in packet["calibration_model_rows"]:
        model["station_id"] = "RKPK"
        model["target_local_date"] = "2026-07-27"
        model["target_timezone"] = "Asia/Seoul"
        model["content_sha256"] = content_sha256({
            key: value for key, value in model.items() if key != "content_sha256"
        })
    for residual in packet["residual_evidence"]:
        residual["station_id"] = "RKPK"
        residual["content_sha256"] = content_sha256({
            key: value for key, value in residual.items() if key != "content_sha256"
        })
    packet["as_of_utc"] = "2026-07-27T15:00:00Z"

    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        assert ingest_calibration_base_facts(conn, packet)["state"] == "VALIDATED_NO_WRITE"


def test_calibration_timezone_mismatch_fails_closed() -> None:
    packet = copy.deepcopy(_packet())
    truth = packet["truth_evidence"][0]
    truth["target_timezone"] = "Asia/Seoul"
    truth["content_sha256"] = content_sha256({
        key: value for key, value in truth.items() if key != "content_sha256"
    })
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="calibration_truth_settlement_timezone_mismatch",
        ):
            ingest_calibration_base_facts(conn, packet)
