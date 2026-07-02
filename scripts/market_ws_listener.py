#!/usr/bin/env python3
"""Polymarket market WebSocket listener for weather token quotes.

The minute REST refresh remains a fallback. This process keeps
event_market_ticks fresh from the official CLOB market channel.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import sqlite3
import ssl
import sys
import time
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

import certifi
import websockets

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scan_weather_market as swm


WS_URL = os.environ.get(
    "PM_MARKET_WS_URL",
    "wss://ws-subscriptions-clob.polymarket.com/ws/market",
)
WS_PROXY_MODE = str(os.environ.get("PM_MARKET_WS_PROXY", "none") or "none").strip()
HEARTBEAT_PATH = Path(
    os.environ.get("PM_MARKET_WS_HEARTBEAT_PATH", str(swm.DATA_DIR / "market_ws_heartbeat.json"))
)


def _env_truthy(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return bool(default)
    text = str(raw).strip().lower()
    if not text:
        return bool(default)
    if text in {"1", "true", "yes", "on", "y"}:
        return True
    if text in {"0", "false", "no", "off", "n"}:
        return False
    return bool(default)


def _env_float(name: str, default: float) -> float:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except Exception:
        return float(default)
    return value if math.isfinite(value) else float(default)


def _live_ssot_writer_active() -> bool:
    if not _env_truthy("PM_MARKET_WS_DEFER_DURING_LIVE_SSOT", True):
        return False
    lock_dir = ROOT / "data" / "live_ssot_writer.lock"
    if not lock_dir.is_dir():
        return False
    pid_text = ""
    try:
        pid_text = (lock_dir / "pid").read_text(encoding="utf-8").strip()
        pid = int(pid_text)
    except Exception:
        return True
    if pid <= 1:
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except Exception:
        return True


def _live_ssot_defer_sleep_sec() -> float:
    return max(0.05, min(1.0, _env_float("PM_MARKET_WS_LIVE_SSOT_DEFER_SLEEP_SEC", 0.2)))


@dataclass(frozen=True)
class TokenBinding:
    token_id: str
    slug: str
    event_date: str
    outcome: str
    side: str


class TickCoalescer:
    """Drop repeated identical quote rows while periodically refreshing freshness."""

    def __init__(self, same_quote_sec: float, same_quote_tolerance: float = 0.0) -> None:
        try:
            value = float(same_quote_sec)
        except Exception:
            value = 0.0
        self.same_quote_sec = max(0.0, value)
        try:
            tolerance = float(same_quote_tolerance)
        except Exception:
            tolerance = 0.0
        self.same_quote_tolerance = max(0.0, tolerance)
        self.skipped = 0
        self._last: Dict[
            Tuple[str, str, str, str],
            Tuple[Tuple[Optional[float], Optional[float], Optional[float], float, float], float],
        ] = {}

    @staticmethod
    def _key(row: Mapping[str, Any]) -> Tuple[str, str, str, str]:
        return (
            str(row.get("slug") or ""),
            str(row.get("event_date") or ""),
            str(row.get("outcome") or ""),
            str(row.get("side") or ""),
        )

    @staticmethod
    def _signature(row: Mapping[str, Any]) -> Tuple[Optional[float], Optional[float], Optional[float], float, float]:
        return (
            _to_float(row.get("best_bid")),
            _to_float(row.get("best_ask")),
            _to_float(row.get("spread")),
            float(row.get("bid_depth_topN") or 0.0),
            float(row.get("ask_depth_topN") or 0.0),
        )

    @staticmethod
    def _sig_almost_equal(
        prev: Tuple[Optional[float], Optional[float], Optional[float], float, float],
        curr: Tuple[Optional[float], Optional[float], Optional[float], float, float],
        *,
        tolerance: float,
    ) -> bool:
        prev_bid, prev_ask, prev_spread, prev_bid_depth, prev_ask_depth = prev
        curr_bid, curr_ask, curr_spread, curr_bid_depth, curr_ask_depth = curr
        for p_v, c_v in (
            (prev_bid, curr_bid),
            (prev_ask, curr_ask),
            (prev_spread, curr_spread),
            (prev_bid_depth, curr_bid_depth),
            (prev_ask_depth, curr_ask_depth),
        ):
            if p_v is None and c_v is None:
                continue
            if p_v is None or c_v is None:
                return False
            if abs(float(p_v) - float(c_v)) > float(tolerance):
                return False
        return True

    def filter_rows(self, rows: Iterable[Dict[str, Any]], *, now: Optional[float] = None) -> List[Dict[str, Any]]:
        if self.same_quote_sec <= 0:
            return list(rows)
        current = time.monotonic() if now is None else float(now)
        out: List[Dict[str, Any]] = []
        for row in rows:
            key = self._key(row)
            sig = self._signature(row)
            previous = self._last.get(key)
            if (
                previous is not None
                and current - previous[1] < self.same_quote_sec
                and self._sig_almost_equal(previous[0], sig, tolerance=self.same_quote_tolerance)
            ):
                self.skipped += 1
                continue
            self._last[key] = (sig, current)
            out.append(row)
        return out


def _ws_proxy_candidates(mode_raw: str) -> List[Tuple[str, Any]]:
    def _explicit_http_proxy() -> Optional[str]:
        for key in ("PM_MARKET_WS_HTTP_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
            raw = str(os.environ.get(key) or "").strip()
            if not raw:
                continue
            low = raw.lower()
            if low.startswith("http://") or low.startswith("https://"):
                return raw
        return None

    explicit_http_proxy = _explicit_http_proxy()
    mode = str(mode_raw or "").strip().lower()
    if mode in {"", "none", "off", "false", "0", "direct"}:
        out: List[Tuple[str, Any]] = [("direct", None)]
        if explicit_http_proxy:
            out.append(("http_proxy_env", explicit_http_proxy))
        return out
    if mode in {"auto", "system", "env"}:
        return [("auto", "__auto__")]
    # Explicit proxy URL (e.g. http://127.0.0.1:7897)
    return [("custom", mode_raw), ("direct", None), ("auto", "__auto__")]


def _to_float(value: Any) -> Optional[float]:
    parsed = swm.to_float(value)
    if parsed is None or not math.isfinite(parsed):
        return None
    return float(parsed)


def _normalize_csv_set(value: Any, *, fallback: Iterable[str]) -> set:
    if not isinstance(value, str):
        value = str(value or "").strip()
    if not value:
        return {v.strip().lower() for v in fallback if str(v).strip()}
    out = {v.strip().lower() for v in value.split(",")}
    return {v for v in out if v}


def _load_token_bindings(days_ahead: int, max_assets: int) -> Dict[str, TokenBinding]:
    swm.init_db()
    modifier = f"+{max(0, int(days_ahead))} day"
    today_et = swm.today_date_str("America/New_York")
    sql = """
        WITH fact_latest AS (
            SELECT h.slug, h.event_date, h.fact_as_of
            FROM event_fact_latest l
            JOIN event_fact_history h
              ON h.id = l.fact_id
            WHERE h.run_date = ?
              AND h.event_date >= date(?, '-1 day')
        ),
        recent_decisions AS (
            SELECT
                slug,
                event_date,
                MAX(created_at) AS decision_at,
                MAX(
                    CASE
                        WHEN COALESCE(will_order, 0) = 1 THEN 1
                        WHEN reason_codes_json LIKE '%weather_rules_contract_incomplete%' THEN 1
                        WHEN reason_codes_json LIKE '%market_tick_stale%' THEN 1
                        WHEN reason_codes_json LIKE '%orderbook_missing%' THEN 1
                        WHEN CAST(COALESCE(json_extract(
                            CASE WHEN json_valid(COALESCE(details_json,'{}')) THEN details_json ELSE '{}' END,
                            '$.exec_edge_entry'
                        ), 0) AS REAL) > 0 THEN 1
                        ELSE 0
                    END
                ) AS decision_priority
            FROM auto_trade_decisions
            WHERE slug IS NOT NULL
              AND slug != ''
              AND event_date IS NOT NULL
              AND event_date != ''
              AND julianday(created_at) >= julianday('now', '-60 minutes')
            GROUP BY slug, event_date
        )
        SELECT
            mt.slug,
            mt.event_date,
            mt.outcome,
            mt.yes_token,
            mt.no_token
        FROM market_tokens mt
        LEFT JOIN fact_latest fl
          ON fl.slug = mt.slug AND fl.event_date = mt.event_date
        LEFT JOIN recent_decisions rd
          ON rd.slug = mt.slug AND rd.event_date = mt.event_date
        WHERE mt.event_date IS NOT NULL
          AND mt.event_date != ''
          AND mt.event_date >= date(?, '-1 day')
          AND mt.event_date <= date(?, ?)
          AND COALESCE(mt.market_closed, 0) = 0
          AND COALESCE(mt.market_archived, 0) = 0
          AND COALESCE(mt.market_active, 1) != 0
        ORDER BY
          CASE
            WHEN mt.event_date = ? THEN 0
            WHEN mt.event_date = date(?, '+1 day') THEN 1
            ELSE 2
          END ASC,
          COALESCE(rd.decision_priority, 0) DESC,
          CASE WHEN fl.slug IS NOT NULL THEN 0 ELSE 1 END ASC,
          COALESCE(rd.decision_at, fl.fact_as_of, mt.updated_at, '') DESC,
          COALESCE(mt.updated_at, '') DESC
    """
    bindings: Dict[str, TokenBinding] = {}
    try:
        with swm.open_db_ro_fast(timeout_sec=2.0, busy_timeout_ms=2000) as conn:
            rows = conn.execute(
                sql,
                (
                    today_et,
                    today_et,
                    today_et,
                    today_et,
                    modifier,
                    today_et,
                    today_et,
                ),
            ).fetchall()
    except Exception:
        rows = []
    for row in rows:
        slug = str(row[0] or "").strip()
        event_date = str(row[1] or "").strip().split("T", 1)[0]
        outcome = str(row[2] or "").strip()
        if not slug or not event_date or not outcome:
            continue
        for side, token_raw in (("YES", row[3]), ("NO", row[4])):
            token_id = str(token_raw or "").strip()
            if not token_id or token_id in bindings:
                continue
            bindings[token_id] = TokenBinding(
                token_id=token_id,
                slug=slug,
                event_date=event_date,
                outcome=outcome,
                side=side,
            )
            if max_assets > 0 and len(bindings) >= max_assets:
                return bindings
    return bindings


def _quote_row(
    binding: TokenBinding,
    *,
    best_bid: Optional[float],
    best_ask: Optional[float],
    spread: Optional[float],
    bid_depth: float,
    ask_depth: float,
    created_at: str,
    source_detail: str,
) -> Dict[str, Any]:
    mid = None
    if best_bid is not None and best_ask is not None:
        mid = (float(best_bid) + float(best_ask)) / 2.0
    if spread is None and best_bid is not None and best_ask is not None:
        spread = max(float(best_ask) - float(best_bid), 0.0)
    return {
        "run_date": swm.today_date_str("America/New_York"),
        "event_date": binding.event_date,
        "slug": binding.slug,
        "outcome": binding.outcome,
        "side": binding.side,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread": spread,
        "bid_depth_topN": float(bid_depth or 0.0),
        "ask_depth_topN": float(ask_depth or 0.0),
        "source": "clob_ws",
        "source_detail": source_detail,
        "created_at": created_at,
    }


def rows_from_ws_message(
    message: Mapping[str, Any],
    bindings: Mapping[str, TokenBinding],
    *,
    depth_levels: int,
    include_source_details: set,
    created_at: Optional[str] = None,
) -> List[Dict[str, Any]]:
    event_type = str(message.get("event_type") or "").strip()
    created = created_at or swm.utc_now_iso()
    out: List[Dict[str, Any]] = []
    if event_type == "book":
        if "book" not in include_source_details:
            return []
        token_id = str(message.get("asset_id") or "").strip()
        binding = bindings.get(token_id)
        if not binding:
            return []
        best_bid, best_ask, spread, bid_depth, ask_depth, _bid_size, _ask_size = swm.best_bid_ask_and_depth(
            dict(message),
            levels=int(depth_levels),
        )
        out.append(
            _quote_row(
                binding,
                best_bid=best_bid,
                best_ask=best_ask,
                spread=spread,
                bid_depth=bid_depth,
                ask_depth=ask_depth,
                created_at=created,
                source_detail="book",
            )
        )
        return out
    if event_type == "price_change":
        if "price_change" not in include_source_details:
            return []
        changes = message.get("price_changes") or []
        if not isinstance(changes, list):
            return []
        for change in changes:
            if not isinstance(change, Mapping):
                continue
            token_id = str(change.get("asset_id") or "").strip()
            binding = bindings.get(token_id)
            if not binding:
                continue
            best_bid = _to_float(change.get("best_bid"))
            best_ask = _to_float(change.get("best_ask"))
            out.append(
                _quote_row(
                    binding,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    spread=None,
                    bid_depth=0.0,
                    ask_depth=0.0,
                    created_at=created,
                    source_detail="price_change",
                )
            )
        return out
    if event_type == "best_bid_ask":
        if "best_bid_ask" not in include_source_details:
            return []
        token_id = str(message.get("asset_id") or "").strip()
        binding = bindings.get(token_id)
        if not binding:
            return []
        out.append(
            _quote_row(
                binding,
                best_bid=_to_float(message.get("best_bid")),
                best_ask=_to_float(message.get("best_ask")),
                spread=_to_float(message.get("spread")),
                bid_depth=0.0,
                ask_depth=0.0,
                created_at=created,
                source_detail="best_bid_ask",
            )
        )
    return out


def _iter_payload_messages(raw: str) -> Iterable[Mapping[str, Any]]:
    if not raw or raw in {"PONG", "PING"}:
        return []
    try:
        payload = json.loads(raw)
    except Exception:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, Mapping)]
    if isinstance(payload, Mapping):
        return [payload]
    return []


def _write_ticks(rows: List[Dict[str, Any]]) -> int:
    if not rows:
        return 0
    if _live_ssot_writer_active():
        time.sleep(_live_ssot_defer_sleep_sec())
    swm.init_db()
    attempts = int(os.environ.get("PM_MARKET_WS_WRITE_RETRY_ATTEMPTS", "2") or "2")
    attempts = max(1, attempts)
    busy_ms = int(os.environ.get("PM_MARKET_WS_DB_BUSY_TIMEOUT_MS", "250") or "250")
    busy_ms = max(50, busy_ms)
    connect_timeout = _env_float("PM_MARKET_WS_DB_CONNECT_TIMEOUT_SEC", 0.3)
    connect_timeout = max(0.05, min(2.0, connect_timeout))
    base_sleep = float(os.environ.get("PM_MARKET_WS_WRITE_RETRY_BASE_SEC", "0.08") or "0.08")
    base_sleep = max(0.01, base_sleep)
    last_exc: Optional[Exception] = None
    for attempt in range(attempts):
        try:
            with closing(swm.open_db(connect_timeout_sec=connect_timeout, busy_timeout_ms=busy_ms)) as conn:
                with conn:
                    written = _write_ticks_with_connection(conn, rows)
                return int(written)
        except sqlite3.OperationalError as exc:
            last_exc = exc
            if "locked" not in str(exc).lower() or attempt >= attempts - 1:
                raise
            time.sleep(min(0.5, base_sleep * (attempt + 1)))
    if last_exc is not None:
        raise last_exc
    return 0


def _write_ticks_with_connection(conn: sqlite3.Connection, rows: List[Dict[str, Any]]) -> int:
    now_ts = time.time()
    if swm._should_run_market_tick_cleanup(now_ts):
        cleanup_iso = swm.utc_now_iso()
        cleanup_deleted = swm._cleanup_expired_market_ticks(
            conn,
            now_iso=cleanup_iso,
        )
        if cleanup_deleted > 0:
            keep_days = swm._env_int("PM_DB_KEEP_TICKS_DAYS")
            print(
                f"[market_ws] event_market_ticks_cleanup removed={cleanup_deleted} "
                f"older_than_days={int(keep_days or 0)}"
            )
        swm._mark_market_tick_cleanup_run(now_ts)
    start_changes = int(conn.total_changes)
    conn.executemany(
        """
        INSERT INTO event_market_ticks (
            run_date, event_date, slug, outcome, side,
            best_bid, best_ask, mid, spread,
            bid_depth_topN, ask_depth_topN, source, source_detail, created_at
        )
        SELECT
            :run_date, :event_date, :slug, :outcome, :side,
            :best_bid, :best_ask, :mid, :spread,
            :bid_depth_topN, :ask_depth_topN, :source, :source_detail, :created_at
        WHERE NOT EXISTS (
            SELECT 1
            FROM event_market_ticks
            WHERE slug IS :slug
              AND outcome IS :outcome
              AND side IS :side
              AND source IS :source
              AND source_detail IS :source_detail
              AND created_at IS :created_at
        )
        """,
        rows,
    )
    return int(conn.total_changes - start_changes)


def _write_heartbeat(payload: Dict[str, Any]) -> None:
    HEARTBEAT_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = HEARTBEAT_PATH.with_suffix(HEARTBEAT_PATH.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=True, sort_keys=True), encoding="utf-8")
    tmp.replace(HEARTBEAT_PATH)


async def _ping_loop(ws: Any, interval_sec: float) -> None:
    while True:
        await asyncio.sleep(interval_sec)
        try:
            await ws.send("PING")
        except Exception:
            return


async def listen_once(
    args: argparse.Namespace,
    coalescer: Optional[TickCoalescer] = None,
) -> int:
    bindings = _load_token_bindings(args.days_ahead, args.max_assets)
    asset_ids = sorted(bindings.keys())
    if not asset_ids:
        now_ts = time.time()
        if swm._should_run_market_tick_cleanup(now_ts):
            try:
                with closing(swm.open_db(connect_timeout_sec=2.0, busy_timeout_ms=2000)) as conn:
                    with conn:
                        cleanup_iso = swm.utc_now_iso()
                        cleanup_deleted = swm._cleanup_expired_market_ticks(
                            conn,
                            now_iso=cleanup_iso,
                        )
                        if cleanup_deleted > 0:
                            keep_days = swm._env_int("PM_DB_KEEP_TICKS_DAYS")
                            print(
                                f"[market_ws] event_market_ticks_cleanup removed={cleanup_deleted} "
                                f"older_than_days={int(keep_days or 0)}"
                            )
                        swm._mark_market_tick_cleanup_run(now_ts)
            except Exception:
                pass
        _write_heartbeat(
            {
                "ok": False,
                "status": "no_assets",
                "updated_at": swm.utc_now_iso(),
                "assets": 0,
            }
        )
        await asyncio.sleep(args.reload_sec)
        return 0

    if coalescer is None:
        coalescer = TickCoalescer(
            float(args.coalesce_same_quote_sec),
            same_quote_tolerance=float(args.coalesce_same_quote_tolerance),
        )

    shard_size = max(1, int(getattr(args, "shard_size", 400) or 400))
    chunks = [asset_ids[i : i + shard_size] for i in range(0, len(asset_ids), shard_size)]
    if len(chunks) > 1:
        _write_heartbeat(
            {
                "ok": True,
                "status": "connecting",
                "updated_at": swm.utc_now_iso(),
                "assets": len(asset_ids),
                "shards": len(chunks),
                "shard_size": shard_size,
            }
        )
        tasks = [
            asyncio.create_task(
                _listen_asset_batch(
                    args,
                    chunk,
                    bindings,
                    total_assets=len(asset_ids),
                    shard_index=idx,
                    shard_count=len(chunks),
                    coalescer=coalescer,
                )
            )
            for idx, chunk in enumerate(chunks)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        total_written = 0
        total_received = 0
        total_skipped = 0
        errors: List[str] = []
        for result in results:
            if isinstance(result, Exception):
                errors.append(f"{result.__class__.__name__}: {str(result)[:160]}")
            else:
                written, received, skipped = result
                total_written += int(written or 0)
                total_received += int(received or 0)
                total_skipped += int(skipped or 0)
        _write_heartbeat(
            {
                "ok": len(errors) < len(chunks),
                "status": "connected" if len(errors) < len(chunks) else "error",
                "updated_at": swm.utc_now_iso(),
                "assets": len(asset_ids),
                "shards": len(chunks),
                "shard_size": shard_size,
                "ticks_written": total_written,
                "ticks_received": total_received,
                "ticks_skipped_same_quote": total_skipped,
                "coalesce_same_quote_sec": float(args.coalesce_same_quote_sec),
                "errors": errors[:3],
            }
        )
        if len(errors) >= len(chunks):
            raise RuntimeError("; ".join(errors[:2]) or "all websocket shards failed")
        return total_written

    written, _received, _skipped = await _listen_asset_batch(
        args,
        asset_ids,
        bindings,
        total_assets=len(asset_ids),
        shard_index=0,
        shard_count=1,
        coalescer=coalescer,
    )
    return written


async def _listen_asset_batch(
    args: argparse.Namespace,
    asset_ids: List[str],
    bindings: Mapping[str, TokenBinding],
    *,
    coalescer: Optional[TickCoalescer] = None,
    total_assets: int,
    shard_index: int,
    shard_count: int,
) -> Tuple[int, int, int]:
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    pending: List[Dict[str, Any]] = []
    written = 0
    received = 0
    last_flush = time.monotonic()
    last_hb = 0.0
    if coalescer is None:
        coalescer = TickCoalescer(
            float(args.coalesce_same_quote_sec),
            same_quote_tolerance=float(args.coalesce_same_quote_tolerance),
        )
    subscription = {
        "assets_ids": asset_ids,
        "type": "market",
        "custom_feature_enabled": True,
    }
    base_connect_kwargs: Dict[str, Any] = {
        "ssl": ssl_context,
        "ping_interval": 20,
        "ping_timeout": 20,
        "open_timeout": 20,
        "close_timeout": 5,
        "max_size": 8 * 1024 * 1024,
    }
    proxy_candidates = _ws_proxy_candidates(WS_PROXY_MODE)
    if shard_index > 0:
        await asyncio.sleep(min(2.0, shard_index * 0.2))
    connect_errors: List[str] = []
    for proxy_label, proxy_value in proxy_candidates:
        connect_kwargs = dict(base_connect_kwargs)
        if proxy_value != "__auto__":
            connect_kwargs["proxy"] = proxy_value
        try:
            async with websockets.connect(WS_URL, **connect_kwargs) as ws:
                await ws.send(json.dumps(subscription))
                ping_task = asyncio.create_task(_ping_loop(ws, args.text_ping_sec))
                try:
                    deadline = time.monotonic() + args.reload_sec
                    while True:
                        timeout = max(0.5, min(args.flush_sec, deadline - time.monotonic()))
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
                        except asyncio.TimeoutError:
                            raw = ""
                        if raw:
                            for message in _iter_payload_messages(str(raw)):
                                rows = rows_from_ws_message(
                                    message,
                                    bindings,
                                    depth_levels=args.depth_levels,
                                    include_source_details=args.include_source_details,
                                )
                                received += len(rows)
                                pending.extend(coalescer.filter_rows(rows))
                        now = time.monotonic()
                        if pending and _live_ssot_writer_active():
                            await asyncio.sleep(_live_ssot_defer_sleep_sec())
                            continue
                        if pending and (len(pending) >= args.batch_size or now - last_flush >= args.flush_sec):
                            batch = pending
                            pending = []
                            written += _write_ticks(batch)
                            last_flush = now
                        if now - last_hb >= args.heartbeat_sec:
                            _write_heartbeat(
                                {
                                    "ok": True,
                                    "status": "connected",
                                    "updated_at": swm.utc_now_iso(),
                                    "assets": int(total_assets),
                                    "shard_assets": len(asset_ids),
                                    "shard_index": int(shard_index),
                                    "shards": int(shard_count),
                                    "ticks_written": written,
                                    "ticks_received": received,
                                    "ticks_skipped_same_quote": coalescer.skipped,
                                    "coalesce_same_quote_sec": float(coalescer.same_quote_sec),
                                    "ws_proxy_mode": WS_PROXY_MODE,
                                    "ws_proxy_active": proxy_label,
                                }
                            )
                            last_hb = now
                        if now >= deadline:
                            break
                finally:
                    ping_task.cancel()
                    if pending:
                        for _ in range(3):
                            if not _live_ssot_writer_active():
                                break
                            await asyncio.sleep(_live_ssot_defer_sleep_sec())
                        written += _write_ticks(pending)
            return written, received, coalescer.skipped
        except Exception as exc:
            connect_errors.append(f"{proxy_label}:{exc.__class__.__name__}:{str(exc)[:140]}")
            continue
    raise RuntimeError("; ".join(connect_errors[:3]) or "ws_connect_failed")


async def run_forever(args: argparse.Namespace) -> None:
    backoff = 1.0
    coalescer = TickCoalescer(
        float(args.coalesce_same_quote_sec),
        same_quote_tolerance=float(args.coalesce_same_quote_tolerance),
    )
    while True:
        try:
            count = await listen_once(args, coalescer=coalescer)
            print(f"[market_ws] cycle_ok ticks={count}", flush=True)
            backoff = 1.0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _write_heartbeat(
                {
                    "ok": False,
                    "status": "error",
                    "error": f"{exc.__class__.__name__}: {str(exc)[:160]}",
                    "updated_at": swm.utc_now_iso(),
                }
            )
            print(f"[market_ws] error {exc.__class__.__name__}: {exc}", flush=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.7, 30.0)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stream Polymarket market WebSocket ticks into archive.db.")
    parser.add_argument("--days-ahead", type=int, default=int(os.environ.get("PM_MARKET_WS_DAYS_AHEAD", "3")))
    parser.add_argument("--max-assets", type=int, default=int(os.environ.get("PM_MARKET_WS_MAX_ASSETS", "1600")))
    parser.add_argument("--shard-size", type=int, default=int(os.environ.get("PM_MARKET_WS_SHARD_SIZE", "400")))
    parser.add_argument("--depth-levels", type=int, default=int(os.environ.get("PM_MARKET_WS_DEPTH_LEVELS", "5")))
    parser.add_argument("--reload-sec", type=float, default=float(os.environ.get("PM_MARKET_WS_RELOAD_SEC", "60")))
    parser.add_argument("--flush-sec", type=float, default=float(os.environ.get("PM_MARKET_WS_FLUSH_SEC", "1.0")))
    parser.add_argument("--batch-size", type=int, default=int(os.environ.get("PM_MARKET_WS_BATCH_SIZE", "100")))
    parser.add_argument("--heartbeat-sec", type=float, default=float(os.environ.get("PM_MARKET_WS_HEARTBEAT_SEC", "5")))
    parser.add_argument("--text-ping-sec", type=float, default=float(os.environ.get("PM_MARKET_WS_TEXT_PING_SEC", "10")))
    parser.add_argument(
        "--coalesce-same-quote-sec",
        type=float,
        default=float(os.environ.get("PM_MARKET_WS_COALESCE_SAME_QUOTE_SEC", "10")),
        help="Skip unchanged quote rows within this many seconds; 0 disables coalescing.",
    )
    parser.add_argument(
        "--coalesce-same-quote-tol",
        type=float,
        default=float(os.environ.get("PM_MARKET_WS_COALESCE_SAME_QUOTE_TOL", "0.0")),
        help="Quote/Depth tolerance for coalescing unchanged rows; default 0 keeps exact-match behavior.",
    )
    parser.add_argument(
        "--include-source-details",
        type=str,
        default=str(os.environ.get("PM_MARKET_WS_INCLUDE_SOURCE_DETAILS", "price_change,book")),
        help="Comma-separated websocket event types to store: price_change,book,best_bid_ask",
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    args.coalesce_same_quote_tolerance = float(args.coalesce_same_quote_tol)
    args.include_source_details = _normalize_csv_set(
        args.include_source_details,
        fallback=("price_change", "book", "best_bid_ask"),
    )
    return args


def main() -> None:
    args = parse_args()
    def _ignore_websockets_close_noise(loop: asyncio.AbstractEventLoop, context: Dict[str, Any]) -> None:
        exc = context.get("exception")
        if isinstance(exc, AttributeError) and "recv_messages" in str(exc):
            return
        loop.default_exception_handler(context)

    loop = asyncio.new_event_loop()
    loop.set_exception_handler(_ignore_websockets_close_noise)
    asyncio.set_event_loop(loop)
    try:
        if args.once:
            loop.run_until_complete(listen_once(args))
        else:
            loop.run_until_complete(run_forever(args))
    finally:
        loop.close()


if __name__ == "__main__":
    main()
