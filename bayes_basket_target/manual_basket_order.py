"""The operator's one-click basket, sent from the board's calculator.

User, 2026-10-05: 「增加选择篮子后，点击确认一键下单功能，选择的篮子按照输入的
金额买入」.  The 单站日 panel's break-even calculator already decides the
legs; this sends them, one leg after another, through the one local gateway
(``clob_order_service`` /place-order) -- never to the exchange directly.

What it is and is not:

* An operator's own order.  Like the operator's earlier manual trades on this
  wallet (ruling 2026-09-18 「不记，是我手动买的」) it is not written into the
  system's ledger, intents or reservations; each batch is appended to
  ``data/manual_basket_orders.jsonl`` as its own receipt, and the gateway
  keeps its idempotency rows as for every order.
* BUY YES only.  No NO leg, no sell.
* The gateway's own gates hold unchanged: the pause flag, the read-only
  bit, the region block, collateral and the minimum lot.  The system's LIVE
  arming is the automatic lanes' switch and is not consulted.
* Re-checked here against the newest official WS snapshot: every bucket must
  be on that day's ladder (its token comes from the snapshot, never from the
  page), the book no older than ``MAX_BOOK_AGE_SECONDS``, each leg at or
  above the market's minimum lot, and a take must still be fillable within
  its stated cap on the book as it stands now.
* Legs go out in the order given and the batch stops at the first leg that
  is not a clean acceptance -- an unknown or a refusal leaves the rest unsent
  rather than building more of a basket whose shape has already broken.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from .order_submission import classify_submission_response

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "bayes_basket_target_ssot.db"
RECEIPT_PATH = ROOT / "data" / "manual_basket_orders.jsonl"
STRATEGY_ID = "operator_manual_board_v1"
CONFIRM_WORD = "BUY"
MODES = {"taker", "maker", "manual"}
MAX_LEGS = 12
MAX_BOOK_AGE_SECONDS = 120.0
#: A sanity ceiling on one batch, not a budget: the gateway still checks the
#: wallet.  Raised by the environment when the operator wants more.
MAX_BATCH_USDC = float(os.environ.get("PM_MANUAL_BASKET_MAX_USDC", "200"))

Transport = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class ManualOrderError(ValueError):
    """A request refused before anything was sent."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _gateway(payload: Mapping[str, Any]) -> Mapping[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from clob_order_service_client import ClobOrderServiceClient  # noqa: PLC0415

    return ClobOrderServiceClient(timeout_seconds=15.0).place_order(payload)


def latest_book(conn: sqlite3.Connection, station: str, day: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT observed_at, buckets_json FROM basket_official_ws_snapshot"
        " WHERE station_id=? AND event_date=? AND state='COMMITTED'"
        " ORDER BY observed_at DESC LIMIT 1",
        (station, day),
    ).fetchone()
    if row is None:
        raise ManualOrderError("没有这一天的官方 WS 盘口")
    buckets = {}
    for b in json.loads(row[1] or "[]"):
        asks = sorted(
            (float(l["price"]), float(l["shares"])) for l in (b.get("ask_levels") or [])
            if float(l.get("price") or 0) > 0 and float(l.get("shares") or 0) > 0
        )
        buckets[str(b.get("bucket"))] = {
            "token_id": str(b.get("token_id") or ""),
            "asks": asks if b.get("ask_available", True) else [],
            "best_bid": float(b.get("bid_price") or 0.0) or None,
            "tick": float(b.get("price_tick_size") or 0.001),
            "min_order_size": float(b.get("min_order_size") or 5.0),
        }
    return {"observed_at": str(row[0]), "buckets": buckets}


def fillable_within(asks: list[tuple[float, float]], shares: float, cap: float) -> float:
    """Shares the book offers at or under ``cap``."""

    return sum(size for price, size in asks if price <= cap + 1e-12)


def venue_tick(price: float) -> float:
    """The exchange's price step at ``price``: a cent in the middle of the
    range, a tenth of a cent near either end.  The snapshot's registry tick
    (0.001 on every rung) is not it -- the gateway answered
    ``exchange_price_min_tick_size: 0.01`` for a 0.29 rung on 2026-10-05."""

    return 0.001 if price < 0.04 or price > 0.96 else 0.01


def _on_tick(price: float) -> float:
    """Down to the venue's grid: a buy never pays more than it was told to."""

    tick = venue_tick(price)
    return round(math.floor(price / tick + 1e-9) * tick, 6)


def plan_orders(request: Mapping[str, Any], book: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """Validate the request against the book and build the gateway payloads."""

    if str(request.get("confirm") or "") != CONFIRM_WORD:
        raise ManualOrderError("缺少确认（confirm 必须是 BUY）")
    station = str(request.get("station") or "").upper()
    day = str(request.get("event_date") or "")
    batch = str(request.get("batch_id") or "")
    mode = str(request.get("mode") or "")
    if not re.fullmatch(r"[A-Z]{4}", station) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ManualOrderError("站点或日期不合法")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", batch):
        raise ManualOrderError("batch_id 不合法")
    if mode not in MODES:
        raise ManualOrderError(f"价格口径不认识：{mode}")
    legs = list(request.get("legs") or [])
    if not legs or len(legs) > MAX_LEGS:
        raise ManualOrderError("腿数必须在 1 到 12 之间")
    seen_at = _utc(book.get("observed_at"))
    if seen_at is None or (now - seen_at).total_seconds() > MAX_BOOK_AGE_SECONDS:
        raise ManualOrderError(f"盘口过期（{book.get('observed_at')}），等行情恢复再下")
    payloads = []
    total = 0.0
    seen = set()
    for index, leg in enumerate(legs):
        bucket = str(leg.get("bucket") or "")
        if bucket in seen:
            raise ManualOrderError(f"{bucket} 重复")
        seen.add(bucket)
        row = (book.get("buckets") or {}).get(bucket)
        if not row or not row["token_id"]:
            raise ManualOrderError(f"{bucket} 不在这一天的梯子上")
        try:
            shares = math.floor(float(leg.get("shares")) * 100 + 1e-6) / 100
            price = float(leg.get("limit_price"))
        except (TypeError, ValueError) as exc:
            raise ManualOrderError(f"{bucket} 份数或价格不是数") from exc
        if not (0.0 < price < 1.0):
            raise ManualOrderError(f"{bucket} 价格必须在 0 与 1 之间")
        price = _on_tick(price)
        if not (0.0 < price < 1.0):
            raise ManualOrderError(f"{bucket} 价格按 tick 取整后不在 0 与 1 之间")
        if shares < row["min_order_size"]:
            raise ManualOrderError(f"{bucket} {shares} 份低于最小单 {row['min_order_size']} 份")
        if mode == "taker":
            available = fillable_within(row["asks"], shares, price)
            if available + 1e-9 < shares:
                raise ManualOrderError(
                    f"{bucket} 盘口已变：上限价 {price} 内只剩 {available:.2f} 份，需要 {shares} 份")
            order = {"order_type": "FOK", "post_only": False, "fok_price_cap": price}
        elif mode == "maker":
            best_ask = row["asks"][0][0] if row["asks"] else None
            if best_ask is not None and price >= best_ask - 1e-12:
                raise ManualOrderError(f"{bucket} 挂单价 {price} 不低于卖一 {best_ask}，会被当成吃单拒绝")
            order = {"order_type": "GTC", "post_only": True}
        else:
            order = {"order_type": "GTC", "post_only": False}
        total += shares * price
        key = hashlib.sha256(f"{STRATEGY_ID}|{batch}|{station}|{day}|{bucket}".encode()).hexdigest()
        payloads.append({
            "bucket": bucket,
            "payload": {
                "strategy_id": STRATEGY_ID,
                "order_intent_id": f"manual-{batch}-{index}",
                "idempotency_key": key,
                "token_id": row["token_id"],
                "outcome_side": "YES",
                "side": "BUY",
                "price": price,
                "size": shares,
                **order,
            },
        })
    if total > MAX_BATCH_USDC + 1e-9:
        raise ManualOrderError(f"这一批 {total:.2f} U 超过单批上限 {MAX_BATCH_USDC:.0f} U（PM_MANUAL_BASKET_MAX_USDC）")
    return {"station": station, "event_date": day, "batch_id": batch, "mode": mode,
            "book_observed_at": book.get("observed_at"), "notional_usdc": round(total, 4),
            "legs": payloads}


def _receipt(record: Mapping[str, Any]) -> None:
    try:
        RECEIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with RECEIPT_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError:
        pass


def place_manual_basket(
    request: Mapping[str, Any],
    *,
    db: Path = DEFAULT_DB,
    transport: Transport | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or _now()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA query_only=1")
        book = latest_book(conn, str(request.get("station") or "").upper(),
                           str(request.get("event_date") or ""))
    finally:
        conn.close()
    plan = plan_orders(request, book, now=now)
    send = transport or _gateway
    results = []
    stopped = None
    for leg in plan["legs"]:
        if stopped is not None:
            results.append({"bucket": leg["bucket"], "event_type": "NOT_SENT",
                            "reason": f"前一腿 {stopped} 未干净成交，后面的腿不再发送"})
            continue
        response = dict(send(leg["payload"]))
        verdict = classify_submission_response(response, order_type=leg["payload"]["order_type"])
        fill = verdict.get("fill") or {}
        results.append({
            "bucket": leg["bucket"], "event_type": verdict["event_type"],
            "reason": verdict.get("reason") or "",
            "order_type": leg["payload"]["order_type"],
            "price": leg["payload"]["price"], "size": leg["payload"]["size"],
            "exchange_order_id": fill.get("exchange_order_id") or "",
            "shares_matched": fill.get("shares_matched"),
            "usdc_paid": fill.get("making_amount"),
            "gateway_error": response.get("error"),
        })
        if verdict["event_type"] not in {"FILL", "POST_ACK"}:
            stopped = leg["bucket"]
    record = {
        "schema": "manual_basket_order_v1", "at_utc": now.isoformat().replace("+00:00", "Z"),
        "station_id": plan["station"], "event_date": plan["event_date"],
        "batch_id": plan["batch_id"], "mode": plan["mode"],
        "book_observed_at": plan["book_observed_at"], "notional_usdc": plan["notional_usdc"],
        "legs": [leg["payload"] | {"bucket": leg["bucket"]} for leg in plan["legs"]],
        "results": results,
    }
    _receipt(record)
    return {"ok": stopped is None, **{k: record[k] for k in (
        "station_id", "event_date", "batch_id", "mode", "book_observed_at", "notional_usdc", "results")}}
