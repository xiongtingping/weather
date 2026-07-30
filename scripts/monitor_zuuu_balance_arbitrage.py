#!/usr/bin/env python3
"""Read-only real-time ZUUU maker-limit complete-set monitor.

Quote evidence comes exclusively from the official Polymarket CLOB WebSocket.
The official CLOB REST API is used once at startup for static taker-only fee
and tick-size parameters;
the script never reads REST prices, writes ticks, submits orders, or runs as a
production service.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import ssl
import sys
import time
from typing import Any, Iterable, Mapping, Sequence
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

import certifi
import websockets


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from balance_arbitrage_monitor import FeeSpec, MarketBinding, build_complete_sets


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_CLOB_HOST = "https://clob.polymarket.com"


class BookState:
    """In-memory full bid/ask books built from one lossless WS connection."""

    def __init__(self, token_ids: Iterable[str]) -> None:
        self.expected = {str(token) for token in token_ids if str(token)}
        self.materialized: set[str] = set()
        self.bids: dict[str, dict[float, float]] = {}
        self.asks: dict[str, dict[float, float]] = {}
        self.version = 0

    @staticmethod
    def _levels(rows: Any) -> dict[float, float]:
        result: dict[float, float] = {}
        for raw in rows if isinstance(rows, list) else []:
            if not isinstance(raw, Mapping):
                continue
            try:
                price, size = float(raw.get("price")), float(raw.get("size"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(price) and math.isfinite(size) and 0.0 < price < 1.0 and size > 0.0:
                result[price] = result.get(price, 0.0) + size
        return result

    def apply(self, message: Mapping[str, Any]) -> bool:
        event_type = str(message.get("event_type") or "").strip()
        changed = False
        if event_type == "book":
            token = str(message.get("asset_id") or "").strip()
            if token not in self.expected:
                return False
            next_bids = self._levels(message.get("bids"))
            next_asks = self._levels(message.get("asks"))
            changed = (
                token not in self.materialized
                or self.bids.get(token) != next_bids
                or self.asks.get(token) != next_asks
            )
            self.bids[token] = next_bids
            self.asks[token] = next_asks
            self.materialized.add(token)
        elif event_type == "price_change":
            for raw in message.get("price_changes") or []:
                if not isinstance(raw, Mapping):
                    continue
                token = str(raw.get("asset_id") or "").strip()
                side = str(raw.get("side") or "").strip().upper()
                if token not in self.materialized or side not in {"BUY", "BID", "SELL", "ASK"}:
                    continue
                try:
                    price, size = float(raw.get("price")), float(raw.get("size"))
                except (TypeError, ValueError):
                    continue
                if not math.isfinite(price) or not 0.0 < price < 1.0 or not math.isfinite(size):
                    continue
                levels = self.bids[token] if side in {"BUY", "BID"} else self.asks[token]
                before = levels.get(price)
                if size <= 0.0:
                    levels.pop(price, None)
                    changed = changed or before is not None
                else:
                    levels[price] = size
                    changed = changed or before != size
        if changed:
            self.version += 1
        return changed

    @property
    def ready(self) -> bool:
        return self.materialized == self.expected

    @staticmethod
    def _rows(values: Mapping[float, float], *, descending: bool) -> list[dict[str, float]]:
        return [
            {"price": price, "size": values[price]}
            for price in sorted(values, reverse=descending)
        ]

    def snapshot(self) -> dict[str, dict[str, list[dict[str, float]]]]:
        return {
            token: {
                "bids": self._rows(self.bids.get(token, {}), descending=True),
                "asks": self._rows(self.asks.get(token, {}), descending=False),
            }
            for token in self.materialized
        }


def _iter_messages(raw: str) -> Iterable[Mapping[str, Any]]:
    if not raw or raw in {"PING", "PONG"}:
        return []
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, Mapping)]
    return [payload] if isinstance(payload, Mapping) else []


def _json_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    try:
        parsed = json.loads(str(value or ""))
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def load_markets(
    db_path: Path, *, slug: str = "", event_date: str = ""
) -> tuple[list[MarketBinding], str]:
    """Load one current, active ZUUU/Chengdu event from the local registry."""

    if not db_path.exists():
        raise RuntimeError(f"registry_db_missing:{db_path}")
    uri = f"file:{urllib.parse.quote(str(db_path.resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=2.0)
    conn.row_factory = sqlite3.Row
    try:
        where = [
            "city_slug='chengdu'",
            "station_id='ZUUU'",
            "source_lane='amsc'",
            "source_eligible=1",
            "market_active=1",
            "market_accepting_orders=1",
            "COALESCE(yes_token,'')<>''",
            "COALESCE(no_token,'')<>''",
        ]
        params: list[Any] = []
        if slug:
            where.append("slug=?")
            params.append(slug)
        if event_date:
            where.append("event_date=?")
            params.append(event_date)
        rows = conn.execute(
            f"""SELECT slug,event_date,outcome,yes_token,no_token,settlement_timezone,registry_json
                FROM reported_trajectory_market_registry
                WHERE {' AND '.join(where)}
                ORDER BY event_date DESC,outcome""",
            params,
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        raise RuntimeError("active_zuuu_market_registry_missing")

    grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for row in rows:
        timezone_name = str(row["settlement_timezone"] or "").strip()
        try:
            local_day = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name)).date().isoformat()
        except Exception as exc:
            raise RuntimeError(f"settlement_timezone_invalid:{timezone_name}") from exc
        if not slug and not event_date and str(row["event_date"]) != local_day:
            continue
        grouped.setdefault((str(row["slug"]), str(row["event_date"])), []).append(row)
    if len(grouped) != 1:
        identities = ",".join(f"{key[0]}:{key[1]}" for key in sorted(grouped))
        raise RuntimeError(f"active_zuuu_event_not_unique:{identities or 'none'}")
    (selected_slug, _selected_date), selected = next(iter(grouped.items()))
    markets: list[MarketBinding] = []
    for row in selected:
        registry = _json_mapping(row["registry_json"])
        condition_id = str(registry.get("condition_id") or "").strip()
        if not condition_id:
            raise RuntimeError(f"condition_id_missing:{row['outcome']}")
        if str(registry.get("yes_token") or row["yes_token"]) != str(row["yes_token"]):
            raise RuntimeError(f"yes_token_lineage_mismatch:{row['outcome']}")
        if str(registry.get("no_token") or row["no_token"]) != str(row["no_token"]):
            raise RuntimeError(f"no_token_lineage_mismatch:{row['outcome']}")
        markets.append(
            MarketBinding(
                slug=selected_slug,
                event_id=str(registry.get("event_id") or "").strip(),
                event_date=str(row["event_date"]),
                outcome=str(row["outcome"]),
                condition_id=condition_id,
                yes_token=str(row["yes_token"]),
                no_token=str(row["no_token"]),
                kind=str(registry.get("kind") or "").strip(),
                lower=_optional_float(registry.get("lower")),
                upper=_optional_float(registry.get("upper")),
            )
        )
    if any(not row.event_id for row in markets):
        raise RuntimeError("event_id_lineage_missing")
    return markets, str(selected[0]["settlement_timezone"])


def _fetch_json(url: str, *, timeout: float) -> Mapping[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "weather-zuuu-balance-monitor/1"},
    )
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(request, timeout=timeout, context=ssl_context) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, Mapping):
        raise RuntimeError("clob_fee_payload_invalid")
    return payload


def fetch_official_fees(
    markets: Sequence[MarketBinding], *, clob_host: str, timeout: float
) -> tuple[dict[str, FeeSpec], dict[str, float], dict[str, Any]]:
    """Read and cross-check maker-fee and tick metadata for every condition."""

    host = str(clob_host).rstrip("/")

    def one(row: MarketBinding) -> tuple[MarketBinding, FeeSpec, dict[str, Any]]:
        condition = urllib.parse.quote(row.condition_id, safe="")
        info = _fetch_json(f"{host}/clob-markets/{condition}", timeout=timeout)
        if str(info.get("c") or "").lower() != row.condition_id.lower():
            raise RuntimeError(f"clob_condition_identity_mismatch:{row.outcome}")
        token_rows = [item for item in info.get("t") or [] if isinstance(item, Mapping)]
        actual_tokens = {str(item.get("t") or "") for item in token_rows}
        if actual_tokens != {row.yes_token, row.no_token}:
            raise RuntimeError(f"clob_condition_token_mismatch:{row.outcome}")
        fee_data = info.get("fd") if isinstance(info.get("fd"), Mapping) else {}
        if fee_data.get("r") is None or fee_data.get("e") is None or fee_data.get("to") is not True:
            raise RuntimeError(f"clob_fee_evidence_missing:{row.outcome}")
        tick_size = float(info.get("mts") or 0.0)
        if not math.isfinite(tick_size) or not 0.0 < tick_size < 1.0:
            raise RuntimeError(f"clob_tick_evidence_missing:{row.outcome}")
        fee = FeeSpec(
            rate=float(fee_data["r"]),
            exponent=float(fee_data["e"]),
            taker_only=True,
        )
        return row, fee, {
            "condition_id": row.condition_id,
            "outcome": row.outcome,
            "rate": fee.rate,
            "exponent": fee.exponent,
            "taker_only": fee.taker_only,
            "maker_fee_rate": 0.0,
            "tick_size": tick_size,
            "minimum_order_shares": float(info.get("mos") or 0.0),
            "accepting_orders": info.get("ao") is True,
            "source": "official_clob_market_info",
        }

    results: list[tuple[MarketBinding, FeeSpec, dict[str, Any]]] = []
    with ThreadPoolExecutor(max_workers=min(4, max(1, len(markets)))) as pool:
        futures = {pool.submit(one, row): row for row in markets}
        for future in as_completed(futures):
            results.append(future.result())
    fees: dict[str, FeeSpec] = {}
    ticks: dict[str, float] = {}
    evidence: dict[str, Any] = {}
    for row, fee, detail in results:
        if not detail["accepting_orders"]:
            raise RuntimeError(f"clob_market_not_accepting_orders:{row.outcome}")
        fees[row.yes_token] = fee
        fees[row.no_token] = fee
        ticks[row.yes_token] = float(detail["tick_size"])
        ticks[row.no_token] = float(detail["tick_size"])
        evidence[row.condition_id] = detail
    return fees, ticks, evidence


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    try:
        return f"{float(value):.{digits}f}"
    except (TypeError, ValueError):
        return str(value)


def _signals(evaluation: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows: list[Mapping[str, Any]] = [
        row for row in evaluation.get("pairs") or [] if isinstance(row, Mapping)
    ]
    rows.extend(
        row
        for row in (evaluation.get("all_yes"), evaluation.get("all_no"))
        if isinstance(row, Mapping)
    )
    return [row for row in rows if row.get("signal") is True]


def render_text(
    evaluation: Mapping[str, Any],
    markets: Sequence[MarketBinding],
    *,
    timezone_name: str,
    minimum_edge: float,
    target_shares: float,
    fee_evidence: Mapping[str, Any],
) -> str:
    now = datetime.now(timezone.utc).astimezone(ZoneInfo(timezone_name))
    rates = sorted({(row.get("rate"), row.get("exponent")) for row in fee_evidence.values()})
    fee_text = ", ".join(
        f"maker=0（taker-only r={float(rate):g},e={float(exp):g}）" for rate, exp in rates
    )
    lines = [
        f"[{now.isoformat(timespec='seconds')}] ZUUU 成都 maker 挂单配平监控",
        f"事件: {markets[0].slug} | 桶数: {len(markets)} | 目标: {target_shares:g} 股 | "
        f"净利差阈值: {minimum_edge:.4f}/组合 | 费用: {fee_text}",
        "",
        "温度桶               YES挂价  NO挂价  挂价和   Maker费  净利差   前方排队量  信号",
    ]
    for row in evaluation.get("pairs") or []:
        legs = row.get("legs") or []
        by_side = {str(leg.get("side")): leg for leg in legs if isinstance(leg, Mapping)}
        lines.append(
            f"{str(row.get('outcome') or ''):<20} "
            f"{_fmt((by_side.get('YES') or {}).get('order_price')):>7} "
            f"{_fmt((by_side.get('NO') or {}).get('order_price')):>7} "
            f"{_fmt(row.get('maker_price_sum')):>7} "
            f"{_fmt(row.get('maker_fee_per_set_usdc')):>8} "
            f"{_fmt(row.get('net_edge_per_set_usdc')):>8} "
            f"{_fmt(row.get('queue_ahead_shares_total'), 2):>10}  "
            f"{'候选' if row.get('signal') else '-'}"
        )
    lines.extend(["", "跨桶组合             挂价成本     Maker含费     净利差   全成净利润  前方排队量  信号"])
    for key in ("all_yes", "all_no"):
        row = evaluation.get(key)
        if not isinstance(row, Mapping):
            lines.append(f"{key.upper():<20} BLOCKED: {evaluation.get('partition_reason')}")
            continue
        lines.append(
            f"{str(row.get('name')):<20} "
            f"{_fmt(row.get('maker_price_sum')):>11} "
            f"{_fmt(row.get('maker_all_in_per_set_usdc')):>12} "
            f"{_fmt(row.get('net_edge_per_set_usdc')):>8} "
            f"{_fmt(row.get('net_profit_if_all_filled_usdc')):>11} "
            f"{_fmt(row.get('queue_ahead_shares_total'), 2):>10}  "
            f"{'候选' if row.get('signal') else '-'}"
        )
    signals = _signals(evaluation)
    lines.extend(
        [
            "",
            f"当前候选: {len(signals)} | 仅表示各腿全成后的价差；挂单多腿不原子，未授权下单。",
        ]
    )
    return "\n".join(lines)


async def _text_ping(ws: Any, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        await ws.send("PING")


async def _monitor_session(args: argparse.Namespace) -> int:
    markets, timezone_name = await asyncio.to_thread(
        load_markets, Path(args.db), slug=args.slug, event_date=args.event_date
    )
    fees, ticks, fee_evidence = await asyncio.to_thread(
        fetch_official_fees, markets, clob_host=args.clob_host, timeout=args.metadata_timeout
    )
    official_minimum = max(float(row["minimum_order_shares"]) for row in fee_evidence.values())
    minimum_order_shares = max(float(args.minimum_order_shares), official_minimum)
    if args.shares + 1e-12 < minimum_order_shares:
        raise RuntimeError(
            f"target_shares_below_official_minimum:{args.shares:g}<{minimum_order_shares:g}"
        )
    token_ids = sorted({token for row in markets for token in (row.yes_token, row.no_token)})
    state = BookState(token_ids)
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    subscription = {"assets_ids": token_ids, "type": "market", "custom_feature_enabled": True}
    connect_kwargs = {
        "ssl": ssl_context,
        "proxy": None,
        "ping_interval": 20,
        "ping_timeout": 20,
        "open_timeout": 20,
        "close_timeout": 5,
        "max_size": 8 * 1024 * 1024,
    }
    deadline = time.monotonic() + float(args.initial_timeout)
    catalog_deadline = time.monotonic() + float(args.catalog_refresh_sec)
    last_render = 0.0
    last_version = -1
    async with websockets.connect(args.ws_url, **connect_kwargs) as ws:
        await ws.send(json.dumps(subscription, separators=(",", ":")))
        ping_task = asyncio.create_task(_text_ping(ws, float(args.text_ping_sec)))
        try:
            while True:
                timeout = float(args.refresh_sec)
                if not state.ready:
                    timeout = max(0.05, min(timeout, deadline - time.monotonic()))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                except asyncio.TimeoutError:
                    raw = ""
                for message in _iter_messages(str(raw)):
                    state.apply(message)
                if not state.ready and time.monotonic() >= deadline:
                    missing = sorted(state.expected - state.materialized)
                    raise RuntimeError(
                        f"initial_book_materialization_timeout:{len(state.materialized)}/{len(state.expected)}:"
                        f"missing={','.join(missing[:3])}"
                    )
                now = time.monotonic()
                if state.ready and not args.once and now >= catalog_deadline:
                    # Close a healthy socket deliberately so the outer loop
                    # reloads the current local-day registry and fee lineage.
                    return 0
                should_render = state.ready and (
                    args.once
                    or state.version != last_version and now - last_render >= float(args.refresh_sec)
                )
                if not should_render:
                    continue
                evaluation = build_complete_sets(
                    markets,
                    state.snapshot(),
                    fees,
                    ticks,
                    target_shares=float(args.shares),
                    minimum_edge=float(args.minimum_edge),
                    minimum_order_shares=minimum_order_shares,
                )
                payload = {
                    "schema": "zuuu_maker_balance_arbitrage_monitor_v1",
                    "observed_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
                    "source": "official_polymarket_clob_websocket",
                    "fee_source": "official_polymarket_clob_market_info_taker_only",
                    "order_style": "maker_limit_buy",
                    "quote_policy": "improve_best_bid_one_tick_without_crossing_best_ask",
                    "fill_contingent": True,
                    "read_only": True,
                    "writes_ticks": False,
                    "submits_orders": False,
                    "slug": markets[0].slug,
                    "event_date": markets[0].event_date,
                    "target_shares": float(args.shares),
                    "minimum_edge_per_set_usdc": float(args.minimum_edge),
                    "minimum_order_shares": minimum_order_shares,
                    "fee_evidence": fee_evidence,
                    "evaluation": evaluation,
                }
                if args.json_lines:
                    print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
                else:
                    output = render_text(
                        evaluation,
                        markets,
                        timezone_name=timezone_name,
                        minimum_edge=float(args.minimum_edge),
                        target_shares=float(args.shares),
                        fee_evidence=fee_evidence,
                    )
                    if sys.stdout.isatty() and not args.once:
                        print("\033[2J\033[H", end="")
                    print(output, flush=True)
                last_render = now
                last_version = state.version
                if args.once:
                    return 0
        finally:
            ping_task.cancel()


async def monitor(args: argparse.Namespace) -> int:
    """Run one snapshot or a fail-closed reconnecting read-only monitor."""

    if args.once:
        return await _monitor_session(args)
    backoff = 1.0
    while True:
        try:
            await _monitor_session(args)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                f"[zuuu_balance_monitor] RECONNECTING {type(exc).__name__}: {exc} "
                f"backoff_sec={backoff:g}",
                file=sys.stderr,
                flush=True,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 30.0)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="只读监控 ZUUU 成都 maker 挂单 YES+NO / 全桶完整组合净利差"
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--slug", default="", help="默认自动选择成都当地今日唯一活跃事件")
    parser.add_argument("--event-date", default="", help="可选 YYYY-MM-DD")
    parser.add_argument("--shares", type=float, default=5.0, help="每条腿等股数深度，默认 5")
    parser.add_argument(
        "--minimum-edge", type=float, default=0.005,
        help="maker 各腿全成后每份完整组合的最小净利差，默认 0.005 USDC",
    )
    parser.add_argument("--minimum-order-shares", type=float, default=1.0)
    parser.add_argument("--refresh-sec", type=float, default=0.25)
    parser.add_argument("--initial-timeout", type=float, default=20.0)
    parser.add_argument("--metadata-timeout", type=float, default=8.0)
    parser.add_argument("--text-ping-sec", type=float, default=5.0)
    parser.add_argument(
        "--catalog-refresh-sec", type=float, default=300.0,
        help="重连并重载当地当日 registry/费用证据的间隔，默认 300 秒",
    )
    parser.add_argument("--ws-url", default=os.environ.get("PM_MARKET_WS_URL", DEFAULT_WS_URL))
    parser.add_argument("--clob-host", default=os.environ.get("PM_CLOB_HOST", DEFAULT_CLOB_HOST))
    parser.add_argument("--json-lines", action="store_true", help="输出机器可读 JSON Lines")
    parser.add_argument("--once", action="store_true", help="完整物化一次盘口后输出并退出")
    args = parser.parse_args(argv)
    if args.shares <= 0 or args.minimum_order_shares <= 0:
        parser.error("shares and minimum-order-shares must be positive")
    if args.minimum_edge < 0:
        parser.error("minimum-edge must be non-negative")
    if (
        args.refresh_sec <= 0
        or args.initial_timeout <= 0
        or args.metadata_timeout <= 0
        or args.catalog_refresh_sec <= 0
    ):
        parser.error("timeout and refresh values must be positive")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return asyncio.run(monitor(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"[zuuu_balance_monitor] BLOCKED {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
