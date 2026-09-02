"""Materialization and read-only gating for the regime SSOT."""

from __future__ import annotations

from dataclasses import dataclass
import json
import sqlite3
from typing import Any, Iterable, Mapping

from .contracts import (
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
    shift_timestamp_utc,
    stage_servable_grace_seconds,
)
from .regime_artifact import (
    REGIME_ARTIFACT_SCHEMA,
    RegimeArtifact,
    resolve_regime_artifact,
)
from .stages import STAGE_PRECEDENCE, select_current_stage
from .fleet import expected_station_count


MAX_RESIDENT_REGIME_STATIONS = expected_station_count()
MAX_RESIDENT_PROBABILITY_ENVELOPES = 512


@dataclass(frozen=True)
class RegimeGate:
    state: str
    direction_allowed: bool
    concentration_multiplier: float
    reasons: tuple[str, ...]
    audit: dict[str, Any]


def materialize_regime_artifact(raw: Mapping[str, Any]) -> RegimeArtifact:
    """Validate a precomputed artifact; this function never reads raw weather."""
    return RegimeArtifact.from_mapping(raw)


def build_regime_artifact(
    envelope: ProbabilityEnvelope | Mapping[str, Any],
    *,
    stability_threshold: float = 0.01,
    direction_threshold: float = 0.05,
    concentration_multiplier: float = 1.0,
) -> RegimeArtifact:
    """Derive the deterministic regime companion from one probability SSOT.

    Model extrema are consumed only as disagreement diagnostics already
    carried by the probability lineage.  They never define the support set or
    fill the buckets between extrema.
    """

    probability = (
        envelope
        if isinstance(envelope, ProbabilityEnvelope)
        else ProbabilityEnvelope.from_mapping(envelope)
    )
    center = probability.center_bucket
    center_ordinal = probability.bucket_ordinals[center]
    ordered_probabilities = sorted(
        probability.probabilities.values(),
        reverse=True,
    )
    runner_up = (
        ordered_probabilities[1]
        if len(ordered_probabilities) > 1
        else 0.0
    )
    center_margin = probability.probabilities[center] - runner_up
    side_up = sum(
        (
            value
            for bucket, value in probability.probabilities.items()
            if probability.bucket_ordinals[bucket] > center_ordinal
        ),
        0.0,
    )
    side_down = sum(
        (
            value
            for bucket, value in probability.probabilities.items()
            if probability.bucket_ordinals[bucket] < center_ordinal
        ),
        0.0,
    )
    direction_margin = side_up - side_down
    stable = center_margin >= float(stability_threshold)
    regime = (
        "NEUTRAL"
        if not stable or abs(direction_margin) < float(direction_threshold)
        else "UP"
        if direction_margin > 0.0
        else "DOWN"
    )
    lower = str(
        probability.lineage.get("model_envelope_lower_bucket") or ""
    ).strip()
    upper = str(
        probability.lineage.get("model_envelope_upper_bucket") or ""
    ).strip()
    span_raw = probability.lineage.get("model_envelope_span_c")
    if not lower or not upper or span_raw in (None, ""):
        raise ContractError("regime_model_disagreement_lineage_missing")
    try:
        span = float(span_raw)
    except (TypeError, ValueError) as exc:
        raise ContractError(
            "regime_model_disagreement_lineage_invalid"
        ) from exc
    identity = {
        "schema": REGIME_ARTIFACT_SCHEMA,
        "artifact_version": "v1",
        "artifact_id": "",
        "station_id": probability.station_id,
        "event_date": probability.event_date,
        "stage": probability.stage,
        "checkpoint_id": probability.checkpoint_id,
        "settlement_timezone": probability.settlement_timezone,
        "as_of_utc": probability.decision_at_utc,
        "valid_from_utc": probability.decision_at_utc,
        "valid_until_utc": probability.fresh_until_utc,
        "probability_sha256": probability.content_sha256,
        "regime": regime,
        "center_stable": stable,
        "center_lead_margin": center_margin,
        "stability_threshold": float(stability_threshold),
        "side_mass_up": side_up,
        "side_mass_down": side_down,
        "direction_mass_margin": direction_margin,
        "direction_threshold": float(direction_threshold),
        "model_disagreement": span,
        "model_extreme_low_bucket": lower,
        "model_extreme_high_bucket": upper,
        "concentration_multiplier": float(concentration_multiplier),
    }
    artifact_seed = content_sha256({
        key: value
        for key, value in identity.items()
        if key != "artifact_id"
    })
    identity["artifact_id"] = "regime_" + artifact_seed[:40]
    identity["content_sha256"] = content_sha256(identity)
    return RegimeArtifact.from_mapping(identity)


def insert_regime_artifact(
    conn: sqlite3.Connection,
    artifact: RegimeArtifact | Mapping[str, Any],
    *,
    created_at_utc: str,
) -> dict[str, Any]:
    """Append one exact probability companion idempotently."""

    resolved = (
        artifact
        if isinstance(artifact, RegimeArtifact)
        else RegimeArtifact.from_mapping(artifact)
    )
    payload = resolved.to_dict()
    scope_rows = conn.execute(
        "SELECT probability_sha256,content_sha256 FROM basket_regime_artifact "
        "WHERE station_id=? AND event_date=? AND stage=? AND checkpoint_id=? "
        "AND state='COMMITTED' LIMIT 2",
        (
            resolved.station_id,
            resolved.event_date,
            resolved.stage,
            resolved.checkpoint_id,
        ),
    ).fetchall()
    if len(scope_rows) > 1:
        raise ContractError("regime_probability_checkpoint_not_unique")
    if scope_rows and (
        str(scope_rows[0][0]) != resolved.probability_sha256
        or str(scope_rows[0][1]) != resolved.content_sha256
    ):
        raise ContractError("regime_probability_content_drift")
    existing = conn.execute(
        "SELECT content_sha256 FROM basket_regime_artifact "
        "WHERE artifact_id=?",
        (resolved.artifact_id,),
    ).fetchone()
    if existing is not None:
        if str(existing[0]) != resolved.content_sha256:
            raise ContractError("regime_artifact_storage_conflict")
        return {
            "artifact_id": resolved.artifact_id,
            "wrote": False,
            "idempotent": True,
        }
    conn.execute(
        "INSERT INTO basket_regime_artifact("
        "artifact_id,station_id,event_date,stage,checkpoint_id,"
        "settlement_timezone,probability_sha256,as_of_utc,valid_from_utc,"
        "valid_until_utc,artifact_json,content_sha256,state,created_at"
        ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            resolved.artifact_id,
            resolved.station_id,
            resolved.event_date,
            resolved.stage,
            resolved.checkpoint_id,
            resolved.settlement_timezone,
            resolved.probability_sha256,
            resolved.as_of_utc,
            resolved.valid_from_utc,
            resolved.valid_until_utc,
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            resolved.content_sha256,
            "COMMITTED",
            created_at_utc,
        ),
    )
    return {
        "artifact_id": resolved.artifact_id,
        "wrote": True,
        "idempotent": False,
    }


def _bounded_envelopes(
    envelopes: Iterable[ProbabilityEnvelope | Mapping[str, Any]],
) -> list[ProbabilityEnvelope]:
    rows: list[ProbabilityEnvelope] = []
    for item in envelopes:
        if len(rows) >= MAX_RESIDENT_PROBABILITY_ENVELOPES:
            raise ContractError("resident_regime_envelope_limit_exceeded")
        rows.append(
            item
            if isinstance(item, ProbabilityEnvelope)
            else ProbabilityEnvelope.from_mapping(item)
        )
    if not rows:
        raise ContractError("resident_regime_probability_missing")
    return rows


def _registry_scope(
    registry_timezones: Mapping[tuple[str, str], str],
) -> dict[tuple[str, str], str]:
    if not isinstance(registry_timezones, Mapping) or not registry_timezones:
        raise ContractError("resident_regime_registry_scope_missing")
    result: dict[tuple[str, str], str] = {}
    for raw_key, raw_timezone in registry_timezones.items():
        if not (
            isinstance(raw_key, tuple)
            and len(raw_key) == 2
            and all(isinstance(item, str) and item.strip() for item in raw_key)
        ):
            raise ContractError("resident_regime_registry_scope_invalid")
        key = (raw_key[0].strip(), raw_key[1].strip())
        timezone = str(raw_timezone or "").strip()
        if not timezone:
            raise ContractError("resident_regime_registry_timezone_missing")
        if key in result:
            raise ContractError("resident_regime_registry_scope_duplicate")
        result[key] = timezone
    if len({station_id for station_id, _ in result}) > MAX_RESIDENT_REGIME_STATIONS:
        raise ContractError("resident_regime_station_limit_exceeded")
    if len({station_id for station_id, _ in result}) != len(result):
        raise ContractError("resident_regime_station_event_date_not_unique")
    return result


def _select_resident_probabilities(
    conn: sqlite3.Connection,
    *,
    envelopes: Iterable[ProbabilityEnvelope | Mapping[str, Any]],
    registry_timezones: Mapping[tuple[str, str], str],
    evaluation_at_utc: str,
) -> tuple[list[ProbabilityEnvelope], list[ProbabilityEnvelope]]:
    registry = _registry_scope(registry_timezones)
    rows = _bounded_envelopes(envelopes)
    grouped: dict[tuple[str, str], list[ProbabilityEnvelope]] = {}
    identities: dict[tuple[str, str, str, str], str] = {}
    for row in rows:
        scope = (row.station_id, row.event_date)
        if scope not in registry:
            raise ContractError("resident_regime_registry_scope_mismatch")
        if row.settlement_timezone != registry[scope]:
            raise ContractError("resident_regime_settlement_timezone_mismatch")
        identity = (
            row.station_id,
            row.event_date,
            row.stage,
            row.checkpoint_id,
        )
        prior_sha = identities.get(identity)
        if prior_sha is not None:
            if prior_sha != row.content_sha256:
                raise ContractError("resident_regime_probability_content_drift")
            continue
        identities[identity] = row.content_sha256
        grouped.setdefault(scope, []).append(row)
    if set(grouped) != set(registry):
        raise ContractError("resident_regime_registry_scope_mismatch")
    if len(grouped) > MAX_RESIDENT_REGIME_STATIONS:
        raise ContractError("resident_regime_station_limit_exceeded")

    selected: list[ProbabilityEnvelope] = []
    for scope in sorted(grouped):
        probability = select_current_stage(
            grouped[scope],
            at_utc=evaluation_at_utc,
        )
        if probability.settlement_timezone != registry[scope]:
            raise ContractError("resident_regime_settlement_timezone_mismatch")
        existing_stages = {
            str(row[0])
            for row in conn.execute(
                "SELECT DISTINCT stage FROM basket_regime_artifact "
                "WHERE station_id=? AND event_date=? AND state='COMMITTED'",
                scope,
            ).fetchall()
        }
        if any(
            STAGE_PRECEDENCE.get(stage, len(STAGE_PRECEDENCE))
            > STAGE_PRECEDENCE[probability.stage]
            for stage in existing_stages
        ):
            raise ContractError("resident_regime_stage_rewind_forbidden")
        selected.append(probability)
    return rows, selected


def materialize_resident_regimes(
    conn: sqlite3.Connection,
    *,
    envelopes: Iterable[ProbabilityEnvelope | Mapping[str, Any]],
    registry_timezones: Mapping[tuple[str, str], str],
    evaluation_at_utc: str,
    created_at_utc: str,
) -> dict[str, Any]:
    """Atomically materialize the one authoritative regime per station-day.

    The highest submitted stage is authoritative (P1, then P0-M, then P0).
    A malformed, ambiguous, or stale higher stage aborts the whole batch;
    lower stages are never used as a recovery path.  Registry timezones are
    exact scope authority and probability content is immutable per
    stage/checkpoint.
    """

    rows, probabilities = _select_resident_probabilities(
        conn,
        envelopes=envelopes,
        registry_timezones=registry_timezones,
        evaluation_at_utc=evaluation_at_utc,
    )
    selected = [
        (probability, build_regime_artifact(probability))
        for probability in probabilities
    ]

    conn.execute("SAVEPOINT regime_resident_batch")
    writes: list[dict[str, Any]] = []
    try:
        for _, artifact in selected:
            writes.append(
                insert_regime_artifact(
                    conn,
                    artifact,
                    created_at_utc=created_at_utc,
                )
            )
    except Exception:
        conn.execute("ROLLBACK TO regime_resident_batch")
        conn.execute("RELEASE regime_resident_batch")
        raise
    conn.execute("RELEASE regime_resident_batch")

    selections = [
        {
            "station_id": probability.station_id,
            "event_date": probability.event_date,
            "stage": probability.stage,
            "checkpoint_id": probability.checkpoint_id,
            "settlement_timezone": probability.settlement_timezone,
            "probability_sha256": probability.content_sha256,
            "regime_artifact_id": artifact.artifact_id,
            "regime_sha256": artifact.content_sha256,
        }
        for probability, artifact in selected
    ]
    identity = {
        "schema": "bayes_basket_target_regime_resident_receipt_v1",
        "evaluation_at_utc": evaluation_at_utc,
        "station_count": len(selections),
        "input_envelope_count": len(rows),
        "selected": selections,
    }
    return {
        **identity,
        "wrote_count": sum(bool(item["wrote"]) for item in writes),
        "idempotent_count": sum(bool(item["idempotent"]) for item in writes),
        "selection_sha256": content_sha256(identity),
    }


def _assert_regime_supersession_schema(conn: sqlite3.Connection) -> None:
    table_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' "
        "AND name='basket_regime_artifact'"
    ).fetchone()
    table_sql = str(table_row[0] if table_row else "").upper()
    if "SUPERSEDED" not in table_sql:
        raise ContractError("regime_superseded_state_schema_required")
    for index_row in conn.execute(
        "PRAGMA index_list(basket_regime_artifact)"
    ).fetchall():
        index_name = str(index_row[1])
        unique = bool(index_row[2])
        partial = bool(index_row[4]) if len(index_row) > 4 else False
        if not unique or partial:
            continue
        columns = tuple(
            str(row[2])
            for row in conn.execute(
                f"PRAGMA index_info('{index_name}')"
            ).fetchall()
        )
        if columns == (
            "station_id",
            "event_date",
            "stage",
            "checkpoint_id",
            "probability_sha256",
        ):
            raise ContractError("regime_partial_committed_unique_index_required")
    trigger_rows = conn.execute(
        "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
        "AND tbl_name='basket_regime_artifact'"
    ).fetchall()
    for name, sql in trigger_rows:
        normalized = " ".join(str(sql or "").upper().split())
        if (
            str(name) == "basket_regime_artifact_no_update"
            or (
                "BEFORE UPDATE ON BASKET_REGIME_ARTIFACT" in normalized
                and "RAISE(ABORT,'APPEND_ONLY')" in normalized
            )
        ):
            raise ContractError(
                "regime_controlled_supersession_trigger_required"
            )


def rematerialize_resident_regimes(
    conn: sqlite3.Connection,
    *,
    envelopes: Iterable[ProbabilityEnvelope | Mapping[str, Any]],
    registry_timezones: Mapping[tuple[str, str], str],
    evaluation_at_utc: str,
    created_at_utc: str,
    apply_production_no_live: bool = False,
) -> dict[str, Any]:
    """Plan or atomically apply exact regime derivation replacements.

    Dry-run is the default.  Apply replaces only a drifted COMMITTED regime
    for the exact same probability checkpoint/hash, preserving the old row as
    SUPERSEDED and inserting the current deterministic artifact.  The
    probability envelope is read-only throughout.
    """

    rows, selected = _select_resident_probabilities(
        conn,
        envelopes=envelopes,
        registry_timezones=registry_timezones,
        evaluation_at_utc=evaluation_at_utc,
    )
    transitions: list[dict[str, Any]] = []
    for probability in selected:
        expected = build_regime_artifact(probability)
        old_rows = conn.execute(
            "SELECT artifact_id,artifact_json,content_sha256,state "
            "FROM basket_regime_artifact WHERE station_id=? AND event_date=? "
            "AND stage=? AND checkpoint_id=? AND probability_sha256=? "
            "AND state='COMMITTED' LIMIT 2",
            (
                probability.station_id,
                probability.event_date,
                probability.stage,
                probability.checkpoint_id,
                probability.content_sha256,
            ),
        ).fetchall()
        if len(old_rows) != 1:
            raise ContractError("regime_rematerialization_source_not_unique")
        old = RegimeArtifact.from_mapping(json.loads(str(old_rows[0][1])))
        if (
            old.artifact_id != str(old_rows[0][0])
            or old.content_sha256 != str(old_rows[0][2])
            or str(old_rows[0][3]) != "COMMITTED"
        ):
            raise ContractError("regime_rematerialization_storage_drift")
        if (
            old.station_id != probability.station_id
            or old.event_date != probability.event_date
            or old.stage != probability.stage
            or old.checkpoint_id != probability.checkpoint_id
            or old.settlement_timezone != probability.settlement_timezone
            or old.probability_sha256 != probability.content_sha256
        ):
            raise ContractError("regime_rematerialization_scope_drift")
        transitions.append(
            {
                "station_id": probability.station_id,
                "event_date": probability.event_date,
                "stage": probability.stage,
                "checkpoint_id": probability.checkpoint_id,
                "settlement_timezone": probability.settlement_timezone,
                "probability_sha256": probability.content_sha256,
                "probability_center_bucket": probability.center_bucket,
                "old_artifact_id": old.artifact_id,
                "old_regime_sha256": old.content_sha256,
                "new_artifact_id": expected.artifact_id,
                "new_regime_sha256": expected.content_sha256,
                "action": (
                    "IDEMPOTENT"
                    if old.content_sha256 == expected.content_sha256
                    else "SUPERSEDE_AND_INSERT"
                ),
                "_artifact": expected,
            }
        )

    if apply_production_no_live:
        _assert_regime_supersession_schema(conn)
        conn.execute("SAVEPOINT regime_resident_rematerialization")
        try:
            for transition in transitions:
                if transition["action"] == "IDEMPOTENT":
                    continue
                cursor = conn.execute(
                    "UPDATE basket_regime_artifact SET state='SUPERSEDED' "
                    "WHERE artifact_id=? AND state='COMMITTED' "
                    "AND probability_sha256=?",
                    (
                        transition["old_artifact_id"],
                        transition["probability_sha256"],
                    ),
                )
                if cursor.rowcount != 1:
                    raise ContractError(
                        "regime_rematerialization_supersede_race"
                    )
                insert_regime_artifact(
                    conn,
                    transition["_artifact"],
                    created_at_utc=created_at_utc,
                )
        except Exception:
            conn.execute("ROLLBACK TO regime_resident_rematerialization")
            conn.execute("RELEASE regime_resident_rematerialization")
            raise
        conn.execute("RELEASE regime_resident_rematerialization")

    public_transitions = [
        {key: value for key, value in transition.items() if key != "_artifact"}
        for transition in transitions
    ]
    identity = {
        "schema": "bayes_basket_target_regime_rematerialization_receipt_v1",
        "status": "PASS",
        "mode": (
            "APPLY_PRODUCTION_NO_LIVE"
            if apply_production_no_live
            else "DRY_RUN"
        ),
        "evaluation_at_utc": evaluation_at_utc,
        "station_count": len(public_transitions),
        "input_envelope_count": len(rows),
        "transition_count": sum(
            item["action"] == "SUPERSEDE_AND_INSERT"
            for item in public_transitions
        ),
        "idempotent_count": sum(
            item["action"] == "IDEMPOTENT"
            for item in public_transitions
        ),
        "transitions": public_transitions,
        "boundary": {
            "probability_envelopes_read_only": True,
            "probability_center_modified": False,
            "calls_order_gateway": False,
            "live_submit_authorized": False,
            "places_orders": False,
        },
    }
    return {**identity, "content_sha256": content_sha256(identity)}


def load_regime_artifact(
    conn: sqlite3.Connection,
    *,
    probability: ProbabilityEnvelope,
    evaluation_at_utc: str,
) -> RegimeArtifact:
    rows = conn.execute(
        "SELECT artifact_json FROM basket_regime_artifact "
        "WHERE station_id=? AND event_date=? AND stage=? "
        "AND checkpoint_id=? AND probability_sha256=? "
        "AND state='COMMITTED' AND valid_from_utc<=? "
        "AND valid_until_utc>? LIMIT 2",
        (
            probability.station_id,
            probability.event_date,
            probability.stage,
            probability.checkpoint_id,
            probability.content_sha256,
            evaluation_at_utc,
            # The window closes ``valid_until`` plus the stage's serving
            # grace, the same grace the envelope itself is served under
            # (``resolve_regime_artifact`` applies it again on the row).
            shift_timestamp_utc(
                evaluation_at_utc,
                -stage_servable_grace_seconds(probability.stage),
            ),
        ),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError("regime_ssot_not_unique")
    return resolve_regime_artifact(
        json.loads(str(rows[0][0])),
        station_id=probability.station_id,
        event_date=probability.event_date,
        stage=probability.stage,
        checkpoint_id=probability.checkpoint_id,
        settlement_timezone=probability.settlement_timezone,
        probability_sha256=probability.content_sha256,
        evaluation_at_utc=evaluation_at_utc,
    )


def evaluate_regime_gate(
    artifact: RegimeArtifact | Mapping[str, Any], *, station_id: str, event_date: str,
    stage: str, checkpoint_id: str, settlement_timezone: str, evaluation_at_utc: str,
    probability_sha256: str, probability_direction: str, center_bucket: str,
) -> RegimeGate:
    resolved = resolve_regime_artifact(
        artifact, station_id=station_id, event_date=event_date, stage=stage,
        checkpoint_id=checkpoint_id, settlement_timezone=settlement_timezone,
        probability_sha256=probability_sha256,
        evaluation_at_utc=evaluation_at_utc,
    )
    probability_direction = str(probability_direction).upper()
    if resolved.center_stable is False:
        state = "NEUTRAL_UNSTABLE"
        reasons = ("center_lead_margin_below_stability_threshold",)
        allowed = False
    elif resolved.regime != probability_direction:
        # A station policy may deliberately raise the probability-side
        # direction deadband and therefore classify the same envelope as
        # ``NEUTRAL`` even though the fixed regime companion is UP/DOWN.
        # Treat that as a conservative direction conflict (no directional
        # actions) rather than a compile error.
        #
        # Until now the other half of that disagreement -- both sides
        # directional and not agreeing -- raised instead, on the reading that
        # opposite claims are a lineage fault.  They are not: by the time
        # control reaches this line the artifact has already been matched to
        # this exact station, event date, stage, checkpoint and probability
        # hash, and resolve_regime_artifact has re-verified every one of them.
        # Lineage is proven.  What is left is two layers reading the same
        # envelope and disagreeing about direction, which is the one thing
        # clause 52c of the 2026-08-16 ruling says this gate does not get a
        # vote on: "the gate now records and does not enforce", direction
        # keeps its single honest source in which side of the centre carries
        # the mass and the EV.  That ruling changed the call site in
        # runtime.py, which appends regime_direction_recorded_not_enforced and
        # carries on; it did not change this function, so the exception went
        # on ending the compile from inside the gate and never reached the
        # recording it was supposed to get.  The veto it once fed is gone
        # too: nothing passes regime_direction_allowed any more.
        #
        # On 2026-08-31 this was the sole blocker on all eight US stations in
        # the cycle, and by construction no receipt of it existed anywhere --
        # the throw is taken before the audit is built, so the only
        # disagreement this system has ever recorded is the half that returns.
        state = (
            "NEUTRAL_REGIME_CONFLICT"
            if probability_direction == "NEUTRAL"
            else "REGIME_DIRECTION_CONFLICT"
        )
        reasons = (
            ("probability_direction_neutral", "regime_direction_not_authoritative")
            if probability_direction == "NEUTRAL"
            else ("regime_direction_conflict", "regime_direction_not_authoritative")
        )
        allowed = False
    elif resolved.regime == "NEUTRAL":
        state = "NEUTRAL"
        reasons = ("stable_neutral_probability_direction",)
        allowed = True
    else:
        state = resolved.regime
        reasons = ("regime_direction_consistent",)
        allowed = True
    # The artifact may only tighten concentration.  It cannot alter p(B),
    # center_bucket, support, or the probability-derived direction.
    multiplier = min(1.0, max(0.05, resolved.concentration_multiplier))
    if not allowed:
        multiplier = min(multiplier, 0.75)
    audit = {
        "schema": "bayes_basket_regime_gate_v1", "state": state,
        "direction_allowed": allowed, "center_bucket": center_bucket,
        "probability_direction": probability_direction,
        "artifact_regime": resolved.regime,
        "center_lead_margin": resolved.center_lead_margin,
        "stability_threshold": resolved.stability_threshold,
        "side_mass_up": resolved.side_mass_up, "side_mass_down": resolved.side_mass_down,
        "direction_mass_margin": resolved.direction_mass_margin,
        "direction_threshold": resolved.direction_threshold,
        "model_disagreement": resolved.model_disagreement,
        "model_extreme_low_bucket": resolved.model_extreme_low_bucket,
        "model_extreme_high_bucket": resolved.model_extreme_high_bucket,
        "concentration_multiplier": multiplier, "artifact_content_sha256": resolved.content_sha256,
        "reasons": list(reasons),
    }
    return RegimeGate(state, allowed, multiplier, reasons, audit)


__all__ = [
    "MAX_RESIDENT_PROBABILITY_ENVELOPES",
    "MAX_RESIDENT_REGIME_STATIONS",
    "RegimeGate",
    "build_regime_artifact",
    "evaluate_regime_gate",
    "insert_regime_artifact",
    "load_regime_artifact",
    "materialize_regime_artifact",
    "materialize_resident_regimes",
    "rematerialize_resident_regimes",
]
