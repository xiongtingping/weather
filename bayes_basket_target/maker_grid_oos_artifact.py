"""Exact-scope Maker price x timeout OOS curve contract.

This module is deliberately a pure contract compiler/validator.  It does not
read files, query a database, call an exchange, estimate a missing point, or
promote an artifact.  A curve is usable only when every requested price x
timeout point has its own formal, target-owned realized Maker lifecycle and
its own official WebSocket evidence.

The important invariant is that a point is the unit of evidence.  A valid
point cannot borrow a neighbouring point's price, timeout, sample, scope,
as-of cutoff, artifact id, or lifecycle.  In particular, this module never
turns a no-live counterfactual into formal evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .basket_kernel.contracts import ContractError, content_sha256


MAKER_GRID_OOS_ARTIFACT_SCHEMA = "target_maker_price_timeout_grid_oos_artifact_v1"
MAKER_GRID_OOS_ARTIFACT_VERSION = "maker_price_timeout_grid_oos_contract_v1"
MAKER_GRID_POINT_SCHEMA = "target_maker_price_timeout_oos_point_v1"
MAKER_GRID_CURVE_SCHEMA = "target_maker_price_timeout_oos_curve_v1"
REALIZED_MAKER_LIFECYCLE = "REALIZED_MAKER_LIFECYCLE"
NO_LIVE_COUNTERFACTUAL_PROXY = "NO_LIVE_COUNTERFACTUAL_PROXY"

MAX_GRID_POINTS = 256
MAX_MAPPING_FIELDS = 64
MAX_TEXT_LENGTH = 512
MIN_EFFECTIVE_SAMPLE_SIZE = 30.0
MIN_HOLDOUT_SAMPLE_SIZE = 30
PRICE_EPSILON = 1e-10

SCOPE_FIELDS = (
    "station_id",
    "season_bin",
    "lead_bucket",
    "issuance",
    "liquidity_bin",
    "price_distance_bin",
    "queue_bin",
)
GRID_FIELDS = ("candidate_price", "price_tick", "timeout_seconds", *SCOPE_FIELDS)
CURVE_FIELDS = (
    "fill_probability",
    "nonfill_opportunity_cost_per_share",
    "queue_cost_per_filled_share",
    "wait_cost_per_filled_share",
    "adverse_selection_cost_per_filled_share",
    "cancel_risk",
    "late_fill_risk",
    "cancel_risk_cost_per_requested_share",
    "late_fill_risk_cost_per_requested_share",
)
PROBABILITY_FIELDS = frozenset({"fill_probability", "cancel_risk", "late_fill_risk"})


def _error(code: str) -> ContractError:
    return ContractError(f"maker_grid_oos_{code}")


def _text(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise _error(f"{name}_invalid")
    result = value.strip()
    if not result:
        raise _error(f"{name}_missing")
    if len(result) > MAX_TEXT_LENGTH:
        raise _error(f"{name}_too_large")
    return result


def _finite(name: str, value: Any) -> float:
    if isinstance(value, bool):
        raise _error(f"{name}_invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise _error(f"{name}_invalid") from exc
    if not math.isfinite(result):
        raise _error(f"{name}_invalid")
    return result


def _positive(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result <= 0:
        raise _error(f"{name}_not_positive")
    return result


def _nonnegative(name: str, value: Any) -> float:
    result = _finite(name, value)
    if result < 0:
        raise _error(f"{name}_negative")
    return result


def _unit(name: str, value: Any) -> float:
    result = _nonnegative(name, value)
    if result > 1.0:
        raise _error(f"{name}_outside_unit_interval")
    return result


def _sha(name: str, value: Any) -> str:
    result = _text(name, value).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise _error(f"{name}_invalid_sha256")
    return result


def _timestamp(name: str, value: Any) -> str:
    result = _text(name, value)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as exc:
        raise _error(f"{name}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _error(f"{name}_timezone_required")
    return result


def _timestamp_value(name: str, value: Any) -> datetime:
    return datetime.fromisoformat(_timestamp(name, value).replace("Z", "+00:00")).astimezone(timezone.utc)


def _mapping(name: str, value: Any, *, max_fields: int = MAX_MAPPING_FIELDS) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(f"{name}_mapping_required")
    if len(value) > max_fields:
        raise _error(f"{name}_too_large")
    return value


def _alias(raw: Mapping[str, Any], name: str, aliases: Sequence[str]) -> Any:
    present = [key for key in aliases if key in raw]
    if len(present) > 1:
        raise _error(f"{name}_ambiguous")
    if not present:
        raise _error(f"{name}_missing")
    return raw[present[0]]


def canonical_scope(raw: Mapping[str, Any]) -> dict[str, str]:
    """Canonicalize the complete seven-dimensional execution scope."""

    value = _mapping("scope", raw)
    allowed = set(SCOPE_FIELDS) | {"issuance_hour"}
    if set(value) - allowed:
        raise _error("scope_unknown_fields")
    if "issuance" in value and "issuance_hour" in value:
        raise _error("scope_issuance_ambiguous")
    result: dict[str, str] = {}
    for field in SCOPE_FIELDS:
        source = "issuance_hour" if field == "issuance" and "issuance" not in value else field
        result[field] = _text(f"scope_{field}", value.get(source))
    return result


def scope_sha256(scope: Mapping[str, Any]) -> str:
    return content_sha256({"schema": "maker_grid_oos_exact_scope_v1", **canonical_scope(scope)})


def _canonical_price(value: Any) -> float:
    result = _finite("candidate_price", value)
    if not 0.0 < result < 1.0:
        raise _error("candidate_price_out_of_range")
    return result


def _canonical_tick(value: Any) -> float:
    result = _positive("price_tick", value)
    if result >= 1.0:
        raise _error("price_tick_out_of_range")
    return result


def _canonical_grid_values(raw: Mapping[str, Any]) -> dict[str, Any]:
    value = _mapping("grid_point", raw)
    candidate_price = _canonical_price(_alias(value, "candidate_price", ("candidate_price",)))
    price_tick = _canonical_tick(_alias(value, "price_tick", ("price_tick", "tick")))
    if abs(candidate_price / price_tick - round(candidate_price / price_tick)) > PRICE_EPSILON:
        raise _error("candidate_price_not_on_tick")
    timeout = _positive(
        "timeout_seconds", _alias(value, "timeout_seconds", ("timeout_seconds", "timeout", "timeout_s"))
    )
    if timeout > 7 * 24 * 60 * 60:
        raise _error("timeout_seconds_too_large")
    scope = canonical_scope({field: value.get(field) for field in SCOPE_FIELDS if field in value})
    return {
        "candidate_price": candidate_price,
        "price_tick": price_tick,
        "timeout_seconds": timeout,
        **scope,
    }


def grid_point_key(raw: Mapping[str, Any]) -> tuple[Any, ...]:
    """Return the exact identity tuple for a price x timeout grid point."""

    point = _canonical_grid_values(raw)
    return tuple(point[field] for field in GRID_FIELDS)


def grid_point_key_text(raw: Mapping[str, Any]) -> str:
    point = _canonical_grid_values(raw)
    return content_sha256({"schema": "maker_grid_oos_point_key_v1", **point})


def _curve(raw: Any) -> dict[str, float]:
    value = _mapping("curve", raw)
    if set(value) != set(CURVE_FIELDS):
        raise _error("curve_fields_invalid")
    result: dict[str, float] = {}
    for field in CURVE_FIELDS:
        result[field] = _unit(field, value[field]) if field in PROBABILITY_FIELDS else _nonnegative(field, value[field])
    return result


def _sample_count(name: str, value: Any, *, minimum: float) -> int | float:
    parsed = _finite(name, value)
    if parsed < minimum or parsed != math.floor(parsed):
        raise _error(f"{name}_too_small_or_nonintegral")
    return int(parsed)


def _formal_lifecycle(raw: Any) -> dict[str, Any]:
    value = _mapping("formal_lifecycle_lineage", raw)
    evidence_class = str(value.get("evidence_class") or value.get("source_class") or "").strip().upper()
    if evidence_class == NO_LIVE_COUNTERFACTUAL_PROXY or "proxy" in evidence_class.lower():
        raise _error("proxy_never_formal_eligible")
    if evidence_class != REALIZED_MAKER_LIFECYCLE:
        raise _error("formal_lifecycle_class_required")
    lifecycle_id = _text("lifecycle_id", value.get("lifecycle_id"))
    lifecycle_sha = _sha("lifecycle_sha256", value.get("lifecycle_sha256"))
    if value.get("live_submit_authorized") is not True:
        raise _error("formal_lifecycle_live_authority_required")
    orders = _finite("orders_submitted", value.get("orders_submitted"))
    if orders <= 0 or orders != math.floor(orders):
        raise _error("formal_lifecycle_orders_submitted_invalid")
    terminal = _text("terminal_state", value.get("terminal_state")).lower()
    if terminal not in {"resolved_filled", "resolved_cancelled"}:
        raise _error("formal_lifecycle_terminal_state_required")
    reconciliation_id = _text(
        "reconciliation_id",
        value.get("reconciliation_id", value.get("final_reconciliation_id")),
    )
    reconciliation_sha = _sha(
        "reconciliation_sha256",
        value.get("reconciliation_sha256", value.get("final_reconciliation_sha256")),
    )
    reconciled_at = _timestamp("reconciled_at_utc", value.get("reconciled_at_utc"))
    result = {
        "evidence_class": REALIZED_MAKER_LIFECYCLE,
        "lifecycle_id": lifecycle_id,
        "lifecycle_sha256": lifecycle_sha,
        "reconciliation_id": reconciliation_id,
        "reconciliation_sha256": reconciliation_sha,
        "terminal_state": terminal,
        "live_submit_authorized": True,
        "orders_submitted": int(orders),
        "reconciled_at_utc": reconciled_at,
    }
    observed = value.get("observed_at_utc")
    if observed is not None:
        result["observed_at_utc"] = _timestamp("lifecycle_observed_at_utc", observed)
    return result


def _promotion(raw: Any, *, point_key: str, curve_sha: str, scope_sha: str, as_of_utc: str, curve: Mapping[str, Any], n_eff: int | float, holdout_n: int | float) -> dict[str, Any]:
    value = _mapping("promotion_evidence", raw)
    required = {"decision_id", "decision", "gate_version", "decided_at_utc", "receipt_sha256", "oos_metrics_sha256", "grid_point_key", "curve_sha256", "scope_sha256", "as_of_utc"}
    if not required <= set(value):
        raise _error("promotion_evidence_incomplete")
    decision = _text("promotion_decision", value["decision"]).upper()
    if decision != "PROMOTED":
        raise _error("promotion_not_promoted")
    result = {
        "decision_id": _text("promotion_decision_id", value["decision_id"]),
        "decision": decision,
        "gate_version": _text("promotion_gate_version", value["gate_version"]),
        "decided_at_utc": _timestamp("promotion_decided_at_utc", value["decided_at_utc"]),
        "receipt_sha256": _sha("promotion_receipt_sha256", value["receipt_sha256"]),
        "oos_metrics_sha256": _sha("promotion_oos_metrics_sha256", value["oos_metrics_sha256"]),
        "grid_point_key": _text("promotion_grid_point_key", value["grid_point_key"]),
        "curve_sha256": _sha("promotion_curve_sha256", value["curve_sha256"]),
        "scope_sha256": _sha("promotion_scope_sha256", value["scope_sha256"]),
        "as_of_utc": _timestamp("promotion_as_of_utc", value["as_of_utc"]),
    }
    if result["grid_point_key"] != point_key:
        raise _error("promotion_grid_point_binding_mismatch")
    if result["curve_sha256"] != curve_sha:
        raise _error("promotion_curve_binding_mismatch")
    if result["scope_sha256"] != scope_sha:
        raise _error("promotion_scope_binding_mismatch")
    if result["as_of_utc"] != as_of_utc:
        raise _error("promotion_as_of_binding_mismatch")
    metrics_sha = content_sha256({"curve": dict(curve), "n_eff": n_eff, "holdout_n": holdout_n})
    if result["oos_metrics_sha256"] != metrics_sha:
        raise _error("promotion_metrics_binding_mismatch")
    supplied = value.get("content_sha256")
    if supplied is not None and _sha("promotion_content_sha256", supplied) != content_sha256(result):
        raise _error("promotion_content_sha256_mismatch")
    result["content_sha256"] = content_sha256(result)
    return result


def _normalise_point(raw: Mapping[str, Any], *, artifact_as_of: str | None = None) -> dict[str, Any]:
    value = _mapping("grid_point", raw)
    grid = _canonical_grid_values(value)
    point_id = _text("grid_point_id", value.get("grid_point_id"))
    as_of = _timestamp("as_of_utc", value.get("as_of_utc", artifact_as_of))
    if artifact_as_of is not None and as_of != artifact_as_of:
        raise _error("cross_as_of_scope")
    train_start = _timestamp("train_start_utc", value.get("train_start_utc"))
    train_end = _timestamp("train_end_utc", value.get("train_end_utc"))
    holdout_start = _timestamp("holdout_start_utc", value.get("holdout_start_utc"))
    holdout_end = _timestamp("holdout_end_utc", value.get("holdout_end_utc"))
    times = [_timestamp_value("train_start_utc", train_start), _timestamp_value("train_end_utc", train_end), _timestamp_value("holdout_start_utc", holdout_start), _timestamp_value("holdout_end_utc", holdout_end), _timestamp_value("as_of_utc", as_of)]
    if not times[0] < times[1] <= times[2] < times[3] <= times[4]:
        raise _error("train_holdout_as_of_window_invalid")
    artifact_id = _text("oos_artifact_id", _alias(value, "oos_artifact_id", ("oos_artifact_id", "artifact_id")))
    artifact_sha = _sha("oos_artifact_sha256", _alias(value, "oos_artifact_sha256", ("oos_artifact_sha256", "artifact_sha256")))
    training_input_sha = _sha(
        "training_input_sha256",
        value.get("training_input_sha256"),
    )
    holdout_input_sha = _sha(
        "holdout_input_sha256",
        value.get("holdout_input_sha256"),
    )
    official_ws = _sha("official_ws_snapshot_sha256", _alias(value, "official_ws_snapshot_sha256", ("official_ws_snapshot_sha256", "official_ws_hash", "official_ws_quote_sha256")))
    lineage = _formal_lifecycle(value.get("formal_lifecycle_lineage"))
    curve = _curve(value.get("curve"))
    n_eff = _sample_count("n_eff", value.get("n_eff"), minimum=MIN_EFFECTIVE_SAMPLE_SIZE)
    holdout_n = _sample_count("holdout_n", value.get("holdout_n"), minimum=MIN_HOLDOUT_SAMPLE_SIZE)
    scope = {field: grid[field] for field in SCOPE_FIELDS}
    key = content_sha256({"schema": "maker_grid_oos_point_key_v1", **grid})
    scope_sha = scope_sha256(scope)
    point_identity = {
        "schema": MAKER_GRID_POINT_SCHEMA,
        "grid_point_id": point_id,
        "grid_point_key": key,
        **grid,
        "train_start_utc": train_start,
        "train_end_utc": train_end,
        "holdout_start_utc": holdout_start,
        "holdout_end_utc": holdout_end,
        "as_of_utc": as_of,
        "training_input_sha256": training_input_sha,
        "holdout_input_sha256": holdout_input_sha,
        "oos_artifact_id": artifact_id,
        "oos_artifact_sha256": artifact_sha,
        "formal_lifecycle_lineage": lineage,
        "official_ws_snapshot_sha256": official_ws,
        "curve": curve,
        "n_eff": n_eff,
        "holdout_n": holdout_n,
    }
    curve_sha = content_sha256({"schema": MAKER_GRID_CURVE_SCHEMA, **point_identity})
    supplied_curve_sha = value.get("curve_sha256")
    if supplied_curve_sha is not None and _sha("curve_sha256", supplied_curve_sha) != curve_sha:
        raise _error("curve_sha256_mismatch")
    promotion = _promotion(value.get("promotion_evidence"), point_key=key, curve_sha=curve_sha, scope_sha=scope_sha, as_of_utc=as_of, curve=curve, n_eff=n_eff, holdout_n=holdout_n)
    return {**point_identity, "scope_sha256": scope_sha, "promotion_evidence": promotion, "curve_sha256": curve_sha}


def _normalise_grid_spec(expected_grid: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if isinstance(expected_grid, (str, bytes)) or not isinstance(expected_grid, Sequence):
        raise _error("expected_grid_sequence_required")
    if not expected_grid or len(expected_grid) > MAX_GRID_POINTS:
        raise _error("expected_grid_size_invalid")
    result: list[dict[str, Any]] = []
    keys: set[tuple[Any, ...]] = set()
    for raw in expected_grid:
        value = _canonical_grid_values(_mapping("expected_grid_point", raw))
        key = tuple(value[field] for field in GRID_FIELDS)
        if key in keys:
            raise _error("expected_grid_duplicate")
        keys.add(key)
        result.append(value)
    return sorted(result, key=lambda item: tuple(str(item[field]) for field in GRID_FIELDS))


def _validate_unique_points(points: Sequence[Mapping[str, Any]]) -> None:
    if not points or len(points) > MAX_GRID_POINTS:
        raise _error("grid_size_invalid")
    key_seen: set[tuple[Any, ...]] = set()
    id_seen: set[str] = set()
    artifact_seen: set[str] = set()
    artifact_hash_seen: set[str] = set()
    lifecycle_seen: set[str] = set()
    lifecycle_hash_seen: set[str] = set()
    reconciliation_seen: set[str] = set()
    reconciliation_hash_seen: set[str] = set()
    training_input_seen: set[str] = set()
    holdout_input_seen: set[str] = set()
    for point in points:
        key = tuple(point[field] for field in GRID_FIELDS)
        if key in key_seen:
            raise _error("duplicate_grid_point")
        key_seen.add(key)
        for seen, field in ((id_seen, "grid_point_id"), (artifact_seen, "oos_artifact_id"), (artifact_hash_seen, "oos_artifact_sha256"), (lifecycle_seen, "lifecycle_id"), (lifecycle_hash_seen, "lifecycle_sha256"), (reconciliation_seen, "reconciliation_id"), (reconciliation_hash_seen, "reconciliation_sha256"), (training_input_seen, "training_input_sha256"), (holdout_input_seen, "holdout_input_sha256")):
            value = point[field] if field in point else point["formal_lifecycle_lineage"][field]
            if value in seen:
                raise _error("single_artifact_reuse")
            seen.add(value)


@dataclass(frozen=True)
class MakerGridOOSArtifact(Mapping[str, Any]):
    """Immutable validated artifact with a small Mapping-compatible surface."""

    payload: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return _clone(self.payload)

    def __getitem__(self, key: str) -> Any:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)

    @property
    def content_sha256(self) -> str:
        return str(self.payload["content_sha256"])

    @property
    def curve_sha256(self) -> str:
        return str(self.payload["curve_sha256"])

    @property
    def formal_activation_eligible(self) -> bool:
        return bool(self.payload["formal_activation_eligible"])

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], *, expected_grid: Sequence[Mapping[str, Any]] | None = None, as_of_utc: str | None = None) -> "MakerGridOOSArtifact":
        value = _mapping("artifact", raw)
        required = {"schema", "artifact_version", "artifact_id", "as_of_utc", "grid_spec", "points", "grid_size", "curve_sha256", "formal_activation_eligible", "evidence_class", "content_sha256"}
        if set(value) != required:
            raise _error("artifact_fields_invalid")
        if value["schema"] != MAKER_GRID_OOS_ARTIFACT_SCHEMA or value["artifact_version"] != MAKER_GRID_OOS_ARTIFACT_VERSION:
            raise _error("artifact_schema_invalid")
        artifact_as_of = _timestamp("as_of_utc", value["as_of_utc"])
        if (
            as_of_utc is not None
            and _timestamp_value("artifact_as_of_utc", artifact_as_of)
            > _timestamp_value("expected_as_of_utc", as_of_utc)
        ):
            raise _error("artifact_from_future")
        expected = _normalise_grid_spec(expected_grid if expected_grid is not None else value["grid_spec"])
        supplied_spec = _normalise_grid_spec(value["grid_spec"])
        if supplied_spec != expected:
            raise _error("grid_spec_mismatch")
        raw_points = value["points"]
        if isinstance(raw_points, (str, bytes)) or not isinstance(raw_points, Sequence):
            raise _error("points_sequence_required")
        if len(raw_points) > MAX_GRID_POINTS:
            raise _error("grid_size_invalid")
        points = [_normalise_point(item, artifact_as_of=artifact_as_of) for item in raw_points]
        _validate_unique_points(points)
        point_keys = {tuple(point[field] for field in GRID_FIELDS) for point in points}
        expected_keys = {tuple(item[field] for field in GRID_FIELDS) for item in expected}
        if point_keys != expected_keys:
            missing = expected_keys - point_keys
            extra = point_keys - expected_keys
            if missing:
                raise _error("grid_point_missing")
            if extra:
                raise _error("grid_point_extra")
        if value["grid_size"] != len(expected):
            raise _error("grid_size_mismatch")
        if value["evidence_class"] != REALIZED_MAKER_LIFECYCLE or value["formal_activation_eligible"] is not True:
            raise _error("formal_activation_only")
        ordered = sorted(points, key=lambda item: item["grid_point_key"])
        curve_sha = content_sha256({"schema": MAKER_GRID_CURVE_SCHEMA, "points": [{"grid_point_key": point["grid_point_key"], "curve_sha256": point["curve_sha256"]} for point in ordered]})
        if _sha("curve_sha256", value["curve_sha256"]) != curve_sha:
            raise _error("artifact_curve_sha256_mismatch")
        identity = {key: _clone(value[key]) for key in required if key != "content_sha256"}
        expected_content = content_sha256(identity)
        if _sha("content_sha256", value["content_sha256"]) != expected_content:
            raise _error("artifact_content_sha256_mismatch")
        payload = identity | {"content_sha256": expected_content}
        return cls(payload=payload)


def _clone(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _clone(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_clone(item) for item in value]
    return value


def compile_maker_grid_oos_artifact(
    grid_points: Sequence[Mapping[str, Any]],
    expected_grid: Sequence[Mapping[str, Any]],
    *,
    artifact_id: str,
    as_of_utc: str,
) -> MakerGridOOSArtifact:
    """Compile a complete price x timeout curve; never fill or extrapolate."""

    root_id = _text("artifact_id", artifact_id)
    as_of = _timestamp("as_of_utc", as_of_utc)
    expected = _normalise_grid_spec(expected_grid)
    if isinstance(grid_points, (str, bytes)) or not isinstance(grid_points, Sequence):
        raise _error("points_sequence_required")
    if len(grid_points) > MAX_GRID_POINTS:
        raise _error("grid_size_invalid")
    points = [_normalise_point(item, artifact_as_of=as_of) for item in grid_points]
    _validate_unique_points(points)
    point_keys = {tuple(point[field] for field in GRID_FIELDS) for point in points}
    expected_keys = {tuple(item[field] for field in GRID_FIELDS) for item in expected}
    if point_keys != expected_keys:
        if expected_keys - point_keys:
            raise _error("grid_point_missing")
        raise _error("grid_point_extra")
    ordered = sorted(points, key=lambda item: item["grid_point_key"])
    curve_sha = content_sha256({"schema": MAKER_GRID_CURVE_SCHEMA, "points": [{"grid_point_key": point["grid_point_key"], "curve_sha256": point["curve_sha256"]} for point in ordered]})
    identity = {
        "schema": MAKER_GRID_OOS_ARTIFACT_SCHEMA,
        "artifact_version": MAKER_GRID_OOS_ARTIFACT_VERSION,
        "artifact_id": root_id,
        "as_of_utc": as_of,
        "grid_spec": expected,
        "points": ordered,
        "grid_size": len(expected),
        "curve_sha256": curve_sha,
        "formal_activation_eligible": True,
        "evidence_class": REALIZED_MAKER_LIFECYCLE,
    }
    raw = {**identity, "content_sha256": content_sha256(identity)}
    return MakerGridOOSArtifact.from_mapping(raw, expected_grid=expected, as_of_utc=as_of)


def build_maker_grid_oos_artifact(
    grid_points: Sequence[Mapping[str, Any]],
    expected_grid: Sequence[Mapping[str, Any]],
    *,
    artifact_id: str,
    as_of_utc: str,
) -> MakerGridOOSArtifact:
    """Public builder alias with an explicit no-extrapolation API."""

    return compile_maker_grid_oos_artifact(grid_points, expected_grid, artifact_id=artifact_id, as_of_utc=as_of_utc)


def validate_maker_grid_oos_artifact(
    raw: Mapping[str, Any] | MakerGridOOSArtifact,
    *,
    expected_grid: Sequence[Mapping[str, Any]] | None = None,
    as_of_utc: str | None = None,
) -> MakerGridOOSArtifact:
    """Validate serialized content and the complete exact grid."""

    if isinstance(raw, MakerGridOOSArtifact):
        raw = raw.to_dict()
    return MakerGridOOSArtifact.from_mapping(raw, expected_grid=expected_grid, as_of_utc=as_of_utc)


# Alternate descriptive names make the contract easy to discover without
# creating a second implementation or a compatibility execution branch.
build_exact_maker_grid_oos_artifact = build_maker_grid_oos_artifact
validate_exact_maker_grid_oos_artifact = validate_maker_grid_oos_artifact


__all__ = [
    "CURVE_FIELDS",
    "GRID_FIELDS",
    "MAKER_GRID_CURVE_SCHEMA",
    "MAKER_GRID_OOS_ARTIFACT_SCHEMA",
    "MAKER_GRID_OOS_ARTIFACT_VERSION",
    "MAKER_GRID_POINT_SCHEMA",
    "NO_LIVE_COUNTERFACTUAL_PROXY",
    "REALIZED_MAKER_LIFECYCLE",
    "MakerGridOOSArtifact",
    "build_exact_maker_grid_oos_artifact",
    "build_maker_grid_oos_artifact",
    "canonical_scope",
    "compile_maker_grid_oos_artifact",
    "grid_point_key",
    "grid_point_key_text",
    "scope_sha256",
    "validate_exact_maker_grid_oos_artifact",
    "validate_maker_grid_oos_artifact",
]
