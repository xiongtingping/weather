#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_WAKEUP = ROOT / "data" / "reports" / "recommended_pilot_settlement_wakeup_latest.json"
DEFAULT_OUT = ROOT / "data" / "reports" / "settlement_actual_repair_plan_latest.json"


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _station_from_source(value: Any) -> str:
    text = str(value or "").strip()
    match = re.search(r"/history/daily/[^?#]+/([A-Za-z0-9_-]+)(?:[/?#]|$)", text)
    return (match.group(1).upper() if match else "")


def _event_rows(db_path: Path, *, slug: str, event_date: str) -> list[dict[str, Any]]:
    if not db_path.exists() or not slug:
        return []
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, slug, event_date, title, event_url, resolution_source, station_id,
                   market_var, market_unit, rounding_rule, lat, lon, tz, market_closed,
                   event_end_utc, settlement_actual, created_at
            FROM event_runs
            WHERE slug = ?
              AND (? = '' OR event_date = ?)
            ORDER BY created_at DESC, id DESC
            LIMIT 20
            """,
            (slug, event_date, event_date),
        ).fetchall()
        return [dict(row) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _first_present(rows: list[Mapping[str, Any]], key: str) -> Any:
    for row in rows:
        value = row.get(key)
        if value not in (None, ""):
            return value
    return ""


def _row_for_market(db_path: Path, market: Mapping[str, Any]) -> dict[str, Any]:
    slug = str(market.get("slug") or "").strip()
    event_date = str(market.get("event_date") or "").split("T", 1)[0]
    rows = _event_rows(db_path, slug=slug, event_date=event_date)
    latest = rows[0] if rows else {}
    resolution_source = str(_first_present(rows, "resolution_source") or "").strip()
    station_id = str(_first_present(rows, "station_id") or "").strip().upper()
    source_station = _station_from_source(resolution_source)
    market_var = str(_first_present(rows, "market_var") or "").strip()
    market_unit = str(_first_present(rows, "market_unit") or "").strip()
    source_ok = bool(resolution_source and "/history/daily/" in resolution_source)
    station_ok = bool(source_station and (not station_id or station_id == source_station))
    metadata_ok = bool(source_ok and station_ok and market_var)
    blockers: list[str] = []
    if not rows:
        blockers.append("event_run_missing")
    if not resolution_source:
        blockers.append("resolution_source_missing")
    elif not source_ok:
        blockers.append("resolution_source_not_wunderground_history")
    if not source_station:
        blockers.append("source_station_missing")
    if station_id and source_station and station_id != source_station:
        blockers.append("station_id_source_mismatch")
    if not market_var:
        blockers.append("market_var_missing")
    if str(market.get("current_status") or "") != "closed_without_settlement_actual":
        blockers.append("not_closed_without_settlement_actual")
    return {
        "city": str(market.get("city") or ""),
        "slug": slug,
        "event_date": event_date,
        "title": str(latest.get("title") or ""),
        "event_url": str(latest.get("event_url") or f"https://polymarket.com/event/{slug}"),
        "current_status": str(market.get("current_status") or ""),
        "event_run_count": len(rows),
        "latest_event_run_id": latest.get("id"),
        "latest_event_run_at": latest.get("created_at"),
        "market_closed": latest.get("market_closed"),
        "event_end_utc": latest.get("event_end_utc"),
        "resolution_source": resolution_source,
        "station_id": station_id,
        "source_station_id": source_station,
        "station_source_match": bool(station_ok),
        "market_var": market_var,
        "market_unit": market_unit,
        "metadata_ok_for_settlement_fetch": bool(metadata_ok),
        "blockers": blockers,
        "safe_default_action": (
            "confirm_resolution_source_then_fetch_settlement_actual"
            if metadata_ok
            else "repair_resolution_source_station_metadata_before_fetch"
        ),
        "suggested_after_confirmation": {
            "impact": "would insert a new event_runs row with settlement_actual for this slug/date if fetch succeeds",
            "function": "scan_weather_market.refresh_settlement_actual_for_pending_event",
            "args": {
                "run_date": event_date,
                "event_date": event_date,
                "slug": slug,
                "title": str(latest.get("title") or slug),
            },
        },
    }


def _attach_fetch_preview(row: dict[str, Any]) -> dict[str, Any]:
    out = dict(row)
    if not bool(out.get("metadata_ok_for_settlement_fetch")):
        out["fetch_preview"] = {
            "attempted": False,
            "ok": False,
            "reason": "metadata_not_ok_for_settlement_fetch",
        }
        return out
    try:
        import scan_weather_market as swm  # noqa: WPS433

        actual = swm.fetch_settlement_actual(
            out.get("resolution_source"),
            str(out.get("event_date") or ""),
            str(out.get("market_var") or ""),
            str(out.get("market_unit") or "") or None,
            station_id=str(out.get("station_id") or "") or None,
        )
    except Exception as exc:
        out["fetch_preview"] = {
            "attempted": True,
            "ok": False,
            "reason": f"fetch_error:{type(exc).__name__}",
            "error": str(exc)[:500],
        }
        return out
    out["fetch_preview"] = {
        "attempted": True,
        "ok": actual is not None,
        "candidate_settlement_actual": actual,
        "source": str(out.get("resolution_source") or ""),
        "station_id": str(out.get("station_id") or ""),
        "market_unit": str(out.get("market_unit") or ""),
        "reason": "candidate_actual_fetched" if actual is not None else "candidate_actual_missing",
    }
    return out


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    wakeup_path: Path = DEFAULT_WAKEUP,
    fetch_preview: bool = False,
) -> dict[str, Any]:
    wakeup = _load_json(wakeup_path)
    triggered = [
        item
        for item in wakeup.get("triggered_markets") or []
        if isinstance(item, Mapping)
        and str(item.get("current_status") or "") == "closed_without_settlement_actual"
    ]
    rows = [_row_for_market(db_path, item) for item in triggered]
    if fetch_preview:
        rows = [_attach_fetch_preview(row) for row in rows]
    return {
        "ok": not rows,
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "wakeup_path": str(wakeup_path),
        "wakeup_loaded": bool(wakeup),
        "fetch_preview_requested": bool(fetch_preview),
        "missing_settlement_actual_count": len(rows),
        "rows": rows,
        "blockers": [f"{row['slug']}:closed_without_settlement_actual" for row in rows],
        "next_action": (
            "no_missing_settlement_actual_repairs"
            if not rows
            else "review_repair_plan_and_confirm_before_any_settlement_actual_write"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only repair plan for closed markets missing settlement_actual.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--wakeup", default=str(DEFAULT_WAKEUP))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--fetch-preview", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(
        Path(args.db),
        wakeup_path=Path(args.wakeup),
        fetch_preview=bool(args.fetch_preview),
    )
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
