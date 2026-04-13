#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "archive.db"
STATE_PATH = DATA_DIR / "daily_trade_sms_summary_state.json"
ENV_PATH = ROOT / ".env"
COMBO_ROLE_ALIAS_MAP = {
    "yes_top": "combo_yes_peak",
    "combo_yes_peak": "combo_yes_peak",
    "no_near": "combo_no_adjacent",
    "combo_no_adjacent": "combo_no_adjacent",
    "no_far": "combo_no_remote",
    "combo_no_remote": "combo_no_remote",
}
COMBO_ROLE_LABELS = {
    "combo_yes_peak": "combo_yes_peak",
    "combo_no_adjacent": "combo_no_adjacent",
    "combo_no_remote": "combo_no_remote",
}


def _load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("'").strip('"')
        if key and key not in __import__("os").environ:
            __import__("os").environ[key] = value


def _env_bool(key: str, default: bool) -> bool:
    raw = __import__("os").environ.get(key)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ")


def _send_sms(message: str, recipient: str) -> bool:
    safe = _escape_applescript(str(message or "").strip())
    if not safe or not recipient:
        return False
    script = (
        'tell application "Messages"\n'
        f'  set targetBuddy to "{recipient}"\n'
        '  set targetService to missing value\n'
        '  try\n'
        '    set targetService to first service whose service type is SMS\n'
        '  end try\n'
        '  if targetService is missing value then\n'
        '    try\n'
        '      set targetService to first service whose service type is iMessage\n'
        '    end try\n'
        '  end if\n'
        '  if targetService is missing value then\n'
        '    error "no messaging service"\n'
        '  end if\n'
        '  send "'
        + safe
        + '" to buddy targetBuddy of targetService\n'
        'end tell'
    )
    try:
        res = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        print(f"[daily_sms] osascript error: {exc}")
        return False
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        print(f"[daily_sms] sms failed: {err}")
        return False
    return True


def _fmt_usdc(value: float) -> str:
    if not math.isfinite(value):
        return "$0.00"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _parse_iso_ts(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _pick_tick_mark(mid: Optional[float], bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    for value in (mid,):
        if value is None:
            continue
        try:
            val = float(value)
        except Exception:
            continue
        if math.isfinite(val):
            return val
    if bid is not None and ask is not None:
        try:
            bid_val = float(bid)
            ask_val = float(ask)
        except Exception:
            bid_val = None
            ask_val = None
        if (
            bid_val is not None
            and ask_val is not None
            and math.isfinite(bid_val)
            and math.isfinite(ask_val)
        ):
            return (bid_val + ask_val) / 2.0
    for value in (bid, ask):
        if value is None:
            continue
        try:
            val = float(value)
        except Exception:
            continue
        if math.isfinite(val):
            return val
    return None


def _slug_to_city(slug: str) -> str:
    text = str(slug or "").strip().lower()
    prefix = "highest-temperature-in-"
    marker = "-on-"
    if text.startswith(prefix) and marker in text:
        city = text[len(prefix): text.index(marker)]
    else:
        city = text
    city = city.replace("-", " ").strip()
    if not city:
        return "unknown"
    return " ".join(part.capitalize() for part in city.split())


def _load_state(path: Path) -> Dict[str, str]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _save_state(path: Path, payload: Dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2), encoding="utf-8")


def _local_tz_name() -> str:
    raw = str(__import__("os").environ.get("PM_SMS_DAILY_SUMMARY_TZ", "Asia/Shanghai") or "").strip()
    return raw or "Asia/Shanghai"


def _local_tz():
    tz_name = _local_tz_name()
    if ZoneInfo is None:
        return timezone(timedelta(hours=8))
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("Asia/Shanghai")


def _resolve_report_date_local(date_arg: str) -> date:
    if date_arg:
        return datetime.strptime(date_arg, "%Y-%m-%d").date()
    now_local = datetime.now(_local_tz())
    return (now_local - timedelta(days=1)).date()


def _day_bounds_utc(day_local: date) -> Tuple[str, str]:
    tz_local = _local_tz()
    start_local = datetime(day_local.year, day_local.month, day_local.day, tzinfo=tz_local)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
        end_local.astimezone(timezone.utc).isoformat(timespec="seconds"),
    )


@dataclass
class DailyStats:
    buy_fill_count: int = 0
    sell_fill_count: int = 0
    buy_manual_fill_count: int = 0
    sell_manual_fill_count: int = 0
    entry_manual_count: int = 0
    entry_order_submit_count: int = 0
    exit_order_submit_count: int = 0
    entry_order_bucket_count: int = 0
    exit_order_bucket_count: int = 0
    buy_count: int = 0
    sell_count: int = 0
    entry_lcb_count: int = 0
    entry_yes_top_count: int = 0
    entry_no_near_count: int = 0
    entry_no_far_count: int = 0
    entry_other_auto_count: int = 0
    open_positions_count: int = 0
    buy_notional: float = 0.0
    sell_notional: float = 0.0
    realized_pnl_total: float = 0.0
    realized_roi_day: Optional[float] = None
    realized_cost_day: float = 0.0
    report_cost_basis_total: float = 0.0
    unrealized_pnl_total: float = 0.0
    net_pnl_total: float = 0.0
    net_roi_total: Optional[float] = None
    lifetime_buy_notional: float = 0.0
    mark_missing_open_count: int = 0
    pnl_scope_event_count: int = 0
    event_counter: Counter = field(default_factory=Counter)


def _load_daily_stats(
    conn: sqlite3.Connection,
    start_utc: str,
    end_utc: str,
    *,
    valuation_cutoff_utc: Optional[str] = None,
    include_live_positions: bool = False,
) -> DailyStats:
    stats = DailyStats()
    cur = conn.cursor()
    window_end_utc = str(valuation_cutoff_utc or end_utc)
    entry_order_bucket_keys = set()
    exit_order_bucket_keys = set()
    manual_entry_keys = set()
    buy_bucket_keys = set()
    sell_bucket_keys = set()

    def _normalize_combo_role(role: str) -> str:
        return str(COMBO_ROLE_ALIAS_MAP.get(str(role or "").strip().lower()) or "")

    def _classify_auto_entry_strategy(strategy: str, combo_role: str) -> str:
        strategy_l = str(strategy or "").strip().lower()
        combo_role_l = _normalize_combo_role(combo_role)
        if strategy_l in {"lcb", "main", "lcb_global", "lcb_global_main"}:
            if combo_role_l in COMBO_ROLE_LABELS:
                return combo_role_l
            return "lcb"
        strategy_role_l = _normalize_combo_role(strategy_l)
        if strategy_role_l in COMBO_ROLE_LABELS:
            return strategy_role_l
        if combo_role_l in COMBO_ROLE_LABELS:
            return combo_role_l
        return "other"

    query = """
    WITH decision_map AS (
      SELECT order_id, lcb_combo_leg, lcb_q_combo_role, strategy_detail
      FROM (
        SELECT
          json_extract(details_json, '$.order_id') AS order_id,
          lower(trim(COALESCE(json_extract(details_json, '$.lcb_combo_leg'), ''))) AS lcb_combo_leg,
          lower(trim(COALESCE(json_extract(details_json, '$.lcb_q_combo_role'), ''))) AS lcb_q_combo_role,
          lower(trim(COALESCE(json_extract(details_json, '$.strategy'), ''))) AS strategy_detail,
          id,
          ROW_NUMBER() OVER (
            PARTITION BY json_extract(details_json, '$.order_id')
            ORDER BY id DESC
          ) AS rn
        FROM auto_trade_decisions
        WHERE action = 'entry'
          AND COALESCE(json_extract(details_json, '$.order_id'), '') <> ''
      ) t
      WHERE rn = 1
    )
    SELECT
      f.id,
      f.order_id,
      f.trade_id,
      f.trade_side,
      f.price,
      f.size,
      f.slug,
      f.event_date,
      f.outcome,
      f.outcome_side,
      lower(trim(COALESCE(ao.strategy, dm.strategy_detail, ''))) AS strategy,
      lower(trim(COALESCE(dm.lcb_combo_leg, ''))) AS lcb_combo_leg
    FROM edge_trade_fills f
    LEFT JOIN auto_orders ao
      ON ao.order_id = f.order_id
    LEFT JOIN decision_map dm
      ON dm.order_id = f.order_id
    WHERE COALESCE(f.trade_time, f.created_at) >= ?
      AND COALESCE(f.trade_time, f.created_at) < ?
      AND f.slug LIKE 'highest-temperature-in-%'
    """
    rows = cur.execute(query, (start_utc, window_end_utc)).fetchall()
    for fill_id, order_id, trade_id, trade_side, price, size, slug, event_date, outcome, outcome_side, strategy, lcb_combo_leg in rows:
        trade_side_u = str(trade_side or "").strip().upper()
        strategy_l = str(strategy or "").strip().lower()
        slug_l = str(slug or "").strip()
        bucket_key = (
            slug_l,
            str(event_date or "").strip(),
            str(outcome or "").strip(),
            str(outcome_side or "").strip().upper(),
        )
        px = float(price) if price is not None else 0.0
        sz = float(size) if size is not None else 0.0
        notional = max(px, 0.0) * max(sz, 0.0)

        stats.event_counter[slug_l] += 1
        if trade_side_u == "BUY":
            stats.buy_fill_count += 1
            if not strategy_l:
                stats.buy_manual_fill_count += 1
                manual_key = str(order_id or "").strip() or str(trade_id or "").strip() or f"fill:{int(fill_id)}"
                manual_entry_keys.add(manual_key)
            buy_bucket_keys.add(bucket_key)
            stats.buy_notional += notional
        elif trade_side_u == "SELL":
            stats.sell_fill_count += 1
            if not strategy_l:
                stats.sell_manual_fill_count += 1
            sell_bucket_keys.add(bucket_key)
            stats.sell_notional += notional

    stats.buy_count = len(buy_bucket_keys)
    stats.sell_count = len(sell_bucket_keys)
    stats.entry_manual_count = len(manual_entry_keys)
    order_rows = cur.execute(
        """
        WITH decision_map AS (
          SELECT order_id, lcb_combo_leg, lcb_q_combo_role, strategy_detail
          FROM (
            SELECT
              json_extract(details_json, '$.order_id') AS order_id,
              lower(trim(COALESCE(json_extract(details_json, '$.lcb_combo_leg'), ''))) AS lcb_combo_leg,
              lower(trim(COALESCE(json_extract(details_json, '$.lcb_q_combo_role'), ''))) AS lcb_q_combo_role,
              lower(trim(COALESCE(json_extract(details_json, '$.strategy'), ''))) AS strategy_detail,
              id,
              ROW_NUMBER() OVER (
                PARTITION BY json_extract(details_json, '$.order_id')
                ORDER BY id DESC
              ) AS rn
            FROM auto_trade_decisions
            WHERE action = 'entry'
              AND COALESCE(json_extract(details_json, '$.order_id'), '') <> ''
          ) t
          WHERE rn = 1
        )
        SELECT
          ao.action,
          ao.slug,
          ao.event_date,
          ao.outcome,
          ao.side,
          lower(trim(COALESCE(ao.strategy, dm.strategy_detail, ''))) AS strategy,
          lower(trim(COALESCE(dm.lcb_combo_leg, ''))) AS lcb_combo_leg,
          lower(trim(COALESCE(dm.lcb_q_combo_role, ''))) AS lcb_q_combo_role
        FROM auto_orders ao
        LEFT JOIN decision_map dm
          ON dm.order_id = ao.order_id
        WHERE created_at >= ?
          AND created_at < ?
          AND ao.slug LIKE 'highest-temperature-in-%'
        """,
        (start_utc, window_end_utc),
    ).fetchall()
    for action, slug, event_date, outcome, side, strategy, lcb_combo_leg, lcb_q_combo_role in order_rows:
        action_l = str(action or "").strip().lower()
        bucket_key = (
            str(slug or "").strip(),
            str(event_date or "").strip(),
            str(outcome or "").strip(),
            str(side or "").strip().upper(),
        )
        if not all(bucket_key):
            continue
        if action_l == "entry":
            stats.entry_order_submit_count += 1
            entry_order_bucket_keys.add(bucket_key)
            strategy_class = _classify_auto_entry_strategy(strategy, lcb_q_combo_role)
            if strategy_class == "lcb":
                stats.entry_lcb_count += 1
            elif strategy_class == "combo_yes_peak":
                stats.entry_yes_top_count += 1
            elif strategy_class == "combo_no_adjacent":
                stats.entry_no_near_count += 1
            elif strategy_class == "combo_no_remote":
                stats.entry_no_far_count += 1
            else:
                stats.entry_other_auto_count += 1
        elif action_l == "exit":
            stats.exit_order_submit_count += 1
            exit_order_bucket_keys.add(bucket_key)
    stats.entry_order_bucket_count = len(entry_order_bucket_keys)
    stats.exit_order_bucket_count = len(exit_order_bucket_keys)

    open_row = cur.execute(
        """
        SELECT COUNT(*)
        FROM edge_trades
        WHERE COALESCE(position_size, 0) > 1e-9
          AND slug LIKE 'highest-temperature-in-%'
        """
    ).fetchone()
    stats.open_positions_count = int(open_row[0] if open_row else 0)

    scope_events_set = {
        (str(slug or "").strip(), str(event_date or "").strip())
        for slug, event_date in cur.execute(
            """
            SELECT DISTINCT slug, event_date
            FROM edge_trade_fills
            WHERE COALESCE(trade_time, created_at) >= ?
              AND COALESCE(trade_time, created_at) < ?
              AND slug LIKE 'highest-temperature-in-%'
            """,
            (start_utc, window_end_utc),
        ).fetchall()
        if str(slug or "").strip() and str(event_date or "").strip()
    }
    if include_live_positions:
        for slug, event_date in cur.execute(
            """
            SELECT DISTINCT slug, event_date
            FROM edge_trades
            WHERE COALESCE(position_size, 0) > 1e-9
              AND slug LIKE 'highest-temperature-in-%'
            """
        ).fetchall():
            slug_key = str(slug or "").strip()
            event_date_key = str(event_date or "").strip()
            if slug_key and event_date_key:
                scope_events_set.add((slug_key, event_date_key))
    scope_events = sorted(scope_events_set)
    stats.pnl_scope_event_count = len(scope_events)

    if scope_events:
        start_dt_utc = datetime.fromisoformat(str(start_utc).replace("Z", "+00:00"))
        end_dt_utc = datetime.fromisoformat(str(window_end_utc).replace("Z", "+00:00"))
        values_sql = ",".join(["(?, ?)"] * len(scope_events))
        scope_params: List[str] = []
        for slug, event_date in scope_events:
            scope_params.extend([slug, event_date])

        fill_rows = cur.execute(
            f"""
            WITH scope_events(slug, event_date) AS (VALUES {values_sql})
            SELECT
              f.id,
              f.slug,
              f.event_date,
              f.outcome,
              f.outcome_side,
              f.trade_side,
              f.price,
              f.size,
              f.fee,
              COALESCE(f.trade_time, f.created_at) AS fill_ts
            FROM edge_trade_fills f
            JOIN scope_events s
              ON s.slug = f.slug
             AND s.event_date = f.event_date
            WHERE f.slug LIKE 'highest-temperature-in-%'
              AND COALESCE(f.trade_time, f.created_at) < ?
            ORDER BY datetime(COALESCE(f.trade_time, f.created_at)) ASC, f.id ASC
            """,
            (*scope_params, end_utc),
        ).fetchall()

        fills_by_contract: Dict[Tuple[str, str, str, str], List[Tuple[str, float, float, float, str]]] = {}
        for _, slug, event_date, outcome, outcome_side, trade_side, price, size, fee, fill_ts in fill_rows:
            trade_side_u = str(trade_side or "").strip().upper()
            if trade_side_u not in {"BUY", "SELL"}:
                continue
            px = float(price) if price is not None else None
            qty = float(size) if size is not None else None
            fee_val = float(fee) if fee is not None else 0.0
            if (
                px is None
                or qty is None
                or not math.isfinite(px)
                or not math.isfinite(qty)
                or px <= 0
                or qty <= 1e-9
            ):
                continue
            if not math.isfinite(fee_val) or fee_val < 0:
                fee_val = 0.0
            key = (
                str(slug or "").strip(),
                str(event_date or "").strip(),
                str(outcome or "").strip(),
                str(outcome_side or "").strip().upper(),
            )
            if all(key):
                fills_by_contract.setdefault(key, []).append(
                    (trade_side_u, float(px), float(qty), float(fee_val), str(fill_ts or ""))
                )

        live_positions_by_contract: Dict[Tuple[str, str, str, str], Tuple[float, float]] = {}
        if include_live_positions:
            live_rows = cur.execute(
                f"""
                WITH scope_events(slug, event_date) AS (VALUES {values_sql})
                SELECT t.slug, t.event_date, t.outcome, t.side, t.position_size, t.avg_price, t.entry_at, t.updated_at
                FROM edge_trades t
                JOIN scope_events s
                  ON s.slug = t.slug
                 AND s.event_date = t.event_date
                WHERE COALESCE(t.position_size, 0) > 1e-9
                  AND t.slug LIKE 'highest-temperature-in-%'
                """,
                scope_params,
            ).fetchall()
            for slug, event_date, outcome, side, position_size, avg_price, entry_at, updated_at in live_rows:
                pos_val = float(position_size) if position_size is not None else None
                avg_val = float(avg_price) if avg_price is not None else None
                if (
                    pos_val is None
                    or avg_val is None
                    or not math.isfinite(pos_val)
                    or not math.isfinite(avg_val)
                    or pos_val <= 1e-9
                    or avg_val <= 0
                ):
                    continue
                ts_text = str(entry_at or updated_at or "").strip()
                include_row = False
                if ts_text:
                    ts_dt = _parse_iso_ts(ts_text)
                    if ts_dt is not None:
                        if ts_dt.tzinfo is None:
                            ts_dt = ts_dt.replace(tzinfo=timezone.utc)
                        ts_dt = ts_dt.astimezone(timezone.utc)
                        include_row = start_dt_utc <= ts_dt < end_dt_utc
                if not include_row and str(event_date or "").strip() == str(datetime.now(_local_tz()).date()):
                    include_row = True
                if not include_row:
                    continue
                key = (
                    str(slug or "").strip(),
                    str(event_date or "").strip(),
                    str(outcome or "").strip(),
                    str(side or "").strip().upper(),
                )
                if all(key):
                    live_positions_by_contract[key] = (float(pos_val), float(avg_val))

        realized_day = 0.0
        realized_cost_day = 0.0
        unrealized_total = 0.0
        sod_unrealized_total = 0.0
        mark_missing_open = 0
        lifetime_buy_notional = 0.0
        report_cost_basis_total = 0.0
        mark_cache: Dict[Tuple[Tuple[str, str, str, str], str], Optional[float]] = {}

        def _mark_before(contract_key: Tuple[str, str, str, str], cutoff_iso: str) -> Optional[float]:
            cache_key = (contract_key, cutoff_iso)
            cached = mark_cache.get(cache_key)
            if cache_key in mark_cache:
                return cached
            row = cur.execute(
                """
                SELECT mid, best_bid, best_ask
                FROM event_market_ticks
                WHERE slug = ?
                  AND event_date = ?
                  AND outcome = ?
                  AND side = ?
                  AND datetime(created_at) <= datetime(?)
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT 1
                """,
                (*contract_key, cutoff_iso),
            ).fetchone()
            mark_val = _pick_tick_mark(*(row or (None, None, None)))
            mark_cache[cache_key] = mark_val
            return mark_val

        for contract_key, contract_fills in fills_by_contract.items():
            pos = 0.0
            avg = 0.0
            sod_pos = 0.0
            sod_avg = 0.0
            day_buy_cost = 0.0
            for trade_side_u, px, qty, fee_val, fill_ts in contract_fills:
                fill_dt = _parse_iso_ts(fill_ts)
                if fill_dt is None:
                    continue
                if fill_dt.tzinfo is None:
                    fill_dt = fill_dt.replace(tzinfo=timezone.utc)
                fill_dt = fill_dt.astimezone(timezone.utc)
                if fill_dt >= start_dt_utc:
                    sod_pos = pos
                    sod_avg = avg
                    break
                if trade_side_u == "BUY":
                    lifetime_buy_notional += px * qty
                    new_pos = pos + qty
                    total_cost = (avg * pos) + (px * qty) + fee_val
                    pos = new_pos
                    avg = total_cost / new_pos if new_pos > 1e-9 else 0.0
                else:
                    if pos <= 1e-9:
                        continue
                    close_qty = min(qty, pos)
                    pos -= close_qty
                    if pos <= 1e-9:
                        pos = 0.0
                        avg = 0.0
            else:
                sod_pos = pos
                sod_avg = avg

            pos = sod_pos
            avg = sod_avg
            if sod_pos > 1e-9:
                sod_mark = _mark_before(contract_key, start_utc)
                if sod_mark is None:
                    sod_mark = sod_avg
                sod_unrealized_total += (float(sod_mark) - sod_avg) * sod_pos
                report_cost_basis_total += sod_avg * sod_pos

            for trade_side_u, px, qty, fee_val, fill_ts in contract_fills:
                fill_dt = _parse_iso_ts(fill_ts)
                if fill_dt is None:
                    continue
                if fill_dt.tzinfo is None:
                    fill_dt = fill_dt.replace(tzinfo=timezone.utc)
                fill_dt = fill_dt.astimezone(timezone.utc)
                if fill_dt < start_dt_utc or fill_dt >= end_dt_utc:
                    continue
                if trade_side_u == "BUY":
                    day_buy_cost += (px * qty) + fee_val
                    new_pos = pos + qty
                    total_cost = (avg * pos) + (px * qty) + fee_val
                    pos = new_pos
                    avg = total_cost / new_pos if new_pos > 1e-9 else 0.0
                else:
                    if pos <= 1e-9:
                        continue
                    close_qty = min(qty, pos)
                    fee_used = fee_val * (close_qty / qty) if qty > 1e-9 else 0.0
                    pnl_piece = (px - avg) * close_qty - fee_used
                    realized_day += pnl_piece
                    realized_cost_day += avg * close_qty
                    pos -= close_qty
                    if pos <= 1e-9:
                        pos = 0.0
                        avg = 0.0
            if pos > 1e-9:
                mark_val = _mark_before(contract_key, end_utc)
                if mark_val is None:
                    mark_missing_open += 1
                    mark_val = avg
                unrealized_total += (float(mark_val) - avg) * pos
            report_cost_basis_total += day_buy_cost

        for contract_key, (pos_val, avg_val) in live_positions_by_contract.items():
            if contract_key in fills_by_contract:
                continue
            mark_val = _mark_before(contract_key, window_end_utc)
            if mark_val is None:
                mark_missing_open += 1
                mark_val = avg_val
            unrealized_total += (float(mark_val) - avg_val) * pos_val
            report_cost_basis_total += avg_val * pos_val

        stats.realized_pnl_total = float(realized_day)
        stats.realized_cost_day = float(realized_cost_day)
        stats.report_cost_basis_total = float(report_cost_basis_total)
        if stats.realized_cost_day > 1e-9:
            stats.realized_roi_day = stats.realized_pnl_total / stats.realized_cost_day
        stats.unrealized_pnl_total = float(unrealized_total)
        stats.net_pnl_total = stats.realized_pnl_total + (stats.unrealized_pnl_total - float(sod_unrealized_total))
        stats.mark_missing_open_count = int(mark_missing_open)
        stats.lifetime_buy_notional = float(lifetime_buy_notional)
        if stats.report_cost_basis_total > 1e-9:
            stats.net_roi_total = stats.net_pnl_total / stats.report_cost_basis_total
    return stats


def _build_message(report_date_local: date, stats: DailyStats) -> str:
    net_roi_text = "—"
    if stats.net_roi_total is not None and math.isfinite(stats.net_roi_total):
        net_roi_text = f"{stats.net_roi_total * 100:.2f}%"
    realized_roi_day_text = "—"
    if stats.realized_roi_day is not None and math.isfinite(stats.realized_roi_day):
        realized_roi_day_text = f"{stats.realized_roi_day * 100:.2f}%"
    entry_total_count = stats.entry_order_submit_count + stats.entry_manual_count
    lines = [
        f"【交易日报 {report_date_local.isoformat()} 本地】",
        f"进入: 总{entry_total_count} | 自动{stats.entry_order_submit_count} | 手动{stats.entry_manual_count}",
        (
            "自动策略: "
            f"lcb={stats.entry_lcb_count} | combo_yes_peak={stats.entry_yes_top_count} "
            f"| combo_no_adjacent={stats.entry_no_near_count} "
            f"| combo_no_remote={stats.entry_no_far_count} | 其他={stats.entry_other_auto_count}"
        ),
        (
            f"持仓: {stats.open_positions_count} | 花费: {_fmt_usdc(stats.buy_notional)} "
            f"| 卖出: {_fmt_usdc(stats.sell_notional)}"
        ),
        (
            f"PnL: {_fmt_usdc(stats.net_pnl_total)} | ROI: {net_roi_text}"
            f" | 已实现: {_fmt_usdc(stats.realized_pnl_total)}"
        ),
        f"估值: {_fmt_usdc(stats.unrealized_pnl_total)} | 已实现ROI: {realized_roi_day_text}",
    ]
    if stats.mark_missing_open_count > 0:
        lines.append(f"估值缺口: {stats.mark_missing_open_count}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Send daily weather trading SMS summary.")
    parser.add_argument("--date", default="", help="本地时区报告日期 YYYY-MM-DD（默认：本地昨天）。")
    parser.add_argument("--recipient", default="", help="Override SMS recipient.")
    parser.add_argument("--dry-run", action="store_true", help="Print message only.")
    parser.add_argument("--force", action="store_true", help="Send even if already sent for this ET date.")
    args = parser.parse_args()

    _load_env(ENV_PATH)
    sms_enabled = _env_bool("PM_SMS_ENABLED", True)
    daily_enabled = _env_bool("PM_SMS_DAILY_SUMMARY_ENABLED", True)
    recipient = str(args.recipient or __import__("os").environ.get("PM_SMS_RECIPIENT", "")).strip()
    report_date_local = _resolve_report_date_local(str(args.date or "").strip())
    report_date_key = report_date_local.isoformat()

    if not args.dry_run:
        if not daily_enabled:
            print("[daily_sms] disabled by PM_SMS_DAILY_SUMMARY_ENABLED=0")
            return 0
        if not sms_enabled:
            print("[daily_sms] PM_SMS_ENABLED=0, skip send")
            return 0
        if not recipient:
            print("[daily_sms] missing PM_SMS_RECIPIENT")
            return 1
        state = _load_state(STATE_PATH)
        last_sent_key = str(
            state.get("last_report_date_local")
            or state.get("last_report_date_et")
            or ""
        )
        if (not args.force) and last_sent_key == report_date_key:
            print(f"[daily_sms] already sent for {report_date_key}, skip")
            return 0

    if not DB_PATH.exists():
        print(f"[daily_sms] missing db: {DB_PATH}")
        return 1

    start_utc, end_utc = _day_bounds_utc(report_date_local)
    now_local_dt = datetime.now(_local_tz())
    include_live_positions = report_date_local == now_local_dt.date()
    valuation_cutoff_utc = end_utc
    if include_live_positions:
        valuation_cutoff_utc = min(
            end_utc,
            now_local_dt.astimezone(timezone.utc).isoformat(timespec="seconds"),
        )
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=5.0)
    try:
        stats = _load_daily_stats(
            conn,
            start_utc,
            end_utc,
            valuation_cutoff_utc=valuation_cutoff_utc,
            include_live_positions=include_live_positions,
        )
    finally:
        conn.close()
    message = _build_message(report_date_local, stats)

    if args.dry_run:
        print(message)
        return 0

    ok = _send_sms(message, recipient)
    if not ok:
        return 1

    now_local = datetime.now(_local_tz()).isoformat(timespec="seconds")
    _save_state(
        STATE_PATH,
        {
            "last_report_date_local": report_date_key,
            "last_sent_at_local": now_local,
            "last_recipient": recipient,
            "report_tz": _local_tz_name(),
        },
    )
    print(f"[daily_sms] sent report for local {report_date_key} ({_local_tz_name()}) to {recipient}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
