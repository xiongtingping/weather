"""Immutable, content-addressed fill-distribution authority for ΔBasketScore v2.

This module intentionally contains only contract/authority logic:
* validate one complete fill-distribution artifact;
* append-only commit to sqlite tables;
* strict resolve from a resolved artifact+promotion record.

It does not call gateways, execute orders, or access remote services.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from datetime import datetime, timezone
import json
import math
import sqlite3
from typing import Any, Mapping, Sequence

from .basket_kernel.contracts import ContractError, content_sha256


FILL_DISTRIBUTION_ARTIFACT_SCHEMA = "delta_basket_score_v2_fill_distribution_artifact_v1"
FILL_DISTRIBUTION_PROMOTION_SCHEMA = "delta_basket_score_v2_fill_distribution_promotion_v1"
TABLE_ARTIFACT = "basket_delta_v2_fill_artifact"
TABLE_PROMOTION = "basket_delta_v2_fill_promotion"
AUTHORITY_SCOPES = frozenset({"PRODUCTION_NO_LIVE", "FORMAL_PROMOTED"})
STAGES = frozenset({"*", "P0", "P0-M", "P1"})
MAKER = "MAKER"
FOK = "FOK"
ORDER_MODES = frozenset({MAKER, FOK})
REQUIRED_BASELINES = frozenset({"v1", "EV_ONLY", "ZERO_WEIGHT", "HOLD"})
# The band an artifact without a rung-offset field implicitly claims: every
# rung.  Wide enough that no real offset falls outside it.
UNRESTRICTED_RUNG_OFFSET_MIN = -1000.0
UNRESTRICTED_RUNG_OFFSET_MAX = 1000.0
NO_LIVE_ACTIVATION_DECISION = "NO_LIVE_ACTIVATED"
DEFAULT_EPS = 1e-12
EXECUTION_COST_FIELDS = (
  "wait_cost",
  "queue_risk",
  "adverse_selection",
  "reprice_risk",
  "capital_lock_cost",
)

FILL_DISTRIBUTION_DDL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_ARTIFACT} (
  artifact_id TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE,
  schema TEXT NOT NULL,
  station_id TEXT NOT NULL,
  stage TEXT NOT NULL,
  season_bin TEXT NOT NULL,
  maturity_bin TEXT NOT NULL,
  liquidity_bin TEXT NOT NULL,
  bucket TEXT NOT NULL,
  price REAL NOT NULL,
  timeout_seconds REAL NOT NULL,
  queue_ahead_min REAL NOT NULL,
  queue_ahead_max REAL NOT NULL,
  spread_min REAL NOT NULL,
  spread_max REAL NOT NULL,
  depth_min REAL NOT NULL,
  depth_max REAL NOT NULL,
  quote_version_scope TEXT NOT NULL,
  oos_gate_version TEXT NOT NULL,
  oos_gate_passed INTEGER NOT NULL,
  consecutive_forward_windows INTEGER NOT NULL,
  compared_baselines TEXT NOT NULL,
  oos_metrics TEXT NOT NULL,
  sample_n INTEGER NOT NULL,
  holdout_n INTEGER NOT NULL,
  trained_through_utc TEXT NOT NULL,
  valid_from_utc TEXT NOT NULL,
  valid_until_utc TEXT NOT NULL,
  authority_scope TEXT NOT NULL,
  fill_distribution_artifact_sha256 TEXT NOT NULL,
  maker_execution_costs TEXT NOT NULL,
  fok_execution_costs TEXT NOT NULL,
  artifact_json TEXT NOT NULL,
  PRIMARY KEY (artifact_id),
  UNIQUE (content_sha256)
);
CREATE TABLE IF NOT EXISTS {TABLE_PROMOTION} (
  promotion_id TEXT NOT NULL PRIMARY KEY,
  artifact_id TEXT NOT NULL,
  artifact_content_sha256 TEXT NOT NULL,
  decision_seq INTEGER NOT NULL,
  decision TEXT NOT NULL CHECK(decision IN (
    'PREREGISTERED','NO_LIVE_ACTIVATED','PROMOTED','REJECTED','SUPERSEDED'
  )),
  gate_version TEXT NOT NULL,
  decided_at_utc TEXT NOT NULL,
  receipt_sha256 TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  created_at_utc TEXT NOT NULL,
  FOREIGN KEY (artifact_id) REFERENCES {TABLE_ARTIFACT}(artifact_id),
  FOREIGN KEY (artifact_content_sha256) REFERENCES {TABLE_ARTIFACT}(content_sha256),
  UNIQUE(artifact_id, decision_seq)
);
CREATE INDEX IF NOT EXISTS idx_fill_artifact_scope ON {TABLE_ARTIFACT}(
  station_id, stage, season_bin, maturity_bin, liquidity_bin, bucket, authority_scope
);
-- The resolver's whole WHERE clause, in its order, so one resolution is an
-- index seek rather than a scan of every price and timeout in the scope.
CREATE INDEX IF NOT EXISTS idx_fill_artifact_resolution ON {TABLE_ARTIFACT}(
  station_id, stage, season_bin, maturity_bin, liquidity_bin, bucket,
  authority_scope, price, timeout_seconds
);
CREATE INDEX IF NOT EXISTS idx_fill_promotion_artifact ON {TABLE_PROMOTION}(
  artifact_id, artifact_content_sha256, decision, decision_seq DESC
);
CREATE TRIGGER IF NOT EXISTS basket_delta_v2_fill_artifact_no_update
BEFORE UPDATE ON {TABLE_ARTIFACT}
BEGIN
  SELECT RAISE(ABORT, 'append_only');
END;
CREATE TRIGGER IF NOT EXISTS basket_delta_v2_fill_artifact_no_delete
BEFORE DELETE ON {TABLE_ARTIFACT}
BEGIN
  SELECT RAISE(ABORT, 'append_only');
END;
CREATE TRIGGER IF NOT EXISTS basket_delta_v2_fill_promotion_no_update
BEFORE UPDATE ON {TABLE_PROMOTION}
BEGIN
  SELECT RAISE(ABORT, 'append_only');
END;
CREATE TRIGGER IF NOT EXISTS basket_delta_v2_fill_promotion_no_delete
BEFORE DELETE ON {TABLE_PROMOTION}
BEGIN
  SELECT RAISE(ABORT, 'append_only');
END;
"""


_FILL_PROMOTION_COLUMNS = (
  "promotion_id",
  "artifact_id",
  "artifact_content_sha256",
  "decision_seq",
  "decision",
  "gate_version",
  "decided_at_utc",
  "receipt_sha256",
  "content_sha256",
  "created_at_utc",
)


def upgrade_fill_promotion_decision_constraint(
  conn: sqlite3.Connection,
) -> dict[str, object]:
  """Upgrade an existing promotion table to the explicit lifecycle CHECK."""

  row = conn.execute(
    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
    (TABLE_PROMOTION,),
  ).fetchone()
  if row is None:
    raise RuntimeError("delta_v2_fill_promotion_table_missing")
  table_sql = str(row[0] or "")
  if (
    "NO_LIVE_ACTIVATED" in table_sql
    and "CHECK" in table_sql.upper()
  ):
    return {"state": "unchanged", "rows_preserved": 0}

  actual_columns = tuple(
    str(info[1])
    for info in conn.execute(f"PRAGMA table_info({TABLE_PROMOTION})")
  )
  if actual_columns != _FILL_PROMOTION_COLUMNS:
    raise RuntimeError("delta_v2_fill_promotion_columns_incompatible")

  archive = "basket_delta_v2_fill_promotion_constraint_v1_archive"
  if conn.execute(
    "SELECT 1 FROM sqlite_master WHERE name=? LIMIT 1", (archive,)
  ).fetchone() is not None:
    raise RuntimeError("delta_v2_fill_promotion_migration_name_collision")
  row_count = int(
    conn.execute(f"SELECT COUNT(*) FROM {TABLE_PROMOTION}").fetchone()[0]
  )
  names = ",".join(_FILL_PROMOTION_COLUMNS)
  savepoint = f"delta_v2_fill_promotion_constraint_{id(conn):x}"
  conn.execute(f"SAVEPOINT {savepoint}")
  try:
    conn.execute(
      "DROP TRIGGER IF EXISTS basket_delta_v2_fill_promotion_no_update"
    )
    conn.execute(
      "DROP TRIGGER IF EXISTS basket_delta_v2_fill_promotion_no_delete"
    )
    conn.execute(f"ALTER TABLE {TABLE_PROMOTION} RENAME TO {archive}")
    conn.execute(
      f"""
      CREATE TABLE {TABLE_PROMOTION} (
        promotion_id TEXT NOT NULL PRIMARY KEY,
        artifact_id TEXT NOT NULL,
        artifact_content_sha256 TEXT NOT NULL,
        decision_seq INTEGER NOT NULL,
        decision TEXT NOT NULL CHECK(decision IN (
          'PREREGISTERED','NO_LIVE_ACTIVATED','PROMOTED',
          'REJECTED','SUPERSEDED'
        )),
        gate_version TEXT NOT NULL,
        decided_at_utc TEXT NOT NULL,
        receipt_sha256 TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        created_at_utc TEXT NOT NULL,
        FOREIGN KEY (artifact_id) REFERENCES {TABLE_ARTIFACT}(artifact_id),
        FOREIGN KEY (artifact_content_sha256)
          REFERENCES {TABLE_ARTIFACT}(content_sha256),
        UNIQUE(artifact_id, decision_seq)
      )
      """
    )
    conn.execute(
      f"INSERT INTO {TABLE_PROMOTION}({names}) "
      f"SELECT {names} FROM {archive}"
    )
    copied = int(
      conn.execute(f"SELECT COUNT(*) FROM {TABLE_PROMOTION}").fetchone()[0]
    )
    if copied != row_count:
      raise RuntimeError("delta_v2_fill_promotion_row_count_drift")
    conn.execute(f"DROP TABLE {archive}")
    conn.execute(
      f"CREATE INDEX IF NOT EXISTS idx_fill_promotion_artifact "
      f"ON {TABLE_PROMOTION}("
      "artifact_id, artifact_content_sha256, decision, decision_seq DESC)"
    )
    conn.execute(
      "CREATE TRIGGER basket_delta_v2_fill_promotion_no_update "
      f"BEFORE UPDATE ON {TABLE_PROMOTION} "
      "BEGIN SELECT RAISE(ABORT, 'append_only'); END"
    )
    conn.execute(
      "CREATE TRIGGER basket_delta_v2_fill_promotion_no_delete "
      f"BEFORE DELETE ON {TABLE_PROMOTION} "
      "BEGIN SELECT RAISE(ABORT, 'append_only'); END"
    )
  except BaseException:
    conn.execute(f"ROLLBACK TO {savepoint}")
    conn.execute(f"RELEASE {savepoint}")
    raise
  conn.execute(f"RELEASE {savepoint}")
  return {"state": "migrated", "rows_preserved": row_count}


def _required_text(name: str, value: Any) -> str:
  result = str(value or "").strip()
  if not result:
    raise ContractError(f"delta_v2_fill_distribution_{name}_missing")
  if len(result) > 512:
    raise ContractError(f"delta_v2_fill_distribution_{name}_too_long")
  return result


def _sha256_text(name: str, value: Any) -> str:
  result = _required_text(name, value).lower()
  if len(result) != 64 or any(ch not in "0123456789abcdef" for ch in result):
    raise ContractError(f"delta_v2_fill_distribution_{name}_invalid_sha256")
  return result


def _float(name: str, value: Any) -> float:
  if isinstance(value, bool):
    raise ContractError(f"delta_v2_fill_distribution_{name}_invalid_float")
  try:
    parsed = float(value)
  except (TypeError, ValueError) as exc:
    raise ContractError(f"delta_v2_fill_distribution_{name}_invalid_float") from exc
  if not math.isfinite(parsed):
    raise ContractError(f"delta_v2_fill_distribution_{name}_not_finite")
  return float(f"{parsed:.12f}".rstrip("0").rstrip(".") or "0")


def _positive(name: str, value: Any) -> float:
  parsed = _float(name, value)
  if parsed <= 0:
    raise ContractError(f"delta_v2_fill_distribution_{name}_must_be_positive")
  return parsed


def _interval(
  name_min: str,
  name_max: str,
  min_value: Any,
  max_value: Any,
  *,
  allow_zero: bool = False,
) -> tuple[float, float]:
  minimum = _float(name_min, min_value) if allow_zero else _nonnegative(name_min, min_value)
  maximum = _float(name_max, max_value) if allow_zero else _nonnegative(name_max, max_value)
  if maximum < minimum:
    raise ContractError(
      f"delta_v2_fill_distribution_{name_min}_{name_max}_invalid_order"
    )
  return minimum, maximum


def _nonnegative(name: str, value: Any) -> float:
  parsed = _float(name, value)
  if parsed < 0:
    raise ContractError(f"delta_v2_fill_distribution_{name}_must_be_nonnegative")
  return parsed


def _unit(name: str, value: Any) -> float:
  parsed = _float(name, value)
  if not (0.0 <= parsed <= 1.0):
    raise ContractError(f"delta_v2_fill_distribution_{name}_not_in_unit")
  return parsed


def _nonnegative_int(name: str, value: Any) -> int:
  try:
    parsed = int(value)
  except (TypeError, ValueError) as exc:
    raise ContractError(f"delta_v2_fill_distribution_{name}_invalid_int") from exc
  if parsed < 0:
    raise ContractError(f"delta_v2_fill_distribution_{name}_must_be_nonnegative_int")
  return parsed


def _positive_int(name: str, value: Any) -> int:
  parsed = _nonnegative_int(name, value)
  if parsed <= 0:
    raise ContractError(f"delta_v2_fill_distribution_{name}_must_be_positive_int")
  return parsed


def _timestamp(name: str, value: Any) -> str:
  result = _required_text(name, value)
  try:
    parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
  except ValueError as exc:
    raise ContractError(f"delta_v2_fill_distribution_{name}_invalid_utc") from exc
  if parsed.tzinfo is None or parsed.utcoffset() is None:
    raise ContractError(f"delta_v2_fill_distribution_{name}_missing_timezone")
  return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _timestamp_value(name: str, value: Any) -> datetime:
  return datetime.fromisoformat(_timestamp(name, value).replace("Z", "+00:00"))


def _date(name: str, value: Any) -> str:
  result = _required_text(name, value)
  try:
    datetime.strptime(result, "%Y-%m-%d")
  except ValueError as exc:
    raise ContractError(f"delta_v2_fill_distribution_{name}_invalid_date") from exc
  return result


def _mapping(name: str, value: Any, *, max_fields: int = 64) -> Mapping[str, Any]:
  if not isinstance(value, Mapping):
    raise ContractError(f"delta_v2_fill_distribution_{name}_mapping_required")
  if len(value) > max_fields:
    raise ContractError(f"delta_v2_fill_distribution_{name}_too_many_fields")
  return value


def _sequence(name: str, value: Any) -> tuple[Any, ...]:
  if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
    raise ContractError(f"delta_v2_fill_distribution_{name}_sequence_required")
  return tuple(value)


def _json(max_bytes: int, value: Any, *, name: str) -> str:
  try:
    dumped = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
  except (TypeError, ValueError) as exc:
    raise ContractError(f"delta_v2_fill_distribution_{name}_invalid_json") from exc
  if len(dumped.encode("utf-8")) > max_bytes:
    raise ContractError(f"delta_v2_fill_distribution_{name}_too_large")
  return dumped


def _execution_costs(name: str, value: Any) -> dict[str, float]:
  if value is None:
    raise ContractError(
      "delta_v2_fill_distribution_execution_cost_fields_missing:"
      + ",".join(EXECUTION_COST_FIELDS)
    )
  payload = _mapping(f"{name}", value, max_fields=len(EXECUTION_COST_FIELDS))
  required = set(EXECUTION_COST_FIELDS)
  missing = required - set(payload)
  if missing:
    raise ContractError(
      "delta_v2_fill_distribution_execution_cost_fields_missing:"
      + ",".join(sorted(missing))
    )
  unknown = set(payload) - required
  if unknown:
    raise ContractError(
      "delta_v2_fill_distribution_execution_cost_unknown_fields:"
      + ",".join(sorted(unknown))
    )
  return {
    field: _nonnegative(
      f"{name}_{field}",
      payload[field],
    )
    for field in EXECUTION_COST_FIELDS
  }


def _close_to_one(values: Sequence[float], eps: float = 1e-10) -> bool:
  return abs(sum(values) - 1.0) <= eps


def _distribution_with_mode(mode: str, raw: Mapping[str, Any]) -> dict[str, float]:
  normalized_mode = _required_text("order_mode", mode).upper()
  if normalized_mode not in ORDER_MODES:
    raise ContractError("delta_v2_fill_distribution_order_mode_invalid")
  required = {
    "fill_probability_zero",
    "fill_probability_partial",
    "fill_probability_full",
    "late_fill_after_cancel_request_probability",
    "partial_fill_fraction",
  }
  payload = _mapping(f"{normalized_mode.lower()}_distribution", raw)
  missing = required - set(payload)
  if missing:
    raise ContractError(
      "delta_v2_fill_distribution_fields_missing:"
      + ",".join(sorted(missing))
    )
  unknown = set(payload) - required
  if unknown:
    raise ContractError(
      "delta_v2_fill_distribution_unknown_fields:"
      + ",".join(sorted(unknown))
    )

  p0 = _unit("fill_probability_zero", payload["fill_probability_zero"])
  pp = _unit("fill_probability_partial", payload["fill_probability_partial"])
  pf = _unit("fill_probability_full", payload["fill_probability_full"])
  late = _unit("late_fill_after_cancel_request_probability", payload["late_fill_after_cancel_request_probability"])
  partial_fraction = _unit("partial_fill_fraction", payload["partial_fill_fraction"])

  if normalized_mode == MAKER:
    if not (_close_to_one((p0, pp, pf), eps=1e-10) and p0 > 0 and pp > 0 and pf > 0 and 0 < partial_fraction < 1.0):
      raise ContractError("delta_v2_fill_distribution_maker_mass_or_support_invalid")
    if late > pp + pf + DEFAULT_EPS:
      raise ContractError("delta_v2_fill_distribution_late_fill_probability_exceeds_active_fill")
  else:
    if pp > DEFAULT_EPS:
      raise ContractError("delta_v2_fill_distribution_fok_partial_forbidden")
    if p0 > DEFAULT_EPS:
      raise ContractError("delta_v2_fill_distribution_fok_zero_forbidden")
    if abs(pf - 1.0) > 1e-10:
      raise ContractError("delta_v2_fill_distribution_fok_mass_must_be_full")
    if late > DEFAULT_EPS:
      raise ContractError("delta_v2_fill_distribution_fok_late_fill_forbidden")
    if partial_fraction > DEFAULT_EPS:
      raise ContractError("delta_v2_fill_distribution_fok_partial_fraction_forbidden")

  return {
    "fill_probability_zero": p0,
    "fill_probability_partial": pp,
    "fill_probability_full": pf,
    "late_fill_after_cancel_request_probability": late,
    "partial_fill_fraction": partial_fraction,
  }


@dataclass(frozen=True)
class DeltaV2FillDistributionArtifact:
  artifact_id: str
  station_id: str
  stage: str
  season_bin: str
  maturity_bin: str
  liquidity_bin: str
  bucket: str
  price: float
  timeout_seconds: float
  queue_ahead_min: float
  queue_ahead_max: float
  spread_min: float
  spread_max: float
  depth_min: float
  depth_max: float
  # How far inside the spread this rung sits, in ticks above the bid.
  #
  # The other conditioning fields describe the book; this one describes
  # where the order stands in it, and it is the variable that separates a
  # ladder's rungs.  Measured over the maker shadow book at a 60 minute
  # horizon: resting at the bid fills 12.1% of the time, one tick inside
  # 17.1%, two ticks 20.1%, mid 20.9% and at the ask 88.3% -- a seven-fold
  # spread that none of price, timeout, queue, spread or depth can express.
  # Queue-ahead cannot stand in for it: the book reports queue at the bid,
  # so it reads 28-30 shares at every rung.
  #
  # An artifact written before this field existed carries the whole range,
  # which is what it always implicitly claimed.
  rung_offset_ticks_min: float
  rung_offset_ticks_max: float
  quote_version_scope: str
  maker_distribution: Mapping[str, float]
  fok_distribution: Mapping[str, float]
  maker_execution_costs: Mapping[str, float]
  fok_execution_costs: Mapping[str, float]
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
  fill_distribution_artifact_sha256: str
  content_sha256: str

  @classmethod
  def from_mapping(
    cls,
    raw: Mapping[str, Any],
  ) -> "DeltaV2FillDistributionArtifact":
    if not isinstance(raw, Mapping):
      raise ContractError("delta_v2_fill_distribution_artifact_mapping_required")
    if raw.get("schema") != FILL_DISTRIBUTION_ARTIFACT_SCHEMA:
      raise ContractError("delta_v2_fill_distribution_schema_invalid")

    station_id = _required_text("station_id", raw.get("station_id")).upper()
    stage = _required_text("stage", raw.get("stage")).upper()
    if stage not in STAGES:
      raise ContractError("delta_v2_fill_distribution_stage_invalid")
    scope = _required_text("authority_scope", raw.get("authority_scope"))
    if scope not in AUTHORITY_SCOPES:
      raise ContractError("delta_v2_fill_distribution_authority_scope_invalid")
    artifact_id = _required_text("artifact_id", raw.get("artifact_id"))
    season_bin = _required_text("season_bin", raw.get("season_bin"))
    maturity_bin = _required_text("maturity_bin", raw.get("maturity_bin"))
    liquidity_bin = _required_text("liquidity_bin", raw.get("liquidity_bin"))
    bucket = _required_text("bucket", raw.get("bucket"))

    new_queue_min = raw.get("queue_ahead_min")
    new_queue_max = raw.get("queue_ahead_max")
    new_spread_min = raw.get("spread_min")
    new_spread_max = raw.get("spread_max")
    new_depth_min = raw.get("depth_min")
    new_depth_max = raw.get("depth_max")
    quote_version_scope = raw.get("quote_version_scope")
    if all(
      value is not None
      for value in (
        new_queue_min,
        new_queue_max,
        new_spread_min,
        new_spread_max,
        new_depth_min,
        new_depth_max,
        quote_version_scope,
      )
    ):
      queue_ahead_min, queue_ahead_max = _interval(
        "queue_ahead_min",
        "queue_ahead_max",
        new_queue_min,
        new_queue_max,
        allow_zero=True,
      )
      spread_min, spread_max = _interval("spread_min", "spread_max", new_spread_min, new_spread_max)
      depth_min, depth_max = _interval(
        "depth_min",
        "depth_max",
        new_depth_min,
        new_depth_max,
        allow_zero=True,
      )
      scope_text = _required_text("quote_version_scope", quote_version_scope)
      quote_version_scope = scope_text
    else:
      raise ContractError("delta_v2_fill_distribution_fields_missing")

    # Absent means unrestricted, which is exactly what every artifact
    # written before this field existed was claiming: one fill curve for
    # every rung.  Present means the curve was fitted for rungs in that
    # band and may not be spent outside it.
    raw_rung_min = raw.get("rung_offset_ticks_min")
    raw_rung_max = raw.get("rung_offset_ticks_max")
    if raw_rung_min is None and raw_rung_max is None:
      rung_offset_ticks_min, rung_offset_ticks_max = (
        UNRESTRICTED_RUNG_OFFSET_MIN,
        UNRESTRICTED_RUNG_OFFSET_MAX,
      )
    elif raw_rung_min is None or raw_rung_max is None:
      raise ContractError("delta_v2_fill_distribution_rung_offset_incomplete")
    else:
      rung_offset_ticks_min, rung_offset_ticks_max = _interval(
        "rung_offset_ticks_min",
        "rung_offset_ticks_max",
        raw_rung_min,
        raw_rung_max,
        # allow_zero routes through _float, which admits the negative
        # offsets a deep resting bid legitimately carries.
        allow_zero=True,
      )

    price = _float("price", raw.get("price"))
    if not (0.0 < price < 1.0):
      raise ContractError("delta_v2_fill_distribution_price_out_of_range")
    timeout_seconds = _positive("timeout_seconds", raw.get("timeout_seconds"))
    if quote_version_scope != "*" and not quote_version_scope:
      raise ContractError("delta_v2_fill_distribution_quote_version_scope_invalid")

    maker_distribution = _distribution_with_mode(
      MAKER, _mapping("maker_distribution", raw.get("maker_distribution"))
    )
    fok_distribution = _distribution_with_mode(
      FOK, _mapping("fok_distribution", raw.get("fok_distribution"))
    )
    maker_execution_costs = _execution_costs(
      "maker_execution_costs",
      raw.get("maker_execution_costs"),
    )
    fok_execution_costs = _execution_costs(
      "fok_execution_costs",
      raw.get("fok_execution_costs"),
    )

    oos_passed = raw.get("oos_gate_passed") is True
    sample_n = (
      _positive_int("sample_n", raw.get("sample_n"))
      if oos_passed
      else _nonnegative_int("sample_n", raw.get("sample_n"))
    )
    holdout_n = (
      _positive_int("holdout_n", raw.get("holdout_n"))
      if oos_passed
      else _nonnegative_int("holdout_n", raw.get("holdout_n"))
    )
    windows = _nonnegative_int(
      "consecutive_forward_windows",
      raw.get("consecutive_forward_windows"),
    )
    raw_baselines = _sequence("compared_baselines", raw.get("compared_baselines"))
    compared_baselines = tuple(
      sorted(
        {
          _required_text(f"baseline_{index}", value)
          for index, value in enumerate(raw_baselines)
        }
      )
    )
    metrics = _mapping("oos_metrics", raw.get("oos_metrics"))
    parsed_metrics: dict[str, float] = {}
    for key, value in metrics.items():
      parsed_metrics[_required_text("oos_metric_name", key)] = _nonnegative(f"oos_metric_{key}", value)

    train_start = _date("train_start_date", raw.get("train_start_date"))
    train_end = _date("train_end_date", raw.get("train_end_date"))
    holdout_start = _date("holdout_start_date", raw.get("holdout_start_date"))
    holdout_end = _date("holdout_end_date", raw.get("holdout_end_date"))
    if not train_start <= train_end < holdout_start <= holdout_end:
      raise ContractError("delta_v2_fill_distribution_oos_window_invalid")

    trained_through = _timestamp(
      "trained_through_utc", raw.get("trained_through_utc")
    )
    valid_from = _timestamp("valid_from_utc", raw.get("valid_from_utc"))
    valid_until = _timestamp("valid_until_utc", raw.get("valid_until_utc"))
    if not trained_through < valid_from < valid_until:
      raise ContractError("delta_v2_fill_distribution_artifact_time_invalid")
    if oos_passed and (windows < 2 or set(compared_baselines) != set(REQUIRED_BASELINES)):
      raise ContractError(
        "delta_v2_fill_distribution_oos_comparison_gate_incomplete"
      )
    if (not oos_passed) and (
      sample_n != 0
      or holdout_n != 0
      or windows != 0
      or compared_baselines
      or parsed_metrics
    ):
      raise ContractError(
        "delta_v2_fill_distribution_preregistered_must_not_claim_oos"
      )

    oos_gate_version = _required_text("oos_gate_version", raw.get("oos_gate_version"))
    fill_distribution_artifact_sha256 = _sha256_text(
      "fill_distribution_artifact_sha256",
      raw.get("fill_distribution_artifact_sha256"),
    )

    value = cls(
      artifact_id=artifact_id,
      station_id=station_id,
      stage=stage,
      season_bin=season_bin,
      maturity_bin=maturity_bin,
      liquidity_bin=liquidity_bin,
      bucket=bucket,
      price=price,
      timeout_seconds=timeout_seconds,
          queue_ahead_min=queue_ahead_min,
          queue_ahead_max=queue_ahead_max,
          spread_min=spread_min,
          spread_max=spread_max,
          depth_min=depth_min,
          depth_max=depth_max,
          rung_offset_ticks_min=rung_offset_ticks_min,
          rung_offset_ticks_max=rung_offset_ticks_max,
          quote_version_scope=quote_version_scope,
      maker_distribution=maker_distribution,
      fok_distribution=fok_distribution,
      maker_execution_costs=maker_execution_costs,
      fok_execution_costs=fok_execution_costs,
      train_start_date=train_start,
      train_end_date=train_end,
      holdout_start_date=holdout_start,
      holdout_end_date=holdout_end,
      sample_n=sample_n,
      holdout_n=holdout_n,
      consecutive_forward_windows=windows,
      compared_baselines=compared_baselines,
      oos_metrics=parsed_metrics,
      oos_gate_version=oos_gate_version,
      oos_gate_passed=oos_passed,
      trained_through_utc=trained_through,
      valid_from_utc=valid_from,
      valid_until_utc=valid_until,
      authority_scope=scope,
      fill_distribution_artifact_sha256=fill_distribution_artifact_sha256,
      content_sha256="",
    )
    supplied = _sha256_text("content_sha256", raw.get("content_sha256"))
    body = value.to_dict(include_content_sha256=False)
    actual = content_sha256(body)
    if actual != supplied:
      raise ContractError("delta_v2_fill_distribution_artifact_content_sha_mismatch")
    value = cls(
      **{**asdict(value), "content_sha256": actual}
    )
    return value

  def to_dict(self, *, include_content_sha256: bool = True) -> dict[str, Any]:
    result = {
      "schema": FILL_DISTRIBUTION_ARTIFACT_SCHEMA,
      "artifact_id": self.artifact_id,
      "station_id": self.station_id,
      "stage": self.stage,
      "season_bin": self.season_bin,
      "maturity_bin": self.maturity_bin,
      "liquidity_bin": self.liquidity_bin,
      "bucket": self.bucket,
      "price": self.price,
      "timeout_seconds": self.timeout_seconds,
      "queue_ahead_min": self.queue_ahead_min,
      "queue_ahead_max": self.queue_ahead_max,
      "spread_min": self.spread_min,
      "spread_max": self.spread_max,
      "depth_min": self.depth_min,
      "depth_max": self.depth_max,
      # The rung band enters the content hash only when the artifact
      # actually declares one.  An artifact minted before this field
      # existed carries the unrestricted band, and emitting it here would
      # change that artifact's hash -- every stored row would fail its own
      # content check, which is the whole table refusing to resolve.
      **(
        {}
        if (
          self.rung_offset_ticks_min == UNRESTRICTED_RUNG_OFFSET_MIN
          and self.rung_offset_ticks_max == UNRESTRICTED_RUNG_OFFSET_MAX
        )
        else {
          "rung_offset_ticks_min": self.rung_offset_ticks_min,
          "rung_offset_ticks_max": self.rung_offset_ticks_max,
        }
      ),
      "quote_version_scope": self.quote_version_scope,
      "maker_distribution": dict(self.maker_distribution),
      "fok_distribution": dict(self.fok_distribution),
      "maker_execution_costs": dict(self.maker_execution_costs),
      "fok_execution_costs": dict(self.fok_execution_costs),
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
      "fill_distribution_artifact_sha256": self.fill_distribution_artifact_sha256,
    }
    if include_content_sha256:
      result["content_sha256"] = self.content_sha256
    return result

  def to_kernel_fill_distribution(self, mode: str) -> tuple[dict[str, float], ...]:
    profile = {
      MAKER: self.maker_distribution,
      FOK: self.fok_distribution,
    }.get(mode)
    if profile is None:
      raise ContractError("delta_v2_fill_distribution_mode_not_supported")
    rows: list[dict[str, float]] = []
    rows.append(
      {
        "fill_fraction": 0.0,
        "probability": profile["fill_probability_zero"],
      }
    )
    if profile["fill_probability_partial"] > DEFAULT_EPS:
      rows.append(
        {
          "fill_fraction": profile["partial_fill_fraction"],
          "probability": profile["fill_probability_partial"],
        }
      )
    if profile["fill_probability_full"] > DEFAULT_EPS:
      rows.append(
        {"fill_fraction": 1.0, "probability": profile["fill_probability_full"]}
      )
    return tuple(sorted(rows, key=lambda row: row["fill_fraction"]))

  def to_kernel_execution_costs(self, mode: str) -> dict[str, float]:
    normalized_mode = _required_text("order_mode", mode).upper()
    if normalized_mode == MAKER:
      profile = self.maker_execution_costs
    elif normalized_mode == FOK:
      profile = self.fok_execution_costs
    else:
      raise ContractError("delta_v2_fill_distribution_mode_not_supported")
    return {
      "wait": profile["wait_cost"],
      "queue": profile["queue_risk"],
      "adverse_selection": profile["adverse_selection"],
      "reprice": profile["reprice_risk"],
      "capital_lock": profile["capital_lock_cost"],
    }


def _validate_promotion(
  raw: Mapping[str, Any],
  artifact: DeltaV2FillDistributionArtifact,
) -> dict[str, Any]:
  if not isinstance(raw, Mapping):
    raise ContractError("delta_v2_fill_distribution_promotion_mapping_required")
  if raw.get("schema") != FILL_DISTRIBUTION_PROMOTION_SCHEMA:
    raise ContractError("delta_v2_fill_distribution_promotion_schema_invalid")
  result = {
    "schema": FILL_DISTRIBUTION_PROMOTION_SCHEMA,
    "promotion_id": _required_text("promotion_id", raw.get("promotion_id")),
    "artifact_id": _required_text("artifact_id", raw.get("artifact_id")),
    "decision_seq": _positive_int("decision_seq", raw.get("decision_seq")),
    "decision": _required_text("decision", raw.get("decision")).upper(),
    "gate_version": _required_text("gate_version", raw.get("gate_version")),
    "decided_at_utc": _timestamp("decided_at_utc", raw.get("decided_at_utc")),
    "receipt_sha256": _sha256_text("receipt_sha256", raw.get("receipt_sha256")),
  }
  if result["artifact_id"] != artifact.artifact_id:
    raise ContractError("delta_v2_fill_distribution_artifact_id_mismatch")
  if result["decision"] not in {
    "PREREGISTERED",
    NO_LIVE_ACTIVATION_DECISION,
    "PROMOTED",
    "REJECTED",
    "SUPERSEDED",
  }:
    raise ContractError("delta_v2_fill_distribution_promotion_decision_invalid")
  if result["decision"] == "PROMOTED" and not artifact.oos_gate_passed:
    raise ContractError("delta_v2_fill_distribution_promotion_without_oos")
  if (
    result["decision"] == NO_LIVE_ACTIVATION_DECISION
    and artifact.authority_scope != "PRODUCTION_NO_LIVE"
  ):
    raise ContractError(
      "delta_v2_fill_distribution_no_live_activation_scope_invalid"
    )
  if result["gate_version"] != artifact.oos_gate_version:
    raise ContractError("delta_v2_fill_distribution_promotion_gate_version_mismatch")
  supplied = _sha256_text("promotion_content_sha256", raw.get("content_sha256"))
  if supplied != content_sha256({k: result[k] for k in result if k != "content_sha256"}):
    raise ContractError("delta_v2_fill_distribution_promotion_content_sha_mismatch")
  return {**result, "content_sha256": supplied}


def _ensure_schema(conn: sqlite3.Connection, *, commit: bool = True) -> None:
  statement = ""
  for line in FILL_DISTRIBUTION_DDL.splitlines(keepends=True):
    statement += line
    if sqlite3.complete_statement(statement):
      sql = statement.strip()
      if sql:
        conn.execute(sql)
      statement = ""
  if statement.strip():
    raise RuntimeError("delta_v2_fill_schema_script_incomplete")
  upgrade_fill_promotion_decision_constraint(conn)
  existing_artifact_columns = {
    row[1]
    for row in conn.execute(f"PRAGMA table_info({TABLE_ARTIFACT})").fetchall()
  }
  if "queue_ahead_min" not in existing_artifact_columns:
    conn.execute(
      f"ALTER TABLE {TABLE_ARTIFACT} "
      "ADD COLUMN queue_ahead_min REAL NOT NULL DEFAULT 0"
    )
  if "queue_ahead_max" not in existing_artifact_columns:
    conn.execute(
      f"ALTER TABLE {TABLE_ARTIFACT} "
      "ADD COLUMN queue_ahead_max REAL NOT NULL DEFAULT 0"
    )
  if "spread_min" not in existing_artifact_columns:
    conn.execute(
      f"ALTER TABLE {TABLE_ARTIFACT} "
      "ADD COLUMN spread_min REAL NOT NULL DEFAULT 0"
    )
  if "spread_max" not in existing_artifact_columns:
    conn.execute(
      f"ALTER TABLE {TABLE_ARTIFACT} "
      "ADD COLUMN spread_max REAL NOT NULL DEFAULT 0"
    )
  if "depth_min" not in existing_artifact_columns:
    conn.execute(
      f"ALTER TABLE {TABLE_ARTIFACT} "
      "ADD COLUMN depth_min REAL NOT NULL DEFAULT 0"
    )
  if "depth_max" not in existing_artifact_columns:
    conn.execute(
      f"ALTER TABLE {TABLE_ARTIFACT} "
      "ADD COLUMN depth_max REAL NOT NULL DEFAULT 0"
    )
  if "quote_version_scope" not in existing_artifact_columns:
    conn.execute(
      f"ALTER TABLE {TABLE_ARTIFACT} "
      "ADD COLUMN quote_version_scope TEXT NOT NULL DEFAULT '*'"
    )
  if "maker_execution_costs" not in existing_artifact_columns:
    conn.execute(
      f"ALTER TABLE {TABLE_ARTIFACT} "
      "ADD COLUMN maker_execution_costs TEXT NOT NULL DEFAULT '{}'"
    )
  if "fok_execution_costs" not in existing_artifact_columns:
    conn.execute(
      f"ALTER TABLE {TABLE_ARTIFACT} "
      "ADD COLUMN fok_execution_costs TEXT NOT NULL DEFAULT '{}'"
    )
  if commit:
    conn.commit()


def commit_delta_v2_fill_distribution_artifact(
  conn: sqlite3.Connection,
  *,
  artifact: DeltaV2FillDistributionArtifact | Mapping[str, Any],
  promotion: Mapping[str, Any],
) -> dict[str, Any]:
  """Append artifact + one promotion record.

  This function is append-only and idempotent for repeated identical writes.
  """
  governed = (
    artifact
    if isinstance(artifact, DeltaV2FillDistributionArtifact)
    else DeltaV2FillDistributionArtifact.from_mapping(artifact)
  )
  validated = _validate_promotion(promotion, governed)
  _ensure_schema(conn, commit=False)

  savepoint = f"delta_v2_fill_commit_{id(conn):x}"
  conn.execute(f"SAVEPOINT {savepoint}")
  try:
    before = conn.total_changes

    existing_id = conn.execute(
      f"SELECT artifact_id, content_sha256 FROM {TABLE_ARTIFACT} WHERE artifact_id=?",
      (governed.artifact_id,),
    ).fetchone()
    if existing_id is not None and existing_id[1] != governed.content_sha256:
      raise ContractError("delta_v2_fill_distribution_artifact_content_conflict")
    existing_hash = conn.execute(
      f"SELECT artifact_id FROM {TABLE_ARTIFACT} WHERE content_sha256=?",
      (governed.content_sha256,),
    ).fetchone()
    if existing_hash is not None and str(existing_hash[0]) != governed.artifact_id:
      raise ContractError("delta_v2_fill_distribution_artifact_duplicate_hash")

    artifact_json = _json(
      1_000_000,
      governed.to_dict(),
      name="artifact_json",
    )
    if existing_id is None:
      conn.execute(
        f"INSERT INTO {TABLE_ARTIFACT}("
        "artifact_id,content_sha256,schema,station_id,stage,season_bin,"
        "maturity_bin,liquidity_bin,bucket,price,timeout_seconds,queue_ahead_min,"
        "queue_ahead_max,spread_min,spread_max,depth_min,depth_max,quote_version_scope,"
        "oos_gate_version,oos_gate_passed,"
        "consecutive_forward_windows,compared_baselines,oos_metrics,sample_n,holdout_n,"
        "trained_through_utc,valid_from_utc,valid_until_utc,authority_scope,"
        "fill_distribution_artifact_sha256,maker_execution_costs,fok_execution_costs,"
        "artifact_json"
        ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
          governed.artifact_id,
          governed.content_sha256,
          FILL_DISTRIBUTION_ARTIFACT_SCHEMA,
          governed.station_id,
          governed.stage,
          governed.season_bin,
          governed.maturity_bin,
          governed.liquidity_bin,
          governed.bucket,
          governed.price,
          governed.timeout_seconds,
          governed.queue_ahead_min,
          governed.queue_ahead_max,
          governed.spread_min,
          governed.spread_max,
          governed.depth_min,
          governed.depth_max,
          governed.quote_version_scope,
          governed.oos_gate_version,
          int(governed.oos_gate_passed),
          governed.consecutive_forward_windows,
          json.dumps(list(governed.compared_baselines), ensure_ascii=False, sort_keys=True),
          json.dumps(governed.oos_metrics, ensure_ascii=False, sort_keys=True),
          governed.sample_n,
          governed.holdout_n,
          governed.trained_through_utc,
          governed.valid_from_utc,
          governed.valid_until_utc,
          governed.authority_scope,
          governed.fill_distribution_artifact_sha256,
          json.dumps(governed.maker_execution_costs, ensure_ascii=False, sort_keys=True),
          json.dumps(governed.fok_execution_costs, ensure_ascii=False, sort_keys=True),
          artifact_json,
        ),
      )

    existing_promotion = conn.execute(
      f"SELECT content_sha256, decision_seq FROM {TABLE_PROMOTION} WHERE promotion_id=?",
      (validated["promotion_id"],),
    ).fetchone()
    if existing_promotion is None:
      latest = conn.execute(
        f"SELECT MAX(decision_seq) FROM {TABLE_PROMOTION} WHERE artifact_id=?",
        (governed.artifact_id,),
      ).fetchone()
      if latest and latest[0] is not None and validated["decision_seq"] <= int(latest[0]):
        raise ContractError("delta_v2_fill_distribution_promotion_seq_not_monotonic")
      conn.execute(
        f"INSERT INTO {TABLE_PROMOTION}("
        "promotion_id,artifact_id,artifact_content_sha256,decision_seq,decision,"
        "gate_version,decided_at_utc,receipt_sha256,content_sha256,created_at_utc"
        ") VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
          validated["promotion_id"],
          governed.artifact_id,
          governed.content_sha256,
          validated["decision_seq"],
          validated["decision"],
          validated["gate_version"],
          validated["decided_at_utc"],
          validated["receipt_sha256"],
          validated["content_sha256"],
          datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        ),
      )
    else:
      if validated["content_sha256"] != str(existing_promotion[0]):
        raise ContractError("delta_v2_fill_distribution_promotion_conflict")
    wrote = conn.total_changes - before
    conn.execute(f"RELEASE {savepoint}")
  except Exception:
    conn.execute(f"ROLLBACK TO {savepoint}")
    conn.execute(f"RELEASE {savepoint}")
    raise
  return {
    "schema": "delta_basket_score_fill_distribution_authority_ingress_v1",
    "artifact_id": governed.artifact_id,
    "artifact_content_sha256": governed.content_sha256,
    "promotion_id": validated["promotion_id"],
    "rows_written": wrote,
    "state": "COMMITTED" if wrote else "UNCHANGED_ZERO_WRITE",
    "execution_mode": "production_no_live",
    "live_submit_authorized": False,
    "calls_order_gateway": False,
    "orders_submitted": 0,
  }


def resolve_delta_v2_fill_distribution(
  conn: sqlite3.Connection,
  *,
  station_id: str,
  stage: str,
  season_bin: str,
  maturity_bin: str,
  liquidity_bin: str,
  bucket: str,
  price: float,
  timeout_seconds: float,
  queue_ahead_shares: float,
  spread: float,
  depth: float,
  quote_version: str,
  evaluation_at_utc: str,
  rung_offset_ticks: float = 0.0,
  required_authority_scope: str = "PRODUCTION_NO_LIVE",
  policy_fill_distribution_artifact_sha256: str | None = None,
) -> DeltaV2FillDistributionArtifact:
  if _required_text("required_authority_scope", required_authority_scope) not in AUTHORITY_SCOPES:
    raise ContractError("delta_v2_fill_distribution_required_scope_invalid")
  evaluated_at = _timestamp("evaluation_at_utc", evaluation_at_utc)
  normalized = {
    "station_id": _required_text("station_id", station_id).upper(),
    "stage": _required_text("stage", stage).upper(),
    "season_bin": _required_text("season_bin", season_bin),
    "maturity_bin": _required_text("maturity_bin", maturity_bin),
    "liquidity_bin": _required_text("liquidity_bin", liquidity_bin),
    "bucket": _required_text("bucket", bucket),
    "price": _float("price", price),
    "timeout_seconds": _positive("timeout_seconds", timeout_seconds),
    "queue_ahead_shares": _nonnegative(
      "queue_ahead_shares", queue_ahead_shares
    ),
    "spread": _nonnegative("spread", spread),
    # Where this order stands in the book, in ticks above the bid.  Zero --
    # resting at the bid -- is the default so a caller that has not been
    # taught the ladder resolves the deepest rung rather than silently
    # resolving whichever band happens to contain zero.
    "rung_offset_ticks": _float("rung_offset_ticks", rung_offset_ticks),
    "depth": _positive("depth", depth),
    "quote_version": _required_text("quote_version", quote_version),
  }
  if not (0.0 < normalized["price"] < 1.0):
    raise ContractError("delta_v2_fill_distribution_price_out_of_range")

  if policy_fill_distribution_artifact_sha256 is not None:
    normalized_policy = _sha256_text(
      "policy_fill_distribution_artifact_sha256",
      policy_fill_distribution_artifact_sha256,
    )
  else:
    normalized_policy = None

  # ``price`` and ``timeout_seconds`` are stored columns *and* the first two
  # things the loop below discards a row for, so leaving them out of the
  # WHERE clause means reading and JSON-parsing every price and timeout in
  # the scope to keep one of them.  That was invisible while the table held
  # 7,291 rows.  Once the rung dimension took it to 86,427, a single
  # resolution read 20,783 rows and parsed 42.3 MB of artifact_json in
  # 1.6 seconds -- per leg, per rung, per candidate -- and a production
  # cycle that ran in 174s did not finish in 36 minutes.  Pushing the two
  # equality tests down returns one row instead of 20,783.
  #
  # The push-down is exactly the loop's own test, not a narrower one: every
  # one of the 86,427 stored rows has
  # ``json_extract(artifact_json,'$.price') IS price`` and the same for
  # timeout_seconds, verified before this was written, because both the
  # column and the JSON are serialized from one Python float.  The loop
  # keeps its checks regardless, so the SQL is a filter and never the
  # authority.
  rows = conn.execute(
    f"SELECT artifact_json FROM {TABLE_ARTIFACT} "
    "WHERE station_id IN (?, '*') AND stage IN (?, '*') "
    "AND season_bin=? AND maturity_bin=? "
    "AND liquidity_bin=? AND bucket IN (?, '*') AND authority_scope=? "
    "AND price=? AND timeout_seconds=?",
    (
      normalized["station_id"],
      normalized["stage"],
      normalized["season_bin"],
      normalized["maturity_bin"],
      normalized["liquidity_bin"],
      normalized["bucket"],
      required_authority_scope,
      normalized["price"],
      normalized["timeout_seconds"],
    ),
  ).fetchall()

  if not rows:
    raise ContractError("delta_v2_fill_distribution_activation_missing")
  scope_candidate_seen = False
  active_candidates: list[tuple[int, dict[str, Any]]] = []
  for row in rows:
    parsed = json.loads(str(row[0]))
    if not isinstance(parsed, Mapping):
      raise ContractError("delta_v2_fill_distribution_artifact_stored_invalid")
    if (
      float(parsed.get("price")) != normalized["price"]
      or float(parsed.get("timeout_seconds")) != normalized["timeout_seconds"]
    ):
      continue
    parsed_qmin = parsed.get("queue_ahead_min")
    parsed_qmax = parsed.get("queue_ahead_max")
    parsed_spread_min = parsed.get("spread_min")
    parsed_spread_max = parsed.get("spread_max")
    parsed_depth_min = parsed.get("depth_min")
    parsed_depth_max = parsed.get("depth_max")
    parsed_scope = parsed.get("quote_version_scope")
    if (
      parsed_qmin is None
      or parsed_qmax is None
      or parsed_spread_min is None
      or parsed_spread_max is None
      or parsed_depth_min is None
      or parsed_depth_max is None
      or parsed_scope is None
    ):
      continue
    else:
      try:
        qmin = float(parsed_qmin)
        qmax = float(parsed_qmax)
        spread_min = float(parsed_spread_min)
        spread_max = float(parsed_spread_max)
        depth_min = float(parsed_depth_min)
        depth_max = float(parsed_depth_max)
      except (TypeError, ValueError) as exc:
        raise ContractError("delta_v2_fill_distribution_artifact_range_invalid") from exc
      scope = str(parsed_scope)
    if not (qmin <= qmax and spread_min <= spread_max and depth_min <= depth_max):
      raise ContractError("delta_v2_fill_distribution_artifact_range_invalid")
    if not (qmin <= normalized["queue_ahead_shares"] <= qmax):
      continue
    if not (spread_min <= normalized["spread"] <= spread_max):
      continue
    if not (depth_min <= normalized["depth"] <= depth_max):
      continue
    # An artifact that names a rung band may only be spent on rungs inside
    # it.  One that names none carries the unrestricted band, which is the
    # claim every pre-2026-08-22 artifact was implicitly making, so those
    # keep resolving for every rung exactly as before.
    rung_min = parsed.get("rung_offset_ticks_min")
    rung_max = parsed.get("rung_offset_ticks_max")
    if rung_min is None or rung_max is None:
      rung_min, rung_max = (
        UNRESTRICTED_RUNG_OFFSET_MIN, UNRESTRICTED_RUNG_OFFSET_MAX
      )
    try:
      rung_min = float(rung_min)
      rung_max = float(rung_max)
    except (TypeError, ValueError) as exc:
      raise ContractError(
        "delta_v2_fill_distribution_artifact_range_invalid"
      ) from exc
    if rung_max < rung_min:
      raise ContractError("delta_v2_fill_distribution_artifact_range_invalid")
    if not (rung_min <= normalized["rung_offset_ticks"] <= rung_max):
      continue
    if scope != "*":
      if scope != normalized["quote_version"]:
        continue
    scope_candidate_seen = True
    if parsed["valid_from_utc"] <= evaluated_at < parsed["valid_until_utc"]:
      specificity = sum(
        (
          str(parsed["station_id"]).upper() == normalized["station_id"],
          str(parsed["stage"]).upper() == normalized["stage"],
          str(parsed["bucket"]) == normalized["bucket"],
          # A curve fitted for this rung band outranks one fitted for every
          # rung.  Without this the unrestricted legacy artifact ties with
          # the rung-specific one and the tie-break falls to valid_from,
          # which is a date, not a statement about fit.
          (
            rung_min > UNRESTRICTED_RUNG_OFFSET_MIN
            or rung_max < UNRESTRICTED_RUNG_OFFSET_MAX
          ),
        )
      )
      active_candidates.append((specificity, dict(parsed)))

  if not scope_candidate_seen:
    raise ContractError("delta_v2_fill_distribution_activation_scope_miss")
  if not active_candidates:
    raise ContractError("delta_v2_fill_distribution_not_active")
  best_specificity = max(item[0] for item in active_candidates)
  most_specific = [
    item[1] for item in active_candidates if item[0] == best_specificity
  ]
  best_valid_from = max(
    str(item["valid_from_utc"]) for item in most_specific
  )
  finalists = [
    item
    for item in most_specific
    if str(item["valid_from_utc"]) == best_valid_from
  ]
  if len(finalists) > 1:
    raise ContractError("delta_v2_fill_distribution_activation_ambiguous")
  candidate = finalists[0]

  artifact = DeltaV2FillDistributionArtifact.from_mapping(candidate)

  if (
    artifact.station_id not in {"*", normalized["station_id"]}
    or artifact.stage not in {"*", normalized["stage"]}
    or artifact.bucket not in {"*", normalized["bucket"]}
  ):
    raise ContractError("delta_v2_fill_distribution_scope_reject")

  if normalized_policy is not None and (
    str(normalized_policy).lower()
    != artifact.fill_distribution_artifact_sha256
  ):
    raise ContractError("delta_v2_fill_distribution_policy_binding_mismatch")

  promotion = conn.execute(
    f"SELECT decision,gate_version FROM {TABLE_PROMOTION} "
    "WHERE artifact_id=? AND artifact_content_sha256=? "
    "ORDER BY decision_seq DESC LIMIT 1",
    (
      artifact.artifact_id,
      artifact.content_sha256,
    ),
  ).fetchone()
  accepted_decisions = (
    {NO_LIVE_ACTIVATION_DECISION, "PROMOTED"}
    if required_authority_scope == "PRODUCTION_NO_LIVE"
    else {"PROMOTED"}
  )
  if (
    promotion is None
    or str(promotion[0]) not in accepted_decisions
    or str(promotion[1]) != artifact.oos_gate_version
  ):
    raise ContractError("delta_v2_fill_distribution_not_promoted")
  if (
    required_authority_scope == "FORMAL_PROMOTED"
    and not artifact.oos_gate_passed
  ):
    raise ContractError("delta_v2_fill_distribution_requires_oos")
  if not (artifact.valid_from_utc <= evaluated_at < artifact.valid_until_utc):
    raise ContractError("delta_v2_fill_distribution_not_active")
  return artifact


__all__ = [
  "FILL_DISTRIBUTION_ARTIFACT_SCHEMA",
  "FILL_DISTRIBUTION_PROMOTION_SCHEMA",
  "FILL_DISTRIBUTION_DDL",
  "DeltaV2FillDistributionArtifact",
  "NO_LIVE_ACTIVATION_DECISION",
  "commit_delta_v2_fill_distribution_artifact",
  "resolve_delta_v2_fill_distribution",
  "upgrade_fill_promotion_decision_constraint",
]
