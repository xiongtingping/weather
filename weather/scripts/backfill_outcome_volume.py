#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT_DIR / "data" / "archive.db"
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def open_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass
    conn.row_factory = sqlite3.Row
    return conn


def to_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def parse_json_field(raw: Any) -> Any:
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return raw


def parse_token_list(raw: Any) -> List[str]:
    val = parse_json_field(raw)
    if not isinstance(val, list):
        return []
    out: List[str] = []
    for item in val:
        token = str(item or "").strip()
        if token:
            out.append(token)
    return out


def is_missing_volume_triplet(row: Dict[str, Any]) -> bool:
    return (
        to_float(row.get("outcome_volume")) is None
        and to_float(row.get("volume_24h")) is None
        and to_float(row.get("volume_total")) is None
    )


def fetch_gamma_event_by_slug(slug: str, timeout: float = 20.0) -> Optional[Dict[str, Any]]:
    if not slug:
        return None
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            resp = requests.get(
                GAMMA_EVENTS_URL,
                params={"slug": slug},
                timeout=timeout,
                headers={"accept": "application/json"},
            )
            if resp.status_code >= 500:
                raise RuntimeError(f"gamma_5xx_{resp.status_code}")
            if resp.status_code != 200:
                return None
            payload = resp.json()
            if isinstance(payload, list) and payload:
                first = payload[0]
                return first if isinstance(first, dict) else None
            if isinstance(payload, dict):
                return payload
            return None
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt < 2:
                time.sleep(1.2 * (attempt + 1))
            continue
    if last_exc:
        raise last_exc
    return None


def build_token_volume_map(event: Dict[str, Any]) -> Dict[str, Dict[str, Optional[float]]]:
    markets = event.get("markets") or []
    token_map: Dict[str, Dict[str, Optional[float]]] = {}
    if not isinstance(markets, list):
        return token_map
    for market in markets:
        if not isinstance(market, dict):
            continue
        vol_24h = to_float(
            market.get("volume24hrClob")
            or market.get("volume24hr")
            or market.get("volume1wkClob")
            or market.get("volume1wk")
        )
        vol_total = to_float(
            market.get("volumeClob")
            or market.get("volume")
            or market.get("volumeNum")
        )
        outcome_volume = to_float(
            market.get("volume24hrClob")
            or market.get("volume24hr")
            or market.get("volumeClob")
            or market.get("volume")
            or market.get("volumeNum")
        )
        info = {
            "volume_24h": vol_24h,
            "volume_total": vol_total,
            "outcome_volume": outcome_volume,
        }
        for token in parse_token_list(market.get("clobTokenIds")):
            token_map[token] = info
    return token_map


def iter_event_runs(
    conn: sqlite3.Connection,
    run_date: Optional[str],
    limit: int,
    all_rows: bool,
) -> Iterable[sqlite3.Row]:
    cur = conn.cursor()
    params: List[Any] = []
    where: List[str] = ["payload_json IS NOT NULL"]
    if run_date:
        where.append("run_date = ?")
        params.append(run_date)
    where_sql = " AND ".join(where)

    if all_rows:
        sql = (
            "SELECT id, run_date, event_date, slug, title, payload_json, created_at "
            "FROM event_runs "
            f"WHERE {where_sql} "
            "ORDER BY created_at DESC"
        )
    else:
        sql = (
            "SELECT id, run_date, event_date, slug, title, payload_json, created_at "
            "FROM ("
            "  SELECT id, run_date, event_date, slug, title, payload_json, created_at, "
            "         ROW_NUMBER() OVER (PARTITION BY slug, event_date ORDER BY created_at DESC) AS rn "
            "  FROM event_runs "
            f"  WHERE {where_sql}"
            ") t "
            "WHERE rn = 1 "
            "ORDER BY created_at DESC"
        )
    if limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    cur.execute(sql, tuple(params))
    for row in cur:
        yield row


def main() -> None:
    ap = argparse.ArgumentParser(
        description=(
            "Backfill event_runs.payload_json volume fields "
            "(outcome_volume / volume_24h / volume_total) from Gamma events API"
        )
    )
    ap.add_argument("--db", default=str(DEFAULT_DB), help="Path to archive.db")
    ap.add_argument("--run-date", default="", help="Only process this run_date (e.g. 2026-02-05)")
    ap.add_argument("--limit", type=int, default=0, help="Limit rows (0=all)")
    ap.add_argument("--batch", type=int, default=200, help="Commit every N updates")
    ap.add_argument("--write-retries", type=int, default=8, help="Retry UPDATE when db is locked")
    ap.add_argument("--write-retry-sleep", type=float, default=0.6, help="Sleep seconds between write retries")
    ap.add_argument("--timeout", type=float, default=20.0, help="Gamma request timeout seconds")
    ap.add_argument("--all-rows", action="store_true", help="Process every event_runs row, not just latest slug+event_date")
    ap.add_argument("--rewrite-nonmissing", action="store_true", help="Rewrite volume fields even when row already has values")
    ap.add_argument("--dry-run", action="store_true", help="Only report, do not update DB")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit(f"DB not found: {db_path}")

    scanned = 0
    skipped_no_rows = 0
    skipped_no_need = 0
    fetch_failed = 0
    write_failed = 0
    update_event_rows = 0
    updated_event_runs = 0
    gamma_hit = 0
    gamma_miss = 0

    conn = open_db(db_path)
    cur = conn.cursor()
    gamma_cache: Dict[str, Optional[Dict[str, Any]]] = {}

    def update_payload_with_retry(event_run_id: int, payload_json: str) -> bool:
        retries = max(1, int(args.write_retries))
        sleep_sec = max(0.0, float(args.write_retry_sleep))
        for attempt in range(retries):
            try:
                cur.execute("UPDATE event_runs SET payload_json = ? WHERE id = ?", (payload_json, event_run_id))
                return True
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt >= retries - 1:
                    return False
                time.sleep(sleep_sec * (attempt + 1))
        return False

    try:
        for rec in iter_event_runs(conn, args.run_date.strip() or None, args.limit, args.all_rows):
            scanned += 1
            row_id = int(rec["id"])
            slug = str(rec["slug"] or "")
            payload_raw = rec["payload_json"]
            try:
                payload = json.loads(payload_raw)
            except Exception:
                skipped_no_rows += 1
                continue
            rows = payload.get("rows") or []
            if not isinstance(rows, list) or not rows:
                skipped_no_rows += 1
                continue

            need = False
            for item in rows:
                if not isinstance(item, dict):
                    continue
                if args.rewrite_nonmissing or is_missing_volume_triplet(item):
                    need = True
                    break
            if not need:
                skipped_no_need += 1
                continue

            event_payload: Optional[Dict[str, Any]]
            if slug in gamma_cache:
                event_payload = gamma_cache[slug]
                gamma_hit += 1
            else:
                try:
                    event_payload = fetch_gamma_event_by_slug(slug, timeout=args.timeout)
                except Exception:
                    event_payload = None
                    fetch_failed += 1
                gamma_cache[slug] = event_payload
                gamma_miss += 1

            if not event_payload:
                continue
            token_map = build_token_volume_map(event_payload)
            if not token_map:
                continue

            changed = False
            changed_rows = 0
            for item in rows:
                if not isinstance(item, dict):
                    continue
                yes_token = str(item.get("yes_token") or "").strip()
                no_token = str(item.get("no_token") or "").strip()
                volume_info = None
                if yes_token and yes_token in token_map:
                    volume_info = token_map[yes_token]
                elif no_token and no_token in token_map:
                    volume_info = token_map[no_token]
                if not volume_info:
                    continue

                before = (
                    item.get("outcome_volume"),
                    item.get("volume_24h"),
                    item.get("volume_total"),
                )

                if args.rewrite_nonmissing or to_float(item.get("outcome_volume")) is None:
                    item["outcome_volume"] = volume_info.get("outcome_volume")
                if args.rewrite_nonmissing or to_float(item.get("volume_24h")) is None:
                    item["volume_24h"] = volume_info.get("volume_24h")
                if args.rewrite_nonmissing or to_float(item.get("volume_total")) is None:
                    item["volume_total"] = volume_info.get("volume_total")

                after = (
                    item.get("outcome_volume"),
                    item.get("volume_24h"),
                    item.get("volume_total"),
                )
                if before != after:
                    changed = True
                    changed_rows += 1

            if not changed:
                continue

            if args.dry_run:
                update_event_rows += changed_rows
                updated_event_runs += 1
                continue

            new_payload = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
            if not update_payload_with_retry(row_id, new_payload):
                write_failed += 1
                continue
            update_event_rows += changed_rows
            updated_event_runs += 1
            if updated_event_runs % max(1, args.batch) == 0:
                conn.commit()

        if not args.dry_run:
            conn.commit()
    finally:
        conn.close()

    summary = {
        "scanned": scanned,
        "updated_event_runs": updated_event_runs,
        "updated_rows": update_event_rows,
        "skipped_no_rows": skipped_no_rows,
        "skipped_no_need": skipped_no_need,
        "fetch_failed": fetch_failed,
        "write_failed": write_failed,
        "gamma_cache_hit": gamma_hit,
        "gamma_cache_miss": gamma_miss,
        "dry_run": bool(args.dry_run),
        "all_rows": bool(args.all_rows),
        "run_date": args.run_date.strip() or None,
    }
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
