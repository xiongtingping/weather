"""Immutable deployment authority for ``DeltaBasketScore v2``.

The v2 runtime has no mapping/config fallback.  A production-no-live consumer
may resolve an exact-as-of artifact with a ``NO_LIVE_ACTIVATED`` receipt and
an activation in the same authority scope.  Forward OOS is produced by that
real no-live run and therefore cannot be a prerequisite for it.  ``PROMOTED``
remains the stricter OOS-passed tier for later parameter/live promotion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
import json
import math
import sqlite3
from typing import Any, Mapping, Sequence

from .basket_kernel.contracts import ContractError, content_sha256
from .basket_kernel.delta_v2 import ExecutionWeightsV2, ValueWeightsV2


ARTIFACT_SCHEMA = "delta_basket_score_policy_artifact_v2"
PROMOTION_SCHEMA = "delta_basket_score_policy_promotion_v2"
ACTIVATION_SCHEMA = "delta_basket_score_policy_activation_v2"
AUTHORITY_SCOPES = frozenset({"PRODUCTION_NO_LIVE", "FORMAL_PROMOTED"})
STAGES = frozenset({"*", "P0", "P0-M", "P1"})
REQUIRED_BASELINES = frozenset({"v1", "EV_ONLY", "ZERO_WEIGHT", "HOLD"})
NO_LIVE_ACTIVATION_DECISION = "NO_LIVE_ACTIVATED"


def _text(name: str, value: Any) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"{name}_missing")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
        raise ContractError(f"{name}_invalid")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    if parsed.tzinfo is None:
        raise ContractError(f"{name}_timezone_required")
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed):
        raise ContractError(f"{name}_must_be_utc")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _date(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        date.fromisoformat(result)
    except ValueError as exc:
        raise ContractError(f"{name}_invalid") from exc
    return result


def _positive_int(name: str, value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if result <= 0:
        raise ContractError(f"{name}_nonpositive")
    return result


def _nonnegative_int(name: str, value: Any) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ContractError(f"{name}_invalid") from exc
    if result < 0:
        raise ContractError(f"{name}_negative")
    return result


def _positive_grid(name: str, value: Any, *, below_one: bool = False) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ContractError(f"{name}_invalid")
    result: list[float] = []
    for raw in value:
        if isinstance(raw, bool):
            raise ContractError(f"{name}_invalid")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ContractError(f"{name}_invalid") from exc
        if not math.isfinite(number) or number <= 0.0:
            raise ContractError(f"{name}_invalid")
        if below_one and number >= 1.0:
            raise ContractError(f"{name}_must_be_below_one")
        result.append(number)
    if not result or len(set(result)) != len(result):
        raise ContractError(f"{name}_empty_or_duplicate")
    return tuple(sorted(result))


def _metrics(value: Any, *, allow_empty: bool) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ContractError("delta_v2_oos_metrics_mapping_required")
    if not value and not allow_empty:
        raise ContractError("delta_v2_oos_metrics_empty")
    result: dict[str, float] = {}
    for key, raw in value.items():
        name = _text("delta_v2_oos_metric_name", key)
        if isinstance(raw, bool):
            raise ContractError("delta_v2_oos_metric_invalid")
        try:
            number = float(raw)
        except (TypeError, ValueError) as exc:
            raise ContractError("delta_v2_oos_metric_invalid") from exc
        if not math.isfinite(number):
            raise ContractError("delta_v2_oos_metric_invalid")
        result[name] = number
    return result


@dataclass(frozen=True)
class DeltaV2PolicyArtifact:
    artifact_id: str
    station_id: str
    stage: str
    season_bin: str
    maturity_bin: str
    liquidity_bin: str
    strategy_version: str
    value_weights: ValueWeightsV2
    execution_weights: ExecutionWeightsV2
    cash_clip_grid: tuple[float, ...]
    price_grid: tuple[float, ...]
    timeout_grid_seconds: tuple[float, ...]
    fill_distribution_artifact_sha256: str
    training_evidence_sha256: str
    train_start_date: str
    train_end_date: str
    holdout_start_date: str
    holdout_end_date: str
    sample_n: int
    holdout_n: int
    consecutive_forward_windows: int
    compared_baselines: tuple[str, ...]
    oos_metrics: Mapping[str, float]
    oos_gate_version: str
    oos_gate_passed: bool
    trained_through_utc: str
    valid_from_utc: str
    valid_until_utc: str
    authority_scope: str
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "DeltaV2PolicyArtifact":
        if not isinstance(raw, Mapping) or raw.get("schema") != ARTIFACT_SCHEMA:
            raise ContractError("delta_v2_artifact_schema_invalid")
        stage = _text("delta_v2_stage", raw.get("stage")).upper()
        if stage not in STAGES:
            raise ContractError("delta_v2_stage_invalid")
        scope = _text(
            "delta_v2_authority_scope", raw.get("authority_scope")
        )
        if scope not in AUTHORITY_SCOPES:
            raise ContractError("delta_v2_authority_scope_invalid")
        oos_passed = raw.get("oos_gate_passed") is True
        sample_n = (
            _positive_int("delta_v2_sample_n", raw.get("sample_n"))
            if oos_passed
            else _nonnegative_int("delta_v2_sample_n", raw.get("sample_n"))
        )
        holdout_n = (
            _positive_int("delta_v2_holdout_n", raw.get("holdout_n"))
            if oos_passed
            else _nonnegative_int("delta_v2_holdout_n", raw.get("holdout_n"))
        )
        windows = _nonnegative_int(
            "delta_v2_consecutive_historical_windows",
            raw.get(
                "consecutive_historical_windows",
                raw.get("consecutive_forward_windows"),
            ),
        )
        oos_data_policy = str(
            raw.get("oos_data_policy") or "HISTORICAL_CLOSED_DAY_ONLY"
        ).strip().upper()
        future_data_policy = str(
            raw.get("future_data_policy")
            or "OBSERVATIONAL_CANDIDATE_NOT_OOS"
        ).strip().upper()
        try:
            future_event_count = int(raw.get("future_event_count", 0))
        except (TypeError, ValueError) as exc:
            raise ContractError(
                "delta_v2_future_event_count_invalid"
            ) from exc
        if oos_data_policy != "HISTORICAL_CLOSED_DAY_ONLY":
            raise ContractError("delta_v2_oos_data_policy_not_historical")
        if future_data_policy != "OBSERVATIONAL_CANDIDATE_NOT_OOS":
            raise ContractError("delta_v2_future_data_policy_invalid")
        if future_event_count != 0:
            raise ContractError("delta_v2_future_event_count_nonzero")
        baselines_raw = raw.get("compared_baselines")
        if not isinstance(baselines_raw, Sequence) or isinstance(
            baselines_raw, (str, bytes, bytearray)
        ):
            raise ContractError("delta_v2_compared_baselines_invalid")
        baselines = tuple(sorted(set(_text("delta_v2_baseline", x) for x in baselines_raw)))
        metrics = _metrics(raw.get("oos_metrics"), allow_empty=not oos_passed)
        if oos_passed and (
            windows < 2 or set(baselines) != set(REQUIRED_BASELINES)
        ):
            raise ContractError("delta_v2_oos_comparison_gate_incomplete")
        if not oos_passed and (
            sample_n != 0
            or holdout_n != 0
            or windows != 0
            or baselines
            or metrics
        ):
            raise ContractError("delta_v2_preregistered_must_not_claim_oos")
        train_start = _date(
            "delta_v2_train_start_date", raw.get("train_start_date")
        )
        train_end = _date(
            "delta_v2_train_end_date", raw.get("train_end_date")
        )
        holdout_start = _date(
            "delta_v2_holdout_start_date", raw.get("holdout_start_date")
        )
        holdout_end = _date(
            "delta_v2_holdout_end_date", raw.get("holdout_end_date")
        )
        if not train_start <= train_end < holdout_start <= holdout_end:
            raise ContractError("delta_v2_oos_window_order_invalid")
        trained_through = _timestamp(
            "delta_v2_trained_through_utc", raw.get("trained_through_utc")
        )
        if oos_passed and holdout_end > trained_through[:10]:
            raise ContractError("delta_v2_oos_holdout_not_closed_asof")
        valid_from = _timestamp(
            "delta_v2_valid_from_utc", raw.get("valid_from_utc")
        )
        valid_until = _timestamp(
            "delta_v2_valid_until_utc", raw.get("valid_until_utc")
        )
        if not trained_through < valid_from < valid_until:
            raise ContractError("delta_v2_artifact_time_order_invalid")
        value = cls(
            artifact_id=_text("delta_v2_artifact_id", raw.get("artifact_id")),
            station_id=_text(
                "delta_v2_station_id", raw.get("station_id")
            ).upper(),
            stage=stage,
            season_bin=_text(
                "delta_v2_season_bin", raw.get("season_bin")
            ),
            maturity_bin=_text(
                "delta_v2_maturity_bin", raw.get("maturity_bin")
            ),
            liquidity_bin=_text(
                "delta_v2_liquidity_bin", raw.get("liquidity_bin")
            ),
            strategy_version=_text(
                "delta_v2_strategy_version", raw.get("strategy_version")
            ),
            value_weights=ValueWeightsV2.from_mapping(
                raw.get("value_weights") or {}
            ),
            execution_weights=ExecutionWeightsV2.from_mapping(
                raw.get("execution_weights") or {}
            ),
            cash_clip_grid=_positive_grid(
                "delta_v2_cash_clip_grid", raw.get("cash_clip_grid")
            ),
            price_grid=_positive_grid(
                "delta_v2_price_grid",
                raw.get("price_grid"),
                below_one=True,
            ),
            timeout_grid_seconds=_positive_grid(
                "delta_v2_timeout_grid_seconds",
                raw.get("timeout_grid_seconds"),
            ),
            fill_distribution_artifact_sha256=_sha(
                "delta_v2_fill_distribution_artifact_sha256",
                raw.get("fill_distribution_artifact_sha256"),
            ),
            training_evidence_sha256=_sha(
                "delta_v2_training_evidence_sha256",
                raw.get("training_evidence_sha256"),
            ),
            train_start_date=train_start,
            train_end_date=train_end,
            holdout_start_date=holdout_start,
            holdout_end_date=holdout_end,
            sample_n=sample_n,
            holdout_n=holdout_n,
            consecutive_forward_windows=windows,
            compared_baselines=baselines,
            oos_metrics=metrics,
            oos_gate_version=_text(
                "delta_v2_oos_gate_version", raw.get("oos_gate_version")
            ),
            oos_gate_passed=oos_passed,
            trained_through_utc=trained_through,
            valid_from_utc=valid_from,
            valid_until_utc=valid_until,
            authority_scope=scope,
            content_sha256=_sha(
                "delta_v2_artifact_content_sha256",
                raw.get("content_sha256"),
            ),
        )
        if content_sha256(
            value.to_dict(include_content_sha256=False)
        ) != value.content_sha256:
            raise ContractError("delta_v2_artifact_content_sha_mismatch")
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
            "value_weights": asdict(self.value_weights),
            "execution_weights": asdict(self.execution_weights),
            "cash_clip_grid": list(self.cash_clip_grid),
            "price_grid": list(self.price_grid),
            "timeout_grid_seconds": list(self.timeout_grid_seconds),
            "fill_distribution_artifact_sha256": (
                self.fill_distribution_artifact_sha256
            ),
            "training_evidence_sha256": self.training_evidence_sha256,
            "train_start_date": self.train_start_date,
            "train_end_date": self.train_end_date,
            "holdout_start_date": self.holdout_start_date,
            "holdout_end_date": self.holdout_end_date,
            "sample_n": self.sample_n,
            "holdout_n": self.holdout_n,
            "consecutive_forward_windows": self.consecutive_forward_windows,
            "compared_baselines": list(self.compared_baselines),
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


def _validate_promotion(
    raw: Mapping[str, Any],
    artifact: DeltaV2PolicyArtifact,
) -> dict[str, Any]:
    if raw.get("schema") != PROMOTION_SCHEMA:
        raise ContractError("delta_v2_promotion_schema_invalid")
    result = {
        "schema": PROMOTION_SCHEMA,
        "promotion_id": _text(
            "delta_v2_promotion_id", raw.get("promotion_id")
        ),
        "artifact_id": _text(
            "delta_v2_promotion_artifact_id", raw.get("artifact_id")
        ),
        "decision_seq": _positive_int(
            "delta_v2_promotion_decision_seq", raw.get("decision_seq")
        ),
        "decision": _text(
            "delta_v2_promotion_decision", raw.get("decision")
        ).upper(),
        "gate_version": _text(
            "delta_v2_promotion_gate_version", raw.get("gate_version")
        ),
        "decided_at_utc": _timestamp(
            "delta_v2_promotion_decided_at_utc", raw.get("decided_at_utc")
        ),
        "receipt_sha256": _sha(
            "delta_v2_promotion_receipt_sha256",
            raw.get("receipt_sha256"),
        ),
    }
    if result["artifact_id"] != artifact.artifact_id:
        raise ContractError("delta_v2_promotion_artifact_mismatch")
    if result["decision"] not in {
        "PREREGISTERED",
        NO_LIVE_ACTIVATION_DECISION,
        "PROMOTED",
        "REJECTED",
        "SUPERSEDED",
    }:
        raise ContractError("delta_v2_promotion_decision_invalid")
    if result["gate_version"] != artifact.oos_gate_version:
        raise ContractError("delta_v2_promotion_gate_version_mismatch")
    if result["decision"] == "PROMOTED" and not artifact.oos_gate_passed:
        raise ContractError("delta_v2_promotion_without_oos")
    if (
        result["decision"] == NO_LIVE_ACTIVATION_DECISION
        and artifact.authority_scope != "PRODUCTION_NO_LIVE"
    ):
        raise ContractError("delta_v2_no_live_activation_scope_invalid")
    supplied = _sha(
        "delta_v2_promotion_content_sha256", raw.get("content_sha256")
    )
    if content_sha256(result) != supplied:
        raise ContractError("delta_v2_promotion_content_sha_mismatch")
    return {**result, "content_sha256": supplied}


def _validate_activation(
    raw: Mapping[str, Any],
    artifact: DeltaV2PolicyArtifact,
) -> dict[str, Any]:
    if raw.get("schema") != ACTIVATION_SCHEMA:
        raise ContractError("delta_v2_activation_schema_invalid")
    result = {
        "schema": ACTIVATION_SCHEMA,
        "activation_id": _text(
            "delta_v2_activation_id", raw.get("activation_id")
        ),
        "artifact_id": _text(
            "delta_v2_activation_artifact_id", raw.get("artifact_id")
        ),
        "station_id": _text(
            "delta_v2_activation_station_id", raw.get("station_id")
        ).upper(),
        "stage": _text(
            "delta_v2_activation_stage", raw.get("stage")
        ).upper(),
        "season_bin": _text(
            "delta_v2_activation_season_bin", raw.get("season_bin")
        ),
        "maturity_bin": _text(
            "delta_v2_activation_maturity_bin", raw.get("maturity_bin")
        ),
        "liquidity_bin": _text(
            "delta_v2_activation_liquidity_bin", raw.get("liquidity_bin")
        ),
        "strategy_version": _text(
            "delta_v2_activation_strategy_version",
            raw.get("strategy_version"),
        ),
        "generation": _positive_int(
            "delta_v2_activation_generation", raw.get("generation")
        ),
        "valid_from_utc": _timestamp(
            "delta_v2_activation_valid_from_utc", raw.get("valid_from_utc")
        ),
        "valid_until_utc": _timestamp(
            "delta_v2_activation_valid_until_utc", raw.get("valid_until_utc")
        ),
        "activated_at_utc": _timestamp(
            "delta_v2_activation_activated_at_utc",
            raw.get("activated_at_utc"),
        ),
        "authority_scope": _text(
            "delta_v2_activation_authority_scope",
            raw.get("authority_scope"),
        ),
    }
    expected = {
        "artifact_id": artifact.artifact_id,
        "station_id": artifact.station_id,
        "stage": artifact.stage,
        "season_bin": artifact.season_bin,
        "maturity_bin": artifact.maturity_bin,
        "liquidity_bin": artifact.liquidity_bin,
        "strategy_version": artifact.strategy_version,
        "authority_scope": artifact.authority_scope,
    }
    if any(result[key] != value for key, value in expected.items()):
        raise ContractError("delta_v2_activation_scope_mismatch")
    if (
        artifact.authority_scope == "FORMAL_PROMOTED"
        and not artifact.oos_gate_passed
    ):
        raise ContractError("delta_v2_formal_activation_requires_oos")
    if not (
        artifact.valid_from_utc
        <= result["valid_from_utc"]
        < result["valid_until_utc"]
        <= artifact.valid_until_utc
    ):
        raise ContractError("delta_v2_activation_window_invalid")
    if result["activated_at_utc"] > result["valid_from_utc"]:
        raise ContractError("delta_v2_activation_after_valid_from")
    supplied = _sha(
        "delta_v2_activation_content_sha256", raw.get("content_sha256")
    )
    if content_sha256(result) != supplied:
        raise ContractError("delta_v2_activation_content_sha_mismatch")
    return {**result, "content_sha256": supplied}


def commit_delta_v2_authority(
    conn: sqlite3.Connection,
    *,
    artifact: DeltaV2PolicyArtifact | Mapping[str, Any],
    promotion: Mapping[str, Any],
    activation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Append a preregistration or an OOS-promoted production-no-live version."""

    governed = (
        artifact
        if isinstance(artifact, DeltaV2PolicyArtifact)
        else DeltaV2PolicyArtifact.from_mapping(artifact)
    )
    promoted = _validate_promotion(promotion, governed)
    activated = (
        _validate_activation(activation, governed)
        if activation is not None
        else None
    )
    if activated is not None and promoted["decision"] not in {
        NO_LIVE_ACTIVATION_DECISION,
        "PROMOTED",
    }:
        raise ContractError("delta_v2_activation_requires_promotion")
    savepoint = f"delta_v2_authority_{id(conn):x}"
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        before = conn.total_changes
        artifact_existing = conn.execute(
            "SELECT content_sha256 FROM basket_delta_v2_policy_artifact "
            "WHERE artifact_id=?",
            (governed.artifact_id,),
        ).fetchone()
        if artifact_existing is not None:
            if str(artifact_existing[0]) != governed.content_sha256:
                raise ContractError("delta_v2_artifact_content_conflict")
        else:
            duplicate_hash = conn.execute(
                "SELECT artifact_id FROM basket_delta_v2_policy_artifact "
                "WHERE content_sha256=?",
                (governed.content_sha256,),
            ).fetchone()
            if duplicate_hash is not None:
                raise ContractError("delta_v2_artifact_duplicate_hash")
            conn.execute(
                "INSERT INTO basket_delta_v2_policy_artifact VALUES"
                "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    governed.artifact_id,
                    governed.station_id,
                    governed.stage,
                    governed.season_bin,
                    governed.maturity_bin,
                    governed.liquidity_bin,
                    governed.strategy_version,
                    governed.trained_through_utc,
                    governed.valid_from_utc,
                    governed.valid_until_utc,
                    governed.authority_scope,
                    int(governed.oos_gate_passed),
                    json.dumps(
                        governed.to_dict(),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    governed.content_sha256,
                ),
            )

        promotion_existing = conn.execute(
            "SELECT content_sha256 FROM basket_delta_v2_policy_promotion "
            "WHERE promotion_id=?",
            (promoted["promotion_id"],),
        ).fetchone()
        if promotion_existing is not None:
            if str(promotion_existing[0]) != promoted["content_sha256"]:
                raise ContractError("delta_v2_promotion_content_conflict")
        else:
            duplicate_promotion_hash = conn.execute(
                "SELECT promotion_id FROM basket_delta_v2_policy_promotion "
                "WHERE content_sha256=?",
                (promoted["content_sha256"],),
            ).fetchone()
            if duplicate_promotion_hash is not None:
                raise ContractError("delta_v2_promotion_duplicate_hash")
            latest = conn.execute(
                "SELECT MAX(decision_seq) "
                "FROM basket_delta_v2_policy_promotion WHERE artifact_id=?",
                (governed.artifact_id,),
            ).fetchone()
            if (
                latest is not None
                and latest[0] is not None
                and promoted["decision_seq"] <= int(latest[0])
            ):
                raise ContractError("delta_v2_promotion_seq_not_monotonic")
            conn.execute(
                "INSERT INTO basket_delta_v2_policy_promotion VALUES"
                "(?,?,?,?,?,?,?,?)",
                tuple(
                    promoted[key]
                    for key in (
                        "promotion_id",
                        "artifact_id",
                        "decision_seq",
                        "decision",
                        "gate_version",
                        "decided_at_utc",
                        "receipt_sha256",
                        "content_sha256",
                    )
                ),
            )
        if activated is not None:
            activation_existing = conn.execute(
                "SELECT content_sha256 "
                "FROM basket_delta_v2_policy_activation "
                "WHERE activation_id=?",
                (activated["activation_id"],),
            ).fetchone()
            if activation_existing is not None:
                if str(activation_existing[0]) != activated["content_sha256"]:
                    raise ContractError("delta_v2_activation_content_conflict")
            else:
                duplicate_activation_hash = conn.execute(
                    "SELECT activation_id "
                    "FROM basket_delta_v2_policy_activation "
                    "WHERE content_sha256=?",
                    (activated["content_sha256"],),
                ).fetchone()
                if duplicate_activation_hash is not None:
                    raise ContractError("delta_v2_activation_duplicate_hash")
                conn.execute(
                    "INSERT INTO basket_delta_v2_policy_activation VALUES"
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    tuple(
                        activated[key]
                        for key in (
                            "activation_id",
                            "artifact_id",
                            "station_id",
                            "stage",
                            "season_bin",
                            "maturity_bin",
                            "liquidity_bin",
                            "strategy_version",
                            "generation",
                            "valid_from_utc",
                            "valid_until_utc",
                            "activated_at_utc",
                            "authority_scope",
                            "content_sha256",
                        )
                    ),
                )
        wrote = conn.total_changes - before
        conn.execute(f"RELEASE {savepoint}")
    except Exception:
        conn.execute(f"ROLLBACK TO {savepoint}")
        conn.execute(f"RELEASE {savepoint}")
        raise
    return {
        "schema": "delta_basket_score_authority_ingress_v2",
        "artifact_id": governed.artifact_id,
        "activation_id": (
            activated["activation_id"] if activated is not None else None
        ),
        "rows_written": wrote,
        "state": "COMMITTED" if wrote else "UNCHANGED_ZERO_WRITE",
        "execution_mode": "production_no_live",
        "live_submit_authorized": False,
        "calls_order_gateway": False,
        "orders_submitted": 0,
    }


def resolve_delta_v2_policy(
    conn: sqlite3.Connection,
    *,
    station_id: str,
    stage: str,
    season_bin: str,
    maturity_bin: str,
    liquidity_bin: str,
    strategy_version: str,
    evaluation_at_utc: str,
    required_authority_scope: str = "PRODUCTION_NO_LIVE",
) -> DeltaV2PolicyArtifact:
    """Resolve one exact no-live/formal v2 policy; no v1 fallback exists."""

    if required_authority_scope not in AUTHORITY_SCOPES:
        raise ContractError("delta_v2_required_authority_scope_invalid")
    station = _text("delta_v2_resolve_station", station_id).upper()
    current_stage = _text("delta_v2_resolve_stage", stage).upper()
    decision_at = _timestamp(
        "delta_v2_resolve_evaluation_at", evaluation_at_utc
    )
    row = conn.execute(
        "SELECT activation_id,artifact_id,generation FROM "
        "basket_delta_v2_policy_activation WHERE station_id IN (?, '*') "
        "AND stage IN (?, '*') AND season_bin=? AND maturity_bin=? "
        "AND liquidity_bin=? AND strategy_version=? AND authority_scope=? "
        "AND valid_from_utc<=? AND valid_until_utc>? "
        "ORDER BY CASE WHEN station_id=? THEN 0 ELSE 1 END,"
        "CASE WHEN stage=? THEN 0 ELSE 1 END,generation DESC LIMIT 1",
        (
            station,
            current_stage,
            season_bin,
            maturity_bin,
            liquidity_bin,
            strategy_version,
            required_authority_scope,
            decision_at,
            decision_at,
            station,
            current_stage,
        ),
    ).fetchone()
    if row is None:
        raise ContractError("delta_v2_activation_missing")
    artifact_row = conn.execute(
        "SELECT artifact_json FROM basket_delta_v2_policy_artifact "
        "WHERE artifact_id=?",
        (row[1],),
    ).fetchone()
    if artifact_row is None:
        raise ContractError("delta_v2_artifact_missing")
    artifact = DeltaV2PolicyArtifact.from_mapping(
        json.loads(str(artifact_row[0]))
    )
    promotion = conn.execute(
        "SELECT decision,gate_version FROM basket_delta_v2_policy_promotion "
        "WHERE artifact_id=? ORDER BY decision_seq DESC LIMIT 1",
        (artifact.artifact_id,),
    ).fetchone()
    decision = str(promotion[0]) if promotion is not None else ""
    accepted_decisions = (
        {NO_LIVE_ACTIVATION_DECISION, "PROMOTED"}
        if required_authority_scope == "PRODUCTION_NO_LIVE"
        else {"PROMOTED"}
    )
    if (
        promotion is None
        or decision not in accepted_decisions
        or str(promotion[1]) != artifact.oos_gate_version
        or (
            required_authority_scope == "FORMAL_PROMOTED"
            and not artifact.oos_gate_passed
        )
    ):
        raise ContractError("delta_v2_artifact_not_promoted")
    if not artifact.valid_from_utc <= decision_at < artifact.valid_until_utc:
        raise ContractError("delta_v2_artifact_not_active_asof")
    return artifact


__all__ = [
    "ACTIVATION_SCHEMA",
    "ARTIFACT_SCHEMA",
    "AUTHORITY_SCOPES",
    "DeltaV2PolicyArtifact",
    "NO_LIVE_ACTIVATION_DECISION",
    "PROMOTION_SCHEMA",
    "commit_delta_v2_authority",
    "resolve_delta_v2_policy",
]
