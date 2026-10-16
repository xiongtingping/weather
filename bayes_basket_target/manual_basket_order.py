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
* BUY only, YES or NO (user 2026-10-06「增加双边下单，增加no」: the
  operator's own order, not an automatic lane).  A NO buy is matched
  against YES bids, so its book is the YES bids mirrored: NO ask = 1 - YES
  bid at that bid's size.  The NO token is the market registry's.  No sell.
* The gateway's own gates hold unchanged: the pause flag, the read-only
  bit, the region block, collateral and the minimum lot.  The system's LIVE
  arming is the automatic lanes' switch and is not consulted.
* Re-checked here against the newest official WS snapshot: every bucket must
  be on that day's ladder (its token comes from the snapshot, never from the
  page), the book no older than ``MAX_BOOK_AGE_SECONDS``, each leg at or
  above the market's minimum lot and a take at or above the venue's 1 U
  floor.  A take is a capped FAK: it fills what the book holds within its
  cap and kills the rest (user 2026-10-16).
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
MAX_LEGS = 24
MAX_BOOK_AGE_SECONDS = 120.0
#: The venue's floor on a marketable buy's notional: 19.2 @ 0.05 came back
#: ``invalid amount for a marketable BUY order ($0.96), min size: 1``.
MIN_TAKE_USDC = 1.0
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


def latest_book(conn: sqlite3.Connection, station: str, day: str,
                now: datetime | None = None) -> dict[str, Any]:
    """The freshest ladder: the WS publisher's own, else the committed snapshot.

    The committed snapshot is a copy the resident writes once a cycle for a
    station; right after a restart it sat 6 minutes old (2026-10-05 12:22Z)
    and refused the operator's basket.  The publisher's manifest carries the
    stream as it arrives -- the read the Proper and tail lanes make.
    """

    from .p1_tail_lane import _latest_snapshot, _ws_manifest_root, live_ws_snapshot  # noqa: PLC0415

    now = now or _now()
    book = live_ws_snapshot(
        conn, _ws_manifest_root(None), station_id=station, event_date=day,
        now_utc=now.isoformat().replace("+00:00", "Z"), include_listing_days=True,
    ) or _latest_snapshot(conn, station, day)
    if book is None:
        raise ManualOrderError("没有这一天的官方 WS 盘口")
    no_tokens = _no_tokens(conn, station, day)
    buckets = {}
    for b in book.get("buckets") or []:
        asks = sorted(
            (float(l["price"]), float(l["shares"])) for l in (b.get("ask_levels") or [])
            if float(l.get("price") or 0) > 0 and float(l.get("shares") or 0) > 0
        )
        bids = sorted(
            ((float(l["price"]), float(l["shares"])) for l in (b.get("bid_levels") or [])
             if float(l.get("price") or 0) > 0 and float(l.get("shares") or 0) > 0),
            key=lambda x: -x[0])
        asks = asks if b.get("ask_available", True) else []
        bucket = str(b.get("bucket"))
        buckets[bucket] = {
            "token_id": str(b.get("token_id") or ""),
            "asks": asks,
            "bids": bids,
            "best_bid": float(b.get("bid_price") or 0.0) or None,
            "tick": float(b.get("price_tick_size") or 0.001),
            "min_order_size": float(b["min_order_size"]) if b.get("min_order_size") is not None else None,
            # The NO side, mirrored off the YES book.
            "no_token": no_tokens.get(bucket, ""),
            "no_asks": [(round(1.0 - p, 6), q) for p, q in bids],
            "no_bids": [(round(1.0 - p, 6), q) for p, q in asks],
            "no_best_bid": None if not asks else round(1.0 - asks[0][0], 6),
        }
    return {"observed_at": str(book.get("observed_at")), "buckets": buckets,
            "source": book.get("book_source") or "committed_snapshot"}


def _no_tokens(conn: sqlite3.Connection, station: str, day: str) -> dict[str, str]:
    """bucket -> NO token, off the market registry production trades from."""

    from .certain_win_recycle import _registry_rows  # noqa: PLC0415

    out = {}
    try:
        rows = _registry_rows(conn, None, station_id=station, event_date=day)
    except Exception:  # noqa: BLE001 -- no NO token means no NO leg, said per leg
        return out
    for bucket, row in rows.items():
        try:
            raw = json.loads(row.get("registry_json") or "{}")
        except (TypeError, ValueError):
            raw = {}
        if raw.get("no_token"):
            out[str(bucket)] = str(raw["no_token"])
    return out


def venue_tick(price: float) -> float:
    """The exchange's price step at ``price``: a cent in the middle of the
    range, a tenth of a cent near either end.  The snapshot's registry tick
    (0.001 on every rung) is not it -- the gateway answered
    ``exchange_price_min_tick_size: 0.01`` for a 0.29 rung on 2026-10-05."""

    return 0.001 if price < 0.04 or price > 0.96 else 0.01


def fok_shares(price: float, shares: float) -> float:
    """The most shares, down from ``shares`` in hundredths, whose cost at
    ``price`` is a whole number of cents.

    A FOK buy is a market order at the venue, and its maker amount (the USDC
    paid) takes at most two decimals: 24.17 shares at 0.32 is 7.7344 USDC and
    came back ``invalid amounts, the market buy orders maker amount supports
    a max accuracy of 2 decimals`` (2026-10-07).  In integers: price in
    thousandths times shares in hundredths has to be a multiple of 1000.
    """

    p = round(price * 1000)
    s = int(math.floor(shares * 100 + 1e-6))
    while s > 0 and (p * s) % 1000:
        s -= 1
    return s / 100


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
        side = str(leg.get("side") or "YES").upper()
        if side not in {"YES", "NO"}:
            raise ManualOrderError(f"{bucket} 方向不认识：{side}")
        if (bucket, side) in seen:
            raise ManualOrderError(f"{bucket} {side} 重复")
        seen.add((bucket, side))
        row = (book.get("buckets") or {}).get(bucket)
        if not row or not row["token_id"]:
            raise ManualOrderError(f"{bucket} 不在这一天的梯子上")
        token = row["token_id"] if side == "YES" else row.get("no_token") or ""
        if not token:
            raise ManualOrderError(f"{bucket} 没有登记 NO token，不能买 NO")
        side_asks = row["asks"] if side == "YES" else row.get("no_asks") or []
        label = bucket if side == "YES" else f"{bucket} NO"
        try:
            shares = math.floor(float(leg.get("shares")) * 100 + 1e-6) / 100
            price = float(leg.get("limit_price"))
        except (TypeError, ValueError) as exc:
            raise ManualOrderError(f"{label} 份数或价格不是数") from exc
        if not (0.0 < price < 1.0):
            raise ManualOrderError(f"{label} 价格必须在 0 与 1 之间")
        price = _on_tick(price)
        if not (0.0 < price < 1.0):
            raise ManualOrderError(f"{label} 价格按 tick 取整后不在 0 与 1 之间")
        if row.get("min_order_size") is None or not math.isfinite(row["min_order_size"]) or row["min_order_size"] <= 0:
            raise ManualOrderError(f"{label} 交易所最小单量缺失或无效")
        if shares < row["min_order_size"]:
            raise ManualOrderError(f"{label} {shares} 份低于最小单 {row['min_order_size']} 份")
        if mode == "taker":
            asked = shares
            shares = fok_shares(price, shares)
            if shares < row["min_order_size"]:
                raise ManualOrderError(
                    f"{label} 吃单金额须是整分钱：{asked} 份按 {price} 只能下 {shares} 份，低于最小单 {row['min_order_size']} 份")
            if shares * price < MIN_TAKE_USDC - 1e-9:
                raise ManualOrderError(
                    f"{label} 吃单金额 {shares * price:.2f} U 低于交易所吃单下限 {MIN_TAKE_USDC:.0f} U")
            # A capped FAK, not a FOK (user 2026-10-16「不要因为这种阻断」): it
            # takes whatever the book holds within the cap when it arrives and
            # kills the rest.  The book here is seconds old, so a depth check
            # against it refused takes the venue would partly have filled and
            # passed ones it then killed whole (22°C 41.5 @ 0.14, 10:17Z).
            order = {"order_type": "FAK", "post_only": False, "fok_price_cap": price}
        elif mode == "maker":
            best_ask = side_asks[0][0] if side_asks else None
            if best_ask is not None and price >= best_ask - 1e-12:
                raise ManualOrderError(f"{label} 挂单价 {price} 不低于卖一 {best_ask}，会被当成吃单拒绝")
            order = {"order_type": "GTC", "post_only": True}
        else:
            order = {"order_type": "GTC", "post_only": False}
        total += shares * price
        key = hashlib.sha256(f"{STRATEGY_ID}|{batch}|{station}|{day}|{bucket}|{side}".encode()).hexdigest()
        payloads.append({
            "bucket": bucket, "side": side,
            "payload": {
                "strategy_id": STRATEGY_ID,
                "order_intent_id": f"manual-{batch}-{index}",
                "idempotency_key": key,
                "token_id": token,
                "outcome_side": side,
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


NEVER_REACHED = "NEVER_REACHED_EXCHANGE"
RECONCILE_TRIES = 4
RECONCILE_WAIT_SECONDS = 5.0


def _gateway_reconcile(idempotency_key: str) -> Mapping[str, Any]:
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    from clob_order_service_client import ClobOrderServiceClient  # noqa: PLC0415

    return ClobOrderServiceClient(timeout_seconds=20.0).lookup_order({"idempotency_key": idempotency_key})


def _settle_unknown(leg: Mapping[str, Any], reconcile: Callable[[str], Mapping[str, Any]],
                    sleep: Callable[[float], Any] | None = None) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """An unknown post, looked up -- read only -- until the gateway knows.

    ``order_never_reached_exchange`` (the exchange has no order under the
    id the signature fixes) is final: nothing was placed, the operator may
    send again.  An accepted or matched answer is the order's own outcome.
    Anything else stays unknown and says so.
    """

    import time  # noqa: PLC0415

    sleep = sleep or time.sleep
    order_type = leg["payload"]["order_type"]
    last: dict[str, Any] = {}
    for attempt in range(RECONCILE_TRIES):
        if attempt:
            sleep(RECONCILE_WAIT_SECONDS)
        try:
            last = dict(reconcile(leg["payload"]["idempotency_key"]) or {})
        except Exception as exc:  # noqa: BLE001 -- unknown stays unknown
            last = {"error": f"reconcile_failed:{type(exc).__name__}"}
            continue
        if str(last.get("error") or "") == "order_never_reached_exchange":
            return ({"event_type": NEVER_REACHED, "submitted": False,
                     "reason": "已对账：订单未到达交易所，没有成交，可以重下"}, last)
        verdict = classify_submission_response(last, order_type=order_type)
        if verdict["event_type"] in {"FILL", "POST_ACK", "EXPIRED"}:
            return ({**verdict, "reason": "已对账：" + ({"FILL": "交易所已成交", "POST_ACK": "交易所已挂单",
                                                        "EXPIRED": "吃单到交易所后未成交、已作废"}[verdict["event_type"]])}, last)
    return ({"event_type": "UNKNOWN_POST", "submitted": True,
             "reason": "对账仍未确定：" + str(last.get("error") or last.get("status") or "无答复") + "——不要重下，稍后再查"}, last)


def _killed_fak(verdict: dict[str, Any], order_type: str) -> dict[str, Any]:
    """A FAK the exchange took without a match was killed, not left resting."""

    if order_type == "FAK" and verdict.get("event_type") == "POST_ACK":
        return {**verdict, "event_type": "EXPIRED", "reason": "FAK 上限价内无成交，已作废"}
    return verdict


COLLATERAL_STALE = "clob_collateral_evidence_unavailable"
#: How long a refused leg waits for the gateway's balance reading to be fresh.
COLLATERAL_WAIT_SECONDS = 45.0
COLLATERAL_FRESH_MS = 30_000


def _gateway_collateral_fresh() -> bool:
    """The gateway's own health: is its balance reading recent?"""

    from urllib.request import ProxyHandler, build_opener  # noqa: PLC0415

    try:
        with build_opener(ProxyHandler({})).open("http://127.0.0.1:8765/health", timeout=5) as resp:
            health = json.loads(resp.read().decode("utf-8") or "{}")
    except Exception:  # noqa: BLE001 -- unreachable reads as not fresh
        return False
    age = health.get("collateral_cache_age_ms")
    return bool(health.get("collateral_cache_ready")) and age is not None and float(age) < COLLATERAL_FRESH_MS


def _wait_collateral(fresh: Callable[[], bool] = _gateway_collateral_fresh,
                     sleep: Callable[[float], Any] | None = None) -> bool:
    import time  # noqa: PLC0415

    sleep = sleep or time.sleep
    waited = 0.0
    while waited <= COLLATERAL_WAIT_SECONDS:
        if fresh():
            return True
        sleep(3.0)
        waited += 3.0
    return False


def place_manual_basket(
    request: Mapping[str, Any],
    *,
    db: Path = DEFAULT_DB,
    transport: Transport | None = None,
    now: datetime | None = None,
    wait_collateral: Callable[[], bool] | None = None,
    reconcile: Callable[[str], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    now = now or _now()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        conn.execute("PRAGMA query_only=1")
        book = latest_book(conn, str(request.get("station") or "").upper(),
                           str(request.get("event_date") or ""), now)
    finally:
        conn.close()
    plan = plan_orders(request, book, now=now)
    send = transport or _gateway
    wait_collateral = wait_collateral or _wait_collateral
    reconcile = reconcile or _gateway_reconcile
    results = []
    stopped = None
    for leg in plan["legs"]:
        if stopped is not None:
            results.append({"bucket": leg["bucket"], "side": leg.get("side", "YES"), "event_type": "NOT_SENT",
                            "reason": f"前一腿 {stopped} 未干净成交，后面的腿不再发送"})
            continue
        response = dict(send(leg["payload"]))
        if (str(response.get("error") or "") == COLLATERAL_STALE
                and response.get("exchange_post_started") is not True):
            # The gateway refused before signing: its background balance read
            # was older than its bound (2026-10-07 05:51Z).  Nothing left the
            # machine and no idempotency row exists, so the same payload may
            # go again once the reading is fresh.
            if wait_collateral():
                response = dict(send(leg["payload"]))
        verdict = _killed_fak(classify_submission_response(response, order_type=leg["payload"]["order_type"]),
                              leg["payload"]["order_type"])
        reconciled = None
        if verdict["event_type"] == "UNKNOWN_POST":
            # The request left and no answer settled it (2026-10-07 10:09Z:
            # "clob_proxy_submit_failed: SSL connection timeout").  Never
            # resend; ask the gateway what the exchange holds for this key.
            verdict, reconciled = _settle_unknown(leg, reconcile)
            verdict = _killed_fak(verdict, leg["payload"]["order_type"])
        fill = verdict.get("fill") or {}
        results.append({
            "bucket": leg["bucket"], "side": leg.get("side", "YES"), "event_type": verdict["event_type"],
            "reason": verdict.get("reason") or "",
            "order_type": leg["payload"]["order_type"],
            "price": leg["payload"]["price"], "size": leg["payload"]["size"],
            "exchange_order_id": fill.get("exchange_order_id") or "",
            "shares_matched": fill.get("shares_matched"),
            "usdc_paid": fill.get("making_amount"),
            "gateway_error": response.get("error"),
            "reconciled": reconciled,
        })
        if verdict["event_type"] not in {"FILL", "POST_ACK"}:
            stopped = leg["bucket"] + ("" if leg.get("side", "YES") == "YES" else " NO")
    record = {
        "schema": "manual_basket_order_v1", "at_utc": now.isoformat().replace("+00:00", "Z"),
        "station_id": plan["station"], "event_date": plan["event_date"],
        "batch_id": plan["batch_id"], "mode": plan["mode"],
        "book_observed_at": plan["book_observed_at"], "book_source": book.get("source"),
        "notional_usdc": plan["notional_usdc"],
        "legs": [leg["payload"] | {"bucket": leg["bucket"], "side": leg.get("side", "YES")} for leg in plan["legs"]],
        "results": results,
    }
    _receipt(record)
    return {"ok": stopped is None, **{k: record[k] for k in (
        "station_id", "event_date", "batch_id", "mode", "book_observed_at", "notional_usdc", "results")}}


# ---------------------------------------------------------------------------
# What the wallet already holds on a station-day (user 2026-10-05「如果临时增加
# 桶呢」): the calculator adds a bucket to a basket already partly bought, so
# it needs each rung's shares and what they cost.

_ADDRESS_KEYS = ("POLY_DATA_API_USER", "PM_FUNDER_ADDRESS")


def funder_address() -> str:
    """The wallet's public address: the environment, else the repo's .env --
    read for these two keys only, nothing else in that file."""

    for key in _ADDRESS_KEYS:
        if os.environ.get(key):
            return str(os.environ[key]).strip()
    try:
        for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
            match = re.match(r"\s*(?:export\s+)?(POLY_DATA_API_USER|PM_FUNDER_ADDRESS)\s*=\s*['\"]?(0x[0-9a-fA-F]{40})", line)
            if match:
                return match.group(2)
    except OSError:
        pass
    return ""


def _data_api_positions(user: str, *, timeout: float = 20.0) -> dict[str, dict[str, float]]:
    """asset -> {size, avg_price} off the public data API.  It is outside the
    machine, so it goes through the Mac's proxy like any other venue read."""

    import ssl  # noqa: PLC0415
    from urllib.parse import urlencode  # noqa: PLC0415
    from urllib.request import Request, urlopen  # noqa: PLC0415

    try:
        import certifi  # noqa: PLC0415 -- this Python has no system CA bundle

        context = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        context = ssl.create_default_context()

    host = os.environ.get("POLY_DATA_API_HOST", "https://data-api.polymarket.com").rstrip("/")
    out: dict[str, dict[str, float]] = {}
    for offset in range(0, 2000, 500):
        query = urlencode({"user": user, "sizeThreshold": 0, "limit": 500, "offset": offset})
        request = Request(f"{host}/positions?{query}", headers={"User-Agent": "weather-board-holdings"})
        with urlopen(request, timeout=timeout, context=context) as response:  # noqa: S310 -- fixed https host
            rows = json.loads(response.read().decode("utf-8") or "[]")
        for row in rows if isinstance(rows, list) else []:
            asset = str(row.get("asset") or "")
            if asset:
                out[asset] = {"size": float(row.get("size") or 0.0),
                              "avg_price": float(row.get("avgPrice") or 0.0)}
        if not isinstance(rows, list) or len(rows) < 500:
            break
    return out


def station_day_holdings(station: str, day: str, *, db: Path = DEFAULT_DB,
                         lookup: Callable[[str], Mapping[str, Any]] | None = None,
                         positions: Callable[[str], Mapping[str, Mapping[str, float]]] | None = None,
                         ) -> dict[str, Any]:
    """Each rung's shares on the account (the gateway's point lookup -- the
    exchange's own balance) and their average cost (the data API).  A rung
    the two disagree on is flagged; the shares shown are the gateway's."""

    station = station.upper()
    if not re.fullmatch(r"[A-Z]{4}", station) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ManualOrderError("站点或日期不合法")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    try:
        book = latest_book(conn, station, day)
    finally:
        conn.close()
    if lookup is None:
        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from clob_order_service_client import ClobOrderServiceClient  # noqa: PLC0415

        # 2026-10-06 01:48Z every rung timed out at 10 s while the egress was
        # slow; a lookup is a read, so a longer wait costs nothing but time.
        client = ClobOrderServiceClient(timeout_seconds=25.0)
        lookup = lambda token: client._post("/lookup-position", {"token_id": token})  # noqa: E731
    user = funder_address()
    api_error = None
    try:
        api = (positions or _data_api_positions)(user) if user else {}
        if not user:
            api_error = "钱包地址未配置"
    except Exception as exc:  # noqa: BLE001 -- shares still come from the gateway
        api, api_error = {}, f"{type(exc).__name__}: {str(exc)[:120]}"
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    ladder = list(book["buckets"].items())
    # One point lookup per token -- the YES and the NO of each rung -- each a
    # venue round trip through the gateway: side by side, not one after
    # another (11 rungs took 17 s).
    tokens = sorted({t for _b, row in ladder for t in (row["token_id"], row.get("no_token")) if t})
    # Four at a time: these lookups share the gateway's balance limiter with
    # its own background balance read, and 22 at once kept that read stale
    # long enough to refuse an order (clob_collateral_evidence_unavailable).
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(tokens)))) as pool:
        answers = dict(zip(tokens, pool.map(lambda t: dict(lookup(t) or {}), tokens)))

    def position(token: str) -> dict[str, Any]:
        if not token:
            return {"shares": 0.0, "source": None, "error": None, "avg": None, "api_size": None}
        answer = answers.get(token) or {}
        from_api = api.get(token) or {}
        shares, source = (float(answer["shares"]), "gateway") if answer.get("ok") else (None, None)
        if shares is None and api_error is None and user:
            # The gateway's lookup failed but the data API answered: it lists
            # every held asset, so a token absent from it holds nothing.
            shares, source = float(from_api.get("size") or 0.0), "data_api"
        return {"shares": shares, "source": source,
                "error": None if answer.get("ok") else answer.get("error") or "lookup_failed",
                "avg": from_api.get("avg_price") or None, "api_size": from_api.get("size")}

    rows = []
    for bucket, row in ladder:
        yes, no = position(row["token_id"]), position(row.get("no_token") or "")
        cost = lambda x: None if x["shares"] is None or x["avg"] is None else round(x["shares"] * x["avg"], 4)  # noqa: E731
        differs = lambda x: (x["shares"] is not None and x["api_size"] is not None  # noqa: E731
                             and abs(x["shares"] - float(x["api_size"])) > 0.01)
        rows.append({
            "bucket": bucket, "shares": yes["shares"], "shares_source": yes["source"],
            "gateway_error": yes["error"], "avg_price": yes["avg"], "cost_usdc": cost(yes),
            "data_api_size": yes["api_size"], "disagrees": differs(yes),
            "no_shares": no["shares"], "no_shares_source": no["source"],
            "no_gateway_error": no["error"], "no_avg_price": no["avg"], "no_cost_usdc": cost(no),
            "no_data_api_size": no["api_size"], "no_disagrees": differs(no),
        })
    return {"station_id": station, "event_date": day,
            "observed_at_utc": _now().isoformat().replace("+00:00", "Z"),
            "data_api_error": api_error, "rows": rows,
            # Nothing known: the board keeps what it had instead of reading
            # "no holdings" off a read that never answered.
            "unknown": all(r["shares"] is None for r in rows)}
