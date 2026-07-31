from __future__ import annotations

import copy
import sqlite3

import pytest

from bayes_basket_target.calibration_ingress import (
    FORMAL_NATIVE,
    NO_LIVE_REPLAY,
    TEMPORARY_AWC_LABEL,
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


def test_incremental_packet_can_bind_existing_truth_without_duplication() -> None:
    first = _packet()
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        ingest_calibration_base_facts(conn, first, commit=True)
        existing = dict(conn.execute(
            "SELECT truth_id,station_id,local_date,official_tmax_c,"
            "source_lane,settlement_definition,revision_no,qc_status,"
            "source_role,authority_scope,content_sha256 "
            "FROM calibration_station_daily_high_truth"
        ).fetchone())
        reference = dict(existing)
        reference["reference_sha256"] = content_sha256(reference)

        incremental = copy.deepcopy(first)
        incremental["truth_evidence"] = []
        incremental["truth_references"] = [reference]
        for model in incremental["calibration_model_rows"]:
            old_id = model["model_run_id"]
            model["model_run_id"] = old_id + "-12z"
            model["run_init_utc"] = "2026-07-19T12:00:00Z"
            model["run_cycle"] = "12Z"
            model["content_sha256"] = content_sha256({
                key: value
                for key, value in model.items()
                if key != "content_sha256"
            })
        for residual in incremental["residual_evidence"]:
            residual["residual_id"] += "-12z"
            residual["model_run_id"] += "-12z"
            residual["run_cycle"] = "12Z"
            residual["paired_asof_utc"] = "2026-07-19T12:00:00Z"
            residual["content_sha256"] = content_sha256({
                key: value
                for key, value in residual.items()
                if key != "content_sha256"
            })

        result = ingest_calibration_base_facts(
            conn,
            incremental,
            commit=True,
        )
        assert result["counts"] == {
            "stations": 0,
            "truths": 0,
            "model_runs": 5,
            "residuals": 5,
        }
        assert result["validated_counts"]["truth_references"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_station_daily_high_truth"
        ).fetchone()[0] == 1

        tampered = copy.deepcopy(incremental)
        tampered["truth_references"][0]["official_tmax_c"] = 99.0
        tampered["truth_references"][0]["reference_sha256"] = content_sha256({
            key: value
            for key, value in tampered["truth_references"][0].items()
            if key != "reference_sha256"
        })
        with pytest.raises(
            ContractError,
            match="truth_reference_content_mismatch",
        ):
            ingest_calibration_base_facts(conn, tampered)


def test_truth_revision_can_bind_existing_model_runs_by_hash() -> None:
    first = _packet()
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        ingest_calibration_base_facts(conn, first, commit=True)

        revised = copy.deepcopy(first)
        truth = revised["truth_evidence"][0]
        truth["truth_id"] = "truth-2"
        truth["revision_no"] = 1
        truth["official_tmax_c"] = 31.0
        truth["content_sha256"] = content_sha256({
            key: value
            for key, value in truth.items()
            if key != "content_sha256"
        })
        model_references = []
        for row in conn.execute(
            "SELECT model_run_id,model_id,station_id,target_local_date,"
            "run_init_utc,lead_bucket,maturity_key,run_cycle,raw_tmax_c,"
            "settlement_definition,authority_scope,content_sha256 "
            "FROM calibration_historical_asof_model_run "
            "ORDER BY model_id"
        ):
            reference = dict(row)
            reference["reference_sha256"] = content_sha256(reference)
            model_references.append(reference)
        revised["calibration_model_rows"] = []
        revised["model_references"] = model_references
        for residual in revised["residual_evidence"]:
            residual["residual_id"] += "-revision-1"
            residual["truth_id"] = "truth-2"
            residual["e_y_minus_f_raw_c"] = 2.0
            residual["content_sha256"] = content_sha256({
                key: value
                for key, value in residual.items()
                if key != "content_sha256"
            })

        tampered = copy.deepcopy(revised)
        tampered["model_references"][0]["raw_tmax_c"] = 99.0
        tampered["model_references"][0]["reference_sha256"] = content_sha256({
            key: value
            for key, value in tampered["model_references"][0].items()
            if key != "reference_sha256"
        })
        with pytest.raises(
            ContractError,
            match="calibration_model_reference_content_mismatch",
        ):
            ingest_calibration_base_facts(conn, tampered)

        result = ingest_calibration_base_facts(
            conn,
            revised,
            commit=True,
        )
        assert result["counts"] == {
            "stations": 0,
            "truths": 1,
            "model_runs": 0,
            "residuals": 5,
        }
        assert result["validated_counts"]["model_references"] == 5
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_historical_asof_model_run"
        ).fetchone()[0] == 5
        assert conn.execute(
            "SELECT COUNT(*) FROM calibration_model_station_residual"
        ).fetchone()[0] == 10


def test_temporary_awc_truth_is_explicit_and_never_formal_or_realtime() -> None:
    packet = copy.deepcopy(_packet())
    station = packet["station_registry"][0]
    station["source_lane"] = "AMSC"
    station["content_sha256"] = content_sha256({
        key: value
        for key, value in station.items()
        if key != "content_sha256"
    })
    truth = packet["truth_evidence"][0]
    truth["source_role"] = TEMPORARY_AWC_LABEL
    truth["content_sha256"] = content_sha256({
        key: value
        for key, value in truth.items()
        if key != "content_sha256"
    })
    packet["checks"] = {
        "truth_source_canonical_ingress": False,
        "temporary_awc_calibration_label_explicit": True,
    }
    packet["boundary"].update({
        "enters_realtime_truth": False,
        "enters_p1": False,
        "enters_hard_no": False,
    })

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        result = ingest_calibration_base_facts(conn, packet, commit=True)

        assert result["temporary_awc_truth_rows"] == 1
        assert tuple(conn.execute(
            "SELECT source_lane,source_role,authority_scope "
            "FROM calibration_station_daily_high_truth"
        ).fetchone()) == (
            "AWC",
            TEMPORARY_AWC_LABEL,
            NO_LIVE_REPLAY,
        )
        assert {
            row[0]
            for row in conn.execute(
                "SELECT DISTINCT authority_scope "
                "FROM calibration_model_station_residual"
            )
        } == {NO_LIVE_REPLAY}


def test_temporary_awc_truth_without_isolation_boundary_fails_closed() -> None:
    packet = copy.deepcopy(_packet())
    truth = packet["truth_evidence"][0]
    truth["source_role"] = TEMPORARY_AWC_LABEL
    truth["content_sha256"] = content_sha256({
        key: value
        for key, value in truth.items()
        if key != "content_sha256"
    })
    packet["checks"] = {
        "truth_source_canonical_ingress": False,
        "temporary_awc_calibration_label_explicit": True,
    }
    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="temporary_awc_calibration_boundary_invalid",
        ):
            ingest_calibration_base_facts(conn, packet)


def test_open_meteo_previous_runs_are_historical_replay_only() -> None:
    packet = _packet()
    replacements = {
        "ECMWF": "ecmwf_ifs_open_meteo_previous_runs",
        "GFS": "gfs_global_open_meteo_previous_runs",
    }
    for row in packet["calibration_model_rows"]:
        provider = replacements.get(row["model_id"])
        if provider is None:
            continue
        row.update({
            "provider_id": provider,
            "source_class": "aggregator_republisher",
            "aggregator_or_republisher": True,
            "open_meteo": True,
            "published_at_utc": None,
            "received_at_utc": "2026-07-22T00:00:00Z",
            "timestamps_complete": False,
            "asof_status": "HISTORICAL_REPLAY_ONLY",
            "historical_replay_eligible": True,
            "prospective_strict_asof_eligible": False,
        })
        row["content_sha256"] = content_sha256({
            key: value for key, value in row.items()
            if key != "content_sha256"
        })

    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        apply_production_ssot_schema(conn)
        result = ingest_calibration_base_facts(conn, packet, commit=True)
        assert result["formal_native_model_rows"] == 0
        assert {
            authority
            for authority, in conn.execute(
                "SELECT DISTINCT authority_scope "
                "FROM calibration_historical_asof_model_run"
            )
        } == {NO_LIVE_REPLAY}


def test_open_meteo_previous_runs_cannot_claim_native_arrival() -> None:
    packet = _packet()
    row = packet["calibration_model_rows"][0]
    row.update({
        "provider_id": "ecmwf_ifs_open_meteo_previous_runs",
        "source_class": "aggregator_republisher",
        "aggregator_or_republisher": True,
        "open_meteo": True,
        "asof_status": "PROSPECTIVE_STRICT_ASOF",
        "historical_replay_eligible": True,
        "prospective_strict_asof_eligible": True,
    })
    row["content_sha256"] = content_sha256({
        key: value for key, value in row.items()
        if key != "content_sha256"
    })

    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="calibration_aggregator_native_arrival_forbidden",
        ):
            ingest_calibration_base_facts(conn, packet)


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


def test_any_single_model_scope_fails_closed() -> None:
    packet = copy.deepcopy(_packet())
    second_scope_model = copy.deepcopy(packet["calibration_model_rows"][0])
    second_scope_model["model_run_id"] = "run-ECMWF-d1"
    second_scope_model["lead_bucket"] = "d1"
    second_scope_model["maturity_key"] = "D1"
    second_scope_model["content_sha256"] = content_sha256({
        key: value
        for key, value in second_scope_model.items()
        if key != "content_sha256"
    })
    packet["calibration_model_rows"].append(second_scope_model)

    second_scope_residual = copy.deepcopy(packet["residual_evidence"][0])
    second_scope_residual["residual_id"] = "res-ECMWF-d1"
    second_scope_residual["model_run_id"] = "run-ECMWF-d1"
    second_scope_residual["lead_bucket"] = "d1"
    second_scope_residual["maturity_key"] = "D1"
    second_scope_residual["content_sha256"] = content_sha256({
        key: value
        for key, value in second_scope_residual.items()
        if key != "content_sha256"
    })
    packet["residual_evidence"].append(second_scope_residual)

    with sqlite3.connect(":memory:") as conn:
        apply_production_ssot_schema(conn)
        with pytest.raises(
            ContractError,
            match="calibration_single_model_scope_forbidden",
        ):
            ingest_calibration_base_facts(conn, packet)


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
