"""Bounded settlement/version evidence for offline policy governance.

The packet is read-only and deliberately separates integrity failures from
missing forward evidence.  Missing labels, attribution terms, calibration
fields, or actual fills are ``NA/BLOCKED_EVIDENCE`` and never silently become
zero.  They may block a new formal promotion, but never the existing
``production_no_live`` lane.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

from .contracts import ContractError, content_sha256
from .resource_bounds import assert_process_memory_budget
from .settled_attribution import (
    MAX_FACTS as MAX_ATTRIBUTION_FACTS,
    build_settled_attribution,
)


SCHEMA = "bayes_basket_target_settlement_version_evidence_packet_v1"
SCORECARD_SCHEMA = "bayes_basket_target_settlement_version_scorecard_v1"
PROMOTION_SCHEMA = "bayes_basket_target_settlement_version_promotion_receipt_v1"
MAX_FACTS = 512
MAX_JSON_BYTES = 262_144
EPS = 1e-8


def _utc(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ContractError("settlement_version_as_of_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError("settlement_version_as_of_timezone_required")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _day(value: str, *, name: str) -> str:
    try:
        return date.fromisoformat(str(value).strip()).isoformat()
    except (TypeError, ValueError) as exc:
        raise ContractError(f"settlement_version_{name}_invalid") from exc


def _stations(values: Iterable[str]) -> tuple[str, ...]:
    result = tuple(sorted({
        str(value).strip().upper()
        for value in values
        if str(value).strip()
    }))
    if not result or len(result) > 45:
        raise ContractError("settlement_version_station_scope_invalid")
    return result


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in conn.execute(f"PRAGMA table_info({table})")
    }


def _bounded_json(raw: Any, *, name: str) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    encoded = raw.encode("utf-8")
    if len(encoded) > MAX_JSON_BYTES:
        raise ContractError(f"settlement_version_{name}_bytes_exceeded")
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ContractError(f"settlement_version_{name}_invalid_json") from exc
    if not isinstance(value, Mapping):
        raise ContractError(f"settlement_version_{name}_mapping_required")
    return dict(value)


def _probability_metrics(
    feature: Mapping[str, Any],
    *,
    winning_bucket: str,
) -> tuple[dict[str, float] | None, str | None]:
    raw = feature.get("probability_by_bucket")
    if not isinstance(raw, Mapping) or not winning_bucket:
        return None, "NA_PROBABILITY_VECTOR_MISSING"
    probabilities: dict[str, float] = {}
    try:
        for bucket, value in raw.items():
            probability = float(value)
            if (
                not isinstance(bucket, str)
                or not bucket.strip()
                or not math.isfinite(probability)
                or probability < 0.0
                or probability > 1.0
            ):
                raise ValueError
            probabilities[bucket] = probability
    except (TypeError, ValueError):
        return None, "probability_vector_invalid"
    if (
        not probabilities
        or abs(sum(probabilities.values()) - 1.0) > EPS
        or winning_bucket not in probabilities
    ):
        return None, "probability_vector_invalid"
    winner_probability = probabilities[winning_bucket]
    brier = sum(
        (probability - (1.0 if bucket == winning_bucket else 0.0)) ** 2
        for bucket, probability in probabilities.items()
    )
    nll = -math.log(max(winner_probability, 1e-15))
    support = feature.get("final_support")
    if not isinstance(support, list):
        support = feature.get("selected_buckets")
    covered = (
        1.0
        if isinstance(support, list) and winning_bucket in support
        else 0.0
    )
    return {
        "brier": brier,
        "nll": nll,
        "winning_bucket_probability": winner_probability,
        "support_covered": covered,
    }, None


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def _policy_version_identity(
    policy: Mapping[str, Any],
) -> tuple[dict[str, Any], str]:
    station_policy = policy.get("station_policy")
    if not isinstance(station_policy, Mapping):
        station_policy = {}
    identity = {
        "schema": "delta_basket_score_stable_policy_version_identity_v1",
        "strategy_version": policy.get("strategy_version"),
        "season_bin": policy.get("season_bin"),
        "maturity_bin": policy.get("maturity_bin"),
        "liquidity_bin": policy.get("liquidity_bin"),
        "station_policy_sha256": station_policy.get("policy_sha256"),
        "cash_phase_policy_version": policy.get(
            "cash_phase_policy_version"
        ),
        "cash_phase_policy_sha256": policy.get(
            "cash_phase_policy_sha256"
        ),
        "delta_v2_artifact_id": policy.get("delta_v2_artifact_id"),
        "delta_v2_artifact_sha256": policy.get(
            "delta_v2_artifact_sha256"
        ),
    }
    required = (
        "strategy_version",
        "season_bin",
        "maturity_bin",
        "liquidity_bin",
        "cash_phase_policy_version",
        "cash_phase_policy_sha256",
        "delta_v2_artifact_id",
        "delta_v2_artifact_sha256",
    )
    if any(identity.get(key) in (None, "") for key in required):
        raise ContractError("settlement_version_policy_identity_incomplete")
    return identity, content_sha256(identity)


def build_settlement_version_evidence_packet(
    conn: sqlite3.Connection,
    *,
    as_of_utc: str,
    station_ids: Iterable[str],
    window_start_date: str,
    window_end_date: str,
    max_facts: int = MAX_FACTS,
) -> dict[str, Any]:
    """Build one immutable, non-authorizing M8/M10 evidence packet."""

    as_of = _utc(as_of_utc)
    stations = _stations(station_ids)
    start = _day(window_start_date, name="window_start_date")
    end = _day(window_end_date, name="window_end_date")
    if start > end:
        raise ContractError("settlement_version_window_reversed")
    if (
        isinstance(max_facts, bool)
        or not isinstance(max_facts, int)
        or not 1 <= max_facts <= MAX_FACTS
    ):
        raise ContractError("settlement_version_max_facts_invalid")

    required = {
        "basket_policy_training_fact": {
            "fact_id", "station_id", "event_date", "settlement_timezone",
            "feature_json", "policy_version_vector_json", "authority_scope",
            "created_at_utc",
        },
        "basket_policy_training_label": {
            "fact_id", "truth_revision_no", "truth_sha256",
            "settlement_closed_at_utc", "labeled_at_utc", "outcome_json",
        },
        "basket_target_market_registry": {
            "station_id", "event_date", "settlement_timezone",
            "source_eligible",
        },
    }
    present = _tables(conn)
    integrity_blockers: list[str] = []
    for table, fields in required.items():
        if table not in present:
            integrity_blockers.append(f"source_table_missing:{table}")
            continue
        missing = sorted(fields - _columns(conn, table))
        if missing:
            integrity_blockers.append(
                f"source_schema_incomplete:{table}:" + ",".join(missing)
            )

    base_identity = {
        "schema": SCHEMA,
        "as_of_utc": as_of,
        "window": {
            "start_event_date": start,
            "end_event_date": end,
            "station_ids": list(stations),
            "denominator_contract": "all_closed_historical_facts_in_explicit_window",
            "max_facts": max_facts,
        },
        "production_no_live": True,
        "production_no_live_blocked_by_forward_oos": False,
        "production_no_live_blocked_by_historical_oos": False,
        "future_data_counts_as_oos": False,
        "formal_promotion_online_tuning_allowed": False,
        "calls_order_gateway": False,
        "live_submit_authorized": False,
        "orders_submitted": 0,
        "writes_production_db": False,
        "read_only": True,
    }
    if integrity_blockers:
        identity = {
            **base_identity,
            "state": "BLOCKED_FAIL_CLOSED",
            "scorecards": [],
            "integrity_blockers": sorted(set(integrity_blockers)),
            "evidence_blockers": [],
            "promotion_receipt": None,
        }
        return {**identity, "content_sha256": content_sha256(identity)}

    placeholders = ",".join("?" for _ in stations)
    cursor = conn.execute(
        "SELECT rowid,fact_id,station_id,event_date,settlement_timezone,"
        "feature_json,policy_version_vector_json,created_at_utc,"
        "content_sha256,probability_envelope_id,posterior_sha256,"
        "ws_snapshot_id,quote_sha256,inventory_snapshot_id,inventory_sha256,"
        "order_state_snapshot_id,order_state_sha256,cycle_id,decision_id,"
        "decision_sha256,plan_sha256 "
        "FROM basket_policy_training_fact "
        "WHERE authority_scope='PRODUCTION_NO_LIVE_FORWARD' "
        f"AND station_id IN ({placeholders}) "
        "AND event_date>=? AND event_date<=? "
        "AND julianday(created_at_utc)<=julianday(?) "
        "ORDER BY event_date,station_id,created_at_utc,fact_id LIMIT ?",
        (*stations, start, end, as_of, max_facts + 1),
    )
    raw_rows = cursor.fetchall()
    if len(raw_rows) > max_facts:
        integrity_blockers.append("settlement_version_fact_row_cap_reached")
        raw_rows = raw_rows[:max_facts]
    facts = [
        {
            str(description[0]): raw[index]
            for index, description in enumerate(cursor.description or ())
        }
        for raw in raw_rows
    ]
    assert_process_memory_budget(
        projected_bytes=sum(
            len(str(row.get("feature_json") or "").encode("utf-8"))
            + len(str(row.get("policy_version_vector_json") or "").encode("utf-8"))
            for row in facts
        ) * 3
    )

    grouped: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "fact_count": 0,
        "labeled_count": 0,
        "unsettled_or_missing_label_count": 0,
        "attributed_counterfactual_count": 0,
        "actual_fill_count": 0,
        "calibration_count": 0,
        "counterfactual_pnl": [],
        "brier": [],
        "nll": [],
        "winning_probability": [],
        "support_covered": [],
        "na_status_counts": Counter(),
        "station_ids": set(),
        "event_dates": set(),
    })
    rows: list[dict[str, Any]] = []
    evidence_blockers: list[str] = []
    attribution_by_fact: dict[str, Mapping[str, Any]] = {}
    attribution_cursor = 0
    attribution_has_more = True
    attribution_hard_blockers: list[str] = []
    while (
        attribution_has_more
        and len(attribution_by_fact) < max_facts
    ):
        page_limit = min(
            MAX_ATTRIBUTION_FACTS,
            max_facts - len(attribution_by_fact),
        )
        attribution_packet = build_settled_attribution(
            conn,
            as_of_utc=as_of,
            max_facts=page_limit,
            after_fact_rowid=attribution_cursor,
            station_ids=stations,
            window_start_date=start,
            window_end_date=end,
        )
        for row in attribution_packet.get("rows", []):
            if isinstance(row, Mapping):
                attribution_by_fact[str(row.get("fact_id") or "")] = row
        if attribution_packet.get("state") == "BLOCKED_FAIL_CLOSED":
            attribution_hard_blockers.extend(
                str(value)
                for value in attribution_packet.get("blockers", [])
            )
        next_cursor = int(
            attribution_packet.get("source_cursor_after")
            or attribution_cursor
        )
        attribution_has_more = (
            attribution_packet.get("has_more") is True
        )
        if attribution_has_more and next_cursor <= attribution_cursor:
            integrity_blockers.append(
                "settlement_version_attribution_cursor_stalled"
            )
            break
        attribution_cursor = next_cursor
    if attribution_has_more:
        integrity_blockers.append(
            "settlement_version_attribution_row_cap_reached"
        )
    integrity_blockers.extend(
        "attribution:" + value
        for value in attribution_hard_blockers
    )
    registry_timezones: dict[tuple[str, str], set[str]] = defaultdict(set)
    registry_rows = conn.execute(
        "SELECT station_id,event_date,settlement_timezone "
        "FROM basket_target_market_registry "
        f"WHERE station_id IN ({placeholders}) "
        "AND event_date>=? AND event_date<=? AND source_eligible=1",
        (*stations, start, end),
    ).fetchall()
    for station, event_date, timezone_name in registry_rows:
        if str(timezone_name or ""):
            registry_timezones[
                (str(station), str(event_date))
            ].add(str(timezone_name))
    for fact in facts:
        fact_id = str(fact.get("fact_id") or "")
        station = str(fact.get("station_id") or "")
        event_date = str(fact.get("event_date") or "")
        timezone_value = str(fact.get("settlement_timezone") or "")
        if registry_timezones.get((station, event_date), set()) != {
            timezone_value
        }:
            integrity_blockers.append(
                f"settlement_timezone_registry_mismatch:{fact_id}"
            )

        attribution_row = attribution_by_fact.get(fact_id, {})
        if not attribution_row:
            integrity_blockers.append(
                f"{fact_id}:attribution_projection_missing"
            )
        policy = _bounded_json(
            fact.get("policy_version_vector_json"),
            name="policy_version_vector",
        )
        feature = _bounded_json(fact.get("feature_json"), name="feature")
        if policy is None or feature is None:
            integrity_blockers.append(f"version_or_feature_missing:{fact_id}")
            continue
        policy_sha = content_sha256(policy)
        policy_identity, policy_identity_sha = _policy_version_identity(
            policy
        )
        binding_status = str(
            attribution_row.get(
                "settlement_version_binding_status"
            )
            or (
                "NA_SETTLEMENT_LABEL_MISSING"
                if str(attribution_row.get("status") or "")
                == "NA_UNSETTLED_OR_LABEL_MISSING"
                else "NA_SETTLEMENT_VERSION_BINDING_MISSING"
            )
        )
        binding_sha = attribution_row.get(
            "settlement_version_binding_sha256"
        )
        if binding_status.startswith("NA_"):
            evidence_blockers.append(binding_status)
        elif binding_status != "BOUND":
            integrity_blockers.append(
                f"{fact_id}:{binding_status}"
            )
        group = grouped[policy_identity_sha]
        group["policy_version_identity"] = policy_identity
        group["fact_count"] += 1
        group["station_ids"].add(station)
        group["event_dates"].add(event_date)
        status = str(attribution_row.get("status") or "BLOCKED_FAIL_CLOSED")
        if status == "NA_UNSETTLED_OR_LABEL_MISSING":
            group["unsettled_or_missing_label_count"] += 1
            group["na_status_counts"][status] += 1
            evidence_blockers.append("settlement_label_coverage_incomplete")
        elif status.startswith("NA_"):
            group["labeled_count"] += 1
            group["na_status_counts"][status] += 1
            evidence_blockers.append(status)
        elif status == "COUNTERFACTUAL_CLOSED":
            group["labeled_count"] += 1
            group["attributed_counterfactual_count"] += 1
        else:
            integrity_blockers.extend(
                f"{fact_id}:{value}"
                for value in attribution_row.get("blockers", [])
            )

        pnl = attribution_row.get("counterfactual_pnl_usdc")
        if isinstance(pnl, (int, float)) and math.isfinite(float(pnl)):
            group["counterfactual_pnl"].append(float(pnl))
        winning_bucket = str(attribution_row.get("winning_bucket") or "")
        metrics, metric_error = _probability_metrics(
            feature,
            winning_bucket=winning_bucket,
        )
        if metric_error == "probability_vector_invalid":
            integrity_blockers.append(f"{fact_id}:{metric_error}")
        elif metric_error:
            group["na_status_counts"][metric_error] += 1
            evidence_blockers.append(metric_error)
        elif metrics is not None:
            group["calibration_count"] += 1
            group["brier"].append(metrics["brier"])
            group["nll"].append(metrics["nll"])
            group["winning_probability"].append(
                metrics["winning_bucket_probability"]
            )
            group["support_covered"].append(metrics["support_covered"])

        # This packet reads only production_no_live counterfactual labels.
        # Actual fill attribution therefore remains explicitly unavailable.
        group["na_status_counts"]["NA_ACTUAL_FILL_EVIDENCE_MISSING"] += 1
        evidence_blockers.append("actual_fill_evidence_missing")
        rows.append({
            "fact_id": fact_id,
            "station_id": station,
            "event_date": event_date,
            "settlement_timezone": timezone_value,
            "policy_version_vector_sha256": policy_sha,
            "policy_version_identity_sha256": policy_identity_sha,
            "settlement_status": status,
            "settlement_version_binding_status": binding_status,
            "settlement_version_binding_sha256": binding_sha,
            "counterfactual_pnl_usdc": pnl,
            "realized_pnl_usdc": None,
            "realized_pnl_status": "NA_ACTUAL_FILL_EVIDENCE_MISSING",
            "calibration_status": (
                "EVALUATED" if metrics is not None else metric_error
            ),
        })

    scorecards: list[dict[str, Any]] = []
    for policy_identity_sha in sorted(grouped):
        group = grouped[policy_identity_sha]
        fact_count = int(group["fact_count"])
        scorecard = {
            "schema": SCORECARD_SCHEMA,
            "policy_version_identity": group[
                "policy_version_identity"
            ],
            "policy_version_identity_sha256": policy_identity_sha,
            "fixed_denominators": {
                "fact_count": fact_count,
                "labeled_count": int(group["labeled_count"]),
                "calibration_count": int(group["calibration_count"]),
                "attribution_count": int(
                    group["attributed_counterfactual_count"]
                ),
                "actual_fill_count": int(group["actual_fill_count"]),
            },
            "scope": {
                "station_ids": sorted(group["station_ids"]),
                "event_dates": sorted(group["event_dates"]),
            },
            "calibration": {
                "mean_brier": _mean(group["brier"]),
                "mean_nll": _mean(group["nll"]),
                "mean_winning_bucket_probability": _mean(
                    group["winning_probability"]
                ),
            },
            "coverage": {
                "support_hit_rate": _mean(group["support_covered"]),
                "label_coverage": (
                    group["labeled_count"] / fact_count
                    if fact_count else None
                ),
            },
            "pnl_attribution": {
                "counterfactual_count": len(group["counterfactual_pnl"]),
                "counterfactual_mean_usdc": _mean(
                    group["counterfactual_pnl"]
                ),
                "realized_pnl_usdc": None,
                "realized_status": "NA_ACTUAL_FILL_EVIDENCE_MISSING",
            },
            "execution_quality": {
                "status": "NA_ACTUAL_FILL_EVIDENCE_MISSING",
                "fill_rate": None,
                "slippage_usdc": None,
                "failure_cost_usdc": None,
            },
            "na_status_counts": dict(sorted(group["na_status_counts"].items())),
        }
        scorecards.append({
            **scorecard,
            "content_sha256": content_sha256(scorecard),
        })

    promotion_core = {
        "schema": PROMOTION_SCHEMA,
        "as_of_utc": as_of,
        "window": base_identity["window"],
        "scorecard_sha256s": [
            row["content_sha256"] for row in scorecards
        ],
        "formal_promotion_eligible": False,
        "activation_written": False,
        "online_tuning_allowed": False,
        "production_no_live_blocked": False,
        "state": (
            "BLOCKED_FAIL_CLOSED"
            if integrity_blockers
            else "BLOCKED_EVIDENCE"
        ),
        "blockers": (
            sorted(set(integrity_blockers))
            if integrity_blockers
            else sorted(set(evidence_blockers))
        ),
    }
    promotion_receipt = {
        **promotion_core,
        "content_sha256": content_sha256(promotion_core),
    }
    state = (
        "BLOCKED_FAIL_CLOSED"
        if integrity_blockers
        else "BLOCKED_EVIDENCE"
        if evidence_blockers or not facts
        else "READY_FOR_OFFLINE_PROMOTION"
    )
    identity = {
        **base_identity,
        "state": state,
        "fact_count": len(facts),
        "rows": rows,
        "scorecards": scorecards,
        "integrity_blockers": sorted(set(integrity_blockers)),
        "evidence_blockers": sorted(set(
            evidence_blockers
            or (["settlement_version_facts_missing"] if not facts else [])
        )),
        "promotion_receipt": promotion_receipt,
    }
    return {**identity, "content_sha256": content_sha256(identity)}


__all__ = [
    "MAX_FACTS",
    "PROMOTION_SCHEMA",
    "SCHEMA",
    "SCORECARD_SCHEMA",
    "build_settlement_version_evidence_packet",
]
