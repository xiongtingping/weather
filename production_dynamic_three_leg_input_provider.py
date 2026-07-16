"""Freeze all authoritative inputs for one production posterior outbox event."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping

from dynamic_three_leg_clob_gateway_adapter import evaluate_dynamic_three_leg_exchange_snapshot
from dynamic_three_leg_position_lot_runtime import load_dynamic_three_leg_position_snapshot
from production_dynamic_three_leg_frozen_bundle import build_production_dynamic_three_leg_frozen_bundle
from settlement_window_runtime import load_approved_settlement_window_policy
from top_yes_wave_reentry_runtime import build_top_yes_wave_reentry_state
from strong_posterior_three_leg_strategy import CAPITAL_POLICY_ID


SCHEMA_VERSION = "production_dynamic_three_leg_input_provider_v1"
LEG_CAPS = {"top_yes_wave": 20.0, "second_yes": 4.99, "strongly_excluded_no": 100.0}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _id(prefix: str, value: Any) -> str:
    return prefix + _sha(value)[:24]


def _parse(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(_text(value) or "{}")
    except (TypeError, ValueError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _dt(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _bucket_key(value: Any) -> str:
    return "".join(_text(value).lower().replace("°", "").split())


def _blocked(*reasons: str, **extra: Any) -> dict[str, Any]:
    return {
        "schema": SCHEMA_VERSION,
        "ok": False,
        "state": "blocked",
        "block_reasons": sorted({_text(reason) for reason in reasons if _text(reason)}),
        "places_orders": False,
        **extra,
    }


def load_latest_realization_state_for_outbox(
    conn: sqlite3.Connection, *, outbox: Mapping[str, Any],
) -> dict[str, Any]:
    event = _mapping(outbox)
    slug, day = _text(event.get("slug")), _text(event.get("event_date"))
    run_id, fact_id = _text(event.get("posterior_run_id")), _text(event.get("fact_snapshot_id"))
    latest = conn.execute(
        """SELECT h.fact_json,h.posterior_run_id,h.fact_snapshot_id
             FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id
            WHERE l.slug=? AND l.event_date=?""",
        (slug, day),
    ).fetchone()
    if latest is None or _text(latest[1]) != run_id or _text(latest[2]) != fact_id:
        return _blocked("production_input_outbox_not_latest_ssot")
    public = _mapping(_parse(latest[0]).get("posterior_ssot_fact"))
    realization_id = _text(public.get("realization_state_id"))
    if _text(public.get("posterior_run_id")) != run_id or _text(public.get("fact_snapshot_id")) != fact_id or not realization_id:
        return _blocked("production_input_public_realization_lineage_invalid")
    row = conn.execute(
        """SELECT realization_state_id,posterior_run_id,fact_snapshot_id,resolution_contract_id,
                  target_bucket,second_target_bucket,credible_bucket_set_json,current_report_bucket,
                  current_report_bucket_probability,probability_above_current_report_bucket,
                  current_report_bucket_hard_zero,settlement_exclusion_certificates_json,state_json,state_sha256
             FROM realization_states WHERE realization_state_id=?""",
        (realization_id,),
    ).fetchone()
    if row is None or _text(row[1]) != run_id or _text(row[2]) != fact_id:
        return _blocked("production_input_realization_row_not_bound")
    payload = _parse(row[12])
    projection = _mapping(payload.get("projection"))
    if payload.get("schema") != "realization_state_l3_v2" or projection.get("ok") is not True or _sha(payload) != _text(row[13]):
        return _blocked("production_input_realization_payload_invalid")
    realization = {
        "realization_state_id": _text(row[0]),
        "posterior_run_id": run_id,
        "fact_snapshot_id": fact_id,
        "resolution_contract_id": _text(row[3]),
        "target_bucket": _text(row[4]),
        "second_target_bucket": _text(row[5]),
        "credible_bucket_set": json.loads(_text(row[6]) or "[]"),
        "current_report_bucket": _text(row[7]),
        "current_report_bucket_probability": row[8],
        "probability_above_current_report_bucket": row[9],
        "current_report_bucket_hard_zero": bool(row[10]),
        "settlement_exclusion_certificates": _parse(row[11]),
        "market_bucket_probabilities": _mapping(projection.get("market_bucket_probabilities")),
        "strong_physical_evidence": _mapping(projection.get("strong_physical_evidence")),
        "peak_timing_state": _mapping(projection.get("peak_timing_state")),
    }
    if not realization["market_bucket_probabilities"]:
        return _blocked("production_input_market_bucket_probabilities_unavailable")
    return {"schema": SCHEMA_VERSION, "ok": True, "state": "available", "realization_state": realization}


def _market_token_rows(
    conn: sqlite3.Connection, *, slug: str, event_date: str, buckets: list[str],
) -> tuple[list[dict[str, str]], list[str]]:
    rows = conn.execute(
        """SELECT outcome,yes_token,no_token,market_active,market_closed,market_archived,
                  market_accepting_orders,market_enable_orderbook
             FROM market_tokens WHERE slug=? AND event_date=?""",
        (_text(slug), _text(event_date)),
    ).fetchall()
    by_key = {_bucket_key(row[0]): row for row in rows}
    result, blockers = [], []
    for bucket in buckets:
        row = by_key.get(_bucket_key(bucket))
        if row is None:
            blockers.append("production_input_market_bucket_token_missing")
            continue
        if not (int(row[3] or 0) == 1 and int(row[4] or 0) == 0 and int(row[5] or 0) == 0 and int(row[6] or 0) == 1 and int(row[7] or 0) == 1):
            blockers.append("production_input_market_bucket_not_orderable")
        if not _text(row[1]) or not _text(row[2]):
            blockers.append("production_input_market_bucket_token_missing")
        result.append({"bucket": bucket, "yes_token_id": _text(row[1]), "no_token_id": _text(row[2])})
    return result, sorted(set(blockers))


def freeze_production_dynamic_three_leg_inputs(
    conn: sqlite3.Connection,
    *,
    outbox: Mapping[str, Any],
    preliminary_as_of: str,
    exchange_snapshot_fetcher: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    production_activation: Mapping[str, Any],
) -> dict[str, Any]:
    """Perform all network reads before outbox claim and return one frozen bundle."""

    event = _mapping(outbox)
    loaded = load_latest_realization_state_for_outbox(conn, outbox=event)
    if loaded.get("ok") is not True:
        return loaded
    realization = _mapping(loaded.get("realization_state"))
    positions = load_dynamic_three_leg_position_snapshot(
        conn,
        slug=_text(event.get("slug")),
        event_date=_text(event.get("event_date")),
        as_of=_text(preliminary_as_of),
    )
    if positions.get("ok") is not True:
        return _blocked(*(positions.get("block_reasons") or ["production_input_position_snapshot_unavailable"]))
    if positions.get("unresolved_intents"):
        return _blocked("production_input_unresolved_execution_intent_requires_reconcile")
    distribution = _mapping(realization.get("market_bucket_probabilities"))
    bucket_labels = sorted({_text(bucket) for bucket in distribution if _text(bucket)}, key=_bucket_key)
    token_rows, token_blockers = _market_token_rows(
        conn,
        slug=_text(event.get("slug")),
        event_date=_text(event.get("event_date")),
        buckets=bucket_labels,
    )
    if token_blockers:
        return _blocked(*token_blockers)
    position_tokens = sorted({_text(row.get("token_id")) for row in positions.get("positions", []) if _text(row.get("token_id"))})
    request_core = {
        "slug": _text(event.get("slug")),
        "event_date": _text(event.get("event_date")),
        "posterior_run_id": _text(event.get("posterior_run_id")),
        "buckets": token_rows,
        "position_token_ids": position_tokens,
    }
    request = {**request_core, "request_id": _id("clobsnapshotrequest_", request_core)}
    try:
        raw_exchange = _mapping(exchange_snapshot_fetcher(request))
    except Exception as exc:
        return _blocked("production_input_exchange_snapshot_exception", exception_type=type(exc).__name__)
    effective_as_of = _text(raw_exchange.get("observed_at"))
    exchange = evaluate_dynamic_three_leg_exchange_snapshot(
        request, raw_exchange, evaluated_at=effective_as_of, max_snapshot_age_ms=1_000
    )
    if exchange.get("ok") is not True:
        return _blocked(*(exchange.get("block_reasons") or ["production_input_exchange_snapshot_invalid"]), exchange_snapshot=exchange)
    observed_at = _dt(exchange.get("as_of"))
    if observed_at is None:
        return _blocked("production_input_exchange_observed_time_invalid")
    expires_at = _iso(observed_at + timedelta(milliseconds=900))

    managed_by_token: dict[str, float] = {}
    augmented_positions = [dict(row) for row in positions.get("positions", [])]
    for row in augmented_positions:
        token_id = _text(row.get("token_id"))
        managed_by_token[token_id] = managed_by_token.get(token_id, 0.0) + float(row.get("position_size") or 0.0)
    for token_id, managed_size in managed_by_token.items():
        observed_balance = float(_mapping(exchange.get("conditional_by_token")).get(token_id, {}).get("balance") or 0.0)
        if observed_balance + 1e-6 < managed_size:
            return _blocked("production_input_strategy_lot_exceeds_exchange_balance")
        residual = observed_balance - managed_size
        if residual > 1e-6:
            augmented_positions.append({
                "management_scope": "external_unmanaged",
                "token_id": token_id,
                "position_size": residual,
            })
    position_core = {
        "source_position_snapshot_id": positions.get("position_snapshot_id"),
        "exchange_request_id": request["request_id"],
        "as_of": exchange.get("as_of"),
        "positions": augmented_positions,
    }
    position_snapshot = {
        **positions,
        "position_snapshot_id": _id("positionsnapshot_", position_core),
        "as_of": exchange.get("as_of"),
        "positions": augmented_positions,
        "exchange_balance_reconciled": True,
    }
    market_core = {
        "request_id": request["request_id"],
        "as_of": exchange.get("as_of"),
        "buckets": exchange.get("buckets"),
    }
    market_snapshot = {
        "market_snapshot_id": _id("marketsnapshot_", market_core),
        "as_of": exchange.get("as_of"),
        "expires_at": expires_at,
        "buckets": exchange.get("buckets"),
        "source": "local_clob_dynamic_three_leg_snapshot",
        "evidence_only": True,
    }
    available_cash = float(exchange.get("available_cash_usdc") or 0.0)
    epoch_core = {
        "request_id": request["request_id"],
        "as_of": exchange.get("as_of"),
        "available_cash_usdc": available_cash,
        "capital_policy_id": CAPITAL_POLICY_ID,
    }
    capital_epoch_id = _id("capitalepoch_", epoch_core)
    capital_snapshot_id = _id("capitalsnapshot_", epoch_core)
    maximum_new_buy = min(sum(LEG_CAPS.values()), available_cash)
    risk_snapshot_id = _id("risksnapshot_", {**epoch_core, "maximum_new_buy_notional_usdc": maximum_new_buy})
    reservation_id = _id("riskreservation_", {"posterior_run_id": event.get("posterior_run_id"), "capital_epoch_id": capital_epoch_id})
    capital_snapshot = {
        "capital_snapshot_id": capital_snapshot_id,
        "risk_snapshot_id": risk_snapshot_id,
        "capital_epoch_id": capital_epoch_id,
        "risk_reservation_id": reservation_id,
        "available_cash_usdc": available_cash,
        "notional_by_leg": dict(LEG_CAPS),
        "capital_policy_id": CAPITAL_POLICY_ID,
        "expires_at": expires_at,
    }
    risk_snapshot = {
        "risk_snapshot_id": risk_snapshot_id,
        "capital_epoch_id": capital_epoch_id,
        "maximum_new_buy_notional_usdc": maximum_new_buy,
        "new_buy_authorized": available_cash > 0.0,
        "expires_at": expires_at,
        "ev_edge_lcb_consumed": False,
        "capital_policy_id": CAPITAL_POLICY_ID,
    }
    from production_market_alpha_take_profit_writer import load_top_yes_wave_reentry_state
    reentry = load_top_yes_wave_reentry_state(
        conn, posterior_run_id=_text(event.get("posterior_run_id"))
    ) if conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='market_alpha_take_profit_cycles'"
    ).fetchone() else build_top_yes_wave_reentry_state(
        posterior_run_id=_text(event.get("posterior_run_id")), completed_cycles=[]
    )
    bundle = build_production_dynamic_three_leg_frozen_bundle(
        outbox=event,
        realization_state=realization,
        market_snapshot=market_snapshot,
        capital_snapshot=capital_snapshot,
        risk_snapshot=risk_snapshot,
        position_snapshot=position_snapshot,
        settlement_window_policy=load_approved_settlement_window_policy(),
        top_yes_wave_reentry_state=reentry,
        strategy_artifact_sha256=_text(production_activation.get("strategy_artifact_sha256")),
        production_activation=production_activation,
        decision_as_of=effective_as_of,
    )
    if bundle.get("ok") is not True:
        return {**bundle, "schema": SCHEMA_VERSION}
    return {
        **bundle,
        "schema": SCHEMA_VERSION,
        "decision_as_of": effective_as_of,
        "position_snapshot": position_snapshot,
        "exchange_request": request,
        "exchange_snapshot": exchange,
        "places_orders": False,
    }


__all__ = [
    "LEG_CAPS",
    "SCHEMA_VERSION",
    "freeze_production_dynamic_three_leg_inputs",
    "load_latest_realization_state_for_outbox",
]
