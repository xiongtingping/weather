"""Canonical unpublished pure-YES basket support plan.

The plan is the durable weather-side handoff to the runtime.  It binds an
already materialized P1_BASE to the official market bucket bridge and freezes
only the eligible YES support set.  Prices, quantities, capital reservations,
order intents and fills remain realtime facts and are deliberately absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import json
import sqlite3
from typing import Any, Mapping

from .contracts import ContractError, content_sha256


CONTRACT_SCHEMA = "bayes_basket_target_canonical_pure_yes_basket_plan_contract_v1"
FACT_TYPE = "pure_yes_basket_plan"
AUTHORITY_SCOPE = "PRODUCTION_NO_LIVE_ONLY"
STATE = "MATERIALIZED_UNPUBLISHED"
SETTLEMENT_TIMEZONE = "America/New_York"
POLICY_VERSION = "CONSERVATIVE_PURE_YES_BASELINE_V1"
SELECTION_METHOD = "P1_MAIN_PLUS_UNREACHED_POSITIVE_PREDECESSORS_V1"
MAIN_SELECTION_METHOD = "MAX_PROBABILITY_LOWEST_ORDINAL_TIEBREAK_V1"
EXECUTION_GATE = "REQUIRES_FRESH_OFFICIAL_CLOB_WS"
SUPPORT_RADIUS = 2

POLICY_ARTIFACT = {
    "policy_version": POLICY_VERSION,
    "selection_method": SELECTION_METHOD,
    "main_selection_method": MAIN_SELECTION_METHOD,
    "support_radius": SUPPORT_RADIUS,
    "probability_stage": "P1_BASE",
    "buy_side": "YES_ONLY",
    "requires_fresh_official_clob_ws": True,
}
POLICY_ARTIFACT_CONTENT_SHA256 = content_sha256(POLICY_ARTIFACT)
_FACT_FIELDS = {
    "station_id", "event_date", "settlement_timezone", "probability_stage",
    "p1_base_content_sha256", "p0_forecast_snapshot_content_sha256",
    "market_bucket_bridge_content_sha256",
    "surface_observation_winner_content_sha256",
    "bucket_probability_vector_sha256", "source_p1_decision_at_utc",
    "source_p1_freshness_expires_at_utc", "plan_at_utc", "policy_version",
    "policy_artifact_content_sha256",
    "selection_method", "main_selection_method", "support_radius",
    "main_ordinal", "reported_high_ordinal", "hard_excluded_ordinals",
    "base_ordinals", "underreach_ordinals", "upside_breakthrough_ordinals",
    "upside_breakthrough_state",
    "candidate_ordinals", "candidate_ordinal_set_sha256", "execution_gate", "buy_yes_only",
    "buy_no_intent_emitted", "orders_submitted", "authority_scope", "state",
    "content_sha256",
}


def _text(value: Any, label: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ContractError(f"pure_yes_basket_plan_{label}_missing")
    return result


def _sha(value: Any, label: str) -> str:
    result = _text(value, label).lower()
    if len(result) != 64 or any(char not in "0123456789abcdef" for char in result):
        raise ContractError(f"pure_yes_basket_plan_{label}_invalid")
    return result


def _utc(value: Any, label: str) -> str:
    text = _text(value, label)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"pure_yes_basket_plan_{label}_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"pure_yes_basket_plan_{label}_timezone_required")
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _date(value: Any, label: str) -> str:
    try:
        return date.fromisoformat(_text(value, label)).isoformat()
    except ValueError as exc:
        raise ContractError(f"pure_yes_basket_plan_{label}_invalid") from exc


def _ordinals(value: Any, label: str) -> list[int]:
    if not isinstance(value, list) or any(
        isinstance(item, bool) or not isinstance(item, int) for item in value
    ):
        raise ContractError(f"pure_yes_basket_plan_{label}_invalid")
    result = list(value)
    if result != sorted(set(result)) or any(item < 0 for item in result):
        raise ContractError(f"pure_yes_basket_plan_{label}_invalid")
    return result


@dataclass(frozen=True)
class CanonicalPureYesBasketPlanFact:
    raw: dict[str, Any]

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanonicalPureYesBasketPlanFact":
        if set(raw) != _FACT_FIELDS:
            raise ContractError("pure_yes_basket_plan_fact_fields_mismatch")
        value = dict(raw)
        value["station_id"] = _text(value["station_id"], "station_id").upper()
        value["event_date"] = _date(value["event_date"], "event_date")
        value["settlement_timezone"] = _text(
            value["settlement_timezone"], "settlement_timezone"
        )
        if value["settlement_timezone"] != SETTLEMENT_TIMEZONE:
            raise ContractError("pure_yes_basket_plan_settlement_timezone_invalid")
        for field in (
            "p1_base_content_sha256", "p0_forecast_snapshot_content_sha256",
            "market_bucket_bridge_content_sha256",
            "surface_observation_winner_content_sha256",
            "bucket_probability_vector_sha256", "policy_artifact_content_sha256",
            "candidate_ordinal_set_sha256",
        ):
            value[field] = _sha(value[field], field)
        for field in (
            "source_p1_decision_at_utc", "source_p1_freshness_expires_at_utc",
            "plan_at_utc",
        ):
            value[field] = _utc(value[field], field)
        if not (
            value["source_p1_decision_at_utc"]
            <= value["plan_at_utc"]
            < value["source_p1_freshness_expires_at_utc"]
        ):
            raise ContractError("pure_yes_basket_plan_clock_order_invalid")
        if (
            value["probability_stage"] != "P1_BASE"
            or value["policy_version"] != POLICY_VERSION
            or value["selection_method"] != SELECTION_METHOD
            or value["main_selection_method"] != MAIN_SELECTION_METHOD
            or value["support_radius"] != SUPPORT_RADIUS
            or value["policy_artifact_content_sha256"]
            != POLICY_ARTIFACT_CONTENT_SHA256
            or value["execution_gate"] != EXECUTION_GATE
            or value["upside_breakthrough_state"]
            != "BLOCKED_NO_REPORTED_HIGH_ADVANCEMENT_EVIDENCE"
            or value["buy_yes_only"] is not True
            or value["buy_no_intent_emitted"] is not False
            or value["orders_submitted"] != 0
            or value["authority_scope"] != AUTHORITY_SCOPE
            or value["state"] != STATE
        ):
            raise ContractError("pure_yes_basket_plan_role_invalid")
        main = value["main_ordinal"]
        reported = value["reported_high_ordinal"]
        if (
            isinstance(main, bool) or not isinstance(main, int) or main < 0
            or isinstance(reported, bool) or not isinstance(reported, int)
            or reported < 0
        ):
            raise ContractError("pure_yes_basket_plan_main_ordinal_invalid")
        excluded = _ordinals(value["hard_excluded_ordinals"], "hard_excluded_ordinals")
        value["hard_excluded_ordinals"] = excluded
        base = _ordinals(value["base_ordinals"], "base_ordinals")
        underreach = _ordinals(value["underreach_ordinals"], "underreach_ordinals")
        upside = _ordinals(
            value["upside_breakthrough_ordinals"],
            "upside_breakthrough_ordinals",
        )
        candidates = _ordinals(value["candidate_ordinals"], "candidate_ordinals")
        if (
            base != [main]
            or any(item >= main or main - item > SUPPORT_RADIUS for item in underreach)
            or upside
            or candidates != sorted(base + underreach)
            or any(item in set(excluded) for item in candidates)
            or (reported >= main and underreach)
        ):
            raise ContractError("pure_yes_basket_plan_candidate_set_invalid")
        if value["candidate_ordinal_set_sha256"] != content_sha256(candidates):
            raise ContractError("pure_yes_basket_plan_candidate_hash_mismatch")
        value["base_ordinals"] = base
        value["underreach_ordinals"] = underreach
        value["upside_breakthrough_ordinals"] = upside
        value["candidate_ordinals"] = candidates
        supplied = _sha(value["content_sha256"], "content_sha256")
        if supplied != content_sha256({
            key: item for key, item in value.items() if key != "content_sha256"
        }):
            raise ContractError("pure_yes_basket_plan_content_sha256_mismatch")
        value["content_sha256"] = supplied
        return cls(value)

    @property
    def content_sha256(self) -> str:
        return str(self.raw["content_sha256"])

    @property
    def scope_key(self) -> str:
        return "|".join(str(self.raw[field]) for field in (
            "station_id", "event_date", "settlement_timezone",
            "p1_base_content_sha256", "plan_at_utc", "policy_version",
        ))


@dataclass(frozen=True)
class CanonicalPureYesBasketPlanContract:
    as_of_utc: str
    builder_id: str
    basket_plan_facts: tuple[CanonicalPureYesBasketPlanFact, ...]
    content_sha256: str

    @classmethod
    def from_mapping(
        cls, raw: Mapping[str, Any]
    ) -> "CanonicalPureYesBasketPlanContract":
        if (
            set(raw) != {
                "schema", "as_of_utc", "builder_id", "basket_plan_facts",
                "content_sha256",
            }
            or raw["schema"] != CONTRACT_SCHEMA
        ):
            raise ContractError("pure_yes_basket_plan_contract_invalid")
        facts_raw = raw["basket_plan_facts"]
        if not isinstance(facts_raw, list) or not facts_raw:
            raise ContractError("pure_yes_basket_plan_fact_required")
        facts = tuple(
            CanonicalPureYesBasketPlanFact.from_mapping(item) for item in facts_raw
        )
        if len({item.scope_key for item in facts}) != len(facts):
            raise ContractError("pure_yes_basket_plan_scope_duplicate")
        as_of = _utc(raw["as_of_utc"], "contract_as_of_utc")
        if any(item.raw["plan_at_utc"] > as_of for item in facts):
            raise ContractError("pure_yes_basket_plan_fact_after_asof")
        supplied = _sha(raw["content_sha256"], "contract_content_sha256")
        if supplied != content_sha256({
            key: item for key, item in raw.items() if key != "content_sha256"
        }):
            raise ContractError("pure_yes_basket_plan_contract_hash_mismatch")
        return cls(
            as_of, _text(raw["builder_id"], "builder_id"), facts, supplied
        )


def _load(conn: sqlite3.Connection, fact_type: str, sha: str, cls: Any) -> Any:
    rows = conn.execute(
        "SELECT fact_json FROM canonical_ssot_fact WHERE fact_type=? AND content_sha256=?",
        (fact_type, _sha(sha, f"{fact_type}_sha256")),
    ).fetchall()
    if len(rows) != 1:
        raise ContractError(f"pure_yes_basket_plan_{fact_type}_not_unique")
    return cls.from_mapping(json.loads(str(rows[0][0])))


def build_pure_yes_basket_plan_contract(
    conn: sqlite3.Connection,
    *,
    p1_base_content_sha256: str,
    plan_at_utc: str,
) -> dict[str, Any]:
    """Freeze the bounded YES support set; never calculate orders or prices."""

    from .market_bucket_bridge import CanonicalMarketBucketBridgeFact
    from .p1_base_contract import (
        CanonicalP1BaseFact,
        CanonicalP1BaseContract,
        build_p1_base_contract,
    )

    p1 = _load(conn, "p1_base", p1_base_content_sha256, CanonicalP1BaseFact)
    decision = datetime.fromisoformat(
        p1.raw["decision_at_utc"].replace("Z", "+00:00")
    )
    expires = datetime.fromisoformat(
        p1.raw["freshness_expires_at_utc"].replace("Z", "+00:00")
    )
    rebuilt = build_p1_base_contract(
        conn,
        p0_forecast_snapshot_content_sha256=p1.raw[
            "p0_forecast_snapshot_content_sha256"
        ],
        surface_observation_winner_content_sha256=p1.raw[
            "surface_observation_winner_content_sha256"
        ],
        decision_at_utc=p1.raw["decision_at_utc"],
        freshness_seconds=int((expires - decision).total_seconds()),
    )
    if CanonicalP1BaseContract.from_mapping(rebuilt).p1_base_facts[0].raw != p1.raw:
        raise ContractError("pure_yes_basket_plan_p1_reference_rebuild_mismatch")
    bridge = _load(
        conn, "market_bucket_bridge",
        p1.raw["market_bucket_bridge_content_sha256"],
        CanonicalMarketBucketBridgeFact,
    )
    if any(
        p1.raw[field] != bridge.raw[field]
        for field in ("station_id", "event_date", "settlement_timezone")
    ):
        raise ContractError("pure_yes_basket_plan_parent_scope_mismatch")
    probabilities = p1.raw["bucket_probabilities"]
    buckets = bridge.raw["bucket_contracts"]
    if len(probabilities) != len(buckets) or any(
        item["ordinal"] != bucket["ordinal"]
        or item["outcome"] != bucket["outcome"]
        for item, bucket in zip(probabilities, buckets)
    ):
        raise ContractError("pure_yes_basket_plan_bucket_contract_mismatch")
    maximum = max(Decimal(item["probability"]) for item in probabilities)
    main = min(
        int(item["ordinal"])
        for item in probabilities
        if Decimal(item["probability"]) == maximum
    )
    excluded = set(int(item) for item in p1.raw["hard_excluded_ordinals"])
    reported_high = Decimal(p1.raw["reported_high_c"])
    reported_matches = [
        int(bucket["ordinal"])
        for bucket in buckets
        if (bucket["lower_c"] is None or Decimal(bucket["lower_c"]) <= reported_high)
        and (bucket["upper_c"] is None or reported_high < Decimal(bucket["upper_c"]))
    ]
    if len(reported_matches) != 1:
        raise ContractError("pure_yes_basket_plan_reported_high_bucket_not_unique")
    positive = set()
    for probability in probabilities:
        ordinal = int(probability["ordinal"])
        if (
            ordinal in excluded
            or Decimal(probability["probability"]) <= 0
            or abs(ordinal - main) > SUPPORT_RADIUS
        ):
            continue
        positive.add(ordinal)
    underreach = sorted(item for item in positive if item < main)
    if reported_matches[0] >= main:
        underreach = []
    candidates = sorted({main, *underreach})
    plan_at = _utc(plan_at_utc, "builder_plan_at_utc")
    identity = {
        "station_id": p1.raw["station_id"],
        "event_date": p1.raw["event_date"],
        "settlement_timezone": p1.raw["settlement_timezone"],
        "probability_stage": "P1_BASE",
        "p1_base_content_sha256": p1.content_sha256,
        "p0_forecast_snapshot_content_sha256": p1.raw[
            "p0_forecast_snapshot_content_sha256"
        ],
        "market_bucket_bridge_content_sha256": bridge.content_sha256,
        "surface_observation_winner_content_sha256": p1.raw[
            "surface_observation_winner_content_sha256"
        ],
        "bucket_probability_vector_sha256": p1.raw[
            "bucket_probability_vector_sha256"
        ],
        "source_p1_decision_at_utc": p1.raw["decision_at_utc"],
        "source_p1_freshness_expires_at_utc": p1.raw[
            "freshness_expires_at_utc"
        ],
        "plan_at_utc": plan_at,
        "policy_version": POLICY_VERSION,
        "policy_artifact_content_sha256": POLICY_ARTIFACT_CONTENT_SHA256,
        "selection_method": SELECTION_METHOD,
        "main_selection_method": MAIN_SELECTION_METHOD,
        "support_radius": SUPPORT_RADIUS,
        "main_ordinal": main,
        "reported_high_ordinal": reported_matches[0],
        "hard_excluded_ordinals": sorted(excluded),
        "base_ordinals": [main],
        "underreach_ordinals": underreach,
        "upside_breakthrough_ordinals": [],
        "upside_breakthrough_state": (
            "BLOCKED_NO_REPORTED_HIGH_ADVANCEMENT_EVIDENCE"
        ),
        "candidate_ordinals": candidates,
        "candidate_ordinal_set_sha256": content_sha256(candidates),
        "execution_gate": EXECUTION_GATE,
        "buy_yes_only": True,
        "buy_no_intent_emitted": False,
        "orders_submitted": 0,
        "authority_scope": AUTHORITY_SCOPE,
        "state": STATE,
    }
    fact = {**identity, "content_sha256": content_sha256(identity)}
    contract_identity = {
        "schema": CONTRACT_SCHEMA,
        "as_of_utc": plan_at,
        "builder_id": "canonical.pure_yes_basket_plan.v1",
        "basket_plan_facts": [fact],
    }
    packet = {
        **contract_identity, "content_sha256": content_sha256(contract_identity)
    }
    CanonicalPureYesBasketPlanContract.from_mapping(packet)
    return packet


__all__ = [
    "AUTHORITY_SCOPE", "CONTRACT_SCHEMA", "EXECUTION_GATE", "FACT_TYPE",
    "MAIN_SELECTION_METHOD", "POLICY_ARTIFACT", "POLICY_ARTIFACT_CONTENT_SHA256",
    "POLICY_VERSION", "SELECTION_METHOD", "STATE",
    "SUPPORT_RADIUS", "CanonicalPureYesBasketPlanContract",
    "CanonicalPureYesBasketPlanFact", "build_pure_yes_basket_plan_contract",
]
