"""Canonical, unpublished P1_BASE fact contract.

This module deliberately defines only the immutable P1_BASE boundary.  It
does not collect observations, select their priority winner, write the SSOT,
or derive probabilities.  Those actions belong to the single ingress and the
lead-specific builders.  A caller must therefore provide every parent and
surface-lineage field explicitly; this contract never supplies source values
or treats one source lane as another.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_FLOOR, ROUND_HALF_EVEN
import json
import sqlite3
from typing import Any, Mapping

from .contracts import ContractError, content_sha256


CONTRACT_SCHEMA = "bayes_basket_target_canonical_p1_base_contract_v1"
FACT_TYPE = "p1_base"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
STATE = "MATERIALIZED_UNPUBLISHED"
SETTLEMENT_TIMEZONE = "America/New_York"
Q = Decimal("0.000000000001")

# ``MADIS`` and ``IEM`` are not aliases for AWC.  Keeping the canonical lane
# and the publication class together makes a false relabel fail at the only
# P1_BASE contract boundary.
SURFACE_SOURCE_POLICY = {
    "AWC": {"source_class": "official_awc", "report_types": {"METAR", "SPECI"}},
    "MADIS": {"source_class": "official_republisher", "report_types": {"ASOS_HF"}},
    "IEM": {"source_class": "aggregator_republisher", "report_types": {"ASOS_1MIN"}},
}
PROJECTION_METHOD = "HARD_LOWER_BOUND_EXCLUSION_LARGEST_REMAINDER_1E12_V1"

FACT_FIELDS = {
    "station_id",
    "event_date",
    "settlement_timezone",
    "p0_forecast_snapshot_content_sha256",
    "market_bucket_bridge_content_sha256",
    "parent_bucket_probability_vector_sha256",
    "surface_observation_winner_content_sha256",
    "p1_oos_artifact_content_sha256",
    "surface_observation_source_lane",
    "surface_observation_source_class",
    "surface_observation_report_type",
    "surface_observation_valid_at_utc",
    "surface_observation_committed_at_utc",
    "decision_at_utc",
    "freshness_checked_at_utc",
    "freshness_expires_at_utc",
    "reported_high_c",
    "hard_lower_bound_c",
    "temperature_resolution_c",
    "hard_lower_bound_method",
    "hard_excluded_ordinals",
    "bucket_probabilities",
    "bucket_probability_vector_sha256",
    "probability_projection_method",
    "authority_scope",
    "state",
    "content_sha256",
}


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"p1_base_{label}_missing")
    return result


def _sha(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"p1_base_{label}_invalid")
    return result


def _date(value: Any, label: str) -> str:
    try:
        return date.fromisoformat(_text(value, label)).isoformat()
    except ValueError as exc:
        raise ContractError(f"p1_base_{label}_invalid") from exc


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"p1_base_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"p1_base_{label}_timezone_required")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _optional_sha(value: Any, label: str) -> str | None:
    return None if value is None else _sha(value, label)


def _decimal(value: Any, label: str) -> Decimal:
    if isinstance(value, bool):
        raise ContractError(f"p1_base_{label}_invalid")
    try:
        result = Decimal(_text(value, label))
    except InvalidOperation as exc:
        raise ContractError(f"p1_base_{label}_invalid") from exc
    if not result.is_finite():
        raise ContractError(f"p1_base_{label}_invalid")
    return result


def _num(value: Decimal) -> str:
    return format(value.quantize(Q, rounding=ROUND_HALF_EVEN), ".12f")


def _bucket_probabilities(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) < 2:
        raise ContractError("p1_base_bucket_probabilities_invalid")
    normalized: list[dict[str, Any]] = []
    for ordinal, item in enumerate(value):
        if not isinstance(item, Mapping) or set(item) != {
            "ordinal", "outcome", "probability"
        } or isinstance(item["ordinal"], bool) or item["ordinal"] != ordinal:
            raise ContractError("p1_base_bucket_probability_fields_invalid")
        probability = _decimal(item["probability"], "bucket_probability")
        if probability < 0 or probability > 1:
            raise ContractError("p1_base_bucket_probability_invalid")
        normalized.append({
            "ordinal": ordinal,
            "outcome": _text(item["outcome"], "bucket_outcome"),
            "probability": _num(probability),
        })
    if sum(Decimal(item["probability"]) for item in normalized) != Decimal("1"):
        raise ContractError("p1_base_bucket_probability_sum_invalid")
    return normalized


def _hard_excluded_ordinals(value: Any, *, probabilities: list[dict[str, Any]]) -> list[int]:
    if not isinstance(value, list):
        raise ContractError("p1_base_hard_excluded_ordinals_invalid")
    normalized: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            raise ContractError("p1_base_hard_excluded_ordinals_invalid")
        normalized.append(item)
    if normalized != sorted(set(normalized)) or any(
        item < 0 or item >= len(probabilities) for item in normalized
    ):
        raise ContractError("p1_base_hard_excluded_ordinals_invalid")
    if any(Decimal(probabilities[item]["probability"]) != 0 for item in normalized):
        raise ContractError("p1_base_hard_exclusion_probability_mismatch")
    return normalized


@dataclass(frozen=True)
class CanonicalP1BaseFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalP1BaseFact":
        if set(raw) != FACT_FIELDS:
            raise ContractError("p1_base_fact_fields_mismatch")
        value = dict(raw)
        value["station_id"] = _text(value["station_id"], "station_id").upper()
        value["event_date"] = _date(value["event_date"], "event_date")
        value["settlement_timezone"] = _text(
            value["settlement_timezone"], "settlement_timezone"
        )
        if value["settlement_timezone"] != SETTLEMENT_TIMEZONE:
            raise ContractError("p1_base_settlement_timezone_invalid")
        for field in (
            "p0_forecast_snapshot_content_sha256",
            "market_bucket_bridge_content_sha256",
            "parent_bucket_probability_vector_sha256",
            "surface_observation_winner_content_sha256",
            "bucket_probability_vector_sha256",
        ):
            value[field] = _sha(value[field], field)
        value["p1_oos_artifact_content_sha256"] = _optional_sha(
            value["p1_oos_artifact_content_sha256"], "p1_oos_artifact_content_sha256"
        )
        lane = _text(
            value["surface_observation_source_lane"], "surface_observation_source_lane"
        ).upper()
        source_class = _text(
            value["surface_observation_source_class"], "surface_observation_source_class"
        )
        policy = SURFACE_SOURCE_POLICY.get(lane)
        if policy is None or source_class != policy["source_class"]:
            raise ContractError("p1_base_surface_source_lineage_invalid")
        # An ASOS republisher may serve P1 only after it has its own exact OOS
        # artifact.  AWC is the documented fallback and does not falsely claim
        # an ASOS artifact when there is none.
        if lane in {"IEM", "MADIS"} and value["p1_oos_artifact_content_sha256"] is None:
            raise ContractError("p1_base_asos_oos_artifact_required")
        value["surface_observation_source_lane"] = lane
        value["surface_observation_source_class"] = source_class
        value["surface_observation_report_type"] = _text(
            value["surface_observation_report_type"], "surface_observation_report_type"
        ).upper()
        if value["surface_observation_report_type"] not in policy["report_types"]:
            raise ContractError("p1_base_surface_report_type_invalid")
        for field in (
            "surface_observation_valid_at_utc",
            "surface_observation_committed_at_utc",
            "decision_at_utc",
            "freshness_checked_at_utc",
            "freshness_expires_at_utc",
        ):
            value[field] = _utc(value[field], field)
        if not (
            value["surface_observation_valid_at_utc"]
            <= value["surface_observation_committed_at_utc"]
            <= value["decision_at_utc"]
            == value["freshness_checked_at_utc"]
            < value["freshness_expires_at_utc"]
        ):
            raise ContractError("p1_base_clock_order_invalid")
        reported = _decimal(value["reported_high_c"], "reported_high_c")
        lower = _decimal(value["hard_lower_bound_c"], "hard_lower_bound_c")
        resolution = _decimal(value["temperature_resolution_c"], "temperature_resolution_c")
        if resolution <= 0 or lower != reported - resolution / 2:
            raise ContractError("p1_base_hard_lower_bound_mismatch")
        value["reported_high_c"] = _num(reported)
        value["hard_lower_bound_c"] = _num(lower)
        value["temperature_resolution_c"] = _num(resolution)
        if value["hard_lower_bound_method"] != "ROUND_NEAREST_RESOLUTION_LOWER_EDGE":
            raise ContractError("p1_base_hard_lower_bound_method_invalid")
        if value["probability_projection_method"] != PROJECTION_METHOD:
            raise ContractError("p1_base_projection_method_invalid")
        probabilities = _bucket_probabilities(value["bucket_probabilities"])
        if value["bucket_probability_vector_sha256"] != content_sha256(probabilities):
            raise ContractError("p1_base_bucket_probability_hash_mismatch")
        value["bucket_probabilities"] = probabilities
        value["hard_excluded_ordinals"] = _hard_excluded_ordinals(
            value["hard_excluded_ordinals"], probabilities=probabilities
        )
        if (
            value["authority_scope"] != AUTHORITY_SCOPE
            or value["state"] != STATE
        ):
            raise ContractError("p1_base_authority_state_invalid")
        supplied = _sha(value["content_sha256"], "content_sha256")
        if supplied != content_sha256({
            key: item for key, item in value.items() if key != "content_sha256"
        }):
            raise ContractError("p1_base_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(str(self.raw[key]) for key in (
            "station_id",
            "event_date",
            "settlement_timezone",
            "p0_forecast_snapshot_content_sha256",
            "surface_observation_winner_content_sha256",
            "decision_at_utc",
        ))


@dataclass(frozen=True)
class CanonicalP1BaseContract:
    as_of_utc: str
    builder_id: str
    p1_base_facts: tuple[CanonicalP1BaseFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalP1BaseContract":
        if (
            set(raw) != {
                "schema", "as_of_utc", "builder_id", "p1_base_facts", "content_sha256"
            }
            or raw["schema"] != CONTRACT_SCHEMA
        ):
            raise ContractError("p1_base_contract_invalid")
        facts_raw = raw["p1_base_facts"]
        if not isinstance(facts_raw, list) or not facts_raw:
            raise ContractError("p1_base_fact_required")
        facts = tuple(CanonicalP1BaseFact.from_mapping(item) for item in facts_raw)
        if len({item.scope_key for item in facts}) != len(facts):
            raise ContractError("p1_base_scope_duplicate")
        as_of = _utc(raw["as_of_utc"], "contract_as_of_utc")
        if any(item.raw["decision_at_utc"] > as_of for item in facts):
            raise ContractError("p1_base_fact_after_asof")
        supplied = _sha(raw["content_sha256"], "contract_content_sha256")
        if supplied != content_sha256({
            key: item for key, item in raw.items() if key != "content_sha256"
        }):
            raise ContractError("p1_base_contract_hash_mismatch")
        return cls(as_of, _text(raw["builder_id"], "builder_id"), facts, supplied)


def _load(conn: sqlite3.Connection, fact_type: str, sha: str, cls: Any) -> Any:
    rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type=? AND content_sha256=?",
        (fact_type, _sha(sha, f"{fact_type}_sha256")),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError(f"p1_base_{fact_type}_not_unique")
    return cls.from_mapping(json.loads(str(rows[0][0])))


def _largest_remainder_probabilities(
    weights: list[Decimal], outcomes: list[str]
) -> list[dict[str, Any]]:
    total = sum(weights)
    if total <= 0:
        raise ContractError("p1_base_all_parent_probability_excluded")
    scale = Decimal(10) ** 12
    exact = [value / total * scale for value in weights]
    units = [int(value.to_integral_value(rounding=ROUND_FLOOR)) for value in exact]
    remaining = int(scale) - sum(units)
    order = sorted(range(len(exact)), key=lambda index: (-(exact[index] - units[index]), index))
    for index in order[:remaining]:
        units[index] += 1
    return [
        {
            "ordinal": ordinal,
            "outcome": outcomes[ordinal],
            "probability": format(Decimal(units[ordinal]) / scale, ".12f"),
        }
        for ordinal in range(len(units))
    ]


def build_p1_base_contract(
    conn: sqlite3.Connection,
    *,
    p0_forecast_snapshot_content_sha256: str,
    surface_observation_winner_content_sha256: str,
    decision_at_utc: str,
    freshness_seconds: int = 900,
) -> dict[str, Any]:
    """Project P0 through the winner's conservative hard lower bound only."""

    from .market_bucket_bridge import CanonicalMarketBucketBridgeFact
    from .p0_forecast_snapshot import (
        CanonicalP0ForecastSnapshotFact,
        assert_p0_forecast_snapshot_rebuild,
    )
    from .surface_observation_contract import CanonicalSurfaceObservationWinnerFact

    p0 = _load(
        conn, "p0_forecast_snapshot", p0_forecast_snapshot_content_sha256,
        CanonicalP0ForecastSnapshotFact,
    )
    assert_p0_forecast_snapshot_rebuild(conn, p0)
    winner = _load(
        conn, "surface_observation_winner", surface_observation_winner_content_sha256,
        CanonicalSurfaceObservationWinnerFact,
    )
    bridge = _load(
        conn, "market_bucket_bridge", p0.raw["market_bucket_bridge_content_sha256"],
        CanonicalMarketBucketBridgeFact,
    )
    scope = ("station_id", "event_date", "settlement_timezone")
    if any(
        p0.raw[field] != winner.raw[field] or p0.raw[field] != bridge.raw[field]
        for field in scope
    ):
        raise ContractError("p1_base_parent_scope_mismatch")
    if winner.raw["selected_source_lane"] in {"IEM", "MADIS"}:
        # No canonical P1 OOS artifact fact type exists in V17 yet.  The
        # business-table hash must never be accepted as a canonical parent.
        raise ContractError("p1_base_asos_canonical_oos_artifact_unavailable")
    if int(freshness_seconds) < 1 or int(freshness_seconds) > 3600:
        raise ContractError("p1_base_freshness_seconds_invalid")
    decision = _utc(decision_at_utc, "builder_decision_at_utc")
    if not (
        p0.raw["forecast_decision_at_utc"]
        <= winner.raw["selected_valid_at_utc"]
        <= winner.raw["selected_committed_at_utc"]
        <= winner.raw["winner_as_of_utc"]
        <= decision
    ):
        raise ContractError("p1_base_parent_clock_order_invalid")
    parent = p0.raw["bucket_probabilities"]
    buckets = bridge.raw["bucket_contracts"]
    if len(parent) != len(buckets) or any(
        item["ordinal"] != bucket["ordinal"] or item["outcome"] != bucket["outcome"]
        for item, bucket in zip(parent, buckets)
    ):
        raise ContractError("p1_base_parent_bucket_contract_mismatch")
    lower_bound = Decimal(winner.raw["hard_lower_bound_c"])
    excluded = [
        int(bucket["ordinal"])
        for bucket in buckets
        if bucket["upper_c"] is not None and Decimal(bucket["upper_c"]) <= lower_bound
    ]
    weights = [
        Decimal("0") if item["ordinal"] in excluded else Decimal(item["probability"])
        for item in parent
    ]
    probabilities = _largest_remainder_probabilities(
        weights, [str(item["outcome"]) for item in parent]
    )
    expires = (
        datetime.fromisoformat(decision.replace("Z", "+00:00"))
        + timedelta(seconds=int(freshness_seconds))
    ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    identity = {
        "station_id": p0.raw["station_id"],
        "event_date": p0.raw["event_date"],
        "settlement_timezone": p0.raw["settlement_timezone"],
        "p0_forecast_snapshot_content_sha256": p0.content_sha256,
        "market_bucket_bridge_content_sha256": bridge.content_sha256,
        "parent_bucket_probability_vector_sha256": p0.raw[
            "bucket_probability_vector_sha256"
        ],
        "surface_observation_winner_content_sha256": winner.content_sha256,
        "p1_oos_artifact_content_sha256": None,
        "surface_observation_source_lane": winner.raw["selected_source_lane"],
        "surface_observation_source_class": winner.raw["selected_source_class"],
        "surface_observation_report_type": winner.raw["selected_report_type"],
        "surface_observation_valid_at_utc": winner.raw["selected_valid_at_utc"],
        "surface_observation_committed_at_utc": winner.raw[
            "selected_committed_at_utc"
        ],
        "decision_at_utc": decision,
        "freshness_checked_at_utc": decision,
        "freshness_expires_at_utc": expires,
        "reported_high_c": winner.raw["reported_high_c"],
        "hard_lower_bound_c": winner.raw["hard_lower_bound_c"],
        "temperature_resolution_c": winner.raw["temperature_resolution_c"],
        "hard_lower_bound_method": winner.raw["hard_lower_bound_method"],
        "hard_excluded_ordinals": excluded,
        "bucket_probabilities": probabilities,
        "bucket_probability_vector_sha256": content_sha256(probabilities),
        "probability_projection_method": PROJECTION_METHOD,
        "authority_scope": AUTHORITY_SCOPE,
        "state": STATE,
    }
    fact = {**identity, "content_sha256": content_sha256(identity)}
    contract_identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": decision,
        "builder_id": "canonical.p1_base.v1",
        "p1_base_facts": [fact],
    }
    packet = {**contract_identity, "content_sha256": content_sha256(contract_identity)}
    CanonicalP1BaseContract.from_mapping(packet)
    return packet


__all__ = [
    "AUTHORITY_SCOPE",
    "CONTRACT_SCHEMA",
    "FACT_FIELDS",
    "FACT_TYPE",
    "CanonicalP1BaseContract",
    "CanonicalP1BaseFact",
    "SETTLEMENT_TIMEZONE",
    "STATE",
    "SURFACE_SOURCE_POLICY",
    "PROJECTION_METHOD",
    "build_p1_base_contract",
]
