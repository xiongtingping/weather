#!/usr/bin/env python3
"""Stage a bounded native-ensemble P0 scope without touching live services.

The default pilot is CYYZ/RJTT/RKSI; ``--station-id`` may select a smaller
follow-on scope using the same ET d2/18Z, 2026-07-02..2026-07-29, NOAA GEFS +
ECMWF PF50 contract. Inputs and reservation are deliberately independent
of the truth database.  Only ``evaluate`` opens closed AWC truth, and only an
explicit ``--commit`` invokes canonical ingress.  No phase publishes PR04,
starts a service, releases live pause, or calls an order gateway.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.canonical_contract import CanonicalTruthFact  # noqa: E402
from bayes_basket_target.canonical_ingress import ingest_canonical_contract  # noqa: E402
from bayes_basket_target.contracts import ContractError, content_sha256  # noqa: E402
from bayes_basket_target.database import DEFAULT_TARGET_DB  # noqa: E402
from bayes_basket_target.model_oos_selection import (  # noqa: E402
    build_historical_holdout_access_receipt,
    build_historical_holdout_reservation,
    build_model_oos_selection_contract,
    validate_historical_holdout_access,
    validate_historical_holdout_reservation,
)
from bayes_basket_target.native_ensemble_oos_builder import (  # noqa: E402
    build_native_ensemble_contract_event_identity,
    build_native_ensemble_oos_event_inputs,
    validate_native_ensemble_oos_event_input,
)
from bayes_basket_target.native_ensemble_oos_contract import (  # noqa: E402
    AUTHORITY_SCOPE,
    CALIBRATION_METHOD_VERSION,
    COHORT_FACT_TYPE,
    EVENT_FACT_TYPE,
    CanonicalNativeEnsembleOOSContract,
    CanonicalNativeEnsembleOOSCohortFact,
    CanonicalNativeEnsembleOOSEventFact,
    derive_native_ensemble_cohort_statistics,
)
from bayes_basket_target.p0_activation_root import (  # noqa: E402
    build_p0_activation_root_contract,
)
from bayes_basket_target.p0_calibrated_distribution import (  # noqa: E402
    build_p0_calibrated_distribution_contract,
)
from bayes_basket_target.resource_bounds import (  # noqa: E402
    MAX_JSON_INPUT_BYTES,
    load_bounded_json,
    write_bounded_json_if_changed,
)


PILOT_STATIONS = ("CYYZ", "RJTT", "RKSI")
PILOT_START = "2026-07-02"
PILOT_END = "2026-07-29"
SETTLEMENT_TIMEZONE = "America/New_York"
SYSTEMS = ("NOAA_GEFS_05", "ECMWF_IFS_ENS_OPEN_PF50")
INPUT_SCHEMA = "native_ensemble_pilot_p0_inputs_v1"
RESERVATION_SCHEMA = "native_ensemble_pilot_p0_reservations_v1"
ACCESS_SCHEMA = "native_ensemble_pilot_p0_access_v1"
EVALUATE_SCHEMA = "native_ensemble_pilot_p0_evaluate_v1"
SELECTION_SCHEMA = "native_ensemble_pilot_p0_selection_v1"
DISTRIBUTION_SCHEMA = "native_ensemble_pilot_p0_distribution_v1"
ACTIVATION_SCHEMA = "native_ensemble_pilot_p0_activation_v1"
PILOT_MAX_JSON_NODES = 2_000_000


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"native_pilot_p0_{label}_missing")
    return result


def _utc(value: Any, label: str) -> str:
    raw = _text(value, label)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"native_pilot_p0_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"native_pilot_p0_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _seal(identity: Mapping[str, Any]) -> dict[str, Any]:
    value = dict(identity)
    return {**value, "content_sha256": content_sha256(value)}


def _load(path: Path) -> Any:
    return load_bounded_json(
        path, limit=MAX_JSON_INPUT_BYTES, max_nodes=PILOT_MAX_JSON_NODES
    )


def _dates() -> list[str]:
    first = date.fromisoformat(PILOT_START)
    return [
        (first + timedelta(days=index)).isoformat()
        for index in range((date.fromisoformat(PILOT_END) - first).days + 1)
    ]


def _load_packet(path: Path) -> Mapping[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    if not resolved.is_file() or resolved.is_symlink():
        raise ContractError("native_pilot_p0_packet_path_invalid")
    raw = _load(resolved)
    if not isinstance(raw, Mapping):
        raise ContractError("native_pilot_p0_packet_mapping_required")
    if raw.get("content_sha256") != content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    ):
        raise ContractError("native_pilot_p0_packet_content_sha256_mismatch")
    return raw


def _packet_paths(directory: Path, prefix: str) -> list[Path]:
    root = directory.expanduser().resolve(strict=True)
    paths: list[Path] = []
    for event_date in _dates():
        stamp = event_date.replace("-", "")
        candidates = [root / f"{prefix}_{stamp}_ET.json"]
        if prefix == "NOAA_GEFS":
            candidates.append(
                root / f"GEFS_45station_{stamp}_official_d2.json"
            )
        elif prefix == "ECMWF_PF50":
            candidates.append(
                root / f"ECMWF_PF50_45station_{stamp}_official_d2.json"
            )
        selected = next((path for path in candidates if path.is_file()), None)
        if selected is None:
            raise ContractError("native_pilot_p0_packet_count_invalid")
        paths.append(selected)
    return paths


def _validate_input_manifest(raw: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "schema", "station_ids", "settlement_timezone", "start_date", "end_date",
        "event_input_facts", "writes_database", "publishes_pr04_pointer",
        "starts_services", "releases_live_pause", "orders_submitted", "content_sha256",
    }
    if set(raw) != expected or raw.get("schema") != INPUT_SCHEMA:
        raise ContractError("native_pilot_p0_inputs_fields_invalid")
    if (
        raw.get("station_ids") != list(PILOT_STATIONS)
        or raw.get("settlement_timezone") != SETTLEMENT_TIMEZONE
        or raw.get("start_date") != PILOT_START
        or raw.get("end_date") != PILOT_END
        or raw.get("writes_database") is not False
        or raw.get("publishes_pr04_pointer") is not False
        or raw.get("starts_services") is not False
        or raw.get("releases_live_pause") is not False
        or raw.get("orders_submitted") != 0
    ):
        raise ContractError("native_pilot_p0_inputs_scope_invalid")
    facts = raw.get("event_input_facts")
    if not isinstance(facts, list) or len(facts) != 56 * len(PILOT_STATIONS):
        raise ContractError("native_pilot_p0_inputs_count_invalid")
    seen: set[tuple[str, str, str]] = set()
    parsed: list[dict[str, Any]] = []
    for item in facts:
        if not isinstance(item, Mapping):
            raise ContractError("native_pilot_p0_input_mapping_required")
        validate_native_ensemble_oos_event_input(item)
        required = {
            "station_id", "event_date", "ensemble_system_id", "content_sha256",
            "settlement_timezone", "lead_bucket", "run_cycle", "source_class",
        }
        if not required <= set(item):
            raise ContractError("native_pilot_p0_input_shape_invalid")
        station, event_date, system = (
            _text(item["station_id"], "input_station").upper(),
            _text(item["event_date"], "input_event_date"),
            _text(item["ensemble_system_id"], "input_system"),
        )
        if (
            station not in PILOT_STATIONS or event_date not in _dates()
            or system not in SYSTEMS or item.get("settlement_timezone") != SETTLEMENT_TIMEZONE
            or item.get("lead_bucket") != "d2" or item.get("run_cycle") != "18Z"
            or item.get("source_class") != "native_official"
        ):
            raise ContractError("native_pilot_p0_input_scope_invalid")
        key = (station, event_date, system)
        if key in seen:
            raise ContractError("native_pilot_p0_input_duplicate")
        seen.add(key)
        parsed.append(dict(item))
    if raw.get("content_sha256") != content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    ):
        raise ContractError("native_pilot_p0_inputs_content_sha256_mismatch")
    return {**dict(raw), "event_input_facts": parsed}


def build_inputs_manifest(packet_paths: Sequence[Path]) -> dict[str, Any]:
    """Capture/normalize only.  This function has no truth or DB dependency."""

    facts: list[dict[str, Any]] = []
    for path in packet_paths:
        packet = _load_packet(path)
        facts.extend(
            item
            for item in build_native_ensemble_oos_event_inputs(
                packet,
                source_packet_ref=path.resolve(),
                station_ids=PILOT_STATIONS,
            )
            if str(item.get("station_id") or "").upper() in PILOT_STATIONS
        )
    facts.sort(key=lambda item: (
        str(item["station_id"]), str(item["ensemble_system_id"]), str(item["event_date"])
    ))
    identity = {
        "schema": INPUT_SCHEMA,
        "station_ids": list(PILOT_STATIONS),
        "settlement_timezone": SETTLEMENT_TIMEZONE,
        "start_date": PILOT_START,
        "end_date": PILOT_END,
        "event_input_facts": facts,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "orders_submitted": 0,
    }
    return _validate_input_manifest(_seal(identity))


def build_reservations_manifest(
    inputs: Mapping[str, Any], *, registered_at_utc: str, absence_basis_sha256: str,
) -> dict[str, Any]:
    """Reserve exact 20+8 masks using only adapter input hashes, never truth."""

    parsed = _validate_input_manifest(inputs)
    grouped: dict[tuple[str, str], list[Mapping[str, Any]]] = defaultdict(list)
    for item in parsed["event_input_facts"]:
        grouped[(str(item["station_id"]), str(item["ensemble_system_id"]))].append(item)
    reservations = []
    for station in PILOT_STATIONS:
        components = []
        for system in SYSTEMS:
            rows = sorted(grouped[(station, system)], key=lambda item: str(item["event_date"]))
            if [item["event_date"] for item in rows] != _dates():
                raise ContractError("native_pilot_p0_reservation_input_dates_invalid")
            first = rows[0]
            identity = {key: first[key] for key in (
                "model_id", "ensemble_system_id", "model_family", "provider_id",
                "provider_product_id", "independence_family_id", "source_class",
            )}
            components.append({
                "component_identity": identity,
                "event_fact_type": EVENT_FACT_TYPE,
                "event_input_commitments": [{
                    "event_date": item["event_date"],
                    "adapter_event_input_content_sha256": item["content_sha256"],
                } for item in rows],
            })
        reservations.append(build_historical_holdout_reservation(
            station_id=station,
            settlement_timezone=SETTLEMENT_TIMEZONE,
            component_input_commitments=components,
            registered_at_utc=registered_at_utc,
            prior_evaluation_absence_basis_sha256=absence_basis_sha256,
        ))
    identity = {
        "schema": RESERVATION_SCHEMA,
        "input_manifest_content_sha256": parsed["content_sha256"],
        "reservations": reservations,
        "writes_database": False,
        "opens_truth": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "orders_submitted": 0,
    }
    return _seal(identity)


def _validate_reservations_manifest(raw: Mapping[str, Any]) -> list[dict[str, Any]]:
    if raw.get("schema") != RESERVATION_SCHEMA or raw.get("opens_truth") is not False:
        raise ContractError("native_pilot_p0_reservations_schema_invalid")
    reservations = raw.get("reservations")
    if not isinstance(reservations, list) or len(reservations) != len(PILOT_STATIONS):
        raise ContractError("native_pilot_p0_reservations_count_invalid")
    result = [validate_historical_holdout_reservation(item) for item in reservations]
    if [item["station_id"] for item in result] != list(PILOT_STATIONS):
        raise ContractError("native_pilot_p0_reservations_scope_invalid")
    if raw.get("content_sha256") != content_sha256(
        {key: value for key, value in raw.items() if key != "content_sha256"}
    ):
        raise ContractError("native_pilot_p0_reservations_content_sha256_mismatch")
    return result


def build_access_manifest(
    reservations: Mapping[str, Any], *, holdout_unsealed_at_utc: str,
    first_evaluated_at_utc: str,
) -> dict[str, Any]:
    rows = _validate_reservations_manifest(reservations)
    accesses = [build_historical_holdout_access_receipt(
        reservation=item,
        holdout_unsealed_at_utc=holdout_unsealed_at_utc,
        first_evaluated_at_utc=first_evaluated_at_utc,
        evaluator_id="native_ensemble_pilot_p0.evaluate.v1",
    ) for item in rows]
    return _seal({
        "schema": ACCESS_SCHEMA,
        "reservation_manifest_content_sha256": reservations["content_sha256"],
        "accesses": accesses,
        "opens_truth": False,
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "orders_submitted": 0,
    })


def _truth_for(conn: sqlite3.Connection, *, station: str, event_date: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type='truth' AND scope_key LIKE ?",
        (f"{station}|{event_date}|%",),
    ).fetchall()
    candidates: list[CanonicalTruthFact] = []
    for (fact_json,) in rows:
        raw = json.loads(str(fact_json))
        truth = CanonicalTruthFact.from_mapping(raw)
        if (
            truth.station_id == station and truth.event_date == event_date
            and truth.closed_day and truth.source_lane == "AWC"
            and truth.settlement_timezone == SETTLEMENT_TIMEZONE
            and truth.authority_scope == AUTHORITY_SCOPE
        ):
            candidates.append(truth)
    if len(candidates) != 1:
        raise ContractError("native_pilot_p0_exact_awc_truth_not_unique")
    truth = candidates[0]
    return {
        "station_id": station,
        "event_date": event_date,
        "forecast_decision_at_utc": "",
        "canonical_truth_fact_content_sha256": truth.content_sha256,
        "truth_id": truth.truth_id,
        "truth_revision_no": truth.truth_revision_no,
        "truth_content_sha256": truth.truth_content_sha256,
        "truth_value_c": truth.truth_value,
        "station_day_end_utc": truth.station_day_end_utc,
        "truth_canonical_committed_at_utc": truth.canonical_committed_at_utc,
    }


def _cohort(events: Sequence[CanonicalNativeEnsembleOOSEventFact], *, as_of_utc: str) -> dict[str, Any]:
    parameters, train_metrics, holdout_metrics, baseline_metrics, evaluation = (
        derive_native_ensemble_cohort_statistics(events)
    )
    base = events[0].raw
    train = [item.content_sha256 for item in events[:20]]
    holdout = [item.content_sha256 for item in events[20:]]
    vector = [
        *({"partition": "train", "event_content_sha256": item} for item in train),
        *({"partition": "holdout", "event_content_sha256": item} for item in holdout),
    ]
    identity = {
        **{key: base[key] for key in (
            "station_id", "model_id", "ensemble_system_id", "model_family",
            "provider_id", "provider_product_id", "independence_family_id",
            "source_class", "authority_scope", "settlement_timezone",
            "lead_bucket", "lead_bucket_scope", "run_cycle", "run_cycle_scope",
            "event_oos_protocol_version",
        )},
        "season_bin": "ALL_YEAR",
        "calibration_method_version": CALIBRATION_METHOD_VERSION,
        "cohort_as_of_utc": _utc(as_of_utc, "cohort_asof"),
        "train_event_content_sha256s": train,
        "holdout_event_content_sha256s": holdout,
        "train_event_set_sha256": content_sha256(train),
        "holdout_event_set_sha256": content_sha256(holdout),
        "cohort_event_vector_sha256": content_sha256(vector),
        "calibration_parameters": parameters,
        "selection_train_metrics": train_metrics,
        "holdout_metrics": holdout_metrics,
        "baseline_holdout_metrics": baseline_metrics,
        "evaluation_state": evaluation,
        "state": "MATERIALIZED_UNPUBLISHED",
    }
    return _seal(identity)


def build_evaluate_contract(
    conn: sqlite3.Connection, *, inputs: Mapping[str, Any], reservations: Mapping[str, Any],
    accesses: Mapping[str, Any], as_of_utc: str,
) -> dict[str, Any]:
    """Open only exact closed ET AWC truth after reservation + access receipts."""

    parsed = _validate_input_manifest(inputs)
    reserved = _validate_reservations_manifest(reservations)
    access_rows = accesses.get("accesses") if isinstance(accesses, Mapping) else None
    if (
        not isinstance(access_rows, list) or len(access_rows) != len(PILOT_STATIONS)
        or accesses.get("schema") != ACCESS_SCHEMA
        or accesses.get("reservation_manifest_content_sha256") != reservations.get("content_sha256")
    ):
        raise ContractError("native_pilot_p0_access_manifest_invalid")
    access_by_reservation = {
        validate_historical_holdout_access(item)["reservation_content_sha256"]:
        validate_historical_holdout_access(item)
        for item in access_rows
    }
    if set(access_by_reservation) != {item["content_sha256"] for item in reserved}:
        raise ContractError("native_pilot_p0_access_reservation_mismatch")
    event_facts: list[dict[str, Any]] = []
    for item in parsed["event_input_facts"]:
        truth = _truth_for(
            conn, station=str(item["station_id"]), event_date=str(item["event_date"])
        )
        truth["forecast_decision_at_utc"] = str(item["official_object_available_by_utc"])
        event_facts.append(build_native_ensemble_contract_event_identity(item, truth))
    event_facts.sort(key=lambda item: (
        item["station_id"], item["ensemble_system_id"], item["event_date"]
    ))
    grouped: dict[tuple[str, str], list[CanonicalNativeEnsembleOOSEventFact]] = defaultdict(list)
    for item in event_facts:
        grouped[(str(item["station_id"]), str(item["ensemble_system_id"]))].append(
            CanonicalNativeEnsembleOOSEventFact.from_mapping(item)
        )
    cohorts = [_cohort(sorted(rows, key=lambda item: str(item.raw["event_date"])), as_of_utc=as_of_utc)
               for _key, rows in sorted(grouped.items())]
    identity = {
        "schema": "bayes_basket_target_native_ensemble_oos_contract_v1",
        "as_of_utc": _utc(as_of_utc, "evaluate_asof"),
        "builder_id": "native_ensemble_pilot_p0.evaluate.v1",
        "event_facts": event_facts,
        "cohort_facts": cohorts,
    }
    contract = _seal(identity)
    CanonicalNativeEnsembleOOSContract.from_mapping(contract)
    return _seal({
        "schema": EVALUATE_SCHEMA,
        "input_manifest_content_sha256": parsed["content_sha256"],
        "reservation_manifest_content_sha256": reservations["content_sha256"],
        "access_manifest_content_sha256": accesses["content_sha256"],
        "native_oos_contract": contract,
        "event_count": len(event_facts),
        "cohort_count": len(cohorts),
        "writes_database": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "orders_submitted": 0,
    })


def _load_pilot_cohorts(
    conn: sqlite3.Connection,
) -> dict[str, list[CanonicalNativeEnsembleOOSCohortFact]]:
    """Read exactly the six already-ingressed native pilot cohorts."""

    rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type=?",
        (COHORT_FACT_TYPE,),
    ).fetchall()
    grouped: dict[str, list[CanonicalNativeEnsembleOOSCohortFact]] = defaultdict(list)
    for (fact_json,) in rows:
        raw = json.loads(str(fact_json))
        cohort = CanonicalNativeEnsembleOOSCohortFact.from_mapping(raw)
        if (
            cohort.raw["station_id"] in PILOT_STATIONS
            and cohort.raw["ensemble_system_id"] in SYSTEMS
            and cohort.raw["settlement_timezone"] == SETTLEMENT_TIMEZONE
            and cohort.raw["lead_bucket"] == "d2"
            and cohort.raw["run_cycle"] == "18Z"
            and cohort.raw["authority_scope"] == AUTHORITY_SCOPE
        ):
            grouped[str(cohort.raw["station_id"])].append(cohort)
    result: dict[str, list[CanonicalNativeEnsembleOOSCohortFact]] = {}
    for station in PILOT_STATIONS:
        candidates = grouped.get(station, [])
        systems = [str(item.raw["ensemble_system_id"]) for item in candidates]
        if len(candidates) != 2 or sorted(systems) != sorted(SYSTEMS):
            raise ContractError("native_pilot_p0_submitted_cohorts_not_exact")
        result[station] = sorted(
            candidates, key=lambda item: str(item.raw["ensemble_system_id"])
        )
    return result


def _load_fact_sha(
    conn: sqlite3.Connection, *, fact_type: str, station: str,
) -> str:
    rows = conn.execute(
        "SELECT content_sha256 FROM canonical_ssot_fact WHERE fact_type=? AND scope_key LIKE ?",
        (fact_type, f"{station}|America/New_York|d2|%"),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError("native_pilot_p0_parent_fact_not_unique")
    return str(rows[0][0])


def _access_by_station(
    reservations: Mapping[str, Any], accesses: Mapping[str, Any],
) -> dict[str, tuple[dict[str, Any], dict[str, Any]]]:
    reserved = _validate_reservations_manifest(reservations)
    raw_accesses = accesses.get("accesses") if isinstance(accesses, Mapping) else None
    if (
        accesses.get("schema") != ACCESS_SCHEMA
        or accesses.get("reservation_manifest_content_sha256") != reservations.get("content_sha256")
        or not isinstance(raw_accesses, list)
    ):
        raise ContractError("native_pilot_p0_access_manifest_invalid")
    parsed_access = [validate_historical_holdout_access(item) for item in raw_accesses]
    access_by_hash = {item["reservation_content_sha256"]: item for item in parsed_access}
    result: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for reservation in reserved:
        access = access_by_hash.get(reservation["content_sha256"])
        if access is None:
            raise ContractError("native_pilot_p0_access_reservation_mismatch")
        result[reservation["station_id"]] = (reservation, access)
    if sorted(result) != list(PILOT_STATIONS):
        raise ContractError("native_pilot_p0_access_scope_invalid")
    return result


def build_selection_contracts(
    conn: sqlite3.Connection, *, reservations: Mapping[str, Any],
    accesses: Mapping[str, Any], as_of_utc: str,
) -> dict[str, dict[str, Any]]:
    """Build one typed formal-native selection contract per pilot station."""

    cohorts = _load_pilot_cohorts(conn)
    masks = _access_by_station(reservations, accesses)
    result: dict[str, dict[str, Any]] = {}
    for station in PILOT_STATIONS:
        reservation, access = masks[station]
        refs = [{
            "event_fact_type": EVENT_FACT_TYPE,
            "cohort_fact_type": COHORT_FACT_TYPE,
            "cohort_content_sha256": item.content_sha256,
        } for item in cohorts[station]]
        try:
            # The exact mask is handed to the canonical builder unchanged.
            # The builder accepts or rejects that whole mask and requires
            # every formal-native component to pass its independent OOS gate.
            result[station] = build_model_oos_selection_contract(
                conn,
                component_cohort_refs=refs,
                pre_registered_mask_receipt=reservation,
                holdout_label_access_receipt=access,
                as_of_utc=as_of_utc,
            )
        except ContractError as exc:
            if str(exc) not in {
                "model_oos_selection_component_calibration_rejected",
                "model_oos_selection_calibration_holdout_rejected",
                "model_oos_selection_holdout_rejected",
            }:
                raise
    return result


def _stations_with_fact(
    conn: sqlite3.Connection, *, fact_type: str,
) -> tuple[str, ...]:
    stations = []
    for station in PILOT_STATIONS:
        rows = conn.execute(
            "SELECT 1 FROM canonical_ssot_fact "
            "WHERE fact_type=? AND scope_key LIKE ?",
            (fact_type, f"{station}|America/New_York|d2|%"),
        ).fetchall()
        if len(rows) > 1:
            raise ContractError("native_pilot_p0_parent_fact_not_unique")
        if rows:
            stations.append(station)
    if not stations:
        raise ContractError("native_pilot_p0_no_eligible_station")
    return tuple(stations)


def build_distribution_contracts(
    conn: sqlite3.Connection, *, as_of_utc: str,
) -> dict[str, dict[str, Any]]:
    """Build one distribution only from an already committed typed selection."""

    return {
        station: build_p0_calibrated_distribution_contract(
            conn,
            selection_content_sha256=_load_fact_sha(
                conn, fact_type="p0_model_family_selection", station=station,
            ),
            as_of_utc=as_of_utc,
        )
        for station in _stations_with_fact(
            conn, fact_type="p0_model_family_selection"
        )
    }


def build_activation_contracts(
    conn: sqlite3.Connection, *, as_of_utc: str,
) -> dict[str, dict[str, Any]]:
    """Build generation-1 roots only; never a forward P0 snapshot."""

    return {
        station: build_p0_activation_root_contract(
            conn,
            selection_content_sha256=_load_fact_sha(
                conn, fact_type="p0_model_family_selection", station=station,
            ),
            calibrated_distribution_content_sha256=_load_fact_sha(
                conn, fact_type="p0_calibrated_distribution", station=station,
            ),
            activation_generation=1,
            predecessor_activation_root_content_sha256=None,
            as_of_utc=as_of_utc,
        )
        for station in _stations_with_fact(
            conn, fact_type="p0_calibrated_distribution"
        )
    }


def _phase_manifest(
    *, schema: str, contracts: Mapping[str, Mapping[str, Any]], parent_sha256: str,
    contract_directory: str,
) -> dict[str, Any]:
    contract_stations = tuple(sorted(contracts))
    if (
        not contract_stations
        or any(station not in PILOT_STATIONS for station in contract_stations)
    ):
        raise ContractError("native_pilot_p0_phase_station_scope_invalid")
    return _seal({
        "schema": schema,
        "parent_content_sha256": parent_sha256,
        "station_contracts": [{
            "station_id": station,
            "contract_file": f"{contract_directory}/{station}.json",
            "contract_content_sha256": contracts[station]["content_sha256"],
        } for station in contract_stations],
        "writes_database": False,
        "builds_forward_snapshot": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "orders_submitted": 0,
    })


def _write_station_contracts(
    directory: Path, contracts: Mapping[str, Mapping[str, Any]],
) -> None:
    for station in sorted(contracts):
        _write(directory / f"{station}.json", contracts[station])


def _write_commit_receipts(
    directory: Path, contracts: Mapping[str, Mapping[str, Any]], *,
    database: Path, ingested_at_utc: str,
) -> None:
    """Commit exactly the passed sealed contracts and store receipts separately."""

    for station in sorted(contracts):
        with sqlite3.connect(database) as conn:
            receipt = ingest_canonical_contract(
                conn, contracts[station], commit=True,
                ingested_at_utc=ingested_at_utc,
            )
        _write(directory / f"{station}_ingress_commit.json", receipt)


def _write(path: Path, value: Mapping[str, Any]) -> None:
    resolved = path.expanduser().resolve()
    if resolved.exists():
        existing = _load(resolved)
        if existing != dict(value):
            raise ContractError("native_pilot_p0_sealed_output_rewrite_forbidden")
        return
    # The sealed 168-event pilot manifest preserves every member×step source
    # receipt and is intentionally larger than the 16 MiB generic artifact
    # default.  Keep it within the existing 64 MiB bounded-input contract;
    # this does not widen any live snapshot or raw-message limit.
    write_bounded_json_if_changed(
        resolved,
        dict(value),
        limit=MAX_JSON_INPUT_BYTES,
        max_nodes=PILOT_MAX_JSON_NODES,
    )


def main() -> int:
    global PILOT_STATIONS
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("inputs", "reserve", "access", "evaluate", "selection", "distribution", "activation"),
        required=True,
    )
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--database", type=Path, default=DEFAULT_TARGET_DB)
    parser.add_argument(
        "--station-id",
        action="append",
        help="bounded station scope; repeat for multiple stations",
    )
    parser.add_argument("--gefs-dir", type=Path)
    parser.add_argument("--ecmwf-dir", type=Path)
    parser.add_argument("--registered-at-utc")
    parser.add_argument("--absence-basis-sha256")
    parser.add_argument("--holdout-unsealed-at-utc")
    parser.add_argument("--first-evaluated-at-utc")
    parser.add_argument("--as-of-utc")
    parser.add_argument("--commit", action="store_true")
    args = parser.parse_args()
    if args.station_id:
        requested = tuple(sorted({
            str(value).strip().upper()
            for value in args.station_id
            if str(value).strip()
        }))
        if not requested or len(requested) != len(args.station_id):
            raise ContractError("native_pilot_p0_station_scope_invalid")
        PILOT_STATIONS = requested
    output = args.output_dir.expanduser().resolve()
    inputs_path, reservation_path = output / "inputs.json", output / "reservations.json"
    access_path, evaluate_path = output / "access.json", output / "evaluate.json"
    selection_path = output / "selection_manifest.json"
    distribution_path = output / "distribution_manifest.json"
    activation_path = output / "activation_manifest.json"
    if args.phase == "inputs":
        if args.gefs_dir is None or args.ecmwf_dir is None:
            raise ContractError("native_pilot_p0_packet_directories_required")
        result = build_inputs_manifest([
            *_packet_paths(args.gefs_dir, "NOAA_GEFS"),
            *_packet_paths(args.ecmwf_dir, "ECMWF_PF50"),
        ])
        _write(inputs_path, result)
    elif args.phase == "reserve":
        if not args.registered_at_utc or not args.absence_basis_sha256:
            raise ContractError("native_pilot_p0_reservation_clock_basis_required")
        result = build_reservations_manifest(
            _load(inputs_path),
            registered_at_utc=args.registered_at_utc,
            absence_basis_sha256=args.absence_basis_sha256,
        )
        _write(reservation_path, result)
    elif args.phase == "access":
        if not args.holdout_unsealed_at_utc or not args.first_evaluated_at_utc:
            raise ContractError("native_pilot_p0_access_clock_required")
        result = build_access_manifest(
            _load(reservation_path),
            holdout_unsealed_at_utc=args.holdout_unsealed_at_utc,
            first_evaluated_at_utc=args.first_evaluated_at_utc,
        )
        _write(access_path, result)
    elif args.phase == "evaluate":
        if not args.as_of_utc:
            raise ContractError("native_pilot_p0_evaluate_asof_required")
        database = args.database.expanduser().resolve(strict=True)
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
            conn.execute("PRAGMA query_only=ON")
            result = build_evaluate_contract(
                conn,
                inputs=_load(inputs_path),
                reservations=_load(reservation_path),
                accesses=_load(access_path),
                as_of_utc=args.as_of_utc,
            )
        _write(evaluate_path, result)
        if args.commit:
            with sqlite3.connect(database) as conn:
                receipt = ingest_canonical_contract(
                    conn, result["native_oos_contract"], commit=True,
                    ingested_at_utc=_utc(args.as_of_utc, "commit_asof"),
                )
            # Keep the evaluated contract content-addressed and immutable.
            # A commit is a later phase with its own receipt; appending that
            # receipt to ``evaluate.json`` would invalidate its sealed hash.
            _write(output / "evaluate_ingress_commit.json", receipt)
    else:
        if not args.as_of_utc:
            raise ContractError("native_pilot_p0_materialization_asof_required")
        database = args.database.expanduser().resolve(strict=True)
        if args.phase == "selection":
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
                conn.execute("PRAGMA query_only=ON")
                contracts = build_selection_contracts(
                    conn,
                    reservations=_load(reservation_path),
                    accesses=_load(access_path),
                    as_of_utc=args.as_of_utc,
                )
            parent_sha = content_sha256({
                "reservations": _load(reservation_path)["content_sha256"],
                "access": _load(access_path)["content_sha256"],
            })
            manifest = _phase_manifest(
                schema=SELECTION_SCHEMA, contracts=contracts, parent_sha256=parent_sha,
                contract_directory="selection_contracts",
            )
            _write_station_contracts(output / "selection_contracts", contracts)
            _write(selection_path, manifest)
            if args.commit:
                _write_commit_receipts(
                    output / "selection_ingress_receipts", contracts,
                    database=database, ingested_at_utc=_utc(args.as_of_utc, "selection_commit_asof"),
                )
        elif args.phase == "distribution":
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
                conn.execute("PRAGMA query_only=ON")
                contracts = build_distribution_contracts(conn, as_of_utc=args.as_of_utc)
            selection_manifest = _load(selection_path)
            manifest = _phase_manifest(
                schema=DISTRIBUTION_SCHEMA,
                contracts=contracts,
                parent_sha256=str(selection_manifest["content_sha256"]),
                contract_directory="distribution_contracts",
            )
            _write_station_contracts(output / "distribution_contracts", contracts)
            _write(distribution_path, manifest)
            if args.commit:
                _write_commit_receipts(
                    output / "distribution_ingress_receipts", contracts,
                    database=database, ingested_at_utc=_utc(args.as_of_utc, "distribution_commit_asof"),
                )
        else:
            with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as conn:
                conn.execute("PRAGMA query_only=ON")
                contracts = build_activation_contracts(conn, as_of_utc=args.as_of_utc)
            distribution_manifest = _load(distribution_path)
            manifest = _phase_manifest(
                schema=ACTIVATION_SCHEMA,
                contracts=contracts,
                parent_sha256=str(distribution_manifest["content_sha256"]),
                contract_directory="activation_contracts",
            )
            _write_station_contracts(output / "activation_contracts", contracts)
            _write(activation_path, manifest)
            if args.commit:
                _write_commit_receipts(
                    output / "activation_ingress_receipts", contracts,
                    database=database, ingested_at_utc=_utc(args.as_of_utc, "activation_commit_asof"),
                )
    print(json.dumps({
        "phase": args.phase, "output": str(output), "commit": args.commit,
        "publishes_pr04_pointer": False, "starts_services": False,
        "releases_live_pause": False, "orders_submitted": 0,
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
