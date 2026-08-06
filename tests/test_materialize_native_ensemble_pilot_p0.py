from __future__ import annotations

from datetime import date, timedelta
from types import SimpleNamespace

from bayes_basket_target.contracts import content_sha256
from bayes_basket_target.contracts import ContractError
from bayes_basket_target.native_ensemble_oos_contract import (
    COHORT_FACT_TYPE,
    EVENT_FACT_TYPE,
)
import scripts.materialize_native_ensemble_pilot_p0 as pilot

INPUT_SCHEMA = pilot.INPUT_SCHEMA
PILOT_STATIONS = pilot.PILOT_STATIONS
SYSTEMS = pilot.SYSTEMS


def _input_manifest() -> dict[str, object]:
    first = date(2026, 7, 2)
    products = {
        "NOAA_GEFS_05": ("GFS", "NOAA", "noaa_gefs_0p5", "NOAA"),
        "ECMWF_IFS_ENS_OPEN_PF50": (
            "ECMWF", "ECMWF", "ecmwf_ifs_ens_open_pf50", "ECMWF"
        ),
    }
    facts = []
    for station in PILOT_STATIONS:
        for system in SYSTEMS:
            model, provider, product, family = products[system]
            for ordinal in range(28):
                identity = {
                    "station_id": station,
                    "event_date": (first + timedelta(days=ordinal)).isoformat(),
                    "ensemble_system_id": system,
                    "settlement_timezone": "America/New_York",
                    "lead_bucket": "d2",
                    "run_cycle": "18Z",
                    "source_class": "native_official",
                    "model_id": model,
                    "model_family": "global_ensemble",
                    "provider_id": provider,
                    "provider_product_id": product,
                    "independence_family_id": family,
                }
                facts.append({
                    **identity,
                    "content_sha256": content_sha256([station, system, ordinal]),
                })
    identity = {
        "schema": INPUT_SCHEMA,
        "station_ids": list(PILOT_STATIONS),
        "settlement_timezone": "America/New_York",
        "start_date": "2026-07-02",
        "end_date": "2026-07-29",
        "event_input_facts": facts,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "orders_submitted": 0,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def test_reservation_and_access_are_truth_free_and_cover_three_pilot_stations(
    monkeypatch,
) -> None:
    # Adapter decoding is covered at its own boundary; this narrow test covers
    # that reservation does not open truth after inputs have passed it.
    monkeypatch.setattr(pilot, "validate_native_ensemble_oos_event_input", lambda _item: None)
    inputs = _input_manifest()
    reservations = pilot.build_reservations_manifest(
        inputs,
        registered_at_utc="2026-08-05T00:00:00Z",
        absence_basis_sha256="a" * 64,
    )
    assert reservations["opens_truth"] is False
    assert [item["station_id"] for item in reservations["reservations"]] == list(PILOT_STATIONS)
    assert all(
        forbidden not in str(reservations).lower()
        for forbidden in (
            "truth_content_sha256", "truth_id", "truth_revision_no",
            "canonical_truth_fact_content_sha256",
        )
    )
    assert all(
        len(item["component_input_commitments"]) == 2
        and len(item["train_event_dates"]) == 20
        and len(item["holdout_event_dates"]) == 8
        for item in reservations["reservations"]
    )

    access = pilot.build_access_manifest(
        reservations,
        holdout_unsealed_at_utc="2026-08-05T00:00:01Z",
        first_evaluated_at_utc="2026-08-05T00:00:02Z",
    )
    assert access["opens_truth"] is False
    assert len(access["accesses"]) == 3


def test_selection_phase_passes_only_two_typed_native_cohort_refs_per_station(
    monkeypatch,
) -> None:
    monkeypatch.setattr(pilot, "validate_native_ensemble_oos_event_input", lambda _item: None)
    reservations = pilot.build_reservations_manifest(
        _input_manifest(),
        registered_at_utc="2026-08-05T00:00:00Z",
        absence_basis_sha256="a" * 64,
    )
    access = pilot.build_access_manifest(
        reservations,
        holdout_unsealed_at_utc="2026-08-05T00:00:01Z",
        first_evaluated_at_utc="2026-08-05T00:00:02Z",
    )
    cohorts = {
            station: [
                SimpleNamespace(
                    content_sha256=content_sha256([station, system]),
                    raw={"evaluation_state": "OOS_PASS"},
                )
                for system in pilot.SYSTEMS
            ]
        for station in pilot.PILOT_STATIONS
    }
    monkeypatch.setattr(pilot, "_load_pilot_cohorts", lambda _conn: cohorts)
    calls: list[dict[str, object]] = []

    def _selection(_conn, **kwargs):
        refs = kwargs["component_cohort_refs"]
        assert len(refs) == 2
        assert {item["event_fact_type"] for item in refs} == {EVENT_FACT_TYPE}
        assert {item["cohort_fact_type"] for item in refs} == {COHORT_FACT_TYPE}
        calls.append(kwargs)
        return {
            "schema": "test.selection.v1",
            "content_sha256": content_sha256([len(calls)]),
        }

    monkeypatch.setattr(pilot, "build_model_oos_selection_contract", _selection)
    contracts = pilot.build_selection_contracts(
        object(), reservations=reservations, accesses=access,
        as_of_utc="2026-08-06T00:00:00Z",
    )
    assert sorted(contracts) == list(PILOT_STATIONS)
    assert len(calls) == 3


def test_selection_phase_passes_reserved_mask_unchanged_to_canonical_builder(
    monkeypatch,
) -> None:
    """The pilot cannot mutate a mask frozen before truth access."""

    monkeypatch.setattr(
        pilot, "validate_native_ensemble_oos_event_input", lambda _item: None
    )
    reservations = pilot.build_reservations_manifest(
        _input_manifest(),
        registered_at_utc="2026-08-05T00:00:00Z",
        absence_basis_sha256="c" * 64,
    )
    access = pilot.build_access_manifest(
        reservations,
        holdout_unsealed_at_utc="2026-08-05T00:00:01Z",
        first_evaluated_at_utc="2026-08-05T00:00:02Z",
    )
    cohorts = {
        station: [
            SimpleNamespace(
                content_sha256=content_sha256([station, system, "rejected"]),
                raw={"evaluation_state": "OOS_REJECTED"},
            )
            for system in pilot.SYSTEMS
        ]
        for station in pilot.PILOT_STATIONS
    }
    monkeypatch.setattr(pilot, "_load_pilot_cohorts", lambda _conn: cohorts)
    evaluated: list[str] = []

    def _selection(_conn, **kwargs):
        station = next(
            item["station_id"]
            for item in reservations["reservations"]
            if item["content_sha256"]
            == kwargs["pre_registered_mask_receipt"]["content_sha256"]
        )
        evaluated.append(station)
        return {
            "schema": "test.selection.v1",
            "content_sha256": content_sha256([station, "whole-mask"]),
        }

    monkeypatch.setattr(pilot, "build_model_oos_selection_contract", _selection)
    contracts = pilot.build_selection_contracts(
        object(), reservations=reservations, accesses=access,
        as_of_utc="2026-08-06T00:00:00Z",
    )
    assert sorted(contracts) == list(PILOT_STATIONS)
    assert sorted(evaluated) == list(PILOT_STATIONS)


def test_selection_phase_still_fails_closed_when_whole_reserved_mask_is_rejected(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        pilot, "validate_native_ensemble_oos_event_input", lambda _item: None
    )
    reservations = pilot.build_reservations_manifest(
        _input_manifest(),
        registered_at_utc="2026-08-05T00:00:00Z",
        absence_basis_sha256="d" * 64,
    )
    access = pilot.build_access_manifest(
        reservations,
        holdout_unsealed_at_utc="2026-08-05T00:00:01Z",
        first_evaluated_at_utc="2026-08-05T00:00:02Z",
    )
    cohorts = {
        station: [
            SimpleNamespace(
                content_sha256=content_sha256([station, system, "rejected-mask"]),
                raw={"evaluation_state": "OOS_REJECTED"},
            )
            for system in pilot.SYSTEMS
        ]
        for station in pilot.PILOT_STATIONS
    }
    monkeypatch.setattr(pilot, "_load_pilot_cohorts", lambda _conn: cohorts)

    def _reject(_conn, **_kwargs):
        raise ContractError("model_oos_selection_holdout_rejected")

    monkeypatch.setattr(pilot, "build_model_oos_selection_contract", _reject)
    assert pilot.build_selection_contracts(
        object(), reservations=reservations, accesses=access,
        as_of_utc="2026-08-06T00:00:00Z",
    ) == {}


def test_selection_phase_fails_closed_when_component_calibration_is_rejected(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        pilot, "validate_native_ensemble_oos_event_input", lambda _item: None
    )
    reservations = pilot.build_reservations_manifest(
        _input_manifest(),
        registered_at_utc="2026-08-05T00:00:00Z",
        absence_basis_sha256="e" * 64,
    )
    access = pilot.build_access_manifest(
        reservations,
        holdout_unsealed_at_utc="2026-08-05T00:00:01Z",
        first_evaluated_at_utc="2026-08-05T00:00:02Z",
    )
    cohorts = {
        station: [
            SimpleNamespace(
                content_sha256=content_sha256([station, system, "component-rejected"]),
                raw={"evaluation_state": "OOS_REJECTED"},
            )
            for system in pilot.SYSTEMS
        ]
        for station in pilot.PILOT_STATIONS
    }
    monkeypatch.setattr(pilot, "_load_pilot_cohorts", lambda _conn: cohorts)

    def _reject(_conn, **_kwargs):
        raise ContractError("model_oos_selection_component_calibration_rejected")

    monkeypatch.setattr(pilot, "build_model_oos_selection_contract", _reject)
    assert pilot.build_selection_contracts(
        object(), reservations=reservations, accesses=access,
        as_of_utc="2026-08-06T00:00:00Z",
    ) == {}


def test_follow_on_single_station_scope_uses_same_20_plus_8_contract(
    monkeypatch,
) -> None:
    monkeypatch.setattr(pilot, "PILOT_STATIONS", ("CYYZ",))
    monkeypatch.setattr(
        pilot, "validate_native_ensemble_oos_event_input", lambda _item: None
    )
    inputs = _input_manifest()
    inputs["station_ids"] = ["CYYZ"]
    inputs["event_input_facts"] = [
        item for item in inputs["event_input_facts"]
        if item["station_id"] == "CYYZ"
    ]
    identity = {
        key: value for key, value in inputs.items()
        if key != "content_sha256"
    }
    inputs["content_sha256"] = content_sha256(identity)
    reservations = pilot.build_reservations_manifest(
        inputs,
        registered_at_utc="2026-08-05T00:00:00Z",
        absence_basis_sha256="b" * 64,
    )
    access = pilot.build_access_manifest(
        reservations,
        holdout_unsealed_at_utc="2026-08-05T00:00:01Z",
        first_evaluated_at_utc="2026-08-05T00:00:02Z",
    )
    assert [item["station_id"] for item in reservations["reservations"]] == ["CYYZ"]
    assert len(access["accesses"]) == 1
