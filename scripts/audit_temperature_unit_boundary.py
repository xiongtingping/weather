#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _json_loads(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        data = json.loads(str(value))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _latest_fact_payload(conn: sqlite3.Connection, slug: str, event_date: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        LIMIT 1
        """,
        (slug, event_date),
    ).fetchone()
    if row is None:
        return {}
    return _json_loads(row["fact_json"])


def _reverse_fahrenheit_profile(actual_c: float, *, tolerance: float) -> dict[str, Any]:
    f_value = float(actual_c) * 9.0 / 5.0 + 32.0
    f_integer = round(f_value)
    maps_to_f_integer = abs(float(f_value) - float(f_integer)) <= float(tolerance)
    actual_is_integer_c = abs(float(actual_c) - round(float(actual_c))) <= float(tolerance)
    reverse_f_integer = bool(maps_to_f_integer and not actual_is_integer_c)
    profile: dict[str, Any] = {
        "reverse_f_integer": bool(reverse_f_integer),
        "maps_to_f_integer": bool(maps_to_f_integer),
        "actual_is_integer_c": bool(actual_is_integer_c),
        "reverse_f_value": float(f_value),
        "reverse_f_integer_value": int(f_integer),
        "boundary_ambiguous": False,
        "interval_c": None,
        "interval_floor_buckets": None,
    }
    if reverse_f_integer:
        lo_c = (float(f_integer) - 0.5 - 32.0) * 5.0 / 9.0
        hi_c = (float(f_integer) + 0.5 - 32.0) * 5.0 / 9.0
        lo_bucket = math.floor(lo_c)
        hi_bucket = math.floor(hi_c)
        profile["interval_c"] = [lo_c, hi_c]
        profile["interval_floor_buckets"] = [int(lo_bucket), int(hi_bucket)]
        profile["boundary_ambiguous"] = bool(lo_bucket != hi_bucket)
    return profile


def _audit_row(conn: sqlite3.Connection, row: Mapping[str, Any], *, tolerance: float) -> dict[str, Any]:
    slug = str(row.get("slug") or "")
    event_date = str(row.get("event_date") or "").split("T", 1)[0]
    actual = _to_float(row.get("settlement_actual"))
    market_unit = str(row.get("market_unit") or "").strip().upper()
    profile = _reverse_fahrenheit_profile(float(actual), tolerance=tolerance) if actual is not None and market_unit == "C" else {}
    fact = _latest_fact_payload(conn, slug, event_date)
    features = fact.get("weather_likelihood_features") if isinstance(fact, Mapping) else {}
    if not isinstance(features, Mapping):
        features = {}
    awc_max = _to_float(features.get("zuuu_metar_raw_awc_today_max_c"))
    awc_raw = str(features.get("zuuu_metar_raw_awc_today_max_raw") or "")
    awc_floor = math.floor(float(awc_max)) if awc_max is not None else None
    actual_floor = math.floor(float(actual)) if actual is not None else None
    awc_conflict = bool(
        awc_floor is not None
        and actual_floor is not None
        and int(awc_floor) != int(actual_floor)
    )
    settlement_below_awc = bool(
        awc_floor is not None
        and actual_floor is not None
        and int(actual_floor) < int(awc_floor)
    )
    if profile.get("boundary_ambiguous"):
        recommended_action = "review_native_c_or_awc_before_using_as_final_settlement"
    elif settlement_below_awc:
        recommended_action = "reject_settlement_actual_below_awc_hard_observation"
    else:
        recommended_action = "ok"
    return {
        "event_date": event_date,
        "slug": slug,
        "settlement_actual": actual,
        "market_unit": market_unit,
        "rounding_rule": row.get("rounding_rule"),
        "created_at": row.get("created_at"),
        "reverse_fahrenheit_profile": profile,
        "unit_boundary_issue": bool(profile.get("boundary_ambiguous")),
        "awc_today_max_c": awc_max,
        "awc_today_max_floor": awc_floor,
        "awc_today_max_raw": awc_raw,
        "actual_floor": actual_floor,
        "awc_floor_conflict": awc_conflict,
        "settlement_below_awc_hard_observation": settlement_below_awc,
        "recommended_action": recommended_action,
    }


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    limit: int = 30,
    slug_like: str = "%chengdu%",
    tolerance: float = 1e-6,
) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            WITH latest AS (
              SELECT *,
                     row_number() OVER (
                       PARTITION BY slug, event_date
                       ORDER BY created_at DESC, id DESC
                     ) AS rn
              FROM event_runs
              WHERE settlement_actual IS NOT NULL
                AND lower(slug) LIKE lower(?)
            )
            SELECT *
            FROM latest
            WHERE rn = 1
            ORDER BY event_date DESC
            LIMIT ?
            """,
            (slug_like, int(limit)),
        ).fetchall()
        items = [_audit_row(conn, dict(row), tolerance=float(tolerance)) for row in rows]
    finally:
        conn.close()
    boundary = [item for item in items if item.get("unit_boundary_issue")]
    conflicts = [item for item in items if item.get("awc_floor_conflict")]
    below_awc = [item for item in items if item.get("settlement_below_awc_hard_observation")]
    return {
        "ok": True,
        "db_path": str(db_path),
        "scope": {"slug_like": slug_like, "limit": int(limit)},
        "checked": len(items),
        "unit_boundary_issue_count": len(boundary),
        "awc_floor_conflict_count": len(conflicts),
        "settlement_below_awc_hard_observation_count": len(below_awc),
        "policy": {
            "native_f_integer_to_c": "ambiguous_if_interval_crosses_c_floor_bucket",
            "safe_use": "do_not_final_lock_or_train_as_exact_settlement_until_native_c_or_hard_observation_confirms",
        },
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only audit for F/C temperature settlement boundary ambiguity.")
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--slug-like", default="%chengdu%")
    parser.add_argument("--tolerance", type=float, default=1e-6)
    args = parser.parse_args()
    print(
        json.dumps(
            build_report(args.db, limit=args.limit, slug_like=args.slug_like, tolerance=args.tolerance),
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
