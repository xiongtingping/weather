"""PR13 canonical 180-scope P1 readiness basis.

The only expected scope authority is the frozen 45-station registry crossed
with the PR13 trajectory contract.  Observed AWC facts and P0 roots are
evidence projected onto that basis; they never define or shrink it.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .awc_observation_contract import (
    CanonicalAWCObservationContract,
    CanonicalAWCObservationRevisionFact,
)
from .canonical_contract import (
    CanonicalTypedScopeEnvelope,
    TYPED_SCOPE_ENVELOPE_SCHEMA,
    normalize_typed_scope_natural_key,
)
from .canonical_expected_scope_manifest import build_scope_basis
from .canonical_ssot_schema import inspect_canonical_ssot_schema
from .contracts import ContractError, content_sha256
from .p0_forecast_snapshot import CanonicalP0ForecastSnapshotFact
from .station_registry import coordinate_registry_manifest


SCHEMA = "bayes_basket_target_pr13_p1_readiness_terminal_receipt_v4"
BUILDER_ID = "pr13.canonical_awc_p1_readiness.v4"
EVENT_ROOT_SCHEMA = "bayes_basket_target_pr13_p1_event_root_basis_v1"
SCOPE_CONTRACT_SCHEMA = "bayes_basket_target_pr13_p1_readiness_scope_contract_v1"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
SOURCE_LANE = "AWC"
SEASON = "JJA"
MATURITY = "D0"
SETTLEMENT_DEFINITION = "official_daily_tmax_c"
INTRADAY_BUCKETS = ("NIGHT", "MORNING", "AFTERNOON", "EVENING")
EXPECTED_SCOPE_COUNT = 45 * len(INTRADAY_BUCKETS)


class PR13P1BasisError(ContractError):
    """A PR13 input cannot be consumed through its canonical contract."""


def _utc_z(value: object, label: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise PR13P1BasisError(f"pr13_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PR13P1BasisError(f"pr13_{label}_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _json_mapping(value: object, label: str) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise PR13P1BasisError(f"pr13_{label}_json_invalid") from exc
    if not isinstance(parsed, dict):
        raise PR13P1BasisError(f"pr13_{label}_mapping_required")
    return parsed


def _sha(value: object, label: str) -> str:
    result = str(value or "").strip().lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise PR13P1BasisError(f"pr13_{label}_sha_invalid")
    return result


def _scope_contract() -> dict[str, Any]:
    registry = coordinate_registry_manifest()
    stations = list(registry["station_ids"])
    if len(stations) != 45 or stations != sorted(set(stations)):
        raise PR13P1BasisError("pr13_registry_not_exact_45")
    identity = {
        "schema": SCOPE_CONTRACT_SCHEMA,
        "canonical_registry_sha256": _sha(
            registry["registry_sha256"], "registry_content"
        ),
        "station_ids": stations,
        "source_lane": SOURCE_LANE,
        "season": SEASON,
        "intraday_buckets": list(INTRADAY_BUCKETS),
        "maturity": MATURITY,
        "settlement_definition": SETTLEMENT_DEFINITION,
        "scope_count": len(stations) * len(INTRADAY_BUCKETS),
    }
    if identity["scope_count"] != EXPECTED_SCOPE_COUNT:
        raise PR13P1BasisError("pr13_expected_scope_count_invalid")
    return {**identity, "content_sha256": content_sha256(identity)}


def _revision_inventory(
    conn: sqlite3.Connection, *, cutoff: str
) -> tuple[dict[tuple[str, str, str], list[dict[str, Any]]], list[dict[str, str]]]:
    rows = conn.execute(
        "SELECT fact_id,content_sha256,fact_json,ingested_at_utc "
        "FROM canonical_ssot_fact WHERE fact_type='truth_observation_revision' "
        "AND ingested_at_utc<=? ORDER BY fact_id",
        (cutoff,),
    ).fetchall()
    by_scope: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    inventory: list[dict[str, str]] = []
    for fact_id, stored_sha, fact_json, ingested_at in rows:
        try:
            fact = CanonicalAWCObservationRevisionFact.from_mapping(
                _json_mapping(fact_json, "truth_observation_revision")
            )
        except ContractError as exc:
            raise PR13P1BasisError(
                "pr13_truth_observation_revision_contract_invalid"
            ) from exc
        if _sha(stored_sha, "truth_observation_revision_content") != fact.content_sha256:
            raise PR13P1BasisError("pr13_truth_observation_revision_hash_mismatch")
        committed = _utc_z(
            fact.raw["canonical_committed_at_utc"],
            "truth_observation_revision_committed_at",
        )
        if committed > cutoff:
            raise PR13P1BasisError("pr13_truth_observation_after_asof")
        if _utc_z(ingested_at, "truth_observation_revision_ingested_at") > cutoff:
            raise PR13P1BasisError("pr13_truth_observation_ingested_after_asof")
        if (
            fact.raw["source_lane"] != SOURCE_LANE
            or fact.raw["authority_scope"] != AUTHORITY_SCOPE
            or fact.raw["qc_state"] != "ACCEPTED"
            or fact.raw["halt_state"] != "CLEAR"
        ):
            raise PR13P1BasisError("pr13_truth_observation_not_p1_eligible")
        scope = (
            str(fact.raw["station_id"]),
            str(fact.raw["event_date"]),
            str(fact.raw["settlement_timezone"]),
        )
        by_scope[scope].append(
            {
                "content_sha256": fact.content_sha256,
                "observation_id": str(fact.raw["observation_id"]),
                "revision_no": int(fact.raw["revision_no"]),
                "observed_at_utc": _utc_z(
                    fact.raw["observed_at_utc"], "truth_observation_observed_at"
                ),
                "canonical_committed_at_utc": committed,
            }
        )
        inventory.append({"fact_id": str(fact_id), "content_sha256": fact.content_sha256})
    for values in by_scope.values():
        values.sort(
            key=lambda item: (
                item["canonical_committed_at_utc"],
                item["observation_id"],
                item["revision_no"],
                item["content_sha256"],
            )
        )
    return by_scope, inventory


def _revision_inventory_from_contract(
    raw_contract: Mapping[str, Any], *, cutoff: str,
) -> tuple[
    dict[tuple[str, str, str], list[dict[str, Any]]],
    list[dict[str, str]],
    str,
]:
    try:
        contract = CanonicalAWCObservationContract.from_mapping(raw_contract)
    except ContractError as exc:
        raise PR13P1BasisError("pr13_awc_observation_parent_contract_invalid") from exc
    if _utc_z(contract.as_of_utc, "awc_observation_parent_as_of") > cutoff:
        raise PR13P1BasisError("pr13_awc_observation_parent_after_asof")
    by_scope: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    inventory: list[dict[str, str]] = []
    for fact in contract.revision_facts:
        committed = _utc_z(
            fact.raw["canonical_committed_at_utc"],
            "truth_observation_revision_committed_at",
        )
        if committed > cutoff:
            raise PR13P1BasisError("pr13_truth_observation_after_asof")
        if (
            fact.raw["source_lane"] != SOURCE_LANE
            or fact.raw["authority_scope"] != AUTHORITY_SCOPE
            or fact.raw["qc_state"] != "ACCEPTED"
            or fact.raw["halt_state"] != "CLEAR"
        ):
            raise PR13P1BasisError("pr13_truth_observation_not_p1_eligible")
        scope = tuple(
            str(fact.raw[field])
            for field in ("station_id", "event_date", "settlement_timezone")
        )
        by_scope[scope].append({
            "content_sha256": fact.content_sha256,
            "observation_id": str(fact.raw["observation_id"]),
            "revision_no": int(fact.raw["revision_no"]),
            "observed_at_utc": _utc_z(
                fact.raw["observed_at_utc"], "truth_observation_observed_at"
            ),
            "canonical_committed_at_utc": committed,
        })
        inventory.append({
            "fact_id": "unpublished_awc_contract:"
            + contract.content_sha256
            + ":"
            + fact.content_sha256,
            "content_sha256": fact.content_sha256,
        })
    for values in by_scope.values():
        values.sort(key=lambda item: (
            item["canonical_committed_at_utc"], item["observation_id"],
            item["revision_no"], item["content_sha256"],
        ))
    return by_scope, inventory, contract.content_sha256


def _v14_p0_inventory(
    conn: sqlite3.Connection, *, cutoff: str
) -> tuple[dict[tuple[str, str, str], list[dict[str, Any]]], list[dict[str, Any]]]:
    rows = conn.execute(
        "SELECT fact_id,content_sha256,fact_json,ingested_at_utc "
        "FROM canonical_ssot_fact WHERE fact_type='p0_forecast_snapshot' "
        "AND ingested_at_utc<=? ORDER BY fact_id",
        (cutoff,),
    ).fetchall()
    by_scope: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    inventory: list[dict[str, Any]] = []
    for fact_id, stored_sha, fact_json, ingested_at in rows:
        try:
            snapshot = CanonicalP0ForecastSnapshotFact.from_mapping(
                _json_mapping(fact_json, "p0_forecast_snapshot")
            )
        except ContractError as exc:
            raise PR13P1BasisError(
                "pr13_p0_forecast_snapshot_contract_invalid"
            ) from exc
        if _sha(stored_sha, "p0_forecast_snapshot_content") != snapshot.content_sha256:
            raise PR13P1BasisError("pr13_p0_forecast_snapshot_hash_mismatch")
        ingested = _utc_z(ingested_at, "p0_forecast_snapshot_ingested_at")
        decision = _utc_z(
            snapshot.raw["forecast_decision_at_utc"],
            "p0_forecast_snapshot_decision_at",
        )
        if ingested > cutoff or decision > cutoff:
            raise PR13P1BasisError("pr13_p0_forecast_snapshot_after_asof")
        if (
            snapshot.raw["authority_scope"] != AUTHORITY_SCOPE
            or snapshot.raw["state"] != "MATERIALIZED_UNPUBLISHED"
        ):
            raise PR13P1BasisError("pr13_p0_forecast_snapshot_authority_invalid")
        scope = (
            str(snapshot.raw["station_id"]),
            str(snapshot.raw["event_date"]),
            str(snapshot.raw["settlement_timezone"]),
        )
        by_scope[scope].append(
            {
                "content_sha256": snapshot.content_sha256,
                "forecast_decision_at_utc": decision,
                "ingested_at_utc": ingested,
                "source_class": str(snapshot.raw["source_class"]),
            }
        )
        inventory.append({
            "fact_id": str(fact_id),
            "content_sha256": snapshot.content_sha256,
            "source_class": str(snapshot.raw["source_class"]),
            "p1_parent_source_eligible": (
                snapshot.raw["source_class"] == "native_official"
            ),
        })
    for values in by_scope.values():
        values.sort(
            key=lambda item: (
                item["forecast_decision_at_utc"],
                item["content_sha256"],
            )
        )
    return by_scope, inventory


def _typed_record(
    *, natural_key: Mapping[str, Any], evidence_content_sha256: str, cutoff: str
) -> dict[str, Any]:
    identity = {
        "scope_type": "TRAJECTORY_P1",
        "scope_kind": "ARTIFACT",
        "natural_key": normalize_typed_scope_natural_key(
            scope_type="TRAJECTORY_P1",
            scope_kind="ARTIFACT",
            raw=natural_key,
        ),
        "version": None,
        "as_of_utc": cutoff,
        "state": "BLOCKED",
        "blockers": ["ACTIVATION_ROOT_MISSING"],
        "evidence_content_sha256": evidence_content_sha256,
        "authority_scope": AUTHORITY_SCOPE,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def _event_root_basis(
    observations: Mapping[tuple[str, str, str], list[dict[str, Any]]],
    snapshots: Mapping[tuple[str, str, str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Bind each exact AWC revision to its latest unambiguous as-of P0."""

    results: list[dict[str, Any]] = []
    for scope in sorted(observations):
        station_id, event_date, settlement_timezone = scope
        candidates = snapshots.get(scope, [])
        for observation in observations[scope]:
            eligible = [
                item
                for item in candidates
                if item["forecast_decision_at_utc"]
                <= observation["canonical_committed_at_utc"]
            ]
            selected: dict[str, Any] | None = None
            blockers: list[str] = []
            if eligible:
                latest_decision = max(
                    item["forecast_decision_at_utc"] for item in eligible
                )
                latest = [
                    item
                    for item in eligible
                    if item["forecast_decision_at_utc"] == latest_decision
                ]
                if len(latest) != 1:
                    raise PR13P1BasisError(
                        "pr13_p0_forecast_snapshot_latest_ambiguous"
                    )
                if latest[0]["source_class"] != "native_official":
                    blockers = ["P1_PARENT_SOURCE_INELIGIBLE"]
                else:
                    selected = latest[0]
            else:
                blockers = ["P1_PARENT_MISSING"]
            identity = {
                "schema": EVENT_ROOT_SCHEMA,
                "station_id": station_id,
                "event_date": event_date,
                "settlement_timezone": settlement_timezone,
                "source_lane": SOURCE_LANE,
                "authority_scope": AUTHORITY_SCOPE,
                "observation_id": observation["observation_id"],
                "observation_revision_no": observation["revision_no"],
                "observation_observed_at_utc": observation["observed_at_utc"],
                "observation_canonical_committed_at_utc": observation[
                    "canonical_committed_at_utc"
                ],
                "truth_observation_revision_content_sha256": observation[
                    "content_sha256"
                ],
                "p0_forecast_snapshot_content_sha256": (
                    None if selected is None else selected["content_sha256"]
                ),
                "p0_forecast_decision_at_utc": (
                    None
                    if selected is None
                    else selected["forecast_decision_at_utc"]
                ),
                "state": (
                    "BLOCKED" if selected is None else "MATERIALIZED_UNPUBLISHED"
                ),
                "blockers": blockers,
                "consumable": False,
                "writes_canonical_fact": False,
            }
            results.append({**identity, "content_sha256": content_sha256(identity)})
    return results


def build_pr13_p1_readiness_artifacts(
    db_path: str | Path, *, as_of_utc: str,
    awc_observation_contract: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Build expected basis, typed terminal envelope, and sealed receipt."""

    cutoff = _utc_z(as_of_utc, "as_of_utc")
    scope_contract = _scope_contract()
    source = Path(db_path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN")
        inspection = inspect_canonical_ssot_schema(conn)
        schema_state = str(inspection.get("state") or "")
        if (
            schema_state not in {"V14", "V15", "V16"}
            or inspection.get("ok") is not True
        ):
            raise PR13P1BasisError("pr13_canonical_v14_schema_invalid")
        if awc_observation_contract is None:
            observations, observation_inventory = _revision_inventory(
                conn, cutoff=cutoff
            )
            observation_parent_sha = None
        else:
            (
                observations,
                observation_inventory,
                observation_parent_sha,
            ) = _revision_inventory_from_contract(
                awc_observation_contract, cutoff=cutoff
            )
        roots, p0_inventory = _v14_p0_inventory(conn, cutoff=cutoff)
        p0_inventory_fact_type = "p0_forecast_snapshot"
        data_version = int(conn.execute("PRAGMA data_version").fetchone()[0])
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.rollback()
    except sqlite3.Error as exc:
        if conn.in_transaction:
            conn.rollback()
        raise PR13P1BasisError("pr13_canonical_read_failed") from exc
    finally:
        conn.close()

    observed_sources = sorted(
        observation_inventory, key=lambda item: (item["fact_id"], item["content_sha256"])
    )
    p0_sources = sorted(
        p0_inventory, key=lambda item: (item["fact_id"], item["content_sha256"])
    )
    source_inventory_sha = content_sha256(
        {
            "truth_observation_revisions": observed_sources,
            "p0_snapshot_fact_type": p0_inventory_fact_type,
            "p0_snapshots": p0_sources,
        }
    )
    canonical_stations = set(scope_contract["station_ids"])
    if (
        {scope[0] for scope in observations} - canonical_stations
        or {scope[0] for scope in roots} - canonical_stations
    ):
        raise PR13P1BasisError("pr13_source_station_not_in_registry")

    event_roots = _event_root_basis(observations, roots)
    event_roots_by_station: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in event_roots:
        event_roots_by_station[item["station_id"]].append(item)

    expected_keys: list[dict[str, Any]] = []
    terminal_scopes: list[dict[str, Any]] = []
    typed_records: list[dict[str, Any]] = []
    for station_id in scope_contract["station_ids"]:
        station_observation_scopes = {
            scope: values
            for scope, values in observations.items()
            if scope[0] == station_id
        }
        revision_shas = sorted(
            item["content_sha256"]
            for values in station_observation_scopes.values()
            for item in values
        )
        station_event_roots = event_roots_by_station.get(station_id, [])
        root_shas = sorted(
            str(item["p0_forecast_snapshot_content_sha256"])
            for item in station_event_roots
            if item["p0_forecast_snapshot_content_sha256"] is not None
        )
        for intraday_bucket in INTRADAY_BUCKETS:
            natural_key = {
                "station_id": station_id,
                "source_lane": SOURCE_LANE,
                "season": SEASON,
                "intraday_bucket": intraday_bucket,
                "maturity": MATURITY,
                "settlement_definition": SETTLEMENT_DEFINITION,
            }
            expected_key = {"scope_kind": "ARTIFACT", "natural_key": natural_key}
            expected_keys.append(expected_key)
            domain_blocker = "ACTIVATION_ROOT_MISSING"
            evidence = {
                "schema": "bayes_basket_target_pr13_p1_readiness_scope_evidence_v1",
                "natural_key": natural_key,
                "observation_station_day_count": len(station_observation_scopes),
                "observation_fact_count": len(revision_shas),
                "observation_fact_set_sha256": content_sha256(revision_shas),
                "p1_eligible_p0_snapshot_count": len(root_shas),
                "p1_eligible_p0_snapshot_set_sha256": content_sha256(root_shas),
                "domain_blocker": domain_blocker,
                "source_inventory_content_sha256": source_inventory_sha,
            }
            evidence_sha = content_sha256(evidence)
            terminal_scopes.append(
                {
                    **natural_key,
                    "scope_key": "|".join(
                        (
                            station_id,
                            SOURCE_LANE.lower(),
                            SEASON,
                            intraday_bucket,
                            MATURITY,
                            SETTLEMENT_DEFINITION,
                        )
                    ),
                    "observation_station_day_count": len(station_observation_scopes),
                    "observation_fact_count": len(revision_shas),
                    "observation_fact_set_sha256": content_sha256(revision_shas),
                    "p1_eligible_p0_snapshot_count": len(root_shas),
                    "p1_eligible_p0_snapshot_set_sha256": content_sha256(root_shas),
                    "state": "BLOCKED",
                    "phase_state": "BLOCKED_EVIDENCE",
                    "blockers": [domain_blocker],
                    "evidence_content_sha256": evidence_sha,
                }
            )
            typed_records.append(
                _typed_record(
                    natural_key=natural_key,
                    evidence_content_sha256=evidence_sha,
                    cutoff=cutoff,
                )
            )

    if len(expected_keys) != EXPECTED_SCOPE_COUNT or len(terminal_scopes) != EXPECTED_SCOPE_COUNT:
        raise PR13P1BasisError("pr13_readiness_scope_set_incomplete")

    expected_basis = build_scope_basis(
        scope_type="TRAJECTORY_P1",
        as_of_utc=cutoff,
        expected_keys=expected_keys,
        source_content_sha256s=(
            scope_contract["canonical_registry_sha256"],
            scope_contract["content_sha256"],
        ),
    )
    envelope_identity = {
        "schema": TYPED_SCOPE_ENVELOPE_SCHEMA,
        "as_of_utc": cutoff,
        "builder_id": BUILDER_ID,
        "scope_type": "TRAJECTORY_P1",
        "records": typed_records,
        "production_no_live": True,
        "future_data_counts_as_oos": False,
    }
    typed_envelope = {
        **envelope_identity,
        "content_sha256": content_sha256(envelope_identity),
    }
    CanonicalTypedScopeEnvelope.from_mapping(typed_envelope)

    blocker_counts: dict[str, int] = defaultdict(int)
    for item in terminal_scopes:
        blocker_counts[item["blockers"][0]] += 1
    receipt_identity = {
        "schema": SCHEMA,
        "as_of_utc": cutoff,
        "builder_id": BUILDER_ID,
        "state": "BLOCKED",
        "release_state": "BLOCKED",
        "authority_scope": AUTHORITY_SCOPE,
        "scope_contract": scope_contract,
        "expected_scope_count": EXPECTED_SCOPE_COUNT,
        "terminal_scope_count": len(terminal_scopes),
        "terminal_scopes": terminal_scopes,
        "blockers": sorted(blocker_counts),
        "blocker_counts": dict(sorted(blocker_counts.items())),
        "formal_eligible_scope_count": 0,
        "canonical_awc_observation_fact_count": len(observed_sources),
        "awc_observation_parent_contract_content_sha256": observation_parent_sha,
        "canonical_awc_observation_station_day_count": len(observations),
        "canonical_awc_observation_station_count": len(
            {scope[0] for scope in observations}
        ),
        "canonical_p0_snapshot_fact_count": len(p0_sources),
        "canonical_p0_snapshot_inventory": p0_sources,
        "canonical_p0_snapshot_fact_type": p0_inventory_fact_type,
        "canonical_schema_state": schema_state,
        "v14_p0_forecast_snapshot_consumer_ready": True,
        "p1_event_root_basis": event_roots,
        "p1_event_root_basis_count": len(event_roots),
        "p1_event_root_materialized_unpublished_count": sum(
            item["state"] == "MATERIALIZED_UNPUBLISHED" for item in event_roots
        ),
        "p1_event_root_blocked_count": sum(
            item["state"] == "BLOCKED" for item in event_roots
        ),
        "source_inventory_content_sha256": source_inventory_sha,
        "expected_basis_content_sha256": expected_basis["content_sha256"],
        "typed_envelope_content_sha256": typed_envelope["content_sha256"],
        "database": str(source),
        "database_data_version": data_version,
        "database_schema_version": schema_version,
        "writes_database": False,
        "writes_probability_envelope": False,
        "writes_canonical_fact": False,
        "publishes_pr04_pointer": False,
        "starts_services": False,
        "releases_live_pause": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
        "production_no_live": True,
        "future_data_counts_as_oos": False,
    }
    terminal_receipt = {
        **receipt_identity,
        "content_sha256": content_sha256(receipt_identity),
    }
    return expected_basis, typed_envelope, terminal_receipt


__all__ = [
    "AUTHORITY_SCOPE",
    "BUILDER_ID",
    "EXPECTED_SCOPE_COUNT",
    "INTRADAY_BUCKETS",
    "PR13P1BasisError",
    "SCHEMA",
    "build_pr13_p1_readiness_artifacts",
]
