from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from bayes_basket_target.canonical_contract import CanonicalModelFact
from bayes_basket_target.canonical_ingress import ingest_canonical_contract
from bayes_basket_target.contracts import ContractError, content_sha256
from bayes_basket_target.historical_scope_builders import (
    D0_RUN_CYCLE,
    D2_ALL_DATES,
    D2_RUN_CYCLE,
    build_d0_global_ensemble_from_connection,
    build_d2_global_deterministic_from_connection,
)
from bayes_basket_target.model_builders import builder_for
from bayes_basket_target.ssot import apply_target_ssot_schema


MODELS = ("ECMWF", "GFS", "ICON", "GEM", "CMA")


def _stamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _connection() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_target_ssot_schema(conn)
    station = {
        "station_id": "TEST",
        "icao_code": "TEST",
        "city_label": "Test",
        "timezone_name": "Etc/UTC",
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "registry_version": "test-v1",
        "source_role": "TEST",
    }
    conn.execute(
        "INSERT INTO calibration_station_registry VALUES(?,?,?,?,?,?,?,?,?)",
        (*station.values(), content_sha256(station)),
    )
    binding = {
        "binding_id": "binding-test",
        "station_id": "TEST",
        "settlement_timezone": "Etc/UTC",
        "market_registry_timezone_count": 1,
        "historical_truth_evidence_sha256": None,
        "binding_version": "test-v1",
        "bound_at_utc": "2026-08-01T00:00:00Z",
    }
    conn.execute(
        "INSERT INTO calibration_station_market_timezone_binding "
        "VALUES(?,?,?,?,?,?,?,?)",
        (*binding.values(), content_sha256(binding)),
    )
    return conn


def _insert_truth(conn: sqlite3.Connection, day: str, value: float) -> str:
    parsed = datetime.combine(date.fromisoformat(day), datetime.min.time(), tzinfo=timezone.utc)
    committed = parsed + timedelta(days=1, hours=1)
    truth_id = f"truth-{day}"
    identity = {
        "truth_id": truth_id,
        "station_id": "TEST",
        "local_date": day,
        "settlement_timezone": "Etc/UTC",
        "official_tmax_c": value,
        "source_lane": "AWC",
        "settlement_definition": "official_daily_tmax_c",
        "revision_no": 1,
        "published_at_utc": _stamp(committed),
        "received_at_utc": _stamp(committed),
        "canonical_committed_at_utc": _stamp(committed),
        "timestamps_complete": 1,
        "qc_status": "ACCEPTED",
        "source_role": "AWC_HISTORICAL_REPLAY_INGRESS",
        "source_payload_sha256": content_sha256({"truth": truth_id}),
        "observation_ids_json": "[]",
        "authority_scope": "NO_LIVE_REPLAY_CANDIDATE",
    }
    conn.execute(
        "INSERT INTO calibration_station_daily_high_truth VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*identity.values(), content_sha256(identity)),
    )
    return truth_id


def _insert_d2_model(
    conn: sqlite3.Connection,
    day: str,
    model: str,
    value: float,
    *,
    lead_bucket: str = "d2",
    run_cycle: str = D2_RUN_CYCLE,
) -> None:
    run_init = datetime.combine(
        date.fromisoformat(day) - timedelta(days=2),
        datetime.min.time(),
        tzinfo=timezone.utc,
    )
    run_id = f"d2-{day}-{model}"
    identity = {
        "model_run_id": run_id,
        "model_id": model,
        "station_id": "TEST",
        "target_local_date": day,
        "settlement_timezone": "Etc/UTC",
        "run_init_utc": _stamp(run_init),
        "published_at_utc": None,
        "received_at_utc": _stamp(run_init),
        "timestamps_complete": 0,
        "lead_bucket": lead_bucket,
        "maturity_key": "D2",
        "run_cycle": run_cycle,
        "raw_tmax_c": value,
        "settlement_definition": "official_daily_tmax_c",
        "provider_id": f"open-meteo-{model.lower()}",
        "provider_role": f"open-meteo-{model.lower()}",
        "source_class": "aggregator_republisher",
        "aggregator_or_republisher": 1,
        "open_meteo": 1,
        "source_url": "https://example.test/historical",
        "raw_object_ref": f"raw/{run_id}",
        "asof_status": "HISTORICAL_REPLAY_ONLY",
        "capture_modes_json": "[\"historical\"]",
        "historical_replay_eligible": 1,
        "prospective_strict_asof_eligible": 0,
        "authority_scope": "NO_LIVE_REPLAY_CANDIDATE",
        "source_object_sha256": content_sha256({"model": run_id}),
    }
    conn.execute(
        "INSERT INTO calibration_historical_asof_model_run VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*identity.values(), content_sha256(identity)),
    )


def _seed_d2(conn: sqlite3.Connection) -> None:
    for index, day in enumerate(D2_ALL_DATES):
        value = 20.0 + (index % 4)
        _insert_truth(conn, day, value)
        for model_index, model in enumerate(MODELS):
            _insert_d2_model(conn, day, model, value + 0.1 * model_index)
    conn.commit()


def test_d2_legacy_projection_builder_is_retired() -> None:
    conn = _connection()
    _seed_d2(conn)

    packet, receipt = build_d2_global_deterministic_from_connection(
        conn,
        stations=["TEST"],
        as_of_utc="2026-08-03T12:00:00Z",
        generation=1,
    )

    assert receipt["state"] == "BLOCKED"
    assert receipt["blockers"] == ["LEGACY_PROJECTION_RETIRED"]
    assert packet is None
    assert conn.execute("SELECT COUNT(*) FROM canonical_ssot_fact").fetchone()[0] == 0


def test_d2_builder_does_not_borrow_a_d1_row_when_its_exact_scope_is_missing() -> None:
    conn = _connection()
    # A realistic-looking d1 row is deliberately irrelevant to the d2 query.
    for day in D2_ALL_DATES:
        _insert_truth(conn, day, 20.0)
    row_day = D2_ALL_DATES[0]
    for model in MODELS:
        _insert_d2_model(
            conn,
            row_day,
            model,
            20.0,
            lead_bucket="d1",
            run_cycle="PREVIOUS_DAY1",
        )
    conn.commit()

    packet, receipt = build_d2_global_deterministic_from_connection(
        conn,
        stations=["TEST"],
        as_of_utc="2026-08-03T12:00:00Z",
        generation=1,
    )

    assert packet is None
    assert receipt["state"] == "BLOCKED"
    assert "EXACT_D2_MODEL_SCOPE_MISSING" in receipt["station_statuses"][0]["blockers"]


@pytest.mark.parametrize(
    ("member_run_inits", "expected_reason"),
    (
        (("", ""), "MEMBER_RUN_INIT_MISSING"),
        (("2026-06-30T00:00:00", "2026-06-30T00:00:00"), "MEMBER_RUN_INIT_NOT_UTC"),
        (("2026-06-30T00:00:00Z", "2026-06-30T01:00:00Z"), "MEMBER_RUN_INIT_INCONSISTENT"),
    ),
)
def test_d0_global_ensemble_fails_closed_when_upstream_run_init_is_unproven(
    monkeypatch: pytest.MonkeyPatch,
    member_run_inits: tuple[str, str],
    expected_reason: str,
) -> None:
    import bayes_basket_target.historical_scope_builders as builders

    conn = _connection()
    days = ("2026-07-01", "2026-07-02", "2026-07-03")
    actuals = {day: 20.0 + index * 2 for index, day in enumerate(days)}
    for day, value in actuals.items():
        _insert_truth(conn, day, value)
    conn.commit()
    systems = ("DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS")
    weights = {systems[0]: 0.5, systems[1]: 0.5}
    candidate = {
        "lead_bucket": "d0",
        "state": "PRODUCTION_NO_LIVE_OOS_PASS",
        "station_id": "TEST",
        "season_bin": "JJA",
        "run_cycle": D0_RUN_CYCLE,
        "run_cycle_by_system": {system: D0_RUN_CYCLE for system in systems},
        "selected_system_ids": list(systems),
        "system_transport_provider_ids": {system: "OPEN_METEO" for system in systems},
        "republisher_fallback_used": True,
        "system_evaluations": {
            system: {
                "train_start_date": days[0],
                "holdout_end_date": days[-1],
                "train_n": 2,
                "holdout_n": 1,
            }
            for system in systems
        },
        "independence_family_ids": ["DWD_ICON_EPS", "ECCC_GEM"],
        "system_weights": weights,
        "available_system_set_hash": content_sha256(sorted(systems)),
        "train_start_date": days[0],
        "train_end_date": days[1],
        "holdout_start_date": days[-1],
        "holdout_end_date": days[-1],
        "threshold_profile": "PRODUCTION_NO_LIVE_BOOTSTRAP_2_TRAIN_1_HISTORICAL_HOLDOUT",
        "min_train_days": 2,
        "min_holdout_days": 1,
    }
    cases = {}
    for system_index, system in enumerate(systems):
        cases[(system, "JJA", "d0", D0_RUN_CYCLE)] = [
            {
                "event_date": day,
                "actual_tmax_c": actuals[day],
                "member_set_sha256": content_sha256({"system": system, "day": day}),
                "aggregator_snapshot_at_utc": f"{day}T01:00:00Z",
                "values": [actuals[day] - 0.2 + system_index * 0.05, actuals[day] + 0.2 + system_index * 0.05],
                "member_native_run_init_utcs": list(member_run_inits),
            }
            for day in days
        ]
    # An unrelated bad scope must not affect the selected d0 candidate.
    cases[("UNSELECTED", "JJA", "d0", D0_RUN_CYCLE)] = [{
        "event_date": days[0],
        "values": [20.0, 20.0],
        "member_native_run_init_utcs": ["", ""],
    }]
    monkeypatch.setattr(builders, "build_native_ensemble_oos", lambda *_args, **_kwargs: {"cross_candidates": [candidate], "readiness": []})
    monkeypatch.setattr(builders, "_load_cases", lambda *_args, **_kwargs: cases)
    monkeypatch.setattr(builders, "_load_systems", lambda *_args, **_kwargs: {
        system: {"provider_id": f"provider-{system.lower()}"} for system in systems
    })

    packet, receipt = build_d0_global_ensemble_from_connection(
        conn,
        stations=["TEST"],
        as_of_utc="2026-07-10T00:00:00Z",
        generation=1,
        system_ids=systems,
    )

    assert packet is None
    assert receipt["state"] == "BLOCKED"
    status = receipt["station_statuses"][0]
    assert "D0_UPSTREAM_RUN_INIT_UNPROVEN" in status["blockers"]
    assert status["upstream_run_init_failures"][0]["reason"] == expected_reason


def test_d0_global_ensemble_uses_selected_real_run_init_not_republisher_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import bayes_basket_target.historical_scope_builders as builders

    conn = _connection()
    days = ("2026-07-01", "2026-07-02", "2026-07-03")
    actuals = {day: 20.0 + index * 2 for index, day in enumerate(days)}
    for day, value in actuals.items():
        _insert_truth(conn, day, value)
    conn.commit()
    systems = ("DWD_ICON_GLOBAL_EPS", "ECCC_GEM_GLOBAL_ENS")
    candidate = {
        "lead_bucket": "d0",
        "state": "PRODUCTION_NO_LIVE_OOS_PASS",
        "station_id": "TEST",
        "season_bin": "JJA",
        "run_cycle": D0_RUN_CYCLE,
        "run_cycle_by_system": {system: D0_RUN_CYCLE for system in systems},
        "selected_system_ids": list(systems),
        "system_transport_provider_ids": {system: "OPEN_METEO" for system in systems},
        "republisher_fallback_used": True,
        "system_evaluations": {
            system: {
                "train_start_date": days[0],
                "holdout_end_date": days[-1],
                "train_n": 2,
                "holdout_n": 1,
            }
            for system in systems
        },
        "independence_family_ids": ["DWD_ICON_EPS", "ECCC_GEM"],
        "system_weights": {systems[0]: 0.5, systems[1]: 0.5},
        "available_system_set_hash": content_sha256(sorted(systems)),
        "train_start_date": days[0],
        "train_end_date": days[1],
        "holdout_start_date": days[-1],
        "holdout_end_date": days[-1],
        "threshold_profile": "PRODUCTION_NO_LIVE_BOOTSTRAP_2_TRAIN_1_HISTORICAL_HOLDOUT",
        "min_train_days": 2,
        "min_holdout_days": 1,
    }
    cases = {
        (system, "JJA", "d0", D0_RUN_CYCLE): [
            {
                "event_date": day,
                "actual_tmax_c": actuals[day],
                "member_set_sha256": content_sha256({"system": system, "day": day}),
                "aggregator_snapshot_at_utc": f"{day}T01:00:00Z",
                "values": [actuals[day] - 0.2, actuals[day] + 0.2],
                "member_native_run_init_utcs": [f"{day}T00:00:00Z"] * 2,
            }
            for day in days
        ]
        for system in systems
    }
    cases[("UNSELECTED", "JJA", "d0", D0_RUN_CYCLE)] = [{
        "event_date": days[0],
        "values": [20.0, 20.0],
        "member_native_run_init_utcs": ["", ""],
    }]
    monkeypatch.setattr(builders, "build_native_ensemble_oos", lambda *_args, **_kwargs: {"cross_candidates": [candidate], "readiness": []})
    monkeypatch.setattr(builders, "_load_cases", lambda *_args, **_kwargs: cases)
    monkeypatch.setattr(builders, "_load_systems", lambda *_args, **_kwargs: {
        system: {"provider_id": f"provider-{system.lower()}"} for system in systems
    })

    packet, receipt = build_d0_global_ensemble_from_connection(
        conn,
        stations=["TEST"],
        as_of_utc="2026-07-10T00:00:00Z",
        generation=1,
        system_ids=systems,
    )

    assert receipt["state"] == "BUILT"
    assert packet is not None
    assert {fact["run_init_utc"] for fact in packet["model_facts"]} == {
        f"{day}T00:00:00Z" for day in days
    }
    assert all(
        fact["run_init_utc"] != fact["received_at_utc"]
        for fact in packet["model_facts"]
    )
    assert ingest_canonical_contract(conn, packet, commit=False)["state"] == "VALIDATED_NO_WRITE"


def test_registry_exposes_only_the_completed_d0_d2_scopes() -> None:
    assert builder_for("d0", "global_ensemble").state == "READY"
    assert builder_for("d2", "global_deterministic").state == "READY"
    assert builder_for("d0", "regional").state == "BLOCKED"
    assert builder_for("d2", "global_ensemble").state == "BLOCKED"
