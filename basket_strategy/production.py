"""Production pure-YES basket executor embedded in the truth-ingress service.

The executor is intentionally narrow: it consumes already materialized P0/P1
and regime evidence, reads only official CLOB WebSocket quote shards, and
submits one FOK order per YES leg.  AWC METAR/SPECI is read only as a
mathematical floor which filters impossible YES buckets; it never creates a
NO order and the executor never imports a forecast fetcher or an order queue.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from dynamic_three_leg_clob_gateway_adapter import LocalDynamicThreeLegClobGateway
from weather_basket_yes_runtime import identity, parse_bucket_interval

from .activation import load_activation
from .policy import compute_macro_topup, load_policy
from .runtime import build_basket_yes_plan
from .schema import apply_basket_strategy_schema


ROOT = Path(__file__).resolve().parents[1]
QUOTE_SNAPSHOT = ROOT / "data" / "market_ws_latest_quotes.json"
QUOTE_SHARDS = ROOT / "data" / "market_ws_quote_shards"
SCHEMA_VERSION = "basket_strategy_production_v1"
ACTIVE_CYCLE_STATES = {"prepared", "submitting", "submitted", "partially_filled", "reconcile_required"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _date(value: Any) -> str:
    return str(value or "").strip()[:10]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


def _quote(token_id: str, *, max_age_sec: float) -> dict[str, Any] | None:
    token = str(token_id or "").strip()
    if not token:
        return None
    shard = QUOTE_SHARDS / (hashlib.sha256(token.encode("utf-8")).hexdigest() + ".json")
    value = _read_json(shard) if shard.exists() else {}
    quote = value.get("quote") if isinstance(value.get("quote"), Mapping) else {}
    if not quote:
        root = _read_json(QUOTE_SNAPSHOT)
        quote = (root.get("quotes_by_token") or {}).get(token) if isinstance(root.get("quotes_by_token"), Mapping) else {}
    if not isinstance(quote, Mapping):
        return None
    observed = str(quote.get("book_observed_at") or quote.get("created_at") or "")
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(observed.replace("Z", "+00:00"))).total_seconds()
    except (TypeError, ValueError):
        return None
    if age < 0 or age > max_age_sec:
        return None
    asks = []
    for raw in quote.get("asks") or []:
        if not isinstance(raw, Mapping):
            continue
        price, size = _float(raw.get("price")), _float(raw.get("size"))
        if price is not None and size is not None and 0 < price < 1 and size > 0:
            asks.append((price, size))
    if not asks:
        return None
    price, size = sorted(asks)[0]
    return {"yes_ask": price, "yes_ask_size": size, "observed_at": observed, "quote": dict(quote)}


def _parse_registry_row(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    nested = raw.get("registry_json")
    value: dict[str, Any] = {}
    if isinstance(nested, str):
        try:
            parsed = json.loads(nested)
            if isinstance(parsed, Mapping):
                value.update(parsed)
        except ValueError:
            pass
    value.update({key: item for key, item in raw.items() if key != "registry_json"})
    outcome = str(value.get("outcome") or "").strip()
    if not outcome:
        return None
    try:
        interval = parse_bucket_interval(outcome)
    except ValueError:
        return None
    # Edge contracts have one finite boundary even though one side is
    # unbounded.  Keep that boundary as the canonical centre coordinate so an
    # edge bucket may legitimately anchor a basket; the selector only includes
    # the edge leg when the posterior centre itself is that edge.
    lower = interval.get("lower")
    if lower is None:
        lower = interval.get("upper")
    value["outcome"] = outcome
    value["kind"] = interval.get("kind")
    value["bucket_c"] = int(round(lower)) if lower is not None and float(lower).is_integer() else None
    value["yes_token"] = str(value.get("yes_token") or "").strip()
    value["no_token"] = str(value.get("no_token") or "").strip()
    return value


def _probability_projection(conn: sqlite3.Connection, slug: str, event_date: str) -> dict[str, Any]:
    row = conn.execute(
        """SELECT h.fact_json,h.fact_snapshot_id,h.posterior_run_id
           FROM event_fact_latest l JOIN event_fact_history h ON h.id=l.fact_id
          WHERE l.slug=? AND l.event_date=? ORDER BY h.id DESC LIMIT 1""",
        (slug, event_date),
    ).fetchone()
    if row is None:
        return {"status": "blocked", "reason_codes": ["materialized_probability_fact_missing"]}
    try:
        fact = json.loads(str(row[0] or "{}"))
    except ValueError:
        return {"status": "blocked", "reason_codes": ["materialized_probability_fact_invalid"]}
    public = fact.get("posterior_ssot_fact") if isinstance(fact, Mapping) else {}
    probabilities = public.get("market_bucket_probabilities") if isinstance(public, Mapping) else {}
    if not isinstance(probabilities, Mapping) or not probabilities:
        return {"status": "blocked", "reason_codes": ["materialized_market_bucket_probabilities_missing"]}
    rows: list[dict[str, Any]] = []
    for label, value in probabilities.items():
        try:
            interval = parse_bucket_interval(str(label))
            probability = float(value)
        except (TypeError, ValueError):
            continue
        kind = str(interval.get("kind") or "")
        if kind not in {"single", "lower_tail", "upper_tail"}:
            continue
        lower = interval.get("lower")
        if lower is None:
            lower = interval.get("upper")
        if lower is None or not math.isfinite(probability) or probability < 0:
            continue
        rows.append({
            "bucket": str(label),
            "canonical_bucket": str(label),
            "bucket_c": int(round(float(lower))),
            "kind": kind,
            "probability": probability,
        })
    total = sum(float(item["probability"]) for item in rows)
    if not rows or total <= 0:
        return {"status": "blocked", "reason_codes": ["materialized_exact_bucket_probabilities_missing"]}
    rows = [{**item, "probability": float(item["probability"]) / total} for item in rows]
    return {
        "status": "available",
        "stage": "P1" if public.get("last_strong_update_id") else "P0",
        "probabilities": {item["bucket"]: item["probability"] for item in rows},
        "rows": rows,
        "posterior_checkpoint_id": public.get("posterior_checkpoint_id"),
        "posterior_run_id": public.get("posterior_run_id") or row[2],
        "fact_snapshot_id": public.get("fact_snapshot_id") or row[1],
    }


def _regime(conn: sqlite3.Connection, event_date: str) -> dict[str, Any]:
    row = conn.execute(
        """SELECT snapshot_json,source_revisions_json
           FROM weak_forecast_evidence_snapshots
          WHERE event_date=? AND state IN ('active','committed')
          ORDER BY decision_as_of DESC LIMIT 1""",
        (event_date,),
    ).fetchone()
    if row is None:
        return {}
    result: dict[str, Any] = {}
    for raw in (row[0], row[1]):
        try:
            value = json.loads(str(raw or "{}"))
        except ValueError:
            continue
        values = list(value) if isinstance(value, list) else [value]
        for item in values:
            if not isinstance(item, Mapping):
                continue
            for key in ("forecast_weather_regime", "basin_regime", "cloud_regime", "precipitation_regime", "convection_regime"):
                if item.get(key) and key not in result:
                    result[key] = item[key]
            physical = item.get("physical_regime")
            if isinstance(physical, Mapping) and "physical_regime" not in result:
                result["physical_regime"] = dict(physical)
    return result


class BasketProductionEngine:
    def __init__(self, conn: sqlite3.Connection, *, gateway: LocalDynamicThreeLegClobGateway | None = None) -> None:
        self.conn = conn
        self.gateway = gateway or LocalDynamicThreeLegClobGateway(timeout_seconds=1.5)
        apply_basket_strategy_schema(conn)
        self._last_block_reason = ""

    def _risk_budget(self, activation: Mapping[str, Any]) -> dict[str, float | str]:
        """Load the persisted risk budget without making it a hot-path dependency."""

        try:
            policy = load_policy()
            raw = policy.get("risk_budget") if isinstance(policy.get("risk_budget"), Mapping) else {}
        except Exception:
            raw = {}
        basket = activation.get("basket") if isinstance(activation.get("basket"), Mapping) else {}
        mode = str(basket.get("risk_budget_mode") or "production").strip().lower()
        if mode not in {"production", "test"}:
            mode = "production"
        total_key = "test_total_usdc" if mode == "test" else "production_total_usdc"
        daily_loss_cap = raw.get("test_daily_loss_cap_usdc", 5.0) if mode == "test" else raw.get("production_daily_loss_cap_usdc")
        return {
            "mode": mode,
            "total_usdc": float(raw.get(total_key, 100.0 if mode == "test" else 1000.0) or 0.0),
            "unallocated_pool_usdc": float(raw.get("unallocated_yes_pool_usdc", 30.0) or 0.0),
            "city_day_cap_usdc": float(raw.get("city_day_cap_usdc", 10.0) or 0.0),
            "single_event_cap_usdc": float(raw.get("single_event_cap_usdc", 5.0) or 0.0),
            "parent_order_cap_usdc": float(raw.get("parent_order_cap_usdc", 2.0) or 0.0),
            "daily_loss_cap_usdc": float(daily_loss_cap) if daily_loss_cap is not None else math.inf,
        }

    def _capital_snapshot(self, *, slug: str, event_date: str, city_slug: str, activation: Mapping[str, Any]) -> dict[str, Any]:
        risk = self._risk_budget(activation)
        active_sql = "state IN ('prepared','submitting','submitted','partially_filled','reconcile_required')"
        global_reserved = float(self.conn.execute(
            f"SELECT COALESCE(SUM(total_cost_usdc),0) FROM basket_strategy_cycles WHERE {active_sql}"
        ).fetchone()[0] or 0.0)
        city_reserved = float(self.conn.execute(
            f"SELECT COALESCE(SUM(total_cost_usdc),0) FROM basket_strategy_cycles WHERE {active_sql} AND event_date=? AND city_slug=?",
            (event_date, city_slug),
        ).fetchone()[0] or 0.0)
        event_reserved = float(self.conn.execute(
            f"SELECT COALESCE(SUM(total_cost_usdc),0) FROM basket_strategy_cycles WHERE {active_sql} AND event_date=? AND slug=?",
            (event_date, slug),
        ).fetchone()[0] or 0.0)
        daily_loss = float(self.conn.execute(
            """SELECT COALESCE(SUM(amount_usdc),0)
                 FROM basket_strategy_capital_ledger
                WHERE entry_type='realized_loss' AND event_date=?""",
            (event_date,),
        ).fetchone()[0] or 0.0)
        global_available = float(risk["total_usdc"]) - float(risk["unallocated_pool_usdc"]) - global_reserved
        city_available = float(risk["city_day_cap_usdc"]) - city_reserved
        event_available = float(risk["single_event_cap_usdc"]) - event_reserved
        loss_remaining = float(risk["daily_loss_cap_usdc"]) - daily_loss
        return {
            **risk,
            "slug": slug,
            "event_date": event_date,
            "city_slug": city_slug,
            "global_reserved_usdc": global_reserved,
            "city_reserved_usdc": city_reserved,
            "event_reserved_usdc": event_reserved,
            "realized_loss_usdc": daily_loss,
            "global_available_usdc": global_available,
            "city_available_usdc": city_available,
            "event_available_usdc": event_available,
            "loss_remaining_usdc": loss_remaining,
            "available_usdc": max(0.0, min(global_available, city_available, event_available, loss_remaining)),
        }

    def _capital_gate(self, *, slug: str, event_date: str, city_slug: str, activation: Mapping[str, Any], proposed_usdc: float = 0.0) -> dict[str, Any]:
        snapshot = self._capital_snapshot(slug=slug, event_date=event_date, city_slug=city_slug, activation=activation)
        proposed = max(0.0, float(proposed_usdc))
        reasons: list[str] = []
        if math.isfinite(float(snapshot["loss_remaining_usdc"])) and snapshot["loss_remaining_usdc"] <= 0:
            reasons.append("daily_loss_cap_exhausted")
        if snapshot["global_available_usdc"] + 1e-9 < proposed:
            reasons.append("global_capital_budget_exhausted")
        if snapshot["city_available_usdc"] + 1e-9 < proposed:
            reasons.append("city_day_cap_exhausted")
        if snapshot["event_available_usdc"] + 1e-9 < proposed:
            reasons.append("single_event_cap_exhausted")
        if snapshot["available_usdc"] <= 1e-9:
            reasons.append("capital_budget_unavailable")
        return {
            "status": "blocked" if reasons else "available",
            "reason_codes": sorted(set(reasons)),
            "snapshot": snapshot,
            "proposed_usdc": proposed,
            "available_usdc": snapshot["available_usdc"],
        }

    def _capital_ledger(self, *, cycle_id: str, slug: str, event_date: str, city_slug: str, entry_type: str, amount_usdc: float, reason: str) -> None:
        now = _utc_now()
        key = identity("basket_capital_ledger_", (cycle_id, entry_type, round(float(amount_usdc), 8)))
        self.conn.execute(
            """INSERT OR IGNORE INTO basket_strategy_capital_ledger(
                 ledger_id,cycle_id,slug,event_date,city_slug,entry_type,amount_usdc,reason,idempotency_key,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (key, cycle_id, slug, event_date, city_slug, entry_type, max(0.0, float(amount_usdc)), reason, key, now),
        )

    def _groups(self) -> list[tuple[str, str, str, list[dict[str, Any]]]]:
        rows = self.conn.execute(
            """SELECT slug,event_date,station_id,registry_json,yes_token,no_token,settlement_unit
               FROM reported_trajectory_market_registry
              WHERE source_eligible=1 AND market_active=1 AND market_accepting_orders=1
              ORDER BY event_date,slug,outcome"""
        ).fetchall()
        grouped: dict[tuple[str, str], tuple[str, str, list[dict[str, Any]]]] = {}
        for slug, event_date, station_id, registry_json, yes_token, no_token, unit in rows:
            row = _parse_registry_row({"registry_json": registry_json, "yes_token": yes_token, "no_token": no_token, "settlement_unit": unit})
            if row is None:
                continue
            row["station_id"] = station_id
            key = (str(slug), _date(event_date))
            if key not in grouped:
                grouped[key] = (str(station_id or ""), str(unit or "C"), [])
            grouped[key][2].append(row)
        return [(slug, event_date, values[0], values[2]) for (slug, event_date), values in grouped.items()]

    def _official_report(self, station_id: str, event_date: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            """SELECT b.max_observation_id,o.raw_text,o.source_lane,o.station_id,
                      EXISTS(
                        SELECT 1 FROM reported_trajectory_station_halts h
                         WHERE h.source_lane='awc' AND h.station_id=b.station_id
                           AND h.event_date=b.event_date AND h.state='active'
                      ) AS correction_suspected
               FROM reported_trajectory_station_day_bounds b
               JOIN reported_trajectory_observations o ON o.observation_id=b.max_observation_id
              WHERE b.source_lane='awc' AND o.source_lane='awc'
                AND b.station_id=? AND b.event_date=?
              ORDER BY b.updated_at DESC LIMIT 1""",
            (station_id, event_date),
        ).fetchone()
        if row is None:
            return None
        return {
            "observation_id": row[0],
            "raw_text": row[1],
            "source_lane": row[2],
            "expected_station": row[3],
            "correction_suspected": bool(row[4]),
        }

    def _cycle_rows(self, slug: str, event_date: str) -> list[tuple[str, str, str, float]]:
        return [
            (str(row[0]), str(row[1]), str(row[2] or "{}"), float(row[3] or 0.0))
            for row in self.conn.execute(
                """SELECT cycle_id,state,plan_json,total_cost_usdc
                     FROM basket_strategy_cycles
                    WHERE slug=? AND event_date=?
                    ORDER BY created_at""",
                (slug, event_date),
            ).fetchall()
        ]

    def _existing_cycle(self, slug: str, event_date: str, tranche_signature: str | None = None) -> bool:
        active_states = {"prepared", "submitting", "submitted", "partially_filled", "reconcile_required"}
        for _cycle_id, state, plan_json, _cost in self._cycle_rows(slug, event_date):
            if state not in active_states:
                continue
            if tranche_signature is None:
                return True
            try:
                plan = json.loads(plan_json)
            except (TypeError, ValueError):
                plan = {}
            basket = plan.get("basket") if isinstance(plan, Mapping) else {}
            if isinstance(basket, Mapping) and str(basket.get("tranche_signature") or "") == tranche_signature:
                return True
        return False

    def _completed_widths(self, slug: str, event_date: str) -> set[int]:
        completed: set[int] = set()
        for _cycle_id, state, plan_json, _cost in self._cycle_rows(slug, event_date):
            if state != "submitted":
                continue
            try:
                plan = json.loads(plan_json)
            except (TypeError, ValueError):
                continue
            basket = plan.get("basket") if isinstance(plan, Mapping) else {}
            width = basket.get("tranche_width") if isinstance(basket, Mapping) else None
            try:
                width_int = int(width)
            except (TypeError, ValueError):
                continue
            if width_int in {2, 3, 4}:
                completed.add(width_int)
        return completed

    def _latest_submitted_stage(self, slug: str, event_date: str) -> dict[str, Any] | None:
        rows = self.conn.execute(
            """SELECT cycle_id,plan_json FROM basket_strategy_cycles
                WHERE slug=? AND event_date=? AND state='submitted'
                ORDER BY updated_at DESC""",
            (slug, event_date),
        ).fetchall()
        for cycle_id, plan_json in rows:
            try:
                plan = json.loads(str(plan_json or "{}"))
            except (TypeError, ValueError):
                continue
            basket = plan.get("basket") if isinstance(plan, Mapping) else {}
            if not isinstance(basket, Mapping):
                continue
            try:
                width = int(basket.get("tranche_width"))
            except (TypeError, ValueError):
                continue
            if width not in {2, 3, 4}:
                continue
            return {"cycle_id": str(cycle_id), "width": width, "plan": plan}
        return None

    def _stage_target_state(self, cycle_id: str) -> dict[str, Any]:
        rows = self.conn.execute(
            """SELECT target_version,bucket,target_quantity,filled_quantity,pending_quantity
                 FROM basket_strategy_position_targets
                WHERE cycle_id=? ORDER BY target_version,bucket""",
            (cycle_id,),
        ).fetchall()
        if not rows:
            return {"target_version": 0, "targets": {}, "filled": {}, "pending": {}}
        version = max(int(row[0] or 0) for row in rows)
        targets: dict[str, float] = {}
        filled: dict[str, float] = {}
        pending: dict[str, float] = {}
        for row in rows:
            if int(row[0] or 0) != version:
                continue
            bucket = str(row[1] or "")
            targets[bucket] = float(row[2] or 0.0)
            filled[bucket] = float(row[3] or 0.0)
            pending[bucket] = float(row[4] or 0.0)
        return {"target_version": version, "targets": targets, "filled": filled, "pending": pending}

    def _maybe_build_same_stage_topup(
        self,
        *,
        request: Mapping[str, Any],
        slug: str,
        event_date: str,
        city_slug: str,
        activation: Mapping[str, Any],
        available_usdc: float,
    ) -> tuple[dict[str, Any] | None, list[str]]:
        config = request.get("config") if isinstance(request.get("config"), Mapping) else {}
        if not bool(config.get("allow_dynamic_additions")):
            return None, []
        latest = self._latest_submitted_stage(slug, event_date)
        if latest is None:
            return None, []
        state = self._stage_target_state(str(latest["cycle_id"]))
        if not state["targets"]:
            return None, ["same_stage_target_state_missing"]
        topup_config = dict(config)
        topup_config.update(
            {
                "dynamic_convergence_enabled": False,
                "basket_width": int(latest["width"]),
                "allow_dynamic_additions": False,
                "bankroll_usdc": float(available_usdc),
                "tranche_budget_usdc": min(float(config.get("tranche_budget_usdc") or available_usdc), float(available_usdc)),
            }
        )
        topup_request = dict(request)
        topup_request["config"] = topup_config
        topup_request["completed_tranche_widths"] = []
        plan = build_basket_yes_plan(topup_request)
        if plan.get("status") != "ready":
            return None, [f"same_stage_plan_blocked:{code}" for code in (plan.get("basket", {}).get("reason_codes") or plan.get("direction_bias", {}).get("reason_codes") or ["same_stage_plan_blocked"])]
        basket = plan.get("basket") if isinstance(plan.get("basket"), Mapping) else {}
        target_quantities = basket.get("quantity_by_bucket") if isinstance(basket.get("quantity_by_bucket"), Mapping) else {}
        if set(target_quantities) != set(state["targets"]):
            return None, ["same_stage_bucket_set_changed"]
        asks = {str(order.get("bucket") or ""): float(order.get("limit_price") or 0.0) for order in basket.get("orders") or [] if isinstance(order, Mapping)}
        macro = compute_macro_topup(
            target_quantities={str(k): float(v) for k, v in target_quantities.items()},
            filled_quantities=state["filled"],
            pending_quantities=state["pending"],
            available_budget_usdc=float(available_usdc),
            ask_by_bucket=asks,
        )
        deltas = macro.get("delta_quantities") if isinstance(macro.get("delta_quantities"), Mapping) else {}
        minimum = float(topup_config.get("minimum_order_shares") or 1.0)
        if not deltas or any(float(deltas.get(bucket, 0.0) or 0.0) < minimum for bucket in target_quantities):
            return None, ["same_stage_topup_not_required"]
        total_cost = float(macro.get("estimated_notional_usdc") or 0.0)
        if total_cost <= 0 or min(float(deltas.get(bucket, 0.0) or 0.0) for bucket in target_quantities) + 1e-9 < total_cost:
            return None, ["same_stage_topup_settlement_floor_failed"]
        orders = []
        for order in basket.get("orders") or []:
            if not isinstance(order, Mapping):
                continue
            bucket = str(order.get("bucket") or "")
            delta = float(deltas.get(bucket, 0.0) or 0.0)
            orders.append(
                {
                    **dict(order),
                    "quantity": delta,
                    "estimated_notional_usdc": delta * float(order.get("limit_price") or 0.0),
                    "intent_id": identity("basket_topup_intent_", (latest["cycle_id"], state["target_version"] + 1, bucket, delta)),
                }
            )
        if len(orders) != len(target_quantities):
            return None, ["same_stage_topup_leg_set_incomplete"]
        new_plan = json.loads(_json(plan))
        new_basket = new_plan["basket"]
        new_version = int(state["target_version"]) + 1
        new_basket.update(
            {
                "orders": orders,
                "target_version": new_version,
                "topup_of_cycle_id": str(latest["cycle_id"]),
                "quantity_per_bucket": min(float(item["quantity"]) for item in orders),
                "quantity_by_bucket": {str(item["bucket"]): float(item["quantity"]) for item in orders},
                "total_cost_usdc": total_cost,
                "worst_case_settlement_payout_usdc": min(float(item["quantity"]) for item in orders),
                "guaranteed_profit_usdc": min(float(item["quantity"]) for item in orders) - total_cost,
                "settlement_profit_by_bucket": {str(item["bucket"]): float(item["quantity"]) - total_cost for item in orders},
                "macro_topup": macro,
                "tranche_signature": identity("basket_topup_", (latest["cycle_id"], new_version, sorted(new_basket.get("selected_buckets") or []), total_cost)),
            }
        )
        new_plan["plan_id"] = identity("basket_topup_plan_", (latest["cycle_id"], new_version, new_basket["tranche_signature"]))
        return new_plan, []

    def _reserved_capital(self, slug: str, event_date: str) -> float:
        reserved_states = {"prepared", "submitting", "submitted", "partially_filled", "reconcile_required"}
        return sum(cost for _cycle_id, state, _plan_json, cost in self._cycle_rows(slug, event_date) if state in reserved_states)

    def _unresolved_cycles(self) -> int:
        row = self.conn.execute(
            """SELECT COUNT(*) FROM basket_strategy_cycles
              WHERE state IN ('prepared','submitting','partially_filled','reconcile_required')"""
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def reconcile_known_fills(self) -> dict[str, Any]:
        """Quarantine locally recorded partial fills before any new target.

        A gateway response can be accepted locally and later be shown as a
        smaller match by the exchange/position evidence.  The old runtime
        trusted the cycle's terminal ``submitted`` state and could therefore
        treat the target as satisfied.  This repair is deliberately local and
        additive: it does not place, cancel, or retry an order.  It only moves
        an inconsistent cycle back to the durable reconciliation boundary.
        """

        rows = self.conn.execute(
            """SELECT l.cycle_id,l.bucket,l.requested_size,l.size_matched,
                              l.state,t.target_quantity,t.state,
                              COALESCE((
                                SELECT co.state
                                  FROM basket_strategy_child_orders co
                                  JOIN basket_strategy_parent_orders po
                                    ON po.parent_order_id=co.parent_order_id
                                 WHERE po.cycle_id=l.cycle_id AND po.bucket=l.bucket
                                 ORDER BY co.created_at DESC LIMIT 1
                              ),'')
                       FROM basket_strategy_legs l
                       JOIN basket_strategy_cycles c ON c.cycle_id=l.cycle_id
                  LEFT JOIN basket_strategy_position_targets t
                         ON t.cycle_id=l.cycle_id AND t.bucket=l.bucket
                      WHERE c.state IN ('submitted','reconcile_required')
                        AND (
                          l.size_matched + 0.005 < l.requested_size
                          OR COALESCE(t.target_quantity,0) + 0.005 < l.requested_size
                        )
                   ORDER BY l.cycle_id,l.bucket"""
        ).fetchall()
        if not rows:
            return {"schema": "basket_known_fill_reconcile_v1", "state": "idle", "cycles": []}

        by_cycle: dict[str, list[dict[str, Any]]] = {}
        now = _utc_now()
        for cycle_id, bucket, requested, matched, leg_state, target, target_state, child_state in rows:
            cycle = str(cycle_id)
            bucket_text = str(bucket or "")
            requested_value = max(0.0, float(requested or 0.0))
            matched_value = max(0.0, float(matched or 0.0))
            remaining = max(0.0, requested_value - matched_value)
            self.conn.execute(
                """UPDATE basket_strategy_legs
                      SET state='reconcile_required',last_error=?,updated_at=?
                    WHERE cycle_id=? AND bucket=?""",
                ("partial_fill_requires_reconcile", now, cycle, bucket_text),
            )
            self.conn.execute(
                """UPDATE basket_strategy_position_targets
                      SET target_quantity=MAX(target_quantity,?),filled_quantity=?,pending_quantity=0,delta_quantity=?,
                          state='reconcile_required',updated_at=?
                    WHERE cycle_id=? AND bucket=?""",
                (requested_value, matched_value, remaining, now, cycle, bucket_text),
            )
            self.conn.execute(
                """UPDATE basket_strategy_parent_orders
                      SET state='reconcile_required',updated_at=?
                    WHERE cycle_id=? AND bucket=?""",
                (now, cycle, bucket_text),
            )
            self.conn.execute(
                """UPDATE basket_strategy_child_orders
                      SET state='reconcile_required',size_matched=?,updated_at=?
                    WHERE parent_order_id IN (
                      SELECT parent_order_id FROM basket_strategy_parent_orders
                       WHERE cycle_id=? AND bucket=?
                    )""",
                (matched_value, now, cycle, bucket_text),
            )
            by_cycle.setdefault(cycle, []).append(
                {
                    "bucket": bucket_text,
                    "requested_size": requested_value,
                    "size_matched": matched_value,
                    "remaining_size": remaining,
                    "prior_leg_state": str(leg_state or ""),
                    "prior_target_state": str(target_state or ""),
                    "prior_child_state": str(child_state or ""),
                }
            )

        for cycle_id in by_cycle:
            self.conn.execute(
                "UPDATE basket_strategy_cycles SET state='reconcile_required',updated_at=? WHERE cycle_id=?",
                (now, cycle_id),
            )
        self.conn.commit()
        return {
            "schema": "basket_known_fill_reconcile_v1",
            "state": "reconcile_required",
            "cycles": [
                {"cycle_id": cycle_id, "partial_legs": legs}
                for cycle_id, legs in sorted(by_cycle.items())
            ],
        }

    def _decision(self, *, slug: str, event_date: str, outcome: str, will_order: bool, reason_codes: list[str], details: Mapping[str, Any], order_intent_id: str = "") -> None:
        decision_id = identity("basket_decision_", (slug, event_date, outcome, details.get("plan_id"), will_order, reason_codes))
        now = _utc_now()
        self.conn.execute(
            """INSERT OR IGNORE INTO auto_trade_decisions(
                 run_id,run_date,slug,event_date,title,outcome,side,action,decided_state,will_order,
                 reason_codes_json,reasons_json,details_json,created_at,correlation_id,fact_snapshot_id,
                 posterior_run_id,decision_id,signal_id,order_intent_id)
               VALUES(0,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (event_date, slug, event_date, "Weather YES basket", outcome, "YES", "entry",
             "will_order" if will_order else "blocked", int(will_order), _json(reason_codes), _json(reason_codes),
             _json(dict(details)), now, identity("basket_corr_", (slug, event_date)), str(details.get("fact_snapshot_id") or ""),
             str(details.get("posterior_run_id") or ""), decision_id, identity("basket_signal_", (slug, event_date, outcome)), order_intent_id or None),
        )

    def _submit(self, plan: Mapping[str, Any], slug: str, event_date: str, city_slug: str) -> dict[str, Any]:
        basket = plan.get("basket") if isinstance(plan.get("basket"), Mapping) else {}
        cycle_id = str(plan.get("plan_id") or identity("basket_cycle_", (slug, event_date, plan.get("plan_id"))))
        plan_sha = _sha(plan)
        now = _utc_now()
        self.conn.execute(
            """INSERT OR IGNORE INTO basket_strategy_cycles(
                 cycle_id,strategy_id,slug,event_date,city_slug,plan_sha256,plan_json,state,total_cost_usdc,quantity_per_bucket,created_at,updated_at)
               VALUES(?,?,?,?,?,? ,?,'prepared',?,?,?,?)""",
            (cycle_id, "weather_basket_yes_v2", slug, event_date, city_slug, plan_sha, _json(plan), float(basket.get("total_cost_usdc") or 0), float(basket.get("quantity_per_bucket") or 0), now, now),
        )
        self._capital_ledger(
            cycle_id=cycle_id,
            slug=slug,
            event_date=event_date,
            city_slug=city_slug,
            entry_type="reserve",
            amount_usdc=float(basket.get("total_cost_usdc") or 0.0),
            reason="basket_cycle_reserved_before_fok_submission",
        )
        # Persist the macro target separately from exchange legs.  A new
        # posterior/quote checkpoint may create an additive top-up target;
        # existing targets are never reduced or silently role-swapped.
        target_version = int(basket.get("target_version") or 1)
        snapshot_id = str(
            (plan.get("probability_projection") or {}).get("fact_snapshot_id")
            or (plan.get("probability_projection") or {}).get("posterior_checkpoint_id")
            or ""
        )
        quantity_by_bucket = basket.get("quantity_by_bucket") if isinstance(basket.get("quantity_by_bucket"), Mapping) else {}
        profit_by_bucket = basket.get("settlement_profit_by_bucket") if isinstance(basket.get("settlement_profit_by_bucket"), Mapping) else {}
        role_by_bucket = {
            str(row.get("bucket") or ""): str(row.get("role") or "")
            for row in (basket.get("role_assignments") or [])
            if isinstance(row, Mapping)
        }
        for bucket, quantity in quantity_by_bucket.items():
            target_id = identity("basket_target_", (cycle_id, target_version, bucket))
            self.conn.execute(
                """INSERT OR IGNORE INTO basket_strategy_position_targets(
                     target_id,cycle_id,snapshot_id,target_version,bucket,role,
                     target_profit_usdc,target_quantity,filled_quantity,pending_quantity,
                     delta_quantity,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,0,0,?,'topup_ready',?,?)""",
                (
                    target_id,
                    cycle_id,
                    snapshot_id,
                    target_version,
                    str(bucket),
                    role_by_bucket.get(str(bucket), ""),
                    float(profit_by_bucket.get(bucket, 0.0) or 0.0),
                    float(quantity or 0.0),
                    float(quantity or 0.0),
                    now,
                    now,
                ),
            )
        legs = basket.get("orders") if isinstance(basket.get("orders"), list) else []
        for order in legs:
            leg_id = identity("basket_leg_", (cycle_id, order.get("token"), order.get("bucket")))
            intent_id = str(order.get("intent_id") or identity("basket_intent_", (cycle_id, order)))
            key = identity("basket_idem_", (cycle_id, intent_id))
            self.conn.execute(
                """INSERT OR IGNORE INTO basket_strategy_legs(
                    leg_id,cycle_id,order_intent_id,idempotency_key,bucket,token_id,side,limit_price,requested_size,requested_notional_usdc,order_type,state,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)""",
                (leg_id, cycle_id, intent_id, key, str(order.get("bucket") or ""), str(order.get("token") or ""), "YES", float(order.get("limit_price") or 0), float(order.get("quantity") or 0), float(order.get("estimated_notional_usdc") or 0), "FOK", now, now),
            )
            parent_id = identity("basket_parent_", (cycle_id, target_version, order.get("bucket")))
            parent_key = identity("basket_parent_idem_", (parent_id, snapshot_id))
            self.conn.execute(
                """INSERT OR IGNORE INTO basket_strategy_parent_orders(
                     parent_order_id,cycle_id,target_version,snapshot_id,bucket,
                     requested_quantity,requested_notional_usdc,state,idempotency_key,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    parent_id,
                    cycle_id,
                    target_version,
                    snapshot_id,
                    str(order.get("bucket") or ""),
                    float(order.get("quantity") or 0.0),
                    float(order.get("estimated_notional_usdc") or 0.0),
                    "prepared",
                    parent_key,
                    now,
                    now,
                ),
            )
            child_id = identity("basket_child_", (parent_id, 0, order.get("limit_price")))
            child_key = identity("basket_child_idem_", (child_id, parent_key))
            self.conn.execute(
                """INSERT OR IGNORE INTO basket_strategy_child_orders(
                     child_order_id,parent_order_id,leg_id,price_level,limit_price,
                     target_quantity,target_notional_usdc,state,idempotency_key,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    child_id,
                    parent_id,
                    leg_id,
                    0,
                    float(order.get("limit_price") or 0.0),
                    float(order.get("quantity") or 0.0),
                    float(order.get("estimated_notional_usdc") or 0.0),
                    "prepared",
                    child_key,
                    now,
                    now,
                ),
            )
            self._decision(
                slug=slug,
                event_date=event_date,
                outcome=str(order.get("bucket") or ""),
                will_order=True,
                reason_codes=["basket_worst_case_floor_below_one", "regime_probability_gate_passed"],
                details={"plan_id": plan.get("plan_id"), "fact_snapshot_id": (plan.get("probability_projection") or {}).get("fact_snapshot_id"), "posterior_run_id": (plan.get("probability_projection") or {}).get("posterior_run_id")},
                order_intent_id=intent_id,
            )
        self.conn.commit()
        self.conn.execute("UPDATE basket_strategy_cycles SET state='submitting',updated_at=? WHERE cycle_id=? AND state='prepared'", (now, cycle_id))
        self.conn.commit()
        results: list[dict[str, Any]] = []
        failed = False
        for order in legs:
            intent_id = str(order.get("intent_id") or identity("basket_intent_", (cycle_id, order)))
            key = identity("basket_idem_", (cycle_id, intent_id))
            payload = {
                "schema": SCHEMA_VERSION,
                "strategy_id": "weather_basket_yes_v2",
                "order_intent_id": intent_id,
                "idempotency_key": key,
                "token_id": str(order.get("token") or ""),
                "side": "BUY",
                "outcome_side": "YES",
                "price": float(order.get("limit_price") or 0),
                "size": float(order.get("quantity") or 0),
                "amount": float(order.get("quantity") or 0),
                "notional": float(order.get("estimated_notional_usdc") or 0),
                "order_type": "FOK",
                # FOK is the marketable order type used for the all-legs basket.
                # The gateway intentionally rejects FOK unless this is explicit.
                "allow_market": True,
                "post_only": False,
                "direct_target_order_context": False,
                "basket_cycle_id": cycle_id,
            }
            self.conn.execute("UPDATE basket_strategy_legs SET state='submitting',updated_at=? WHERE order_intent_id=? AND state='prepared'", (_utc_now(), intent_id))
            self.conn.commit()
            try:
                response = self.gateway.place_order(payload)
            except Exception as exc:
                response = {"ok": False, "reconcile_required": True, "error": f"{type(exc).__name__}:{exc}"}
            matched = _float(response.get("size_matched") or response.get("matched_size")) or 0.0
            exchange_id = str(response.get("orderID") or response.get("order_id") or "")
            requested_size = float(order.get("quantity") or 0)
            status_text = str(response.get("status") or "").strip().lower()
            open_statuses = {"open", "live", "pending", "submitted", "partial", "partially_filled"}
            full = (
                matched > 0
                and matched + 0.005 >= requested_size
                and status_text not in open_statuses
            )
            ok = response.get("ok") is True and bool(exchange_id) and full
            # ``exchange_post_started`` is expected on a definitive successful
            # response.  It only makes the result ambiguous when the response
            # is not a complete, acknowledged fill.
            ambiguous = (
                not ok
                and (
                    response.get("pending") is True
                    or response.get("reconcile_required") is True
                    or response.get("exchange_post_started") is True
                    or (response.get("ok") is True and bool(exchange_id))
                    or matched > 0
                )
            )
            if ambiguous and not ok and matched > 0 and matched + 0.005 < requested_size:
                response = {
                    **dict(response),
                    "partial_fill": True,
                    "requested_size": requested_size,
                    "size_matched": matched,
                    "remaining_size": max(0.0, requested_size - matched),
                }
            state = "exchange_accepted" if ok else "reconcile_required" if ambiguous else "submit_failed"
            self.conn.execute(
                """UPDATE basket_strategy_legs SET state=?,exchange_order_id=?,size_matched=?,gateway_response_json=?,last_error=?,updated_at=? WHERE order_intent_id=?""",
                (state, exchange_id or None, matched, _json(response), str(response.get("error") or "") or None, _utc_now(), intent_id),
            )
            target_state = "satisfied" if ok else "reconcile_required" if ambiguous else "blocked"
            self.conn.execute(
                """UPDATE basket_strategy_position_targets
                      SET filled_quantity=?,pending_quantity=0,delta_quantity=MAX(target_quantity-?,0),state=?,updated_at=?
                    WHERE cycle_id=? AND bucket=? AND target_version=?""",
                (matched, matched, target_state, _utc_now(), cycle_id, str(order.get("bucket") or ""), target_version),
            )
            parent_state = "filled" if ok else "reconcile_required" if ambiguous else "cancelled"
            child_state = "filled" if ok else "reconcile_required" if ambiguous else "submit_failed"
            self.conn.execute(
                "UPDATE basket_strategy_parent_orders SET state=?,updated_at=? WHERE cycle_id=? AND bucket=? AND target_version=?",
                (parent_state, _utc_now(), cycle_id, str(order.get("bucket") or ""), target_version),
            )
            self.conn.execute(
                """UPDATE basket_strategy_child_orders SET state=?,exchange_order_id=?,size_matched=?,gateway_response_json=?
                    WHERE parent_order_id IN (SELECT parent_order_id FROM basket_strategy_parent_orders WHERE cycle_id=? AND bucket=? AND target_version=?)""",
                (child_state, exchange_id or None, matched, _json(response), cycle_id, str(order.get("bucket") or ""), target_version),
            )
            self.conn.commit()
            results.append({"bucket": order.get("bucket"), "state": state, "gateway": response})
            if state not in {"exchange_accepted", "filled"}:
                failed = True
                break
        # Any exchange identity or non-zero match that is not a verified full
        # fill remains a reconciliation blocker, even when it is the first or
        # only leg.  The previous condition looked only for an earlier
        # exchange_accepted leg, released capital on a lone partial fill, and
        # allowed the next run to submit a duplicate target.
        final_state = (
            "reconcile_required"
            if failed and any(item["state"] == "reconcile_required" for item in results)
            else "failed" if failed else "submitted"
        )
        self.conn.execute("UPDATE basket_strategy_cycles SET state=?,updated_at=? WHERE cycle_id=?", (final_state, _utc_now(), cycle_id))
        if final_state == "failed":
            self._capital_ledger(
                cycle_id=cycle_id,
                slug=slug,
                event_date=event_date,
                city_slug=city_slug,
                entry_type="release",
                amount_usdc=float(basket.get("total_cost_usdc") or 0.0),
                reason="basket_cycle_failed_without_exchange_acceptance",
            )
        self.conn.commit()
        return {"cycle_id": cycle_id, "state": final_state, "legs": results}

    def run_once(self) -> dict[str, Any]:
        try:
            activation = load_activation()
        except Exception as exc:
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": f"activation_invalid:{type(exc).__name__}"}
        no_live = activation.get("execution_mode") == "production_no_live"
        if activation.get("state") != "enabled" or (
            not no_live and activation.get("live_submit_authorized") is not True
        ):
            return {"schema": SCHEMA_VERSION, "ok": True, "state": "disabled", "plans": 0, "orders": 0}
        known_fill_reconcile = self.reconcile_known_fills()
        if known_fill_reconcile.get("state") != "idle":
            return {
                "schema": SCHEMA_VERSION,
                "ok": False,
                "state": "blocked",
                "reason": "basket_partial_fill_requires_reconcile",
                "reconcile": known_fill_reconcile,
            }
        if self._unresolved_cycles():
            return {"schema": SCHEMA_VERSION, "ok": False, "state": "blocked", "reason": "basket_unresolved_cycle_requires_reconcile"}
        plans = 0
        submitted = 0
        blocked: list[str] = []
        results: list[dict[str, Any]] = []
        basket_config = dict(activation.get("basket") or {})
        for slug, event_date, station_id, registry_rows in self._groups():
            city_slug = str(registry_rows[0].get("city_slug") or station_id or "unknown").strip().lower()
            capital_gate = self._capital_gate(
                slug=slug,
                event_date=event_date,
                city_slug=city_slug,
                activation=activation,
            )
            if capital_gate["status"] != "available":
                blocked.append(f"{slug}:{','.join(capital_gate['reason_codes'])}")
                continue
            completed_widths = self._completed_widths(slug, event_date)
            reserved_capital = self._reserved_capital(slug, event_date)
            configured_bankroll = float(basket_config.get("bankroll_usdc", 0.0) or 0.0)
            remaining_capital = min(configured_bankroll - reserved_capital, float(capital_gate["available_usdc"]))
            if remaining_capital <= 1e-9:
                continue
            projection = _probability_projection(self.conn, slug, event_date)
            if projection.get("status") != "available":
                continue
            quotes: list[dict[str, Any]] = []
            weighted_sum = weighted_total = 0.0
            for row in registry_rows:
                if row.get("kind") != "single" or not row.get("yes_token") or row.get("bucket_c") is None:
                    continue
                quote = _quote(str(row["yes_token"]), max_age_sec=float(basket_config.get("max_quote_age_sec", 5.0)))
                if quote is None:
                    continue
                item = {**row, **quote}
                quotes.append(item)
                weighted_sum += float(row["bucket_c"]) * float(quote["yes_ask"])
                weighted_total += float(quote["yes_ask"])
            dynamic_enabled = bool(basket_config.get("dynamic_convergence_enabled"))
            required_quotes = min(int(item) for item in (basket_config.get("convergence_widths") or [3])) if dynamic_enabled else int(basket_config.get("basket_width", 3))
            if len(quotes) < required_quotes or weighted_total <= 0:
                continue
            event = {"event_id": slug + "|" + event_date, "slug": slug, "event_date": event_date, "target_event_date": event_date, "market_unit": "C", "market_anchor_c": weighted_sum / weighted_total, "station_id": station_id}
            report = self._official_report(station_id, event_date)
            request_config = dict(basket_config)
            request_config["bankroll_usdc"] = remaining_capital
            request_config["tranche_budget_usdc"] = min(
                float(request_config.get("tranche_budget_usdc") or remaining_capital),
                remaining_capital,
            )
            request_config["parent_order_cap_usdc"] = float(capital_gate["snapshot"]["parent_order_cap_usdc"])
            request = {
                "as_of": _utc_now(),
                "event": event,
                "buckets": quotes,
                "probability_projection": projection,
                # When the basket gate is disabled, do not even consume the
                # materialized regime surface.  This keeps the temporary
                # override scoped to the basket and avoids consuming regime
                # materialization when the agreement gate is disabled.
                "regime": _regime(self.conn, event_date)
                if request_config.get("regime_gate_enabled", True)
                else {},
                "report": report,
                "completed_tranche_widths": sorted(completed_widths),
                "config": request_config,
            }
            topup_plan, _topup_reasons = self._maybe_build_same_stage_topup(
                request=request,
                slug=slug,
                event_date=event_date,
                city_slug=city_slug,
                activation=activation,
                available_usdc=remaining_capital,
            )
            if topup_plan is not None:
                topup_basket = topup_plan.get("basket") if isinstance(topup_plan.get("basket"), Mapping) else {}
                parent_cap = float(capital_gate["snapshot"]["parent_order_cap_usdc"])
                if any(float(order.get("estimated_notional_usdc") or 0.0) > parent_cap + 1e-9 for order in (topup_basket.get("orders") or []) if isinstance(order, Mapping)):
                    blocked.append(f"{slug}:parent_order_cap_exceeded")
                    continue
                topup_gate = self._capital_gate(
                    slug=slug,
                    event_date=event_date,
                    city_slug=city_slug,
                    activation=activation,
                    proposed_usdc=float(topup_basket.get("total_cost_usdc") or 0.0),
                )
                if topup_gate["status"] != "available":
                    blocked.append(f"{slug}:{','.join(topup_gate['reason_codes'])}")
                    continue
                plans += 1
                if no_live:
                    results.append({
                        "state": "prepared_no_live",
                        "slug": slug,
                        "event_date": event_date,
                        "plan": topup_plan,
                        "orders_submitted": 0,
                        "calls_order_gateway": False,
                        "writes_production_db": False,
                    })
                    continue
                result = self._submit(topup_plan, slug, event_date, city_slug)
                results.append(result)
                submitted += 1 if result.get("state") in {"submitted", "reconcile_required"} else 0
                if result.get("state") == "reconcile_required":
                    break
                continue
            plan = build_basket_yes_plan(request)
            plans += 1
            if plan.get("status") != "ready" or plan.get("live_submit_authorized") is not True:
                if no_live and plan.get("status") == "ready":
                    # The strategy plan is evaluated in the production
                    # service, but the final authority is deliberately
                    # absent.  Preserve the complete candidate for audit and
                    # stop before cycle/intent/gateway mutation.
                    results.append({
                        "state": "prepared_no_live",
                        "slug": slug,
                        "event_date": event_date,
                        "plan": plan,
                        "orders_submitted": 0,
                        "calls_order_gateway": False,
                        "writes_production_db": False,
                    })
                    continue
                blocked.append(f"{slug}:{','.join(plan.get('direction_bias', {}).get('reason_codes') or plan.get('basket', {}).get('reason_codes') or ['plan_blocked'])}")
                continue
            basket = plan.get("basket") if isinstance(plan.get("basket"), Mapping) else {}
            parent_cap = float(capital_gate["snapshot"]["parent_order_cap_usdc"])
            if any(float(order.get("estimated_notional_usdc") or 0.0) > parent_cap + 1e-9 for order in (basket.get("orders") or []) if isinstance(order, Mapping)):
                blocked.append(f"{slug}:parent_order_cap_exceeded")
                continue
            plan_gate = self._capital_gate(
                slug=slug,
                event_date=event_date,
                city_slug=city_slug,
                activation=activation,
                proposed_usdc=float(basket.get("total_cost_usdc") or 0.0),
            )
            if plan_gate["status"] != "available":
                blocked.append(f"{slug}:{','.join(plan_gate['reason_codes'])}")
                continue
            tranche_signature = str(basket.get("tranche_signature") or "")
            if tranche_signature and self._existing_cycle(slug, event_date, tranche_signature):
                continue
            if no_live:
                results.append({
                    "state": "prepared_no_live",
                    "slug": slug,
                    "event_date": event_date,
                    "plan": plan,
                    "orders_submitted": 0,
                    "calls_order_gateway": False,
                    "writes_production_db": False,
                })
                continue
            result = self._submit(plan, slug, event_date, city_slug)
            results.append(result)
            submitted += 1 if result.get("state") in {"submitted", "reconcile_required"} else 0
            if result.get("state") == "reconcile_required":
                break
        return {
            "schema": SCHEMA_VERSION,
            "ok": True,
            "state": "production_no_live" if no_live else "active",
            "execution_mode": "production_no_live" if no_live else "production_live",
            "plans": plans,
            "prepared_no_live_cycles": sum(item.get("state") == "prepared_no_live" for item in results),
            "submitted_cycles": submitted,
            "orders_submitted": 0 if no_live else None,
            "calls_order_gateway": False if no_live else None,
            "writes_production_db": False if no_live else None,
            "blocked": blocked[-20:],
            "results": results[-20:],
            "checked_at": _utc_now(),
        }


__all__ = ["BasketProductionEngine", "SCHEMA_VERSION"]
