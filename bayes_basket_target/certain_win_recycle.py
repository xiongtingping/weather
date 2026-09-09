"""Sell a won position at 0.998 instead of waiting for settlement.

Operator ruling 2026-08-31: 确定赢的仓位提前 0.2% 折价回笼资金再周转 -- a
position that has already won pays 1.000 at settlement, hours or days from
now; a resting GTC sell at 0.998 pays 0.2% less, now, and the freed capital
re-enters the budget the same cycle.  The 0.2% is the price of not waiting,
and it also covers the residual risk that a truth revision moves the high by
a fraction of a degree.

"Won" is measured, not judged: the station's local settlement day is closed,
and the canonical observed maximum -- the same
``reported_trajectory_canonical_station_day_bounds`` row settlement itself
follows -- landed strictly inside a bucket this book holds.  Mid-day highs do
not qualify no matter how safe they look: an afternoon record can leave the
bucket, a closed day cannot.

This is a charter exception granted by the same ruling: the basket doctrine
forbids selling the M/A/F buckets, and this lane sells whichever bucket won.
The exception is bounded by the price: only at 0.998, only after close, only
the full held size, post-only so it cannot cross the spread and leak more
than the 0.2% it is chartered to pay.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any

from .contracts import ContractError, content_sha256
from .settlement_time import settlement_day_state

RECYCLE_PRICE = 0.998
STRATEGY_ID = "bayes_basket_target_v1"
INTENT_PREFIX = "recycle998"
SCHEMA = "certain_win_recycle_plan_v1"


def _bucket_interval(registry_json: str) -> tuple[float | None, float | None]:
    try:
        raw = json.loads(registry_json or "{}")
    except ValueError:
        return None, None
    lower = raw.get("lower")
    upper = raw.get("upper")
    return (
        float(lower) if lower is not None else None,
        float(upper) if upper is not None else None,
    )


def _high_inside(lower: float | None, upper: float | None, high_c: float) -> bool:
    """Whether the observed high settles this bucket, on the registry's own
    half-open convention: a bounded bucket [lower, upper] wins when
    lower <= high < upper + 1 is not the rule -- settlement is whole-degree
    containment, so the safe reading is inclusive bounds with open tails."""

    if lower is not None and high_c < lower:
        return False
    if upper is not None and high_c > upper:
        return False
    return True


def plan_certain_win_recycles(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection,
    *,
    now_utc: str,
) -> list[dict[str, Any]]:
    """Every held bucket whose day has closed on a high inside it.

    Reads only; returns the sells the executor should place.  Idempotence is
    by deterministic intent id, checked against the intents table, so a
    replanned cycle re-proposes nothing already minted.
    """

    plans: list[dict[str, Any]] = []
    # The newest complete inventory of each station-day, in one pass.
    #
    # This ran as a self-join against a MAX() sub-select, which materialised
    # a co-routine over the whole table and then seeked back into it once per
    # group.  The table is 271,747 snapshots over 1,579 station-days -- 172
    # versions each -- and none of it is narrowed by event date, so the whole
    # cost was paid on every production cycle.  Measured 2026-09-09 against
    # the live store: 154.7s returning 1,579 rows, on the writer connection,
    # after the main cycle.  While it ran the forecast lane could not take a
    # writer turn at all: from the 12:35Z restart to 13:05Z it took none,
    # 2026-09-10's packets stayed expired past their 6h TTL, and 45 stations
    # fail-closed on native_ensemble_snapshot_stale with the decision loop
    # losing 41 scopes a cycle to latest_stage_stale_fail_closed three layers
    # downstream.
    #
    # The rewrite rests on SQLite's min/max bare-column rule: the columns
    # beside MAX() come from the row that produced it.  Verified equivalent
    # on the live store before the switch -- 1,579 scopes both ways, the same
    # scope set, every row the group-by picks is one the self-join returned,
    # and no scope had two rows tied on observed_at.
    #
    # The rewrite is not itself the speed: measured back to back the two
    # forms are 93.0s and 82.4s, and the 3.2x an earlier isolated run showed
    # was a warm cache.  What makes it fast is that this form can be served
    # by an index, and basket_confirmed_inventory_snapshot_recycle_idx exists
    # for it: with that index the plan is a SEARCH on complete=? instead of a
    # scan of every row.
    inventories = target_conn.execute(
        "SELECT station_id, event_date, inventory_json, MAX(observed_at)"
        " FROM basket_confirmed_inventory_snapshot"
        " WHERE complete=1"
        " GROUP BY station_id, event_date"
    ).fetchall()
    for station_id, event_date, inventory_json, _observed_at in inventories:
        try:
            inventory = json.loads(inventory_json or "{}")
        except ValueError:
            continue
        held = {
            str(bucket): float(shares)
            for bucket, shares in (
                inventory.get("filled_shares_by_bucket") or {}
            ).items()
            if float(shares or 0.0) > 1e-9
        }
        if not held:
            continue
        # Charter 100 (2026-09-06): the settled truth, not the trajectory
        # lane's running bound.  The bound froze on 87 station-days -- KAUS
        # 2026-08-23 held 28.0C against a day its own reports took to 40.0C
        # -- because a correction halt stopped it advancing, and a recycle
        # decided on that number would pay out the wrong bucket.  The
        # calibration lane's settled high is the number the ledger and the
        # OOS gates already settle on, and it was clean across every
        # station-day checked.  No settled truth, no recycle: capital left
        # in place costs nothing, capital recycled off a wrong bucket does.
        truth = target_conn.execute(
            "SELECT t.official_tmax_c, t.settlement_timezone,"
            " t.observation_ids_json"
            " FROM calibration_station_daily_high_truth AS t"
            " WHERE t.station_id=? AND t.local_date=? AND t.source_lane='AWC'"
            " AND t.qc_status='ACCEPTED'"
            " AND t.authority_scope IN"
            " ('FORMAL_NATIVE_ELIGIBLE','NO_LIVE_REPLAY_CANDIDATE')"
            " ORDER BY t.revision_no DESC LIMIT 1",
            (str(station_id), str(event_date)),
        ).fetchone()
        if truth is None or truth[0] is None:
            continue
        high_c = float(truth[0])
        timezone_name = str(truth[1] or "")
        if not timezone_name:
            bound = canonical_conn.execute(
                "SELECT settlement_timezone"
                " FROM reported_trajectory_canonical_station_day_bounds"
                " WHERE station_id=? AND event_date=?",
                (str(station_id), str(event_date)),
            ).fetchone()
            timezone_name = str((bound or [""])[0] or "")
        if not timezone_name:
            continue
        try:
            state = settlement_day_state(
                str(event_date),
                as_of_utc=now_utc,
                settlement_timezone=timezone_name,
            )["state"]
        except Exception:
            continue
        if state != "closed":
            continue
        rows = target_conn.execute(
            "SELECT outcome, yes_token, registry_json"
            " FROM basket_target_market_registry"
            " WHERE station_id=? AND event_date=?"
            "   AND source_eligible=1",
            (str(station_id), str(event_date)),
        ).fetchall()
        for outcome, yes_token, registry_json in rows:
            bucket = str(outcome)
            shares = held.get(bucket)
            if not shares or not yes_token:
                continue
            lower, upper = _bucket_interval(str(registry_json or ""))
            if lower is None and upper is None:
                continue
            if not _high_inside(lower, upper, high_c):
                continue
            intent_id = (
                f"{INTENT_PREFIX}_"
                + content_sha256({
                    "schema": SCHEMA,
                    "station_id": str(station_id),
                    "event_date": str(event_date),
                    "bucket": bucket,
                    "token_id": str(yes_token),
                })[:40]
            )
            already = target_conn.execute(
                "SELECT 1 FROM basket_target_order_intents"
                " WHERE order_intent_id=?",
                (intent_id,),
            ).fetchone()
            if already:
                continue
            plans.append({
                "schema": SCHEMA,
                "order_intent_id": intent_id,
                "station_id": str(station_id),
                "event_date": str(event_date),
                "bucket": bucket,
                "token_id": str(yes_token),
                "shares": round(float(shares), 6),
                "limit_price": RECYCLE_PRICE,
                "observed_max_c": high_c,
                "truth_observation_ids_json": str(truth[2] or ""),
            })
    return plans


def execute_certain_win_recycles(
    target_conn: sqlite3.Connection,
    canonical_conn: sqlite3.Connection,
    *,
    transport: Callable[[str, Mapping[str, Any]], Mapping[str, Any]],
    now_utc: str,
    live_submit_authorized: bool,
) -> dict[str, Any]:
    """Mint the intents, place the resting sells, record what came back.

    The intent row is written first, in the same table every other order
    lives in, marked live by the same column -- so the dashboard's live-order
    card and the ledger see these without knowing this lane exists.  A
    gateway refusal leaves the intent in ``submit_refused`` rather than
    deleting it: a refusal is a fact about this cycle, and the deterministic
    id means the next cycle retries by minting nothing and reading the state.
    """

    plans = plan_certain_win_recycles(
        target_conn, canonical_conn, now_utc=now_utc,
    )
    receipts: list[dict[str, Any]] = []
    for plan in plans:
        payload = {
            "strategy_id": STRATEGY_ID,
            "token_id": plan["token_id"],
            "side": "SELL",
            "order_type": "GTC",
            "price": plan["limit_price"],
            "size": plan["shares"],
            "notional": round(plan["shares"] * plan["limit_price"], 6),
            "post_only": True,
            "idempotency_key": plan["order_intent_id"],
            "order_intent_id": plan["order_intent_id"],
        }
        state = "prepared"
        response: Mapping[str, Any] = {}
        if live_submit_authorized:
            response = transport("/place-order", payload)
            state = (
                "submitted"
                if response.get("ok") or response.get("accepted")
                or response.get("pending")
                else "submit_refused"
            )
        else:
            state = "not_authorized"
        target_conn.execute(
            "INSERT INTO basket_target_order_intents ("
            " order_intent_id, decision_id, cycle_id, bucket, token_id,"
            " side, order_action, limit_price, requested_size,"
            " requested_notional_usdc, state, live_submit_authorized,"
            " idempotency_key, intent_json, intent_sha256, created_at,"
            " target_execution_hard_deadline_at"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan["order_intent_id"],
                f"recycle_{plan['station_id']}_{plan['event_date']}",
                f"recycle_cycle_{plan['event_date']}",
                plan["bucket"],
                plan["token_id"],
                "YES",
                "SELL",
                plan["limit_price"],
                plan["shares"],
                round(plan["shares"] * plan["limit_price"], 6),
                state,
                1 if live_submit_authorized else 0,
                plan["order_intent_id"],
                json.dumps({
                    **plan,
                    "gateway_state": state,
                    "gateway_error": str(response.get("error") or ""),
                }, ensure_ascii=False, sort_keys=True),
                content_sha256(plan),
                now_utc,
                None,
            ),
        )
        receipts.append({
            "order_intent_id": plan["order_intent_id"],
            "station_id": plan["station_id"],
            "bucket": plan["bucket"],
            "shares": plan["shares"],
            "state": state,
            "gateway_error": str(response.get("error") or ""),
        })
    return {
        "schema": "certain_win_recycle_receipt_v1",
        "planned": len(plans),
        "submitted": sum(1 for r in receipts if r["state"] == "submitted"),
        "receipts": receipts,
    }


__all__ = [
    "RECYCLE_PRICE",
    "plan_certain_win_recycles",
    "execute_certain_win_recycles",
]
