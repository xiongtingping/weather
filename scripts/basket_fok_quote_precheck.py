#!/usr/bin/env python3
"""Read-only, production-equivalent dynamic-stage YES FOK quote precheck.

This command deliberately stops before ``BasketProductionEngine._submit``.  It
uses the same materialized P0/P1 projection, regime gate, registry rows and
official CLOB WebSocket quote shards as production, but opens ``archive.db`` in
SQLite read-only mode and never creates a cycle, decision, intent or order.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
import sys

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from basket_strategy.activation import load_activation
from basket_strategy.production import (
    _date,
    _parse_registry_row,
    _probability_projection,
    _quote,
    _regime,
)
from basket_strategy.runtime import (
    BasketStrategyConfig,
    _select_dynamic_basket_quotes,
    build_basket_yes_plan,
)


DEFAULT_DB = ROOT / "data" / "archive.db"


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _age_seconds(value: str, *, now: datetime) -> float | None:
    try:
        observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if observed.tzinfo is None:
        observed = observed.replace(tzinfo=timezone.utc)
    return max(0.0, (now - observed).total_seconds())


def _open_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _registry_rows(conn: sqlite3.Connection, *, slug: str, event_date: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for raw in conn.execute(
        """SELECT registry_json,yes_token,no_token,settlement_unit,station_id,outcome
             FROM reported_trajectory_market_registry
            WHERE slug=? AND event_date=? AND source_eligible=1
              AND market_active=1 AND market_accepting_orders=1
            ORDER BY outcome""",
        (slug, event_date),
    ).fetchall():
        item = _parse_registry_row(
            {
                "registry_json": raw["registry_json"],
                "yes_token": raw["yes_token"],
                "no_token": raw["no_token"],
                "settlement_unit": raw["settlement_unit"],
            }
        )
        if item is None:
            continue
        item["station_id"] = str(raw["station_id"] or "")
        rows.append(item)
    return rows


def _completed_widths(conn: sqlite3.Connection, *, slug: str, event_date: str) -> list[int]:
    result: set[int] = set()
    try:
        source = conn.execute(
            """SELECT state,plan_json FROM basket_strategy_cycles
                WHERE slug=? AND event_date=?""",
            (slug, event_date),
        ).fetchall()
    except sqlite3.Error:
        return []
    for row in source:
        if str(row["state"] or "") != "submitted":
            continue
        try:
            plan = json.loads(str(row["plan_json"] or "{}"))
        except (TypeError, ValueError):
            continue
        basket = plan.get("basket") if isinstance(plan, Mapping) else {}
        try:
            width = int(basket.get("tranche_width")) if isinstance(basket, Mapping) else 0
        except (TypeError, ValueError):
            width = 0
        if width in {2, 3, 4}:
            result.add(width)
    return sorted(result)


def _quote_rows(rows: list[dict[str, Any]], *, max_age_sec: float, now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    fresh: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    for row in rows:
        token = str(row.get("yes_token") or "")
        quote = _quote(token, max_age_sec=86400.0) if token else None
        observed_at = str(quote.get("observed_at") or "") if quote else ""
        age_sec = _age_seconds(observed_at, now=now) if observed_at else None
        item: dict[str, Any] = {
            "bucket": str(row.get("outcome") or ""),
            "bucket_c": row.get("bucket_c"),
            "yes_token": token,
            "quote_state": "missing" if quote is None else "fresh" if age_sec is not None and age_sec <= max_age_sec else "stale",
            "observed_at": observed_at,
            "age_sec": round(age_sec, 3) if age_sec is not None else None,
        }
        if quote is not None:
            item.update(
                {
                    "yes_ask": float(quote["yes_ask"]),
                    "yes_ask_size": float(quote["yes_ask_size"]),
                }
            )
        diagnostics.append(item)
        if item["quote_state"] == "fresh":
            fresh.append({**row, **quote})
    return fresh, diagnostics


def _event_slug(conn: sqlite3.Connection, *, event_date: str, slug: str) -> str:
    if slug:
        return slug
    row = conn.execute(
        """SELECT slug FROM reported_trajectory_market_registry
            WHERE event_date=? AND source_eligible=1 AND market_active=1
              AND market_accepting_orders=1 AND slug LIKE 'highest-temperature-in-chengdu-on-%'
            ORDER BY slug LIMIT 1""",
        (event_date,),
    ).fetchone()
    if row is not None:
        return str(row[0] or "")
    row = conn.execute(
        """SELECT slug FROM reported_trajectory_market_registry
            WHERE event_date=? AND source_eligible=1 AND market_active=1
              AND market_accepting_orders=1
            ORDER BY slug LIMIT 1""",
        (event_date,),
    ).fetchone()
    return str(row[0] or "") if row is not None else ""


def _posterior_semantics(conn: sqlite3.Connection, *, slug: str, event_date: str) -> dict[str, Any]:
    """Expose the two posterior namespaces without changing production input."""

    row = conn.execute(
        """SELECT h.fact_json
             FROM event_fact_latest l
             JOIN event_fact_history h ON h.id=l.fact_id
            WHERE l.slug=? AND l.event_date=?
            ORDER BY h.id DESC LIMIT 1""",
        (slug, event_date),
    ).fetchone()
    if row is None:
        return {"state": "missing", "reason": "posterior_fact_missing"}
    try:
        fact = json.loads(str(row[0] or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        fact = {}
    public = fact.get("posterior_ssot_fact") if isinstance(fact, Mapping) else {}
    if not isinstance(public, Mapping):
        return {"state": "missing", "reason": "posterior_ssot_fact_missing"}
    physical = public.get("reported_physical_x_distribution")
    physical_rows: list[tuple[float, float]] = []
    if isinstance(physical, Mapping):
        physical_iter = physical.items()
    elif isinstance(physical, list):
        physical_iter = (
            (
                item.get("temperature_c") if item.get("temperature_c") is not None else item.get("value_c"),
                item.get("probability") if item.get("probability") is not None else item.get("mass"),
            )
            for item in physical
            if isinstance(item, Mapping)
        )
    else:
        physical_iter = ()
    for temperature, probability in physical_iter:
        try:
            temperature_value = float(temperature)
            probability_value = float(probability)
        except (TypeError, ValueError):
            continue
        if math.isfinite(temperature_value) and math.isfinite(probability_value) and probability_value >= 0:
            physical_rows.append((temperature_value, probability_value))
    physical_top = max(physical_rows, key=lambda item: item[1], default=None)
    return {
        "state": "available" if physical_top else "missing",
        "physical_ssot_top_bucket": (
            f"{int(round(physical_top[0]))}°C" if physical_top and abs(physical_top[0] - round(physical_top[0])) < 1e-9 else
            f"{physical_top[0]:g}°C" if physical_top else ""
        ),
        "physical_ssot_top_probability": physical_top[1] if physical_top else None,
        "market_projection_top_bucket": str(public.get("market_top_bucket") or ""),
        "market_projection_top_probability": public.get("market_top_probability"),
        "physical_source": "posterior_ssot_fact.reported_physical_x_distribution",
        "market_source": "posterior_ssot_fact.market_bucket_probabilities",
    }


def _precheck_market(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
    max_age_sec: float,
    now: datetime,
) -> dict[str, Any]:
    rows = _registry_rows(conn, slug=slug, event_date=event_date)
    if not rows:
        return {
            "slug": slug,
            "event_date": event_date,
            "state": "blocked",
            "reason_codes": ["market_registry_missing_or_not_eligible"],
            "quote_precheck": {"state": "not_run", "reason_codes": ["market_registry_missing_or_not_eligible"]},
        }

    fresh, quote_diagnostics = _quote_rows(rows, max_age_sec=max_age_sec, now=now)
    probability = _probability_projection(conn, slug, event_date)
    posterior_semantics = _posterior_semantics(conn, slug=slug, event_date=event_date)
    regime = _regime(conn, event_date)
    total_ask = sum(float(item["yes_ask"]) for item in fresh)
    weighted_sum = sum(float(item.get("bucket_c") or 0) * float(item["yes_ask"]) for item in fresh)
    market_anchor = weighted_sum / total_ask if total_ask > 0 else None
    activation = load_activation()
    config = dict(activation.get("basket") or {})
    # Preserve production selection while making the precheck explicitly dry-run.
    config["live_submit_authorized"] = False
    event = {
        "event_id": f"{slug}|{event_date}",
        "slug": slug,
        "event_date": event_date,
        "target_event_date": event_date,
        "market_unit": "C",
        "market_anchor_c": market_anchor,
        "station_id": str(rows[0].get("station_id") or ""),
    }
    request = {
        "as_of": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "event": event,
        "buckets": fresh,
        "probability_projection": probability,
        "regime": regime,
        "report": None,
        "completed_tranche_widths": _completed_widths(conn, slug=slug, event_date=event_date),
        "config": config,
    }
    plan = build_basket_yes_plan(request)
    direction = plan.get("direction_bias") if isinstance(plan.get("direction_bias"), Mapping) else {}
    basket = plan.get("basket") if isinstance(plan.get("basket"), Mapping) else {}

    center_row = max(probability.get("rows") or [], key=lambda item: float(item.get("probability") or 0.0), default={})
    center_c = center_row.get("bucket_c") if isinstance(center_row, Mapping) else None
    # The production basket is not a fixed four-leg order.  The current
    # stage is selected from the materialized convergence plan (4 -> 3 -> 2)
    # and may narrow when posterior coverage changes.  Keep the quote check
    # aligned to that exact stage instead of prechecking a hard-coded width.
    try:
        inferred_stage_width = int(basket.get("tranche_width"))
    except (TypeError, ValueError):
        inferred_stage_width = 0
    if inferred_stage_width not in {2, 3, 4}:
        configured_widths = config.get("convergence_widths") or [4, 3, 2]
        completed = set(_completed_widths(conn, slug=slug, event_date=event_date))
        if bool(config.get("dynamic_convergence_enabled")):
            eligible = [int(width) for width in configured_widths if int(width) in {2, 3, 4} and int(width) not in completed]
            inferred_stage_width = eligible[0] if eligible else 2
        else:
            try:
                inferred_stage_width = int(config.get("basket_width") or 3)
            except (TypeError, ValueError):
                inferred_stage_width = 3
    stage_width = inferred_stage_width if inferred_stage_width in {2, 3, 4} else 3
    candidate: dict[str, Any] = {
        "state": "not_run",
        "reason_codes": [],
        "stage": str(stage_width),
        "stage_width": stage_width,
        "selected_buckets": [],
        "legs": [],
        "all_in_yes_ask_sum": None,
        "min_ask_depth": None,
        "price_floor_passed": False,
        "depth_passed": False,
    }
    if isinstance(center_c, int):
        quote_config = BasketStrategyConfig.from_mapping(config)
        selected, issues, coverage, target = _select_dynamic_basket_quotes(
            fresh,
            center_bucket_c=center_c,
            width=stage_width,
            probabilities=probability.get("probabilities"),
            config=quote_config,
            reported_floor_c=None,
        )
        candidate["selected_buckets"] = [str(item.get("outcome") or "") for item in selected]
        candidate["coverage"] = coverage
        candidate["coverage_target"] = target
        candidate["reason_codes"] = list(issues)
        candidate["legs"] = [
            {
                "bucket": str(item.get("outcome") or ""),
                "yes_ask": float(item.get("yes_ask") or 0.0),
                "yes_ask_size": float(item.get("yes_ask_size") or 0.0),
                "observed_at": str(item.get("observed_at") or ""),
            }
            for item in selected
        ]
        if len(selected) == stage_width:
            all_in = sum(float(item.get("yes_ask") or 0.0) * (1.0 + float(config.get("fee_rate") or 0.0)) for item in selected)
            depth = min(float(item.get("yes_ask_size") or 0.0) for item in selected)
            candidate["all_in_yes_ask_sum"] = all_in
            candidate["min_ask_depth"] = depth
            candidate["price_floor_passed"] = bool(all_in < 1.0)
            candidate["depth_passed"] = bool(depth >= float(config.get("minimum_order_shares") or 1.0))
            candidate["state"] = "ready" if not issues and all_in < 1.0 and depth >= float(config.get("minimum_order_shares") or 1.0) else "blocked"
        else:
            candidate["state"] = "blocked"
            if not candidate["reason_codes"]:
                candidate["reason_codes"] = ["current_stage_selection_unavailable"]
    plan_blockers = list(direction.get("reason_codes") or []) + list(basket.get("reason_codes") or [])
    if candidate.get("state") != "ready":
        plan_blockers.append("quote_snapshot_not_fresh_for_current_stage")
    ready = bool(
        plan.get("status") == "ready"
        and basket.get("tranche_width") == stage_width
        and candidate.get("state") == "ready"
    )
    return {
        "slug": slug,
        "event_date": event_date,
        "state": "ready" if ready else "blocked",
        "reason_codes": sorted(set(str(item) for item in plan_blockers if str(item))) or ([] if ready else ["dynamic_stage_fok_quote_precheck_blocked"]),
        "regime_gate": {
            "status": str(direction.get("status") or "blocked"),
            "state": str(direction.get("direction") or "BLOCKED"),
            "center_bucket": str(direction.get("center_bucket") or ""),
            "center_bucket_c": direction.get("center_bucket_c"),
            "center_probability": direction.get("center_probability"),
            "market_anchor_c": direction.get("market_anchor_c"),
            "center_direction": str(direction.get("center_direction") or ""),
            "regime_state": str((direction.get("regime") or {}).get("state") or "UNKNOWN") if isinstance(direction.get("regime"), Mapping) else "UNKNOWN",
            "regime_gate": str(direction.get("regime_gate") or ""),
            "reason_codes": list(direction.get("reason_codes") or []),
        },
        "quote_precheck": candidate,
        "quote_snapshot": {
            "fresh_count": len(fresh),
            "registry_count": len(rows),
            "fresh_buckets": [str(item.get("bucket") or "") for item in quote_diagnostics if item.get("quote_state") == "fresh"],
            "stale_count": sum(1 for item in quote_diagnostics if item.get("quote_state") == "stale"),
            "missing_count": sum(1 for item in quote_diagnostics if item.get("quote_state") == "missing"),
            "max_quote_age_sec": max_age_sec,
            "checked_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "buckets": quote_diagnostics,
        },
        "production_plan": {
            "status": str(plan.get("status") or "blocked"),
            "plan_state": str(plan.get("plan_state") or ""),
            "basket_status": str(basket.get("status") or "blocked"),
            "basket_width": basket.get("tranche_width"),
            "current_stage_width": stage_width,
            "leg_count": len(basket.get("selected_buckets") or []),
            "convergence_sequence": list(config.get("convergence_widths") or [4, 3, 2]),
            "selected_buckets": list(basket.get("selected_buckets") or []),
            "per_share_all_in_cost": basket.get("per_share_all_in_cost"),
            "guaranteed_profit_usdc": basket.get("guaranteed_profit_usdc"),
        },
        "posterior_semantics": posterior_semantics,
        "guards": {
            "read_only": True,
            "db_writes": False,
            "orders_submitted": False,
            "order_type": "FOK",
            "current_stage_width": stage_width,
            "stage_sequence": [4, 3, 2],
            "all_legs_required": True,
            "official_quote_source": "market_ws_quote_shards",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only production-equivalent dynamic-stage YES FOK quote precheck")
    parser.add_argument("--event-date", default=datetime.now(timezone.utc).date().isoformat())
    parser.add_argument("--slug", default="", help="Exact market slug; defaults to Chengdu for the date")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--max-quote-age-sec", type=float, default=5.0)
    args = parser.parse_args()
    event_date = str(args.event_date or "").strip()[:10]
    db_path = Path(args.db).expanduser()
    with _open_read_only(db_path) as conn:
        slug = _event_slug(conn, event_date=event_date, slug=str(args.slug or "").strip())
        payload = {
            "schema": "basket_fok_quote_precheck_v1",
            "event_date": event_date,
            "slug": slug,
            "state": "blocked",
            "read_only": True,
            "checks": [],
        }
        if slug:
            payload["checks"].append(
                _precheck_market(
                    conn,
                    slug=slug,
                    event_date=event_date,
                    max_age_sec=max(0.1, float(args.max_quote_age_sec)),
                    now=_utc_now(),
                )
            )
        else:
            payload["checks"].append({"state": "blocked", "reason_codes": ["target_market_missing"]})
        payload["state"] = "ready" if payload["checks"] and all(item.get("state") == "ready" for item in payload["checks"]) else "blocked"
        payload["ok"] = True
        payload["orders_submitted"] = False
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
