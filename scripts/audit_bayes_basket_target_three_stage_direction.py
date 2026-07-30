#!/usr/bin/env python3
"""Bounded, read-only M1-08 audit for P0 -> P0-M -> P1 direction.

This audit deliberately does not materialize, repair, or promote anything.  It
reads only the already committed probability and regime SSOT rows, derives
direction again from each stage's own probability vector, and emits a bounded
receipt.  In particular, a market quote (or a market-shaped lineage field)
cannot become an implicit direction input.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bayes_basket_target.contracts import (  # noqa: E402
    ContractError,
    ProbabilityEnvelope,
    content_sha256,
)
from bayes_basket_target.regime_artifact import RegimeArtifact  # noqa: E402
from bayes_basket_target.regime_materialization import (  # noqa: E402
    build_regime_artifact,
)
from bayes_basket_target.ssot import (  # noqa: E402
    SCHEMA_VERSION,
    production_ssot_schema_status,
)


DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
RECEIPT_SCHEMA = "bayes_basket_target_three_stage_direction_audit_v1"
STAGES = ("P0", "P0-M", "P1")
REQUIRED_STAGES = ("P0", "P1")
MAX_ENVELOPES = 500
MAX_REGIME_ARTIFACTS = 500
MAX_JSON_FIELD_BYTES = 256 * 1024
STABILITY_THRESHOLD = 0.01
DIRECTION_THRESHOLD = 0.05
_TOLERANCE = 1e-9

# These are explicit direction dependencies, not generic metadata words.
# ``market_registry_*``, ``market_id``, ``market_slug``, ``decision_anchor_*``
# and ordinary ``price_unit`` metadata are allowed.  Model extrema remain
# diagnostic-only fields and are never consulted by ``_direction_diagnostics``.
_EXPLICIT_DIRECTION_KEYS = frozenset(
    {
        "market_anchor",
        "market_price",
        "market_bid",
        "market_ask",
        "price_anchor",
        "anchor_price",
        "direction_anchor",
        "direction_market",
        "direction_price",
        "direction_bid",
        "direction_ask",
        "bid",
        "ask",
        "bid_price",
        "ask_price",
    }
)
_DIRECTION_CONTEXT_TOKENS = ("direction", "direction_input", "directional", "bias_input")
_MARKET_DEPENDENCY_TOKENS = ("market", "price", "anchor", "bid", "ask")
_LINEAGE_STAGE_KEYS = ("stage", "probability_stage", "stage_name")
_LINEAGE_CHECKPOINT_KEYS = (
    "checkpoint_id",
    "probability_checkpoint_id",
    "stage_checkpoint_id",
)


def _compact(value: Any, *, limit: int = 600) -> Any:
    """Keep receipt details bounded even when a corrupt DB has huge strings."""

    if isinstance(value, Mapping):
        return {
            str(key)[:120]: _compact(child, limit=limit)
            for key, child in list(value.items())[:32]
        }
    if isinstance(value, (list, tuple)):
        return [_compact(child, limit=limit) for child in list(value)[:32]]
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "..."
    return value


def _reason(code: str, **details: Any) -> dict[str, Any]:
    result = {"code": str(code)}
    if details:
        result["details"] = _compact(details)
    return result


def _json_mapping(name: str, raw: Any) -> Mapping[str, Any]:
    if not isinstance(raw, str):
        raise ContractError(f"{name}_json_invalid")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_json_invalid") from exc
    if not isinstance(value, Mapping):
        raise ContractError(f"{name}_mapping_required")
    return value


def _forbidden_paths(value: Any, path: str = "") -> list[str]:
    """Find market/price/anchor/bid/ask fields in an input tree.

    Model extrema are explicitly accepted as diagnostics.  A market token
    nested below an extrema diagnostic is still rejected: the exception is
    for the extrema field itself, not a covert market input attached to it.
    """

    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in list(value.items())[:2048]:
            name = str(key)
            lowered = name.strip().lower().replace("-", "_")
            child_path = f"{path}.{name}" if path else name
            key_tokens = {
                token for token in lowered.replace("-", "_").split("_") if token
            }
            path_tokens = {
                token
                for token in path.lower().replace("-", "_").split("_")
                if token
            }
            explicit_dependency = (
                lowered in _EXPLICIT_DIRECTION_KEYS
                or (
                    bool(key_tokens.intersection(_MARKET_DEPENDENCY_TOKENS))
                    and bool(path_tokens.intersection(_DIRECTION_CONTEXT_TOKENS))
                )
                or (
                    "direction" in key_tokens
                    and bool(key_tokens.intersection(_MARKET_DEPENDENCY_TOKENS))
                )
                or (
                    "market" in key_tokens
                    and bool(key_tokens.intersection({"anchor", "price", "bid", "ask"}))
                )
            )
            if explicit_dependency:
                found.append(child_path)
            # Traverse all children, including model diagnostics, so a nested
            # price/anchor cannot be hidden under an otherwise permitted key.
            found.extend(_forbidden_paths(child, child_path))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(list(value)[:2048]):
            found.extend(_forbidden_paths(child, f"{path}[{index}]"))
    return found[:32]


def _envelope_from_row(row: sqlite3.Row) -> ProbabilityEnvelope:
    raw: dict[str, Any] = {
        "schema": "bayes_basket_probability_envelope_v1",
        "stage": row["stage"],
        "checkpoint_id": row["checkpoint_id"],
        "station_id": row["station_id"],
        "event_date": row["event_date"],
        "settlement_timezone": row["settlement_timezone"],
        "decision_at_utc": row["decision_at_utc"],
        "fresh_until_utc": row["fresh_until_utc"],
        "bucket_ordinals": _json_mapping("bucket_ordinals", row["bucket_ordinals_json"]),
        "probabilities": _json_mapping("probabilities", row["probabilities_json"]),
        "posterior_version": row["posterior_version"],
        "posterior_sha256": row["posterior_sha256"],
        "bridge_sha256": row["bridge_sha256"],
        "lineage": _json_mapping("lineage", row["lineage_json"]),
    }
    forbidden = _forbidden_paths(raw)
    if forbidden:
        raise ContractError("direction_market_field_forbidden:" + ",".join(forbidden))
    envelope = ProbabilityEnvelope.from_mapping(raw)
    if str(row["envelope_id"] or "") != envelope.checkpoint_id:
        raise ContractError("probability_envelope_storage_identity_mismatch")
    if str(row["content_sha256"] or "").lower() != envelope.content_sha256:
        raise ContractError("probability_envelope_content_sha_mismatch")
    lineage_timezone = str(envelope.lineage.get("settlement_timezone") or "").strip()
    if not lineage_timezone or lineage_timezone != envelope.settlement_timezone:
        raise ContractError("probability_settlement_timezone_lineage_mismatch")
    for key in _LINEAGE_STAGE_KEYS:
        if key in envelope.lineage and str(envelope.lineage[key]).strip() != envelope.stage:
            raise ContractError("probability_stage_lineage_mismatch")
    for key in _LINEAGE_CHECKPOINT_KEYS:
        if key in envelope.lineage and str(envelope.lineage[key]).strip() != envelope.checkpoint_id:
            raise ContractError("probability_checkpoint_lineage_mismatch")
    return envelope


def _regime_from_row(row: sqlite3.Row) -> tuple[RegimeArtifact, Mapping[str, Any]]:
    raw = _json_mapping("regime_artifact", row["artifact_json"])
    forbidden = _forbidden_paths(raw)
    if forbidden:
        raise ContractError("direction_market_field_forbidden:" + ",".join(forbidden))
    artifact = RegimeArtifact.from_mapping(raw)
    storage_scope = (
        str(row["station_id"] or ""),
        str(row["event_date"] or ""),
        str(row["stage"] or ""),
        str(row["checkpoint_id"] or ""),
        str(row["settlement_timezone"] or ""),
    )
    artifact_scope = (
        artifact.station_id,
        artifact.event_date,
        artifact.stage,
        artifact.checkpoint_id,
        artifact.settlement_timezone,
    )
    if storage_scope != artifact_scope:
        raise ContractError("regime_storage_scope_mismatch")
    if str(row["artifact_id"] or "") != artifact.artifact_id:
        raise ContractError("regime_storage_identity_mismatch")
    if str(row["probability_sha256"] or "").lower() != artifact.probability_sha256:
        raise ContractError("regime_storage_probability_hash_mismatch")
    if str(row["content_sha256"] or "").lower() != artifact.content_sha256:
        raise ContractError("regime_storage_content_hash_mismatch")
    return artifact, raw


def _direction_diagnostics(envelope: ProbabilityEnvelope) -> dict[str, Any]:
    """Derive all directional fields solely from this envelope's p_s(B)."""

    center = envelope.center_bucket
    center_ordinal = envelope.bucket_ordinals[center]
    ranked = sorted(
        envelope.probabilities,
        key=lambda bucket: (
            -float(envelope.probabilities[bucket]),
            int(envelope.bucket_ordinals[bucket]),
            str(bucket),
        ),
    )
    runner_up = ranked[1] if len(ranked) > 1 else None
    lead = (
        float(envelope.probabilities[center])
        - float(envelope.probabilities[runner_up])
        if runner_up is not None
        else 1.0
    )
    lead = max(0.0, lead)
    lower = sum(
        float(value)
        for bucket, value in envelope.probabilities.items()
        if envelope.bucket_ordinals[bucket] < center_ordinal
    )
    upper = sum(
        float(value)
        for bucket, value in envelope.probabilities.items()
        if envelope.bucket_ordinals[bucket] > center_ordinal
    )
    skew = upper - lower
    stable = lead + _TOLERANCE >= STABILITY_THRESHOLD
    direction = (
        "NEUTRAL"
        if not stable or abs(skew) < DIRECTION_THRESHOLD
        else "UP"
        if skew > 0.0
        else "DOWN"
    )
    return {
        "center_bucket": center,
        "runner_up_bucket": runner_up,
        "center_lead_probability": lead,
        "center_stable": stable,
        "stability_threshold": STABILITY_THRESHOLD,
        "side_mass_down": lower,
        "side_mass_up": upper,
        "direction_mass_margin": skew,
        "direction_threshold": DIRECTION_THRESHOLD,
        "direction": direction,
        "probability_vector_sha256": content_sha256(
            {
                "bucket_ordinals": dict(envelope.bucket_ordinals),
                "probabilities": dict(envelope.probabilities),
            }
        ),
    }


def _compare_number(left: float, right: float) -> bool:
    return abs(float(left) - float(right)) <= _TOLERANCE


def _verify_stage_bindings(
    selected: Mapping[str, ProbabilityEnvelope],
    known_checkpoints: set[str],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Verify stage/checkpoint identity without requiring p_s(B) to differ.

    A TAF confirmation or an unchanged official observation may legitimately
    leave the numeric vector unchanged.  Independent calculation is therefore
    proven by the immutable stage/checkpoint identity and, when supplied, the
    explicit stage lineage and parent/root checkpoint bindings.
    """

    blockers: list[dict[str, Any]] = []
    selected_stages = tuple(
        stage for stage in STAGES if stage in selected
    )
    stage_ids = [selected[stage].stage for stage in selected_stages]
    checkpoint_ids = [
        selected[stage].checkpoint_id for stage in selected_stages
    ]
    checks = {
        "distinct_stage_ids": len(set(stage_ids)) == len(selected_stages),
        "distinct_checkpoint_ids": (
            len(set(checkpoint_ids)) == len(selected_stages)
        ),
    }
    calculation_ids = [
        str(
            selected[stage].lineage.get("stage_calculation_id")
            or selected[stage].lineage.get("probability_calculation_id")
            or selected[stage].lineage.get("calculation_id")
            or selected[stage].checkpoint_id
        ).strip()
        for stage in selected_stages
    ]
    checks["stage_calculation_lineage_distinct"] = (
        len(set(calculation_ids)) == len(selected_stages)
    )
    for name, passed in checks.items():
        if not passed:
            blockers.append(_reason("stage_checkpoint_identity_invalid", check=name))

    p0_checkpoint = selected["P0"].checkpoint_id
    for stage in selected_stages:
        envelope = selected[stage]
        lineage = envelope.lineage
        root = str(lineage.get("p0_root_checkpoint_id") or "").strip()
        if root:
            expected_root = envelope.checkpoint_id if stage == "P0" else p0_checkpoint
            if root != expected_root or root not in known_checkpoints:
                blockers.append(
                    _reason(
                        "stage_parent_binding_mismatch",
                        stage=stage,
                        field="p0_root_checkpoint_id",
                        expected=expected_root,
                        actual=root,
                    )
                )
        predecessor = str(lineage.get("predecessor_checkpoint_id") or "").strip()
        if predecessor:
            if predecessor == envelope.checkpoint_id or predecessor not in known_checkpoints:
                blockers.append(
                    _reason(
                        "stage_parent_binding_mismatch",
                        stage=stage,
                        field="predecessor_checkpoint_id",
                        actual=predecessor,
                    )
                )

    return {
        "stage_ids_distinct": checks["distinct_stage_ids"],
        "checkpoint_ids_distinct": checks["distinct_checkpoint_ids"],
        "stage_calculation_lineage_distinct": checks["stage_calculation_lineage_distinct"],
        "numeric_vector_reuse_allowed": True,
        "lineage_parent_bindings_checked": True,
    }, blockers


def _verify_regime(
    envelope: ProbabilityEnvelope,
    artifact: RegimeArtifact,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    expected = _direction_diagnostics(envelope)
    blockers: list[dict[str, Any]] = []
    checks = {
        "stage": artifact.stage == envelope.stage,
        "scope": (
            artifact.station_id == envelope.station_id
            and artifact.event_date == envelope.event_date
            and artifact.checkpoint_id == envelope.checkpoint_id
            and artifact.settlement_timezone == envelope.settlement_timezone
        ),
        "probability_binding": artifact.probability_sha256 == envelope.content_sha256,
        "center_bucket": artifact.center_lead_margin >= 0.0,
        "center_lead": _compare_number(
            artifact.center_lead_margin, expected["center_lead_probability"]
        ),
        "stability_threshold": _compare_number(
            artifact.stability_threshold, STABILITY_THRESHOLD
        ),
        "center_stability": artifact.center_stable is expected["center_stable"],
        "side_mass_down": _compare_number(
            artifact.side_mass_down, expected["side_mass_down"]
        ),
        "side_mass_up": _compare_number(
            artifact.side_mass_up, expected["side_mass_up"]
        ),
        "direction_mass_margin": _compare_number(
            artifact.direction_mass_margin, expected["direction_mass_margin"]
        ),
        "direction_threshold": _compare_number(
            artifact.direction_threshold, DIRECTION_THRESHOLD
        ),
        "direction": artifact.regime == expected["direction"],
    }
    for name, passed in checks.items():
        if not passed:
            blockers.append(_reason("regime_probability_check_failed", check=name))

    # Independently rebuild the artifact.  This catches an internally
    # self-consistent but probability-inconsistent stored regime payload.
    try:
        rebuilt = build_regime_artifact(
            envelope,
            stability_threshold=STABILITY_THRESHOLD,
            direction_threshold=DIRECTION_THRESHOLD,
            concentration_multiplier=artifact.concentration_multiplier,
        )
        if rebuilt.to_dict() != artifact.to_dict():
            blockers.append(_reason("regime_independent_rebuild_mismatch"))
    except (ContractError, TypeError, ValueError) as exc:
        blockers.append(_reason("regime_independent_rebuild_failed", error=str(exc)))

    return {
        "checks": checks,
        "expected": expected,
        "stored": {
            "regime": artifact.regime,
            "center_lead_margin": artifact.center_lead_margin,
            "center_stable": artifact.center_stable,
            "side_mass_down": artifact.side_mass_down,
            "side_mass_up": artifact.side_mass_up,
            "direction_mass_margin": artifact.direction_mass_margin,
        },
        "model_extrema_diagnostic_only": True,
    }, blockers


def _schema_check(conn: sqlite3.Connection) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    try:
        status = dict(production_ssot_schema_status(conn))
    except (sqlite3.Error, TypeError, ValueError) as exc:
        return {"schema": None, "ok": False}, [_reason("v4_schema_probe_failed", error=str(exc))]
    blockers: list[dict[str, Any]] = []
    if status.get("schema") != SCHEMA_VERSION:
        blockers.append(_reason("v4_schema_required", actual=status.get("schema")))
    if status.get("ok") is not True:
        blockers.append(_reason("v4_schema_incomplete", status=status))
    return status, blockers


def _read_rows(
    conn: sqlite3.Connection,
    *,
    table: str,
    columns: str,
    limit: int,
) -> tuple[list[sqlite3.Row], int, list[dict[str, Any]]]:
    """Count first, then fetch at most ``limit`` payload rows."""

    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE state='COMMITTED'"
        ).fetchone()[0]
    )
    blockers: list[dict[str, Any]] = []
    if total > limit:
        blockers.append(_reason(f"{table}_read_limit_exceeded", total=total, limit=limit))
        return [], total, blockers
    if table == "basket_probability_envelope":
        size_guard = (
            "length(bucket_ordinals_json)<=? AND length(probabilities_json)<=? "
            "AND length(lineage_json)<=?"
        )
        order_by = "station_id,event_date,stage,decision_at_utc,checkpoint_id"
    else:
        size_guard = "length(artifact_json)<=?"
        order_by = "station_id,event_date,stage,valid_from_utc,checkpoint_id,artifact_id"
    rows = conn.execute(
        f"SELECT {columns} FROM {table} WHERE state='COMMITTED' "
        f"AND {size_guard} ORDER BY {order_by}",
        (MAX_JSON_FIELD_BYTES,) * (3 if table == "basket_probability_envelope" else 1),
    ).fetchall()
    if len(rows) != total:
        return rows, total, [
            _reason(
                f"{table}_payload_size_limit_exceeded",
                max_bytes=MAX_JSON_FIELD_BYTES,
                loaded=len(rows),
                committed=total,
            )
        ]
    # The count-first invariant makes this a defensive assertion, not a way
    # to consume an unbounded result after the query.
    if len(rows) > limit:
        return [], total, [_reason(f"{table}_read_limit_exceeded", limit=limit)]
    return rows, total, blockers


def _audit_connection(
    conn: sqlite3.Connection,
    *,
    max_envelopes: int = MAX_ENVELOPES,
    max_regime_artifacts: int = MAX_REGIME_ARTIFACTS,
    db_path: str = "",
) -> dict[str, Any]:
    if max_envelopes < 1 or max_envelopes > MAX_ENVELOPES:
        max_envelopes = MAX_ENVELOPES
    if max_regime_artifacts < 1 or max_regime_artifacts > MAX_REGIME_ARTIFACTS:
        max_regime_artifacts = MAX_REGIME_ARTIFACTS
    blockers: list[dict[str, Any]] = []
    try:
        conn.execute("PRAGMA query_only=ON")
        query_only = int(conn.execute("PRAGMA query_only").fetchone()[0]) == 1
    except sqlite3.Error as exc:
        query_only = False
        blockers.append(_reason("query_only_failed", error=str(exc)))
    schema, schema_blockers = _schema_check(conn)
    blockers.extend(schema_blockers)
    envelope_columns = (
        "envelope_id,stage,checkpoint_id,station_id,event_date,"
        "settlement_timezone,decision_at_utc,fresh_until_utc,"
        "bucket_ordinals_json,probabilities_json,posterior_version,"
        "posterior_sha256,bridge_sha256,lineage_json,content_sha256"
    )
    regime_columns = (
        "artifact_id,station_id,event_date,stage,checkpoint_id,"
        "settlement_timezone,probability_sha256,artifact_json,"
        "content_sha256,state"
    )
    try:
        envelope_rows, envelope_total, read_blockers = _read_rows(
            conn,
            table="basket_probability_envelope",
            columns=envelope_columns,
            limit=max_envelopes,
        )
        regime_rows, regime_total, regime_read_blockers = _read_rows(
            conn,
            table="basket_regime_artifact",
            columns=regime_columns,
            limit=max_regime_artifacts,
        )
        blockers.extend(read_blockers)
        blockers.extend(regime_read_blockers)
    except sqlite3.Error as exc:
        envelope_rows, regime_rows = [], []
        envelope_total = regime_total = 0
        blockers.append(_reason("ssot_read_failed", error=str(exc)))

    envelopes: list[ProbabilityEnvelope] = []
    envelope_errors: list[dict[str, Any]] = []
    for row in envelope_rows:
        try:
            envelopes.append(_envelope_from_row(row))
        except (ContractError, TypeError, ValueError, KeyError) as exc:
            envelope_errors.append(
                _reason("probability_envelope_invalid", checkpoint_id=row["checkpoint_id"], error=str(exc))
            )
    blockers.extend(envelope_errors)

    regimes: dict[tuple[str, str, str, str, str], list[RegimeArtifact]] = defaultdict(list)
    regime_errors: list[dict[str, Any]] = []
    for row in regime_rows:
        try:
            artifact, _raw = _regime_from_row(row)
            key = (
                artifact.station_id,
                artifact.event_date,
                artifact.stage,
                artifact.checkpoint_id,
                artifact.probability_sha256,
            )
            regimes[key].append(artifact)
        except (ContractError, TypeError, ValueError, KeyError) as exc:
            regime_errors.append(
                _reason("regime_artifact_invalid", artifact_id=row["artifact_id"], error=str(exc))
            )
    blockers.extend(regime_errors)
    first_regime_event_date: dict[tuple[str, str], str] = {}
    for artifacts in regimes.values():
        for artifact in artifacts:
            key = (artifact.station_id, artifact.settlement_timezone)
            previous = first_regime_event_date.get(key)
            if previous is None or artifact.event_date < previous:
                first_regime_event_date[key] = artifact.event_date

    grouped: dict[tuple[str, str, str], dict[str, list[ProbabilityEnvelope]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for envelope in envelopes:
        grouped[(envelope.station_id, envelope.event_date, envelope.settlement_timezone)][
            envelope.stage
        ].append(envelope)

    scopes: list[dict[str, Any]] = []
    for scope, by_stage in sorted(grouped.items()):
        scope_blockers: list[dict[str, Any]] = []
        selected: dict[str, ProbabilityEnvelope] = {}
        p1_candidates = sorted(
            by_stage.get("P1", []),
            key=lambda item: (item.decision_at_utc, item.checkpoint_id),
        )
        p0_candidates = sorted(
            by_stage.get("P0", []),
            key=lambda item: (item.decision_at_utc, item.checkpoint_id),
        )
        if not p1_candidates:
            scope_blockers.append(
                _reason("stage_envelope_missing", stage="P1")
            )
        if not p0_candidates:
            scope_blockers.append(
                _reason("stage_envelope_missing", stage="P0")
            )
        if p1_candidates and p0_candidates:
            selected["P1"] = p1_candidates[-1]
            p0_root = str(
                selected["P1"].lineage.get("p0_root_checkpoint_id")
                or ""
            ).strip()
            rooted_p0 = [
                item
                for item in p0_candidates
                if item.checkpoint_id == p0_root
            ]
            selected["P0"] = (
                rooted_p0[-1] if rooted_p0 else p0_candidates[-1]
            )
            p0m_candidates = sorted(
                (
                    item
                    for item in by_stage.get("P0-M", [])
                    if str(
                        item.lineage.get("p0_root_checkpoint_id") or ""
                    ).strip()
                    == selected["P0"].checkpoint_id
                    and item.decision_at_utc
                    <= selected["P1"].decision_at_utc
                ),
                key=lambda item: (
                    item.decision_at_utc,
                    item.checkpoint_id,
                ),
            )
            if p0m_candidates:
                selected["P0-M"] = p0m_candidates[-1]
        stage_reports: dict[str, Any] = {}
        stage_binding_report: dict[str, Any] = {}
        if all(stage in selected for stage in REQUIRED_STAGES):
            known_checkpoints = {
                envelope.checkpoint_id
                for candidates in by_stage.values()
                for envelope in candidates
            }
            binding_report, binding_errors = _verify_stage_bindings(
                selected, known_checkpoints
            )
            scope_blockers.extend(binding_errors)
            stage_binding_report = binding_report

            for stage in STAGES:
                if stage not in selected:
                    continue
                envelope = selected[stage]
                key = (
                    envelope.station_id,
                    envelope.event_date,
                    envelope.stage,
                    envelope.checkpoint_id,
                    envelope.content_sha256,
                )
                matches = regimes.get(key, [])
                if len(matches) != 1:
                    first_regime_day = first_regime_event_date.get(
                        (
                            envelope.station_id,
                            envelope.settlement_timezone,
                        )
                    )
                    if (
                        not matches
                        and first_regime_day is not None
                        and envelope.event_date < first_regime_day
                    ):
                        stage_reports[stage] = {
                            "status": "NOT_APPLICABLE_PRE_REGIME",
                            "envelope_id": envelope.checkpoint_id,
                            "first_regime_event_date": first_regime_day,
                        }
                        continue
                    stage_reports[stage] = {"status": "BLOCKED", "envelope_id": envelope.checkpoint_id}
                    scope_blockers.append(
                        _reason("regime_exactly_one_required", stage=stage, matches=len(matches))
                    )
                    continue
                report, stage_errors = _verify_regime(envelope, matches[0])
                stage_reports[stage] = {
                    "status": "PASS" if not stage_errors else "BLOCKED",
                    "envelope_id": envelope.checkpoint_id,
                    "direction": report,
                }
                scope_blockers.extend(
                    _reason(item["code"], stage=stage, **item.get("details", {}))
                    for item in stage_errors
                )
        scopes.append(
            {
                "station_id": scope[0],
                "event_date": scope[1],
                "settlement_timezone": scope[2],
                "status": "PASS" if not scope_blockers else "BLOCKED",
                "stage_path": [
                    stage for stage in STAGES if stage in selected
                ],
                "stage_binding": stage_binding_report,
                "stages": stage_reports,
                "blockers": scope_blockers,
            }
        )
        blockers.extend(
            _reason(item["code"], scope=scope, **item.get("details", {}))
            for item in scope_blockers
        )

    if not envelopes:
        blockers.append(_reason("no_committed_probability_envelopes"))
    if not query_only:
        blockers.append(_reason("query_only_not_enabled"))
    # De-duplicate only identical small reason objects; preserve first-seen
    # order so a receipt remains deterministic and bounded.
    unique_blockers: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in blockers:
        marker = json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        if marker not in seen:
            seen.add(marker)
            unique_blockers.append(item)
    return {
        "schema": RECEIPT_SCHEMA,
        "status": "PASS" if not unique_blockers else "BLOCKED",
        "ok": not unique_blockers,
        "db_path": str(db_path),
        "read_only": True,
        "query_only": query_only,
        "limits": {
            "max_envelopes": MAX_ENVELOPES,
            "max_regime_artifacts": MAX_REGIME_ARTIFACTS,
        },
        "counts": {
            "committed_envelopes": envelope_total,
            "committed_regime_artifacts": regime_total,
            "envelopes_loaded": len(envelope_rows),
            "regime_artifacts_loaded": len(regime_rows),
            "scopes": len(scopes),
        },
        "schema_check": _compact(schema),
        "scopes": _compact(scopes),
        "blockers": _compact(unique_blockers),
    }


def audit_three_stage_direction(
    db_path: str | Path = DEFAULT_DB,
    *,
    max_envelopes: int = MAX_ENVELOPES,
    max_regime_artifacts: int = MAX_REGIME_ARTIFACTS,
) -> dict[str, Any]:
    """Audit a database through a SQLite URI opened in read-only mode."""

    path = Path(db_path).expanduser()
    receipt: dict[str, Any]
    conn: sqlite3.Connection | None = None
    try:
        if not path.is_file():
            return {
                "schema": RECEIPT_SCHEMA,
                "status": "BLOCKED",
                "ok": False,
                "db_path": str(path),
                "read_only": True,
                "query_only": False,
                "limits": {"max_envelopes": MAX_ENVELOPES, "max_regime_artifacts": MAX_REGIME_ARTIFACTS},
                "counts": {},
                "schema_check": {},
                "scopes": [],
                "blockers": [_reason("database_missing")],
            }
        conn = sqlite3.connect(
            f"file:{path.resolve()}?mode=ro",
            uri=True,
            timeout=1.0,
        )
        conn.row_factory = sqlite3.Row
        receipt = _audit_connection(
            conn,
            max_envelopes=max_envelopes,
            max_regime_artifacts=max_regime_artifacts,
            db_path=str(path),
        )
    except (sqlite3.Error, OSError, ValueError) as exc:
        receipt = {
            "schema": RECEIPT_SCHEMA,
            "status": "BLOCKED",
            "ok": False,
            "db_path": str(path),
            "read_only": True,
            "query_only": False,
            "limits": {"max_envelopes": MAX_ENVELOPES, "max_regime_artifacts": MAX_REGIME_ARTIFACTS},
            "counts": {},
            "schema_check": {},
            "scopes": [],
            "blockers": [_reason("database_open_failed", error=str(exc))],
        }
    finally:
        if conn is not None:
            conn.close()
    return receipt


# Explicit aliases make the small audit convenient to call from existing
# audit runners without creating a second implementation.
audit_bayes_basket_target_three_stage_direction = audit_three_stage_direction


def write_bounded_receipt(receipt: Mapping[str, Any], output: str | Path) -> Path:
    """Write at most 1 MiB of JSON; this function never opens a database."""

    target = Path(output).expanduser()
    payload = json.dumps(_compact(receipt), ensure_ascii=False, sort_keys=True, indent=2)
    if len(payload.encode("utf-8")) > 1_000_000:
        minimal = {
            "schema": RECEIPT_SCHEMA,
            "status": "BLOCKED",
            "ok": False,
            "receipt_truncated": True,
            "blockers": [_reason("receipt_size_limit_exceeded")],
        }
        payload = json.dumps(minimal, ensure_ascii=False, sort_keys=True, indent=2)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(payload + "\n", encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args(argv)
    receipt = audit_three_stage_direction(args.db)
    if args.output is not None:
        write_bounded_receipt(receipt, args.output)
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
