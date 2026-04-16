from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


def to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def days_to_event(event_date: str, created_at: str) -> Optional[float]:
    ts = parse_ts(created_at)
    if ts is None:
        return None
    try:
        ev_day = datetime.strptime(str(event_date or "")[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    return float((ev_day - ts.date()).days)


def payload_rows(payload_json: Any) -> List[Dict[str, Any]]:
    try:
        payload = json.loads(str(payload_json or ""))
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    rows = payload.get("rows") or payload.get("distribution") or []
    if not isinstance(rows, list):
        return []
    out: List[Dict[str, Any]] = []
    for row in rows:
        if isinstance(row, dict):
            out.append(dict(row))
    return out


def rows_have_market_quotes(rows: List[Dict[str, Any]]) -> bool:
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for key in (
            "best_ask",
            "best_bid",
            "best_ask_no",
            "best_bid_no",
            "ask",
            "bid",
            "yes_price",
            "no_price",
        ):
            if to_float(row.get(key)) is not None:
                return True
    return False


def rows_have_usable_q(rows: List[Dict[str, Any]]) -> bool:
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        for key in (
            "q_raw",
            "q_round",
            "q_entry",
            "q_calibrated",
        ):
            if to_float(row.get(key)) is not None:
                return True
    return False


def merge_rows_fill_missing(base_rows: List[Dict[str, Any]], counterpart_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    lookup: Dict[str, Dict[str, Any]] = {}
    for row in counterpart_rows or []:
        if not isinstance(row, dict):
            continue
        outcome = str(row.get("outcome") or "").strip()
        if outcome and outcome not in lookup:
            lookup[outcome] = row
    merged: List[Dict[str, Any]] = []
    for base in base_rows or []:
        if not isinstance(base, dict):
            continue
        row = dict(base)
        outcome = str(row.get("outcome") or "").strip()
        src = lookup.get(outcome)
        if src:
            for key, value in src.items():
                if key == "outcome":
                    continue
                cur = row.get(key)
                if cur is None or cur == "":
                    row[key] = value
        merged.append(row)
    return merged


def _load_event_snapshots(
    conn: sqlite3.Connection,
    since_iso: str,
) -> Dict[Tuple[str, str], List[Dict[str, Any]]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, slug, event_date, created_at, settlement_actual, rounding_rule, payload_json
        FROM event_runs
        WHERE payload_json IS NOT NULL
          AND payload_json != ''
          AND datetime(created_at) >= datetime(?)
        ORDER BY datetime(created_at) ASC, id ASC
        """,
        (since_iso,),
    )
    grouped: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for row in cur.fetchall():
        slug = str(row[1] or "").strip()
        event_date = str(row[2] or "").strip()
        created_at = str(row[3] or "").strip()
        if not slug or not event_date or not created_at:
            continue
        rows = payload_rows(row[6])
        if not rows:
            continue
        item = {
            "id": int(row[0] or 0),
            "slug": slug,
            "event_date": event_date,
            "created_at": created_at,
            "settlement_actual": to_float(row[4]),
            "rounding_rule": str(row[5] or ""),
            "rows": rows,
            "any_market": rows_have_market_quotes(rows),
            "any_q": rows_have_usable_q(rows),
        }
        grouped.setdefault((slug, event_date), []).append(item)
    return grouped


def _choose_nearest_snapshot(
    snapshots: List[Dict[str, Any]],
    anchor: Dict[str, Any],
    *,
    predicate_key: str,
    max_join_sec: float,
) -> Optional[Dict[str, Any]]:
    if not snapshots:
        return None
    if bool(anchor.get(predicate_key)):
        return anchor
    anchor_ts = parse_ts(anchor.get("created_at"))
    ranked: List[Tuple[float, int, str, Dict[str, Any]]] = []
    for snap in snapshots:
        if not bool(snap.get(predicate_key)):
            continue
        ts = parse_ts(snap.get("created_at"))
        if anchor_ts is None or ts is None:
            continue
        delta_sec = (ts - anchor_ts).total_seconds()
        abs_sec = abs(delta_sec)
        if max_join_sec > 0 and abs_sec > max_join_sec:
            continue
        ranked.append(
            (
                abs_sec,
                0 if delta_sec >= 0 else 1,
                str(snap.get("created_at") or ""),
                snap,
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda item: (item[0], item[1], item[2]))
    return ranked[0][3]


def load_joined_open_market_dataset(
    conn: sqlite3.Connection,
    since_iso: str,
    limit_events: int,
    *,
    require_settlement: bool = False,
    max_join_sec: float = 6 * 3600,
) -> List[Dict[str, Any]]:
    grouped = _load_event_snapshots(conn, since_iso)
    out: List[Dict[str, Any]] = []
    for key, snapshots in grouped.items():
        if not snapshots:
            continue
        latest_settled = None
        for snap in snapshots:
            if to_float(snap.get("settlement_actual")) is not None:
                latest_settled = snap
        if require_settlement and latest_settled is None:
            continue
        market_anchor = next((snap for snap in snapshots if bool(snap.get("any_market"))), None)
        if market_anchor is None:
            continue
        q_snap = _choose_nearest_snapshot(
            snapshots,
            market_anchor,
            predicate_key="any_q",
            max_join_sec=float(max_join_sec),
        )
        if q_snap is None:
            continue
        merged_rows = merge_rows_fill_missing(
            list(market_anchor.get("rows") or []),
            list(q_snap.get("rows") or []),
        )
        if not rows_have_market_quotes(merged_rows) or not rows_have_usable_q(merged_rows):
            continue
        settlement_src = latest_settled or snapshots[-1]
        out.append(
            {
                "slug": key[0],
                "event_date": key[1],
                "created_at": str(market_anchor.get("created_at") or ""),
                "market_snapshot_id": market_anchor.get("id"),
                "market_snapshot_at": str(market_anchor.get("created_at") or ""),
                "q_snapshot_id": q_snap.get("id"),
                "q_snapshot_at": str(q_snap.get("created_at") or ""),
                "join_sec": _join_delta_sec(market_anchor, q_snap),
                "settlement_actual": to_float(settlement_src.get("settlement_actual")),
                "rounding_rule": str(settlement_src.get("rounding_rule") or ""),
                "rows": merged_rows,
            }
        )
    out.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("slug") or "")))
    return out[: max(1, int(limit_events))]


def load_first_q_settled_dataset(
    conn: sqlite3.Connection,
    since_iso: str,
    limit_events: int,
    *,
    max_join_sec: float = 6 * 3600,
) -> List[Dict[str, Any]]:
    grouped = _load_event_snapshots(conn, since_iso)
    out: List[Dict[str, Any]] = []
    for key, snapshots in grouped.items():
        if not snapshots:
            continue
        latest_settled = None
        for snap in snapshots:
            if to_float(snap.get("settlement_actual")) is not None:
                latest_settled = snap
        if latest_settled is None:
            continue
        q_anchor = next((snap for snap in snapshots if bool(snap.get("any_q"))), None)
        if q_anchor is None:
            continue
        market_snap = _choose_nearest_snapshot(
            snapshots,
            q_anchor,
            predicate_key="any_market",
            max_join_sec=float(max_join_sec),
        )
        merged_rows = list(q_anchor.get("rows") or [])
        if market_snap is not None and market_snap is not q_anchor:
            merged_rows = merge_rows_fill_missing(
                merged_rows,
                list(market_snap.get("rows") or []),
            )
        if not rows_have_usable_q(merged_rows):
            continue
        out.append(
            {
                "slug": key[0],
                "event_date": key[1],
                "created_at": str(q_anchor.get("created_at") or ""),
                "anchor_snapshot_id": q_anchor.get("id"),
                "q_snapshot_id": q_anchor.get("id"),
                "q_snapshot_at": str(q_anchor.get("created_at") or ""),
                "market_snapshot_id": market_snap.get("id") if isinstance(market_snap, dict) else None,
                "market_snapshot_at": str((market_snap or {}).get("created_at") or ""),
                "join_sec": _join_delta_sec(q_anchor, market_snap),
                "settlement_actual": to_float(latest_settled.get("settlement_actual")),
                "rounding_rule": str(latest_settled.get("rounding_rule") or ""),
                "rows": merged_rows,
            }
        )
    out.sort(key=lambda item: (str(item.get("created_at") or ""), str(item.get("slug") or "")))
    return out[: max(1, int(limit_events))]


def _join_delta_sec(left: Optional[Dict[str, Any]], right: Optional[Dict[str, Any]]) -> Optional[float]:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return None
    ts_left = parse_ts(left.get("created_at"))
    ts_right = parse_ts(right.get("created_at"))
    if ts_left is None or ts_right is None:
        return None
    return float((ts_right - ts_left).total_seconds())
