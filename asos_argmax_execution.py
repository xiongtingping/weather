"""Budgeted live execution adapter for canonical ASOS argmax entry facts."""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.request import Request, urlopen


STRATEGY_ID = "asos_argmax_new_high_yes_v1"
EXIT_STRATEGY_ID = "asos_argmax_yes_exit_099_v1"
ROTATION_SELL_STRATEGY_ID = "asos_argmax_rotation_sell_fak_v1"
EXIT_LIMIT_PRICE = 0.99
SHARE_TOLERANCE = 0.000001
TOTAL_BUDGET_USDC = 20.0
STATION_BUDGET_USDC = 2.0
ACTIVE_BUDGET_STATES = ("PREPARED", "SUBMITTING", "EXCHANGE_ACCEPTED", "RECONCILE_REQUIRED")
GATEWAY = "http://127.0.0.1:8765"


def _canonical(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _post_json(path: str, payload: Mapping[str, Any], *, timeout: float = 4.0) -> dict[str, Any]:
    request = Request(
        GATEWAY + path, data=_canonical(payload), method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        decoded = json.loads(response.read(2 * 1024 * 1024))
    if not isinstance(decoded, dict):
        raise RuntimeError("gateway_response_not_object")
    return decoded


def _official_ws_quote(token_id: str, *, root: Path) -> tuple[str, Mapping[str, Any]]:
    shard = root / "data" / "market_ws_quote_shards" / f"{hashlib.sha256(token_id.encode()).hexdigest()}.json"
    payload = json.loads(shard.read_text(encoding="utf-8"))
    supplied = str(payload.get("snapshot_sha256") or "")
    unsigned = dict(payload)
    unsigned.pop("snapshot_sha256", None)
    if (
        payload.get("schema") != "clob_ws_token_quote_snapshot_v1"
        or payload.get("source") != "official_polymarket_clob_websocket"
        or payload.get("token_id") != token_id
        or payload.get("evidence_only") is not True
        or payload.get("authorizes_order") is not False
        or supplied != _sha(unsigned)
    ):
        raise ValueError("official_ws_token_shard_invalid")
    quote = payload.get("quote") or {}
    observed = datetime.fromisoformat(str(quote.get("book_observed_at") or "").replace("Z", "+00:00"))
    age = (datetime.now(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
    if age < 0 or age > 120:
        raise ValueError("official_ws_token_shard_stale")
    return supplied, quote


def _official_ws_buy_sweep(
    token_id: str, *, budget: float, root: Path,
) -> dict[str, Any]:
    supplied, quote = _official_ws_quote(token_id, root=root)
    asks = sorted(
        (
            {"price": float(row["price"]), "size": float(row["size"])}
            for row in quote.get("asks") or []
            if float(row.get("price") or 0) > 0 and float(row.get("size") or 0) > 0
        ), key=lambda row: row["price"],
    )
    if not math.isfinite(budget) or budget < 0.01:
        raise ValueError("market_buy_budget_invalid")
    if not asks or asks[0]["price"] >= 1:
        raise ValueError("official_ws_executable_ask_missing")
    cumulative = 0.0
    best: dict[str, float] | None = None
    for level in asks:
        if level["price"] >= 1:
            break
        cumulative += level["size"]
        max_notional = min(budget, level["price"] * cumulative)
        notional = math.floor(max_notional * 100 + 1e-9) / 100
        size = notional / level["price"] if notional >= 0.01 else 0.0
        if size <= cumulative + SHARE_TOLERANCE and (
            best is None or size > best["size"] + SHARE_TOLERANCE
        ):
            best = {
                "limit_price": level["price"], "size": size,
                "notional": notional,
            }
    if best is None or best["size"] <= SHARE_TOLERANCE:
        raise ValueError("market_buy_ws_ask_depth_insufficient")
    remaining = best["size"]
    consumed: list[dict[str, float]] = []
    for level in asks:
        if level["price"] > best["limit_price"] + 1e-12:
            break
        take = min(remaining, level["size"])
        if take > 0:
            consumed.append({"price": level["price"], "size": take})
            remaining -= take
        if remaining <= SHARE_TOLERANCE:
            break
    if remaining > SHARE_TOLERANCE or not consumed:
        raise ValueError("market_buy_ws_ask_depth_insufficient")
    return {
        "snapshot_sha256": supplied,
        **best,
        "consumed_asks": consumed,
        "book_observed_at": quote["book_observed_at"],
    }


def _official_ws_sell_sweep(
    token_id: str, *, size: float, root: Path
) -> dict[str, Any]:
    supplied, quote = _official_ws_quote(token_id, root=root)
    bids = sorted(
        (
            {"price": float(row["price"]), "size": float(row["size"])}
            for row in quote.get("bids") or []
            if 0 < float(row.get("price") or 0) < 1
            and float(row.get("size") or 0) > 0
        ),
        key=lambda row: row["price"],
        reverse=True,
    )
    if not math.isfinite(size) or size <= 0:
        raise ValueError("rotation_sell_size_invalid")
    remaining = size
    consumed: list[dict[str, float]] = []
    for level in bids:
        take = min(remaining, level["size"])
        if take > 0:
            consumed.append({"price": level["price"], "size": take})
            remaining -= take
        if remaining <= SHARE_TOLERANCE:
            break
    if remaining > SHARE_TOLERANCE or not consumed:
        raise ValueError("rotation_sell_ws_bid_depth_insufficient")
    return {
        "snapshot_sha256": supplied,
        "limit_price": consumed[-1]["price"],
        "size": size,
        "consumed_bids": consumed,
        "book_observed_at": quote["book_observed_at"],
    }


def _budget_used(conn: sqlite3.Connection, station_id: str | None = None) -> float:
    placeholders = ",".join("?" for _ in ACTIVE_BUDGET_STATES)
    sql = (
        "SELECT COALESCE(SUM(i.requested_notional),0) "
        "FROM reported_trajectory_asos_argmax_order_intents i "
        f"WHERE i.state IN ({placeholders}) "
        "AND NOT EXISTS ("
        "SELECT 1 FROM reported_trajectory_asos_argmax_rotation_sell_intents r "
        "WHERE r.source_buy_order_intent_id=i.order_intent_id "
        "AND r.state IN ('FILLED','NO_POSITION'))"
    )
    params: list[Any] = list(ACTIVE_BUDGET_STATES)
    if station_id is not None:
        sql += " AND i.station_id=?"
        params.append(station_id)
    return float(conn.execute(sql, params).fetchone()[0] or 0.0)


def _reconcile_pending_argmax_orders(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Resolve previously unknown submissions without ever resubmitting them."""

    rows = conn.execute(
        """SELECT order_intent_id,entry_fact_id,idempotency_key,size
             FROM reported_trajectory_asos_argmax_order_intents
            WHERE state='RECONCILE_REQUIRED'
         ORDER BY updated_at_utc,order_intent_id"""
    ).fetchall()
    results: list[dict[str, Any]] = []
    for intent_id, entry_fact_id, idempotency_key, requested_size in rows:
        try:
            response = _post_json(
                "/reconcile-order", {"idempotency_key": str(idempotency_key)}, timeout=8.0,
            )
            # Release budget only on an explicit terminal exchange receipt.
            # Missing gateway memory, lookup failure, open/pending orders and
            # ambiguous responses remain reconcile-required.
            status = str(response.get("status") or "").lower()
            try:
                matched = float(response.get("size_matched"))
            except (TypeError, ValueError):
                matched = None
            if (
                response.get("ok") is True
                and response.get("reconcile_required") is not True
                and response.get("orderID")
                and status in {"matched", "filled", "executed"}
                and matched is not None
                and math.isclose(
                    matched, float(requested_size), rel_tol=0,
                    abs_tol=SHARE_TOLERANCE,
                )
            ):
                state = "EXCHANGE_ACCEPTED"
            elif (
                response.get("reconcile_attempted") is True
                and response.get("reconcile_required") is not True
                and response.get("accepted") is False
                and response.get("pending") is False
                and response.get("error") == "exchange_order_not_filled"
            ):
                state = "RELEASED"
            elif (
                # The gateway remembers this key and states that it refused the
                # order before any exchange call: nothing can be resting, so the
                # budget is free.  Geoblocking (HTTP 403) arrives this way, and
                # without this branch three stations' entire allowance is held
                # by orders that provably never left the machine.
                #
                # This reads only the gateway's own assertions.  `found` must be
                # true -- an unknown key is ambiguous, because the gateway may
                # have lost a record of an order that did go out, and that case
                # must keep its budget reserved.
                response.get("found") is True
                and response.get("pending") is not True
                and response.get("exchange_post_started") is False
                and response.get("fatal") is True
                and response.get("retryable") is not True
                and not response.get("orderID")
            ):
                state = "RELEASED"
            else:
                state = "RECONCILE_REQUIRED"
            conn.execute(
                """UPDATE reported_trajectory_asos_argmax_order_intents
                      SET state=?,gateway_response_json=?,exchange_order_id=COALESCE(?,exchange_order_id),
                          size_matched=COALESCE(?,size_matched),updated_at_utc=?
                    WHERE order_intent_id=? AND state='RECONCILE_REQUIRED'""",
                (
                    state, json.dumps(response, sort_keys=True, separators=(",", ":")),
                    response.get("orderID"), response.get("size_matched"), _iso_now(), intent_id,
                ),
            )
            conn.commit()
            results.append({
                "entry_fact_id": entry_fact_id, "order_intent_id": intent_id,
                "state": state, "response": response,
            })
        except Exception as exc:
            # Unknown remains reserved and is retried on the next bounded loop.
            results.append({
                "entry_fact_id": entry_fact_id, "order_intent_id": intent_id,
                "state": "RECONCILE_REQUIRED", "reason": f"{type(exc).__name__}:{exc}",
            })
    return results


def _conditional_balance_shares(guard: Mapping[str, Any]) -> float | None:
    evidence = guard.get("conditional_balance_allowance")
    if not isinstance(evidence, Mapping):
        return None
    try:
        balance = int(str(evidence.get("balance")))
    except (TypeError, ValueError):
        return None
    return balance / 1_000_000.0 if balance >= 0 else None


def _reconcile_pending_argmax_exits(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT exit_order_intent_id,idempotency_key
             FROM reported_trajectory_asos_argmax_exit_order_intents
            WHERE state='RECONCILE_REQUIRED'
         ORDER BY updated_at_utc,exit_order_intent_id"""
    ).fetchall()
    results: list[dict[str, Any]] = []
    for exit_intent_id, idempotency_key in rows:
        try:
            response = _post_json(
                "/reconcile-order", {"idempotency_key": str(idempotency_key)},
                timeout=8.0,
            )
            if response.get("ok") is True and response.get("orderID"):
                state = "EXCHANGE_ACCEPTED"
            elif (
                response.get("reconcile_attempted") is True
                and response.get("reconcile_required") is not True
                and response.get("accepted") is False
                and response.get("pending") is False
                and response.get("error") == "exchange_order_not_filled"
            ):
                state = "RELEASED"
            else:
                state = "RECONCILE_REQUIRED"
            conn.execute(
                """UPDATE reported_trajectory_asos_argmax_exit_order_intents
                      SET state=?,gateway_response_json=?,
                          exchange_order_id=COALESCE(?,exchange_order_id),
                          size_matched=COALESCE(?,size_matched),updated_at_utc=?
                    WHERE exit_order_intent_id=? AND state='RECONCILE_REQUIRED'""",
                (
                    state, json.dumps(response, sort_keys=True, separators=(",", ":")),
                    response.get("orderID"), response.get("size_matched"),
                    _iso_now(), exit_intent_id,
                ),
            )
            conn.commit()
            results.append({
                "exit_order_intent_id": exit_intent_id,
                "state": state,
                "response": response,
            })
        except Exception as exc:
            results.append({
                "exit_order_intent_id": exit_intent_id,
                "state": "RECONCILE_REQUIRED",
                "reason": f"{type(exc).__name__}:{exc}",
            })
    return results


def _materialize_filled_buy_exits(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """SELECT i.order_intent_id,i.entry_fact_id,i.station_id,i.event_date,
                  i.yes_token,i.size_matched
             FROM reported_trajectory_asos_argmax_order_intents i
        LEFT JOIN reported_trajectory_asos_argmax_exit_order_intents x
               ON x.buy_order_intent_id=i.order_intent_id
        LEFT JOIN reported_trajectory_asos_argmax_rotation_sell_intents r
               ON r.source_buy_order_intent_id=i.order_intent_id
              AND r.state IN ('FILLED','NO_POSITION')
            WHERE i.state='EXCHANGE_ACCEPTED' AND i.size_matched>0
              AND x.exit_order_intent_id IS NULL
              AND r.rotation_sell_intent_id IS NULL
         ORDER BY i.updated_at_utc,i.order_intent_id"""
    ).fetchall()
    inserted = 0
    for buy_intent_id, entry_fact_id, station, event_date, token, matched in rows:
        shares = float(matched)
        if not math.isfinite(shares) or shares <= 0:
            continue
        identity = {
            "strategy_id": EXIT_STRATEGY_ID,
            "buy_order_intent_id": str(buy_intent_id),
            "entry_fact_id": str(entry_fact_id),
            "yes_token": str(token),
            "side": "SELL",
            "order_type": "GTC",
            "limit_price": EXIT_LIMIT_PRICE,
            "size": shares,
        }
        exit_intent_id = "asosexit_" + _sha(identity)[:32]
        idempotency_key = "asosexitidem_" + _sha({"exit": exit_intent_id})[:40]
        now = _iso_now()
        cursor = conn.execute(
            """INSERT OR IGNORE INTO reported_trajectory_asos_argmax_exit_order_intents(
                 exit_order_intent_id,buy_order_intent_id,entry_fact_id,strategy_id,
                 station_id,event_date,yes_token,side,order_type,limit_price,size,
                 source_buy_size_matched,idempotency_key,state,created_at_utc,updated_at_utc
               ) VALUES(?,?,?, ?,?,?,?,'SELL','GTC',0.99,?,?,?,'PREPARED',?,?)""",
            (
                exit_intent_id, buy_intent_id, entry_fact_id, EXIT_STRATEGY_ID,
                station, event_date, token, shares, shares, idempotency_key, now, now,
            ),
        )
        inserted += int(cursor.rowcount or 0)
    conn.commit()
    return inserted


def _process_prepared_argmax_exits(conn: sqlite3.Connection) -> dict[str, Any]:
    reconcile_results = _reconcile_pending_argmax_exits(conn)
    materialized = _materialize_filled_buy_exits(conn)
    rows = conn.execute(
        """SELECT exit_order_intent_id,buy_order_intent_id,entry_fact_id,
                  station_id,event_date,yes_token,size,idempotency_key
             FROM reported_trajectory_asos_argmax_exit_order_intents
            WHERE state='PREPARED'
         ORDER BY created_at_utc,exit_order_intent_id"""
    ).fetchall()
    results: list[dict[str, Any]] = []
    gateway_calls = len(reconcile_results)
    submissions = 0
    for exit_id, buy_id, entry_id, station, event_date, token, raw_size, idem in rows:
        size = float(raw_size)
        guard_payload = {
            "token_id": str(token), "side": "SELL",
            "price": EXIT_LIMIT_PRICE, "size": size,
            "notional": size * EXIT_LIMIT_PRICE,
        }
        submitted = False
        try:
            gateway_calls += 1
            guard = _post_json("/execution-guard-snapshot", guard_payload)
            balance = _conditional_balance_shares(guard)
            if not (
                guard.get("ok") is True
                and guard.get("side") == "SELL"
                and guard.get("evidence_only") is True
                and guard.get("authorizes_order") is False
                and not guard.get("evidence_errors")
                and balance is not None
                and balance + SHARE_TOLERANCE >= size
            ):
                results.append({
                    "exit_order_intent_id": exit_id,
                    "state": "PREPARED",
                    "reason": "ASOS_EXIT_CONDITIONAL_BALANCE_GUARD_BLOCKED",
                })
                continue
            guard_sha = _sha(guard)
            request_payload = {
                "strategy_id": EXIT_STRATEGY_ID,
                "buy_order_intent_id": str(buy_id),
                "entry_fact_id": str(entry_id),
                "order_intent_id": str(exit_id),
                "idempotency_key": str(idem),
                "token_id": str(token),
                "side": "SELL", "order_type": "GTC",
                "price": EXIT_LIMIT_PRICE, "limit_price": EXIT_LIMIT_PRICE,
                "size": size, "notional": 0,
                "allow_market": False, "post_only": False,
                "guard_snapshot_sha256": guard_sha,
            }
            now = _iso_now()
            conn.execute("BEGIN IMMEDIATE")
            parent = conn.execute(
                """SELECT state,size_matched,yes_token
                     FROM reported_trajectory_asos_argmax_order_intents
                    WHERE order_intent_id=?""",
                (buy_id,),
            ).fetchone()
            if not parent or parent[0] != "EXCHANGE_ACCEPTED" or not math.isclose(
                float(parent[1] or 0), size, rel_tol=0, abs_tol=SHARE_TOLERANCE
            ) or str(parent[2]) != str(token):
                conn.rollback()
                results.append({
                    "exit_order_intent_id": exit_id,
                    "state": "PREPARED",
                    "reason": "ASOS_EXIT_PARENT_FILL_CHANGED",
                })
                continue
            conn.execute(
                """UPDATE reported_trajectory_asos_argmax_exit_order_intents
                      SET guard_snapshot_sha256=?,guard_snapshot_json=?,state='SUBMITTING',
                          gateway_request_json=?,updated_at_utc=?
                    WHERE exit_order_intent_id=? AND state='PREPARED'""",
                (
                    guard_sha, json.dumps(guard, sort_keys=True, separators=(",", ":")),
                    json.dumps(request_payload, sort_keys=True, separators=(",", ":")),
                    now, exit_id,
                ),
            )
            conn.commit()
            submitted = True
            gateway_calls += 1
            submissions += 1
            response = _post_json("/place-order", request_payload, timeout=8.0)
            if response.get("ok") is True and response.get("orderID"):
                state = "EXCHANGE_ACCEPTED"
            elif response.get("reconcile_required") or response.get("exchange_post_started"):
                state = "RECONCILE_REQUIRED"
            else:
                state = "RELEASED"
            conn.execute(
                """UPDATE reported_trajectory_asos_argmax_exit_order_intents
                      SET state=?,gateway_response_json=?,exchange_order_id=?,
                          size_matched=?,updated_at_utc=?
                    WHERE exit_order_intent_id=? AND state='SUBMITTING'""",
                (
                    state, json.dumps(response, sort_keys=True, separators=(",", ":")),
                    response.get("orderID"), response.get("size_matched"),
                    _iso_now(), exit_id,
                ),
            )
            conn.commit()
            results.append({
                "exit_order_intent_id": exit_id,
                "state": state,
                "response": response,
            })
        except Exception as exc:
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            if submitted:
                conn.execute(
                    """UPDATE reported_trajectory_asos_argmax_exit_order_intents
                          SET state='RECONCILE_REQUIRED',updated_at_utc=?
                        WHERE exit_order_intent_id=? AND state='SUBMITTING'""",
                    (_iso_now(), exit_id),
                )
                conn.commit()
            results.append({
                "exit_order_intent_id": exit_id,
                "state": "RECONCILE_REQUIRED" if submitted else "PREPARED",
                "reason": f"{type(exc).__name__}:{exc}",
            })
    return {
        "state": "processed",
        "materialized_exit_intent_count": materialized,
        "prepared_exit_count": len(rows),
        "gateway_call_count": gateway_calls,
        "submission_attempt_count": submissions,
        "reconcile_results": reconcile_results,
        "results": results,
    }


def _sell_shares_matched(response: Mapping[str, Any]) -> float | None:
    for value in (response.get("shares_matched"),):
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(parsed) and parsed >= 0:
            return parsed
    exchange = response.get("exchange_response")
    if isinstance(exchange, Mapping):
        for key in ("makingAmount", "making_amount"):
            try:
                parsed = float(exchange.get(key))
            except (TypeError, ValueError):
                continue
            if math.isfinite(parsed) and parsed >= 0:
                return parsed
    return None


def _cancel_exit_for_rotation(
    conn: sqlite3.Connection, *, buy_order_intent_id: str
) -> dict[str, Any]:
    row = conn.execute(
        """SELECT exit_order_intent_id,state,exchange_order_id
             FROM reported_trajectory_asos_argmax_exit_order_intents
            WHERE buy_order_intent_id=?""",
        (buy_order_intent_id,),
    ).fetchone()
    if row is None:
        return {"ok": True, "state": "NO_EXIT_ORDER", "gateway_call_count": 0}
    exit_id, state, exchange_order_id = map(lambda value: str(value or ""), row)
    if state == "PREPARED":
        receipt = {"ok": True, "state": "PREPARED_RELEASED_BEFORE_SUBMIT"}
        conn.execute(
            """UPDATE reported_trajectory_asos_argmax_exit_order_intents
                  SET state='RELEASED',gateway_response_json=?,updated_at_utc=?
                WHERE exit_order_intent_id=? AND state='PREPARED'""",
            (json.dumps(receipt, sort_keys=True, separators=(",", ":")), _iso_now(), exit_id),
        )
        conn.commit()
        return {
            "ok": True, "state": "PREPARED_RELEASED_BEFORE_SUBMIT",
            "exit_order_intent_id": exit_id, "gateway_call_count": 0,
            "receipt": receipt,
        }
    if state == "RELEASED":
        return {
            "ok": True, "state": "ALREADY_RELEASED",
            "exit_order_intent_id": exit_id, "gateway_call_count": 0,
        }
    if state != "EXCHANGE_ACCEPTED" or not exchange_order_id:
        return {
            "ok": False, "state": "RECONCILE_REQUIRED",
            "reason": "ASOS_ROTATION_EXIT_ORDER_NOT_CANCELLABLE",
            "exit_order_intent_id": exit_id, "gateway_call_count": 0,
        }
    lookup_before = _post_json(
        "/lookup-exchange-order", {"order_id": exchange_order_id}, timeout=8.0,
    )
    status_before = str(lookup_before.get("status") or "").lower()
    gateway_calls = 1
    terminal = {"filled", "matched", "executed", "cancelled", "canceled", "expired"}
    if lookup_before.get("ok") is True and status_before in terminal:
        receipt = {"lookup_before": lookup_before, "cancel_not_required": True}
    else:
        cancel_key = "asoscancel_" + _sha({"exit": exit_id, "order": exchange_order_id})[:40]
        cancel = _post_json(
            "/cancel-order",
            {"order_id": exchange_order_id, "idempotency_key": cancel_key},
            timeout=8.0,
        )
        gateway_calls += 1
        if cancel.get("ok") is not True or cancel.get("cancelled") is not True:
            conn.execute(
                """UPDATE reported_trajectory_asos_argmax_exit_order_intents
                      SET state='RECONCILE_REQUIRED',gateway_response_json=?,updated_at_utc=?
                    WHERE exit_order_intent_id=? AND state='EXCHANGE_ACCEPTED'""",
                (
                    json.dumps(
                        {"lookup_before": lookup_before, "cancel": cancel},
                        sort_keys=True, separators=(",", ":"),
                    ),
                    _iso_now(), exit_id,
                ),
            )
            conn.commit()
            return {
                "ok": False, "state": "RECONCILE_REQUIRED",
                "reason": "ASOS_ROTATION_EXIT_CANCEL_UNKNOWN",
                "exit_order_intent_id": exit_id,
                "gateway_call_count": gateway_calls,
                "receipt": {"lookup_before": lookup_before, "cancel": cancel},
            }
        lookup_after = _post_json(
            "/lookup-exchange-order", {"order_id": exchange_order_id}, timeout=8.0,
        )
        gateway_calls += 1
        status_after = str(lookup_after.get("status") or "").lower()
        if lookup_after.get("ok") is not True or status_after not in terminal:
            unknown_receipt = {
                "lookup_before": lookup_before, "cancel": cancel,
                "lookup_after": lookup_after,
            }
            conn.execute(
                """UPDATE reported_trajectory_asos_argmax_exit_order_intents
                      SET state='RECONCILE_REQUIRED',gateway_response_json=?,updated_at_utc=?
                    WHERE exit_order_intent_id=? AND state='EXCHANGE_ACCEPTED'""",
                (
                    json.dumps(unknown_receipt, sort_keys=True, separators=(",", ":")),
                    _iso_now(), exit_id,
                ),
            )
            conn.commit()
            return {
                "ok": False, "state": "RECONCILE_REQUIRED",
                "reason": "ASOS_ROTATION_EXIT_CANCEL_NOT_CONFIRMED",
                "exit_order_intent_id": exit_id,
                "gateway_call_count": gateway_calls,
                "receipt": unknown_receipt,
            }
        receipt = {
            "lookup_before": lookup_before, "cancel": cancel,
            "lookup_after": lookup_after,
        }
    conn.execute(
        """UPDATE reported_trajectory_asos_argmax_exit_order_intents
              SET state='RELEASED',gateway_response_json=?,updated_at_utc=?
            WHERE exit_order_intent_id=? AND state='EXCHANGE_ACCEPTED'""",
        (json.dumps(receipt, sort_keys=True, separators=(",", ":")), _iso_now(), exit_id),
    )
    conn.commit()
    return {
        "ok": True, "state": "EXIT_RELEASED_FOR_ROTATION",
        "exit_order_intent_id": exit_id, "gateway_call_count": gateway_calls,
        "receipt": receipt,
    }


def _process_rotation_leg(
    conn: sqlite3.Connection,
    *,
    target_entry_fact_id: str,
    source_buy_order_intent_id: str,
    station_id: str,
    event_date: str,
    old_yes_token: str,
    new_yes_token: str,
    source_buy_size_matched: float,
    root: Path,
) -> dict[str, Any]:
    existing = conn.execute(
        """SELECT rotation_sell_intent_id,state
             FROM reported_trajectory_asos_argmax_rotation_sell_intents
            WHERE target_entry_fact_id=? AND source_buy_order_intent_id=?""",
        (target_entry_fact_id, source_buy_order_intent_id),
    ).fetchone()
    if existing is not None and str(existing[1]) in {"FILLED", "NO_POSITION"}:
        return {
            "ok": True, "state": str(existing[1]),
            "rotation_sell_intent_id": str(existing[0]), "gateway_call_count": 0,
            "submission_attempt_count": 0,
        }
    if existing is not None:
        return {
            "ok": False, "state": str(existing[1]),
            "reason": "ASOS_ROTATION_EXISTING_LEG_NOT_TERMINAL",
            "rotation_sell_intent_id": str(existing[0]), "gateway_call_count": 0,
            "submission_attempt_count": 0,
        }

    cancellation = _cancel_exit_for_rotation(
        conn, buy_order_intent_id=source_buy_order_intent_id,
    )
    gateway_calls = int(cancellation.get("gateway_call_count") or 0)
    if cancellation.get("ok") is not True:
        return {
            **cancellation,
            "submission_attempt_count": 0,
        }
    exit_row = conn.execute(
        """SELECT exit_order_intent_id
             FROM reported_trajectory_asos_argmax_exit_order_intents
            WHERE buy_order_intent_id=?""",
        (source_buy_order_intent_id,),
    ).fetchone()
    source_exit_id = str(exit_row[0]) if exit_row else None
    rotation_identity = {
        "target_entry_fact_id": target_entry_fact_id,
        "source_buy_order_intent_id": source_buy_order_intent_id,
        "old_yes_token": old_yes_token,
        "new_yes_token": new_yes_token,
    }
    rotation_id = "asosrotate_" + _sha(rotation_identity)[:32]
    idempotency = "asosrotateidem_" + _sha({"rotation": rotation_id})[:40]

    def persist_reconcile_required(
        *, reason: str, guard: Mapping[str, Any],
        ws: Mapping[str, Any] | None = None, size: float | None = None,
    ) -> None:
        now = _iso_now()
        ws_value = dict(ws or {})
        guard_value = dict(guard)
        conn.execute(
            """INSERT INTO reported_trajectory_asos_argmax_rotation_sell_intents(
                 rotation_sell_intent_id,target_entry_fact_id,source_buy_order_intent_id,
                 source_exit_order_intent_id,strategy_id,station_id,event_date,
                 old_yes_token,new_yes_token,side,order_type,limit_price,size,
                 ws_snapshot_sha256,guard_snapshot_sha256,state,idempotency_key,
                 cancellation_receipt_json,ws_snapshot_json,guard_snapshot_json,
                 gateway_response_json,created_at_utc,updated_at_utc
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                rotation_id, target_entry_fact_id, source_buy_order_intent_id,
                source_exit_id, ROTATION_SELL_STRATEGY_ID, station_id, event_date,
                old_yes_token, new_yes_token, "SELL", "FAK",
                ws_value.get("limit_price"), size,
                ws_value.get("snapshot_sha256"), _sha(guard_value) if guard_value else None,
                "RECONCILE_REQUIRED", idempotency,
                json.dumps(cancellation, sort_keys=True, separators=(",", ":")),
                json.dumps(ws_value, sort_keys=True, separators=(",", ":")),
                json.dumps(guard_value, sort_keys=True, separators=(",", ":")),
                json.dumps({"reason": reason}, sort_keys=True, separators=(",", ":")),
                now, now,
            ),
        )
        conn.commit()

    probe_payload = {
        "token_id": old_yes_token, "side": "SELL", "price": 0.01,
        "size": source_buy_size_matched,
        "notional": source_buy_size_matched * 0.01,
    }
    gateway_calls += 1
    balance_probe = _post_json("/execution-guard-snapshot", probe_payload)
    balance = _conditional_balance_shares(balance_probe)
    if not (
        balance_probe.get("ok") is True
        and balance_probe.get("side") == "SELL"
        and balance_probe.get("evidence_only") is True
        and balance_probe.get("authorizes_order") is False
        and not balance_probe.get("evidence_errors")
        and balance is not None
    ):
        persist_reconcile_required(
            reason="ASOS_ROTATION_BALANCE_PROBE_BLOCKED", guard=balance_probe,
        )
        return {
            "ok": False, "state": "RECONCILE_REQUIRED",
            "reason": "ASOS_ROTATION_BALANCE_PROBE_BLOCKED",
            "gateway_call_count": gateway_calls, "submission_attempt_count": 0,
        }
    sell_size = min(float(balance), float(source_buy_size_matched))
    now = _iso_now()
    if sell_size <= SHARE_TOLERANCE:
        conn.execute(
            """INSERT INTO reported_trajectory_asos_argmax_rotation_sell_intents(
                 rotation_sell_intent_id,target_entry_fact_id,source_buy_order_intent_id,
                 source_exit_order_intent_id,strategy_id,station_id,event_date,
                 old_yes_token,new_yes_token,side,order_type,state,idempotency_key,
                 cancellation_receipt_json,guard_snapshot_json,created_at_utc,updated_at_utc
               ) VALUES(?,?,?,?,?,?,?, ?,?,'SELL','FAK','NO_POSITION',?,?,?, ?,?)""",
            (
                rotation_id, target_entry_fact_id, source_buy_order_intent_id,
                source_exit_id, ROTATION_SELL_STRATEGY_ID, station_id, event_date,
                old_yes_token, new_yes_token, idempotency,
                json.dumps(cancellation, sort_keys=True, separators=(",", ":")),
                json.dumps(balance_probe, sort_keys=True, separators=(",", ":")),
                now, now,
            ),
        )
        conn.commit()
        return {
            "ok": True, "state": "NO_POSITION",
            "rotation_sell_intent_id": rotation_id,
            "gateway_call_count": gateway_calls, "submission_attempt_count": 0,
        }

    try:
        ws = _official_ws_sell_sweep(old_yes_token, size=sell_size, root=root)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        return {
            "ok": False, "state": "PREPARED",
            "reason": f"ASOS_ROTATION_WS_SELL_DEPTH_BLOCKED:{type(exc).__name__}:{exc}",
            "gateway_call_count": gateway_calls, "submission_attempt_count": 0,
        }
    guard_payload = {
        "token_id": old_yes_token, "side": "SELL",
        "price": ws["limit_price"], "size": sell_size,
        "notional": sell_size * float(ws["limit_price"]),
    }
    gateway_calls += 1
    guard = _post_json("/execution-guard-snapshot", guard_payload)
    guarded_balance = _conditional_balance_shares(guard)
    if not (
        guard.get("ok") is True
        and guard.get("side") == "SELL"
        and guard.get("evidence_only") is True
        and guard.get("authorizes_order") is False
        and not guard.get("evidence_errors")
        and guarded_balance is not None
        and guarded_balance + SHARE_TOLERANCE >= sell_size
    ):
        persist_reconcile_required(
            reason="ASOS_ROTATION_EXACT_SELL_GUARD_BLOCKED",
            guard=guard, ws=ws, size=sell_size,
        )
        return {
            "ok": False, "state": "RECONCILE_REQUIRED",
            "reason": "ASOS_ROTATION_EXACT_SELL_GUARD_BLOCKED",
            "gateway_call_count": gateway_calls, "submission_attempt_count": 0,
        }
    guard_sha = _sha(guard)
    request_payload = {
        "strategy_id": ROTATION_SELL_STRATEGY_ID,
        "target_entry_fact_id": target_entry_fact_id,
        "source_buy_order_intent_id": source_buy_order_intent_id,
        "order_intent_id": rotation_id,
        "idempotency_key": idempotency,
        "token_id": old_yes_token,
        "new_yes_token": new_yes_token,
        "side": "SELL", "order_type": "FAK",
        "price": ws["limit_price"], "limit_price": ws["limit_price"],
        "size": sell_size, "notional": 0,
        "allow_market": True, "post_only": False,
        "ws_snapshot_sha256": ws["snapshot_sha256"],
        "guard_snapshot_sha256": guard_sha,
    }
    conn.execute(
        """INSERT INTO reported_trajectory_asos_argmax_rotation_sell_intents(
             rotation_sell_intent_id,target_entry_fact_id,source_buy_order_intent_id,
             source_exit_order_intent_id,strategy_id,station_id,event_date,
             old_yes_token,new_yes_token,side,order_type,limit_price,size,
             ws_snapshot_sha256,guard_snapshot_sha256,state,idempotency_key,
             cancellation_receipt_json,ws_snapshot_json,guard_snapshot_json,
             gateway_request_json,created_at_utc,updated_at_utc
           ) VALUES(?,?,?,?,?,?,?, ?,?,'SELL','FAK',?,?,?,?, 'SUBMITTING',?,?,?,?,?,?,?)""",
        (
            rotation_id, target_entry_fact_id, source_buy_order_intent_id,
            source_exit_id, ROTATION_SELL_STRATEGY_ID, station_id, event_date,
            old_yes_token, new_yes_token, ws["limit_price"], sell_size,
            ws["snapshot_sha256"], guard_sha, idempotency,
            json.dumps(cancellation, sort_keys=True, separators=(",", ":")),
            json.dumps(ws, sort_keys=True, separators=(",", ":")),
            json.dumps(guard, sort_keys=True, separators=(",", ":")),
            json.dumps(request_payload, sort_keys=True, separators=(",", ":")),
            now, now,
        ),
    )
    conn.commit()
    gateway_calls += 1
    response = _post_json("/place-order", request_payload, timeout=8.0)
    shares_matched = _sell_shares_matched(response)
    status = str(response.get("status") or "").lower()
    full_fill = (
        response.get("ok") is True
        and response.get("orderID")
        and status in {"matched", "filled", "executed"}
        and shares_matched is not None
        and math.isclose(shares_matched, sell_size, rel_tol=0, abs_tol=SHARE_TOLERANCE)
    )
    if full_fill:
        state = "FILLED"
    elif (
        response.get("reconcile_required")
        or response.get("exchange_post_started")
        or response.get("ok") is True
        or response.get("orderID")
        or status in {"matched", "filled", "executed", "partial", "partially_filled"}
        or (shares_matched is not None and shares_matched > SHARE_TOLERANCE)
    ):
        state = "RECONCILE_REQUIRED"
    else:
        state = "RELEASED"
    conn.execute(
        """UPDATE reported_trajectory_asos_argmax_rotation_sell_intents
              SET state=?,gateway_response_json=?,exchange_order_id=?,shares_matched=?,
                  updated_at_utc=?
            WHERE rotation_sell_intent_id=? AND state='SUBMITTING'""",
        (
            state, json.dumps(response, sort_keys=True, separators=(",", ":")),
            response.get("orderID"), shares_matched, _iso_now(), rotation_id,
        ),
    )
    conn.commit()
    return {
        "ok": full_fill, "state": state,
        "reason": None if full_fill else "ASOS_ROTATION_SELL_NOT_FULLY_CONFIRMED",
        "rotation_sell_intent_id": rotation_id,
        "gateway_call_count": gateway_calls, "submission_attempt_count": 1,
        "response": response,
    }


def _ensure_old_buckets_sold_before_new_buy(
    conn: sqlite3.Connection,
    *,
    entry_fact_id: str,
    station_id: str,
    event_date: str,
    new_yes_token: str,
    root: Path,
) -> dict[str, Any]:
    same_bucket = conn.execute(
        """SELECT 1 FROM reported_trajectory_asos_argmax_order_intents
            WHERE station_id=? AND event_date=? AND yes_token=?
              AND state='EXCHANGE_ACCEPTED' AND size_matched>0 LIMIT 1""",
        (station_id, event_date, new_yes_token),
    ).fetchone()
    if same_bucket is not None:
        return {
            "ok": False, "state": "SAME_BUCKET_ALREADY_BOUGHT",
            "skip_new_buy": True, "gateway_call_count": 0,
            "submission_attempt_count": 0, "legs": [],
        }
    unresolved = conn.execute(
        """SELECT order_intent_id,state FROM reported_trajectory_asos_argmax_order_intents
            WHERE station_id=? AND event_date=? AND yes_token<>?
              AND state IN ('PREPARED','SUBMITTING','RECONCILE_REQUIRED')
            ORDER BY order_intent_id LIMIT 1""",
        (station_id, event_date, new_yes_token),
    ).fetchone()
    if unresolved is not None:
        return {
            "ok": False, "state": "OLD_BUY_RECONCILE_REQUIRED",
            "reason": f"{unresolved[0]}:{unresolved[1]}",
            "skip_new_buy": False, "gateway_call_count": 0,
            "submission_attempt_count": 0, "legs": [],
        }
    old_buys = conn.execute(
        """SELECT order_intent_id,yes_token,size_matched
             FROM reported_trajectory_asos_argmax_order_intents
            WHERE station_id=? AND event_date=? AND yes_token<>?
              AND state='EXCHANGE_ACCEPTED' AND size_matched>0
         ORDER BY updated_at_utc,order_intent_id""",
        (station_id, event_date, new_yes_token),
    ).fetchall()
    legs: list[dict[str, Any]] = []
    calls = 0
    submissions = 0
    for buy_id, old_token, matched in old_buys:
        leg = _process_rotation_leg(
            conn,
            target_entry_fact_id=entry_fact_id,
            source_buy_order_intent_id=str(buy_id),
            station_id=station_id,
            event_date=event_date,
            old_yes_token=str(old_token),
            new_yes_token=new_yes_token,
            source_buy_size_matched=float(matched),
            root=root,
        )
        legs.append(leg)
        calls += int(leg.get("gateway_call_count") or 0)
        submissions += int(leg.get("submission_attempt_count") or 0)
        if leg.get("ok") is not True:
            return {
                "ok": False, "state": "OLD_BUCKET_SELL_BLOCKED",
                "reason": leg.get("reason"), "skip_new_buy": False,
                "gateway_call_count": calls,
                "submission_attempt_count": submissions,
                "legs": legs,
            }
    return {
        "ok": True, "state": "OLD_BUCKETS_CLEARED",
        "skip_new_buy": False, "gateway_call_count": calls,
        "submission_attempt_count": submissions, "legs": legs,
    }


def process_pending_argmax_orders(conn: sqlite3.Connection, *, root: Path) -> dict[str, Any]:
    """Prepare, submit, and persist exact FOK outcomes one fact at a time."""

    reconcile_results = _reconcile_pending_argmax_orders(conn)
    rows = conn.execute(
        """SELECT f.entry_fact_id,f.station_id,f.event_date,f.yes_token,f.content_sha256
             FROM reported_trajectory_asos_argmax_entry_facts f
        LEFT JOIN reported_trajectory_asos_argmax_order_intents i
               ON i.entry_fact_id=f.entry_fact_id
            WHERE i.order_intent_id IS NULL
              AND NOT EXISTS (
                  SELECT 1 FROM reported_trajectory_asos_argmax_entry_facts newer
                   WHERE newer.station_id=f.station_id AND newer.event_date=f.event_date
                     AND (
                       newer.created_at_utc>f.created_at_utc OR
                       (newer.created_at_utc=f.created_at_utc AND newer.entry_fact_id>f.entry_fact_id)
                     )
              )
         ORDER BY f.created_at_utc,f.entry_fact_id"""
    ).fetchall()
    results = []
    gateway_call_count = len(reconcile_results)
    submission_attempt_count = 0
    rotation_gateway_call_count = 0
    rotation_submission_attempt_count = 0
    for entry_fact_id, station, event_date, token, fact_sha in rows:
        intent_id = ""
        rotation = _ensure_old_buckets_sold_before_new_buy(
            conn,
            entry_fact_id=str(entry_fact_id),
            station_id=str(station),
            event_date=str(event_date),
            new_yes_token=str(token),
            root=root,
        )
        rotation_gateway_call_count += int(rotation.get("gateway_call_count") or 0)
        rotation_submission_attempt_count += int(
            rotation.get("submission_attempt_count") or 0
        )
        if rotation.get("skip_new_buy") is True:
            results.append({
                "entry_fact_id": entry_fact_id,
                "state": "same_bucket_no_new_buy",
                "rotation": rotation,
            })
            continue
        if rotation.get("ok") is not True:
            results.append({
                "entry_fact_id": entry_fact_id,
                "state": "rotation_blocked_before_new_buy",
                "rotation": rotation,
            })
            continue
        total_remaining = max(0.0, TOTAL_BUDGET_USDC - _budget_used(conn))
        station_remaining = max(0.0, STATION_BUDGET_USDC - _budget_used(conn, str(station)))
        budget = min(total_remaining, station_remaining)
        if budget < 0.01:
            results.append({"entry_fact_id": entry_fact_id, "state": "budget_exhausted"})
            continue
        try:
            ws = _official_ws_buy_sweep(str(token), budget=budget, root=root)
            price = float(ws["limit_price"])
            notional = float(ws["notional"])
            size = float(ws["size"])
            provisional = {
                "token_id": str(token), "side": "BUY", "price": price,
                "limit_price": price, "size": size, "notional": notional,
            }
            gateway_call_count += 1
            guard = _post_json("/execution-guard-snapshot", provisional)
            if guard.get("ok") is not True or guard.get("evidence_errors"):
                raise RuntimeError("execution_guard_failed:" + ",".join(guard.get("evidence_errors") or []))
            guard_asks = sorted(
                (
                    (float(item["price"]), float(item["size"]))
                    for item in (guard.get("order_book") or {}).get("asks") or []
                    if float(item.get("price") or 0) > 0 and float(item.get("size") or 0) > 0
                ), key=lambda item: item[0],
            )
            executable_guard_depth = sum(
                ask_size for ask_price, ask_size in guard_asks
                if ask_price <= price + 1e-9
            )
            if not guard_asks or executable_guard_depth + SHARE_TOLERANCE < size:
                raise RuntimeError("execution_guard_market_buy_depth_changed")
            guard_sha = _sha(guard)
            intent_id = "asosintent_" + _sha({"entry": entry_fact_id, "fact": fact_sha})[:32]
            idempotency = "asosidem_" + _sha({"intent": intent_id})[:40]
            request_payload = {
                "strategy_id": STRATEGY_ID,
                "entry_fact_id": str(entry_fact_id),
                "order_intent_id": intent_id,
                "idempotency_key": idempotency,
                "token_id": str(token), "side": "BUY", "order_type": "FOK",
                "price": price, "size": size, "notional": notional,
                "allow_market": True, "post_only": False,
                "ws_snapshot_sha256": ws["snapshot_sha256"],
                "guard_snapshot_sha256": guard_sha,
            }
            now = _iso_now()
            conn.execute("BEGIN IMMEDIATE")
            # Recheck both ledgers under the writer lock.
            if _budget_used(conn) + notional > TOTAL_BUDGET_USDC + 1e-9 or _budget_used(conn, str(station)) + notional > STATION_BUDGET_USDC + 1e-9:
                conn.rollback()
                results.append({"entry_fact_id": entry_fact_id, "state": "budget_race_blocked"})
                continue
            conn.execute(
                """INSERT INTO reported_trajectory_asos_argmax_order_intents(
                     order_intent_id,entry_fact_id,strategy_id,station_id,event_date,
                     yes_token,side,order_type,limit_price,size,requested_notional,
                     idempotency_key,ws_snapshot_sha256,guard_snapshot_sha256,
                     guard_snapshot_json,state,gateway_request_json,gateway_response_json,
                     created_at_utc,updated_at_utc
                   ) VALUES(?,?,?,?,?,?,'BUY','FOK',?,?,?,?,?,?,?,'SUBMITTING',?,'{}',?,?)""",
                (
                    intent_id, entry_fact_id, STRATEGY_ID, station, event_date, token,
                    price, size, notional, idempotency, ws["snapshot_sha256"], guard_sha,
                    json.dumps(guard, sort_keys=True, separators=(",", ":")),
                    json.dumps(request_payload, sort_keys=True, separators=(",", ":")), now, now,
                ),
            )
            conn.commit()
            gateway_call_count += 1
            submission_attempt_count += 1
            response = _post_json("/place-order", request_payload, timeout=8.0)
            exchange_started = response.get("exchange_post_started") is True
            status = str(response.get("status") or "").lower()
            try:
                matched = float(response.get("size_matched"))
            except (TypeError, ValueError):
                matched = None
            full_fill = (
                response.get("ok") is True
                and response.get("orderID")
                and status in {"matched", "filled", "executed"}
                and matched is not None
                and math.isclose(matched, size, rel_tol=0, abs_tol=SHARE_TOLERANCE)
            )
            if full_fill:
                state = "EXCHANGE_ACCEPTED"
            elif (
                response.get("reconcile_required") or exchange_started
                or response.get("ok") is True or response.get("orderID")
            ):
                state = "RECONCILE_REQUIRED"
            else:
                state = "RELEASED"
            conn.execute(
                """UPDATE reported_trajectory_asos_argmax_order_intents
                      SET state=?,gateway_response_json=?,exchange_order_id=?,size_matched=?,updated_at_utc=?
                    WHERE order_intent_id=? AND state='SUBMITTING'""",
                (
                    state, json.dumps(response, sort_keys=True, separators=(",", ":")),
                    response.get("orderID"), response.get("size_matched"), _iso_now(), intent_id,
                ),
            )
            conn.commit()
            results.append({"entry_fact_id": entry_fact_id, "order_intent_id": intent_id, "state": state, "response": response})
        except Exception as exc:
            if intent_id:
                conn.execute(
                    """UPDATE reported_trajectory_asos_argmax_order_intents
                          SET state='RECONCILE_REQUIRED',updated_at_utc=?
                        WHERE order_intent_id=? AND state='SUBMITTING'""",
                    (_iso_now(), intent_id),
                )
                conn.commit()
            results.append({"entry_fact_id": entry_fact_id, "state": "blocked", "reason": f"{type(exc).__name__}:{exc}"})
    exit_result = _process_prepared_argmax_exits(conn)
    return {
        "ok": True, "state": "processed", "results": results,
        "orders_considered": len(rows), "reconcile_results": reconcile_results,
        "orders_reconciled": len(reconcile_results),
        "gateway_call_count": (
            gateway_call_count + rotation_gateway_call_count
            + int(exit_result["gateway_call_count"])
        ),
        "submission_attempt_count": (
            submission_attempt_count + rotation_submission_attempt_count
            + int(exit_result["submission_attempt_count"])
        ),
        "buy_submission_attempt_count": submission_attempt_count,
        "rotation_sell_submission_attempt_count": rotation_submission_attempt_count,
        "exit_submission_attempt_count": int(exit_result["submission_attempt_count"]),
        "exit_orders": exit_result,
    }
