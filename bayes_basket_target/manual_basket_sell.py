"""Explicit operator reductions, priced on official WS bids and sent via the gateway.

Quote and submit both read the exchange's own position. Durable pending state
precedes submission, so an interrupted request must reconcile before another sale.
No SSOT writes or automatic lane changes are involved.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import re
import shutil
import sqlite3
from pathlib import Path
from typing import Any, Callable, Mapping

from . import manual_basket_order as M
from .order_submission import classify_submission_response

STATE_PATH = M.ROOT / "data/manual_basket_sell_pending.json"
MAX_STATE_BYTES = 1024 * 1024


def _lookup(token: str) -> Mapping[str, Any]:
    from clob_order_service_client import ClobOrderServiceClient
    return ClobOrderServiceClient(timeout_seconds=25)._post("/lookup-position", {"token_id": token})


def _number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (ValueError, TypeError) as exc:
        raise M.ManualOrderError(f"{label}不是数") from exc
    if not math.isfinite(result):
        raise M.ManualOrderError(f"{label}不是有限数")
    return result


def _identity(request: Mapping[str, Any]) -> tuple[str, str, str, str]:
    station, day = str(request.get("station") or "").upper(), str(request.get("event_date") or "")
    bucket, side = str(request.get("bucket") or ""), str(request.get("side") or "")
    if not re.fullmatch(r"[A-Z]{4}", station) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise M.ManualOrderError("站点或日期不合法")
    if side not in {"YES", "NO"}:
        raise M.ManualOrderError("卖出方向必须是 YES 或 NO")
    return station, day, bucket, side


def _book(station: str, day: str, db: Path, now) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA query_only=1")
        return M.latest_book(conn, station, day, now)
    finally:
        conn.close()


def _position(row: Mapping[str, Any], side: str, lookup: Callable) -> tuple[str, float]:
    token = str(row.get("token_id" if side == "YES" else "no_token") or "")
    if not token:
        raise M.ManualOrderError("本桶没有登记对应 token")
    answer = lookup(token)
    if answer.get("ok") is not True:
        raise M.ManualOrderError("交易所持仓未知，不能卖出（不采用公开接口滞后份额）")
    held = _number(answer.get("shares"), "持仓")
    if held < 0:
        raise M.ManualOrderError("持仓无效")
    return token, held


def _levels(book: Mapping, bucket: str, side: str, now) -> tuple[Mapping, list]:
    at = M._utc(book.get("observed_at"))
    if at is None or not 0 <= (now - at).total_seconds() <= M.MAX_BOOK_AGE_SECONDS:
        raise M.ManualOrderError("官方 WS 盘口过期")
    row = (book.get("buckets") or {}).get(bucket)
    if not row:
        raise M.ManualOrderError("本桶不在该站日梯子上")
    levels = sorted(row.get("bids" if side == "YES" else "no_bids") or [], reverse=True)
    if not levels or any(not (math.isfinite(p) and 0 < p < 1 and math.isfinite(q) and q > 0) for p, q in levels):
        raise M.ManualOrderError("官方 WS 买盘深度缺失或无效")
    minimum = _number(row.get("min_order_size"), "最小单")
    if minimum <= 0:
        raise M.ManualOrderError("最小单缺失")
    return row, levels


def quote_manual_sell(request: Mapping[str, Any], *, db: Path = M.DEFAULT_DB,
                      lookup: Callable | None = None, now=None) -> dict:
    station, day, bucket, side = _identity(request)
    # A share count (the board's PM-style ticket, user 2026-10-09「和市场盘口一样
    # 这种」) or one of the four fractions of the exchange's own position.
    asked = request.get("shares")
    percent = None
    if asked is None:
        percent = _number(request.get("percent"), "比例")
        if percent not in {25, 50, 75, 100}:
            raise M.ManualOrderError("卖出比例必须为 25%、50%、75% 或 100%")
    else:
        asked = _number(asked, "份数")
        if asked <= 0 or abs(asked * 100 - round(asked * 100)) > 1e-6:
            raise M.ManualOrderError("卖出份数须为正数且最多两位小数")
    lookup = lookup or _lookup
    # Position may take seconds; read the book after that round trip.
    first = _book(station, day, db, now or M._now())
    row = (first.get("buckets") or {}).get(bucket)
    if not row:
        raise M.ManualOrderError("本桶不在该站日梯子上")
    token, held = _position(row, side, lookup)
    book = _book(station, day, db, now or M._now())
    row, levels = _levels(book, bucket, side, now or M._now())
    if token != row.get("token_id" if side == "YES" else "no_token"):
        raise M.ManualOrderError("盘口 token 已变化，重新读取")
    if percent is None:
        if asked > held + 1e-9:
            raise M.ManualOrderError(f"卖出 {asked:.2f} 份超过交易所持仓 {held:.2f} 份")
        shares = round(asked, 2)
    else:
        shares = math.floor(held * percent + 1e-7) / 100
    if shares < row["min_order_size"]:
        raise M.ManualOrderError(f"卖出 {shares:.2f} 份低于最小单 {row['min_order_size']} 份")
    left, proceeds, floor = shares, 0.0, 0.0
    for price, size in levels:
        take = min(left, size)
        proceeds += take * price
        left -= take
        floor = price
        if left <= 1e-9:
            break
    if left > 1e-9:
        raise M.ManualOrderError(f"官方 WS 买盘深度不足：买盘合计只有 {sum(q for _, q in levels):.2f} 份")
    return {"station": station, "event_date": day, "bucket": bucket, "side": side,
            "percent": percent, "held_shares": held, "shares": shares, "limit_price": floor,
            "estimated_proceeds": proceeds, "book_observed_at": book.get("observed_at")}


def _save_pending(state: dict) -> None:
    raw = json.dumps(state, ensure_ascii=False)
    if len(raw.encode()) > MAX_STATE_BYTES or shutil.disk_usage(STATE_PATH.parent).free < 64 * 1024 * 1024:
        raise M.ManualOrderError("卖出对账状态或剩余磁盘超限")
    temp = STATE_PATH.with_suffix(".tmp")
    try:
        with temp.open("w", encoding="utf-8") as handle:
            handle.write(raw)
            handle.flush()
            import os
            os.fsync(handle.fileno())
        temp.replace(STATE_PATH)
    finally:
        temp.unlink(missing_ok=True)


def place_manual_sell(request: Mapping[str, Any], *, db: Path = M.DEFAULT_DB,
                      transport: Callable | None = None, lookup: Callable | None = None,
                      reconcile: Callable | None = None, now=None) -> dict:
    station, day, bucket, side = _identity(request)
    if request.get("confirm") != "SELL":
        raise M.ManualOrderError("缺少 SELL 确认")
    batch = str(request.get("batch_id") or "")
    if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", batch):
        raise M.ManualOrderError("batch_id 不合法")
    lookup, reconcile = lookup or _lookup, reconcile or M._gateway_reconcile
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with STATE_PATH.with_suffix(".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise M.ManualOrderError("另一个卖出请求处理中，请等待") from exc
        if STATE_PATH.exists() and STATE_PATH.stat().st_size > MAX_STATE_BYTES:
            raise M.ManualOrderError("卖出对账状态超限")
        state = json.loads(STATE_PATH.read_text()) if STATE_PATH.exists() else {}
        # Reconcile interrupted sales before a new payload is even constructed.
        for key, pending in list(state.items()):
            verdict, _ = M._settle_unknown({"payload": pending}, reconcile)
            if verdict["event_type"] == "UNKNOWN_POST":
                raise M.ManualOrderError("reconcile_required：上次卖出结果未知，已对账仍未确定，禁止重发")
            del state[key]
        _save_pending(state)
        first = _book(station, day, db, now or M._now())
        row = (first.get("buckets") or {}).get(bucket)
        if not row:
            raise M.ManualOrderError("本桶不在该站日梯子上")
        token, held = _position(row, side, lookup)
        book = _book(station, day, db, now or M._now())
        row, levels = _levels(book, bucket, side, now or M._now())
        if token != row.get("token_id" if side == "YES" else "no_token"):
            raise M.ManualOrderError("盘口 token 已变化")
        expected_held = _number(request.get("expected_held_shares"), "确认时持仓")
        if abs(held - expected_held) > 1e-6:
            raise M.ManualOrderError("持仓已变，重新读取比例和报价后确认")
        shares = _number(request.get("shares"), "份数")
        price = _number(request.get("limit_price"), "最低成交价")
        if shares <= 0 or abs(shares * 100 - round(shares * 100)) > 1e-6:
            raise M.ManualOrderError("卖出份数须为正数且最多两位小数")
        if shares > held + 1e-9 or shares < row["min_order_size"]:
            raise M.ManualOrderError("持仓已变或低于最小单，重新读取")
        if not 0 < price < 1 or abs(price / M.venue_tick(price, row.get("venue_tick")) - round(price / M.venue_tick(price, row.get("venue_tick")))) > 1e-6:
            raise M.ManualOrderError("最低成交价无效或不在交易所 tick 上")
        if sum(size for p, size in levels if p >= price - 1e-12) + 1e-9 < shares:
            raise M.ManualOrderError("盘口已变：最低价以内的买盘深度不足")
        if shares * price > M.MAX_BATCH_USDC:
            raise M.ManualOrderError("卖出名义金额超过单批上限")
        key = hashlib.sha256(f"{M.STRATEGY_ID}|SELL|{batch}|{station}|{day}|{bucket}|{side}".encode()).hexdigest()
        payload = {"strategy_id": M.STRATEGY_ID, "order_intent_id": f"manual-sell-{batch}",
                   "idempotency_key": key, "token_id": token, "outcome_side": side, "side": "SELL",
                   "price": price, "size": shares, "order_type": "FOK", "post_only": False,
                   "allow_market": True}
        state[key] = payload
        _save_pending(state)
        try:
            response = dict((transport or M._gateway)(payload))
        except Exception as exc:
            response = {"transport_ambiguous": True, "error": type(exc).__name__}
        verdict = classify_submission_response(response, order_type="FOK")
        reco = None
        if verdict["event_type"] == "UNKNOWN_POST":
            verdict, reco = M._settle_unknown({"payload": payload}, reconcile)
        fill = verdict.get("fill") or {}
        result = {"bucket": bucket, "side": side, "action": "SELL", "event_type": verdict["event_type"],
                  "reason": verdict.get("reason") or "", "order_type": "FOK", "price": price, "size": shares,
                  "shares_matched": fill.get("shares_matched"), "usdc_received": fill.get("taking_amount"),
                  "exchange_order_id": fill.get("exchange_order_id"), "reconciled": reco}
        unknown = verdict["event_type"] == "UNKNOWN_POST"
        if not unknown:
            state.pop(key, None)
            _save_pending(state)
        # Never infer the remaining account position from a requested amount.
        try:
            _, after = _position(row, side, lookup)
        except Exception:
            after = None
        record = {"schema": "manual_basket_sell_v1", "at_utc": (now or M._now()).isoformat(),
                  "station_id": station, "event_date": day, "batch_id": batch, "action": "SELL",
                  "book_observed_at": book.get("observed_at"), "notional_usdc": shares * price,
                  "results": [result], "position_after": after, "reconcile_required": unknown}
        M._receipt(record)
        return {"ok": verdict["event_type"] == "FILL", **record}
