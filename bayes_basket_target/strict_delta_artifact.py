"""Versioned OOS authority for the strict incremental basket objective.

Runtime code may consume :class:`StrictDeltaPolicy` only through an append-only
artifact, promotion receipt and as-of activation stored in the target SSOT.
There is deliberately no mapping/config fallback on the production adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
import json
import math
import sqlite3
from typing import Any, Mapping

from .basket_kernel.strict_delta import StrictDeltaPolicy
from .contracts import ContractError, content_sha256


ARTIFACT_SCHEMA = "strict_delta_policy_artifact_v1"
PROMOTION_SCHEMA = "strict_delta_policy_promotion_v1"
ACTIVATION_SCHEMA = "strict_delta_policy_activation_v1"
AUTHORITY_SCOPES = frozenset({
    "PRODUCTION_NO_LIVE_CANDIDATE",
    "FORMAL_PROMOTED",
})
STAGES = frozenset({"*", "P0", "P0-M", "P1"})


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractError(f"{name}_must_be_utc")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _date(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
        raise ContractError(f"{name}_invalid")
    return result


def _positive_int(name: str, value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if result <= 0:
        raise ContractError(f"{name}_must_be_positive")
    return result


def _metrics(value: Any, *, allow_empty: bool = False) -> dict[str, float]:
    if not isinstance(value, Mapping) or (not value and not allow_empty):
        raise ContractError("strict_delta_oos_metrics_missing")
    result: dict[str, float] = {}
    for key, raw in value.items():
        name = _text("strict_delta_oos_metric_name", key)
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ContractError("strict_delta_oos_metric_invalid") from exc
        if not math.isfinite(number):
            raise ContractError("strict_delta_oos_metric_invalid")
        result[name] = number
    return result


def _nonnegative_int(name: str, value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if result < 0:
        raise ContractError(f"{name}_must_be_nonnegative")
    return result


def _strict_keys(
    raw: Mapping[str, Any],
    *,
    allowed: frozenset[str],
    contract: str,
) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ContractError(f"{contract}_unknown_fields:" + ",".join(unknown))


@dataclass(frozen=True)
class StrictDeltaPolicyArtifact:
    artifact_id: str
    station_id: str
    stage: str
    season_bin: str
    maturity_bin: str
    liquidity_bin: str
    strategy_version: str
    policy: StrictDeltaPolicy
    training_evidence_sha256: str
    train_start_date: str
    train_end_date: str
    holdout_start_date: str
    holdout_end_date: str
    sample_n: int
    holdout_n: int
    oos_metrics: Mapping[str, float]
    oos_gate_version: str
    oos_gate_passed: bool
    trained_through_utc: str
    valid_from_utc: str
    valid_until_utc: str
    authority_scope: str
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "StrictDeltaPolicyArtifact":
        if raw.get("schema") not in {None, ARTIFACT_SCHEMA}:
            raise ContractError("strict_delta_artifact_schema_invalid")
        policy_raw = raw.get("policy")
        if isinstance(policy_raw, str):
            try:
                policy_raw = json.loads(policy_raw)
            except ValueError as exc:
                raise ContractError("strict_delta_policy_json_invalid") from exc
        if not isinstance(policy_raw, Mapping):
            raise ContractError("strict_delta_policy_mapping_required")
        stage = _text("strict_delta_stage", raw.get("stage")).upper()
        if stage not in STAGES:
            raise ContractError("strict_delta_stage_invalid")
        authority_scope = _text(
            "strict_delta_authority_scope", raw.get("authority_scope")
        )
        if authority_scope not in AUTHORITY_SCOPES:
            raise ContractError("strict_delta_authority_scope_invalid")
        train_start = _date("strict_delta_train_start_date", raw.get("train_start_date"))
        train_end = _date("strict_delta_train_end_date", raw.get("train_end_date"))
        holdout_start = _date(
            "strict_delta_holdout_start_date", raw.get("holdout_start_date")
        )
        holdout_end = _date(
            "strict_delta_holdout_end_date", raw.get("holdout_end_date")
        )
        if not train_start <= train_end < holdout_start <= holdout_end:
            raise ContractError("strict_delta_oos_window_invalid")
        trained_through = _timestamp(
            "strict_delta_trained_through_utc", raw.get("trained_through_utc")
        )
        valid_from = _timestamp(
            "strict_delta_valid_from_utc", raw.get("valid_from_utc")
        )
        valid_until = _timestamp(
            "strict_delta_valid_until_utc", raw.get("valid_until_utc")
        )
        if not trained_through < valid_from < valid_until:
            raise ContractError("strict_delta_artifact_time_order_invalid")
        oos_gate_passed = raw.get("oos_gate_passed") is True
        is_preregistered_candidate = (
            authority_scope == "PRODUCTION_NO_LIVE_CANDIDATE"
            and not oos_gate_passed
        )
        sample_n = (
            _nonnegative_int("strict_delta_sample_n", raw.get("sample_n"))
            if is_preregistered_candidate
            else _positive_int("strict_delta_sample_n", raw.get("sample_n"))
        )
        holdout_n = (
            _nonnegative_int("strict_delta_holdout_n", raw.get("holdout_n"))
            if is_preregistered_candidate
            else _positive_int("strict_delta_holdout_n", raw.get("holdout_n"))
        )
        oos_metrics = _metrics(
            raw.get("oos_metrics"),
            allow_empty=is_preregistered_candidate,
        )
        if is_preregistered_candidate and (
            sample_n != 0 or holdout_n != 0 or oos_metrics
        ):
            raise ContractError(
                "strict_delta_candidate_must_not_claim_oos_evidence"
            )
        value = cls(
            artifact_id=_text("strict_delta_artifact_id", raw.get("artifact_id")),
            station_id=_text("strict_delta_station_id", raw.get("station_id")).upper(),
            stage=stage,
            season_bin=_text("strict_delta_season_bin", raw.get("season_bin")),
            maturity_bin=_text("strict_delta_maturity_bin", raw.get("maturity_bin")),
            liquidity_bin=_text("strict_delta_liquidity_bin", raw.get("liquidity_bin")),
            strategy_version=_text(
                "strict_delta_strategy_version", raw.get("strategy_version")
            ),
            policy=StrictDeltaPolicy.from_mapping(policy_raw),
            training_evidence_sha256=_sha(
                "strict_delta_training_evidence_sha256",
                raw.get("training_evidence_sha256"),
            ),
            train_start_date=train_start,
            train_end_date=train_end,
            holdout_start_date=holdout_start,
            holdout_end_date=holdout_end,
            sample_n=sample_n,
            holdout_n=holdout_n,
            oos_metrics=oos_metrics,
            oos_gate_version=_text(
                "strict_delta_oos_gate_version", raw.get("oos_gate_version")
            ),
            oos_gate_passed=oos_gate_passed,
            trained_through_utc=trained_through,
            valid_from_utc=valid_from,
            valid_until_utc=valid_until,
            authority_scope=authority_scope,
            content_sha256=_sha(
                "strict_delta_artifact_content_sha256", raw.get("content_sha256")
            ),
        )
        if value.policy.policy_version != value.artifact_id:
            raise ContractError("strict_delta_policy_artifact_version_mismatch")
        if content_sha256(value.to_dict(include_content_sha256=False)) != value.content_sha256:
            raise ContractError("strict_delta_artifact_content_sha_mismatch")
        return value

    def to_dict(self, *, include_content_sha256: bool = True) -> dict[str, Any]:
        result = {
            "schema": ARTIFACT_SCHEMA,
            "artifact_id": self.artifact_id,
            "station_id": self.station_id,
            "stage": self.stage,
            "season_bin": self.season_bin,
            "maturity_bin": self.maturity_bin,
            "liquidity_bin": self.liquidity_bin,
            "strategy_version": self.strategy_version,
            "policy": self.policy.to_dict(),
            "training_evidence_sha256": self.training_evidence_sha256,
            "train_start_date": self.train_start_date,
            "train_end_date": self.train_end_date,
            "holdout_start_date": self.holdout_start_date,
            "holdout_end_date": self.holdout_end_date,
            "sample_n": self.sample_n,
            "holdout_n": self.holdout_n,
            "oos_metrics": dict(self.oos_metrics),
            "oos_gate_version": self.oos_gate_version,
            "oos_gate_passed": self.oos_gate_passed,
            "trained_through_utc": self.trained_through_utc,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "authority_scope": self.authority_scope,
        }
        if include_content_sha256:
            result["content_sha256"] = self.content_sha256
        return result


def validate_strict_delta_promotion(
    raw: Mapping[str, Any],
    *,
    artifact: StrictDeltaPolicyArtifact,
) -> dict[str, Any]:
    allowed = frozenset({
        "schema", "promotion_id", "artifact_id", "decision_seq", "decision",
        "gate_version", "decided_at_utc", "receipt_sha256",
        "content_sha256",
    })
    _strict_keys(raw, allowed=allowed, contract="strict_delta_promotion")
    if raw.get("schema") != PROMOTION_SCHEMA:
        raise ContractError("strict_delta_promotion_schema_invalid")
    result = {
        "schema": PROMOTION_SCHEMA,
        "promotion_id": _text(
            "strict_delta_promotion_id", raw.get("promotion_id")
        ),
        "artifact_id": _text(
            "strict_delta_promotion_artifact_id", raw.get("artifact_id")
        ),
        "decision_seq": _positive_int(
            "strict_delta_promotion_decision_seq", raw.get("decision_seq")
        ),
        "decision": _text(
            "strict_delta_promotion_decision", raw.get("decision")
        ).upper(),
        "gate_version": _text(
            "strict_delta_promotion_gate_version", raw.get("gate_version")
        ),
        "decided_at_utc": _timestamp(
            "strict_delta_promotion_decided_at_utc",
            raw.get("decided_at_utc"),
        ),
        "receipt_sha256": _sha(
            "strict_delta_promotion_receipt_sha256",
            raw.get("receipt_sha256"),
        ),
    }
    if result["artifact_id"] != artifact.artifact_id:
        raise ContractError("strict_delta_promotion_artifact_mismatch")
    if result["decision"] not in {
        "CANDIDATE",
        "PROMOTED",
        "REJECTED",
        "SUPERSEDED",
    }:
        raise ContractError("strict_delta_promotion_decision_invalid")
    if result["gate_version"] != artifact.oos_gate_version:
        raise ContractError("strict_delta_promotion_gate_version_mismatch")
    supplied = _sha(
        "strict_delta_promotion_content_sha256", raw.get("content_sha256")
    )
    if content_sha256(result) != supplied:
        raise ContractError("strict_delta_promotion_content_sha_mismatch")
    return {**result, "content_sha256": supplied}


def validate_strict_delta_activation(
    raw: Mapping[str, Any],
    *,
    artifact: StrictDeltaPolicyArtifact,
) -> dict[str, Any]:
    allowed = frozenset({
        "schema", "activation_id", "artifact_id", "station_id", "stage",
        "season_bin", "maturity_bin", "liquidity_bin", "strategy_version",
        "generation", "valid_from_utc", "valid_until_utc",
        "activated_at_utc", "authority_scope", "content_sha256",
    })
    _strict_keys(raw, allowed=allowed, contract="strict_delta_activation")
    if raw.get("schema") != ACTIVATION_SCHEMA:
        raise ContractError("strict_delta_activation_schema_invalid")
    result = {
        "schema": ACTIVATION_SCHEMA,
        "activation_id": _text(
            "strict_delta_activation_id", raw.get("activation_id")
        ),
        "artifact_id": _text(
            "strict_delta_activation_artifact_id", raw.get("artifact_id")
        ),
        "station_id": _text(
            "strict_delta_activation_station_id", raw.get("station_id")
        ).upper(),
        "stage": _text(
            "strict_delta_activation_stage", raw.get("stage")
        ).upper(),
        "season_bin": _text(
            "strict_delta_activation_season_bin", raw.get("season_bin")
        ),
        "maturity_bin": _text(
            "strict_delta_activation_maturity_bin", raw.get("maturity_bin")
        ),
        "liquidity_bin": _text(
            "strict_delta_activation_liquidity_bin", raw.get("liquidity_bin")
        ),
        "strategy_version": _text(
            "strict_delta_activation_strategy_version",
            raw.get("strategy_version"),
        ),
        "generation": _positive_int(
            "strict_delta_activation_generation", raw.get("generation")
        ),
        "valid_from_utc": _timestamp(
            "strict_delta_activation_valid_from_utc",
            raw.get("valid_from_utc"),
        ),
        "valid_until_utc": _timestamp(
            "strict_delta_activation_valid_until_utc",
            raw.get("valid_until_utc"),
        ),
        "activated_at_utc": _timestamp(
            "strict_delta_activation_activated_at_utc",
            raw.get("activated_at_utc"),
        ),
        "authority_scope": _text(
            "strict_delta_activation_authority_scope",
            raw.get("authority_scope"),
        ),
    }
    expected_scope = {
        "artifact_id": artifact.artifact_id,
        "station_id": artifact.station_id,
        "stage": artifact.stage,
        "season_bin": artifact.season_bin,
        "maturity_bin": artifact.maturity_bin,
        "liquidity_bin": artifact.liquidity_bin,
        "strategy_version": artifact.strategy_version,
        "authority_scope": artifact.authority_scope,
    }
    if any(result[key] != value for key, value in expected_scope.items()):
        raise ContractError("strict_delta_activation_artifact_scope_mismatch")
    if not (
        artifact.valid_from_utc
        <= result["valid_from_utc"]
        < result["valid_until_utc"]
        <= artifact.valid_until_utc
    ):
        raise ContractError("strict_delta_activation_window_invalid")
    if result["activated_at_utc"] > result["valid_from_utc"]:
        raise ContractError("strict_delta_activation_after_valid_from")
    supplied = _sha(
        "strict_delta_activation_content_sha256", raw.get("content_sha256")
    )
    if content_sha256(result) != supplied:
        raise ContractError("strict_delta_activation_content_sha_mismatch")
    return {**result, "content_sha256": supplied}


def _row_dict(
    row: sqlite3.Row | tuple[Any, ...],
    columns: tuple[str, ...],
) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        return {column: row[column] for column in columns}
    return {
        column: row[index]
        for index, column in enumerate(columns)
    }


def resolve_strict_delta_policy(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    stage: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    strategy_version: str,
    evaluation_at_utc: str,
    required_authority_scope: str,
) -> StrictDeltaPolicyArtifact:
    """Resolve one promoted exact-as-of policy; never synthesize a fallback."""

    station = _text("strict_delta_station_id", station_id).upper()
    current_stage = _text("strict_delta_stage", stage).upper()
    decision_at = _timestamp("strict_delta_evaluation_at_utc", evaluation_at_utc)
    if required_authority_scope not in AUTHORITY_SCOPES:
        raise ContractError("strict_delta_required_authority_scope_invalid")
    activation_columns = (
        "activation_id", "artifact_id", "station_id", "stage", "season_bin",
        "maturity_bin", "liquidity_bin", "strategy_version", "generation",
        "valid_from_utc", "valid_until_utc", "activated_at_utc",
        "authority_scope", "content_sha256",
    )
    rows = conn.execute(
        "SELECT " + ",".join(activation_columns)
        + " FROM basket_strict_delta_policy_activation"
        + " WHERE station_id IN (?, '*') AND stage IN (?, '*')"
        + " AND season_bin=? AND maturity_bin=? AND liquidity_bin=?"
        + " AND strategy_version=? AND valid_from_utc<=? AND valid_until_utc>?"
        + " AND authority_scope=?"
        + " ORDER BY CASE WHEN station_id=? THEN 0 ELSE 1 END,"
        + " CASE WHEN stage=? THEN 0 ELSE 1 END,generation DESC",
        (
            station, current_stage, season_bin, maturity_bin, liquidity_bin,
            strategy_version, decision_at, decision_at,
            required_authority_scope, station, current_stage,
        ),
    ).fetchall()
    if not rows:
        raise ContractError("strict_delta_activation_missing")
    best = _row_dict(rows[0], activation_columns)
    artifact_row = conn.execute(
        "SELECT artifact_json FROM basket_strict_delta_policy_artifact"
        " WHERE artifact_id=?",
        (best["artifact_id"],),
    ).fetchone()
    if artifact_row is None:
        raise ContractError("strict_delta_artifact_missing")
    artifact = StrictDeltaPolicyArtifact.from_mapping(json.loads(artifact_row[0]))
    validate_strict_delta_activation(
        {"schema": ACTIVATION_SCHEMA, **best},
        artifact=artifact,
    )
    if not artifact.valid_from_utc <= decision_at < artifact.valid_until_utc:
        raise ContractError("strict_delta_artifact_not_active_asof")
    promotion_columns = (
        "promotion_id", "artifact_id", "decision_seq", "decision",
        "gate_version", "decided_at_utc", "receipt_sha256", "content_sha256",
    )
    promotion = conn.execute(
        "SELECT " + ",".join(promotion_columns)
        + " FROM basket_strict_delta_policy_promotion"
        " WHERE artifact_id=? ORDER BY decision_seq DESC LIMIT 1",
        (artifact.artifact_id,),
    ).fetchone()
    if promotion is None:
        raise ContractError("strict_delta_artifact_not_promoted")
    promotion_raw = validate_strict_delta_promotion(
        {"schema": PROMOTION_SCHEMA, **_row_dict(promotion, promotion_columns)},
        artifact=artifact,
    )
    decision = str(promotion_raw["decision"])
    if required_authority_scope == "PRODUCTION_NO_LIVE_CANDIDATE":
        if decision not in {"CANDIDATE", "PROMOTED"}:
            raise ContractError("strict_delta_artifact_not_candidate_active")
        if decision == "CANDIDATE" and artifact.oos_gate_passed:
            raise ContractError("strict_delta_candidate_oos_state_invalid")
    elif decision != "PROMOTED":
        raise ContractError("strict_delta_artifact_not_promoted")
    if str(promotion_raw["gate_version"]) != artifact.oos_gate_version:
        raise ContractError("strict_delta_promotion_gate_version_mismatch")
    if decision == "PROMOTED" and not artifact.oos_gate_passed:
        raise ContractError("strict_delta_oos_gate_not_passed")
    return artifact


def commit_strict_delta_authority(
    conn: sqlite3.Connection,
    *,
    artifact: StrictDeltaPolicyArtifact | Mapping[str, Any],
    promotion: Mapping[str, Any],
    activation: Mapping[str, Any],
) -> dict[str, Any]:
    """Atomically append one validated artifact, promotion and activation."""

    governed = (
        artifact
        if isinstance(artifact, StrictDeltaPolicyArtifact)
        else StrictDeltaPolicyArtifact.from_mapping(artifact)
    )
    promoted = validate_strict_delta_promotion(promotion, artifact=governed)
    activated = validate_strict_delta_activation(activation, artifact=governed)
    own_transaction = not conn.in_transaction
    savepoint = "strict_delta_authority_ingress"
    if own_transaction:
        conn.execute("BEGIN IMMEDIATE")
    else:
        conn.execute(f"SAVEPOINT {savepoint}")
    try:
        before = conn.total_changes
        conn.execute(
            "INSERT OR IGNORE INTO basket_strict_delta_policy_artifact VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                governed.artifact_id, governed.station_id, governed.stage,
                governed.season_bin, governed.maturity_bin,
                governed.liquidity_bin, governed.strategy_version,
                governed.trained_through_utc, governed.valid_from_utc,
                governed.valid_until_utc, governed.authority_scope,
                json.dumps(
                    governed.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                governed.content_sha256,
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO basket_strict_delta_policy_promotion VALUES"
            "(?,?,?,?,?,?,?,?)",
            tuple(
                promoted[key]
                for key in (
                    "promotion_id", "artifact_id", "decision_seq", "decision",
                    "gate_version", "decided_at_utc", "receipt_sha256",
                    "content_sha256",
                )
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO basket_strict_delta_policy_activation VALUES"
            "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(
                activated[key]
                for key in (
                    "activation_id", "artifact_id", "station_id", "stage",
                    "season_bin", "maturity_bin", "liquidity_bin",
                    "strategy_version", "generation", "valid_from_utc",
                    "valid_until_utc", "activated_at_utc",
                    "authority_scope", "content_sha256",
                )
            ),
        )
        stored = (
            conn.execute(
                "SELECT content_sha256 FROM basket_strict_delta_policy_artifact"
                " WHERE artifact_id=?",
                (governed.artifact_id,),
            ).fetchone(),
            conn.execute(
                "SELECT content_sha256 FROM basket_strict_delta_policy_promotion"
                " WHERE promotion_id=?",
                (promoted["promotion_id"],),
            ).fetchone(),
            conn.execute(
                "SELECT content_sha256 FROM basket_strict_delta_policy_activation"
                " WHERE activation_id=?",
                (activated["activation_id"],),
            ).fetchone(),
        )
        expected = (
            governed.content_sha256,
            promoted["content_sha256"],
            activated["content_sha256"],
        )
        if any(
            row is None or str(row[0]) != expected[index]
            for index, row in enumerate(stored)
        ):
            raise ContractError("strict_delta_authority_idempotency_drift")
        wrote = conn.total_changes - before
        if own_transaction:
            conn.commit()
        else:
            conn.execute(f"RELEASE {savepoint}")
    except Exception:
        if own_transaction:
            conn.rollback()
        else:
            conn.execute(f"ROLLBACK TO {savepoint}")
            conn.execute(f"RELEASE {savepoint}")
        raise
    return {
        "schema": "strict_delta_authority_ingress_receipt_v1",
        "artifact_id": governed.artifact_id,
        "activation_id": activated["activation_id"],
        "rows_written": wrote,
        "state": "COMMITTED" if wrote else "UNCHANGED_ZERO_WRITE",
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
    }


__all__ = [
    "ACTIVATION_SCHEMA",
    "ARTIFACT_SCHEMA",
    "PROMOTION_SCHEMA",
    "StrictDeltaPolicyArtifact",
    "commit_strict_delta_authority",
    "resolve_strict_delta_policy",
    "validate_strict_delta_activation",
    "validate_strict_delta_promotion",
]
