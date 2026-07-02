#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_PLAN = ROOT / "data" / "reports" / "recommended_pilot_gap_scan_plan_latest.json"
DEFAULT_OUT = ROOT / "data" / "reports" / "recommended_pilot_gap_refresh_health_latest.json"
DEFAULT_TOPN = 4


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _parse_ts(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _age_sec(value: Any, now: float) -> int | None:
    ts = _parse_ts(value)
    if ts is None:
        return None
    return int(max(0, now - ts))


def _priority_slugs(plan: Mapping[str, Any], *, topn: int = 0) -> list[str]:
    out: list[str] = []
    for value in plan.get("priority_slugs") or []:
        text = str(value or "").strip()
        if text and text not in out:
            out.append(text)
    for item in plan.get("plan") or []:
        if not isinstance(item, Mapping):
            continue
        for value in item.get("open_market_slugs") or []:
            text = str(value or "").strip()
            if text and text not in out:
                out.append(text)
    if int(topn or 0) > 0:
        return out[: int(topn)]
    return out


def _latest_by_slug(db_path: Path, slugs: list[str]) -> dict[str, dict[str, Any]]:
    if not db_path.exists() or not slugs:
        return {}
    placeholders = ",".join("?" for _ in slugs)
    out: dict[str, dict[str, Any]] = {slug: {} for slug in slugs}
    conn = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        for table, column in (
            ("event_market_ticks", "tick_at"),
            ("model_snapshots", "model_at"),
            ("event_runs", "event_run_at"),
        ):
            try:
                rows = cur.execute(
                    f"""
                    SELECT slug, MAX(created_at) AS ts
                    FROM {table}
                    WHERE slug IN ({placeholders})
                    GROUP BY slug
                    """,
                    slugs,
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
            for row in rows:
                slug = str(row["slug"] or "").strip()
                if slug:
                    out.setdefault(slug, {})[column] = row["ts"]
        try:
            rows = cur.execute(
                f"""
                SELECT slug, MAX(fact_as_of) AS ts
                FROM event_fact_history
                WHERE slug IN ({placeholders})
                GROUP BY slug
                """,
                slugs,
            ).fetchall()
        except sqlite3.OperationalError:
            rows = []
        for row in rows:
            slug = str(row["slug"] or "").strip()
            if slug:
                out.setdefault(slug, {})["fact_as_of"] = row["ts"]
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
    return out


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    plan_path: Path = DEFAULT_PLAN,
    max_tick_age_sec: int = 900,
    max_model_age_sec: int = 21600,
    max_fact_age_sec: int = 21600,
    topn: int = DEFAULT_TOPN,
) -> dict[str, Any]:
    now = time.time()
    plan = _load_json(plan_path)
    all_slugs = _priority_slugs(plan, topn=0)
    slugs = _priority_slugs(plan, topn=max(0, int(topn or 0)))
    blockers: list[str] = []
    warnings: list[str] = []
    rows: list[dict[str, Any]] = []
    latest = _latest_by_slug(db_path, slugs)
    queue_count = int(plan.get("queue_count") or 0) if isinstance(plan, Mapping) else 0
    if queue_count > 0 and not slugs:
        blockers.append("recommended_pilot_gap_priority_slugs_missing")
    if not plan:
        warnings.append("recommended_pilot_gap_plan_missing_or_unreadable")
    for slug in slugs:
        item = latest.get(slug) or {}
        tick_age = _age_sec(item.get("tick_at"), now)
        model_age = _age_sec(item.get("model_at"), now)
        fact_age = _age_sec(item.get("fact_as_of"), now)
        slug_blockers: list[str] = []
        slug_warnings: list[str] = []
        if tick_age is None:
            slug_blockers.append("tick_missing")
        elif tick_age > int(max_tick_age_sec):
            slug_blockers.append("tick_stale")
        if model_age is None:
            slug_blockers.append("model_missing")
        elif model_age > int(max_model_age_sec):
            slug_warnings.append("model_stale")
        if fact_age is None:
            slug_blockers.append("fact_missing")
        elif fact_age > int(max_fact_age_sec):
            slug_warnings.append("fact_stale")
        blockers.extend(f"{slug}:{code}" for code in slug_blockers)
        warnings.extend(f"{slug}:{code}" for code in slug_warnings)
        rows.append(
            {
                "slug": slug,
                "tick_at": item.get("tick_at"),
                "tick_age_sec": tick_age,
                "model_at": item.get("model_at"),
                "model_age_sec": model_age,
                "fact_as_of": item.get("fact_as_of"),
                "fact_age_sec": fact_age,
                "event_run_at": item.get("event_run_at"),
                "blockers": slug_blockers,
                "warnings": slug_warnings,
                "next_action": (
                    "refresh_market_ticks_for_priority_gap_slug"
                    if "tick_missing" in slug_blockers or "tick_stale" in slug_blockers
                    else (
                        "refresh_model_fact_for_priority_gap_slug"
                        if "model_missing" in slug_blockers or "fact_missing" in slug_blockers
                        else "priority_gap_slug_refresh_ok"
                    )
                ),
            }
        )
    return {
        "ok": not blockers,
        "read_only": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "db_path": str(db_path),
        "plan_path": str(plan_path),
        "plan_loaded": bool(plan),
        "queue_count": queue_count,
        "checked_topn": max(0, int(topn or 0)),
        "priority_slug_count": len(slugs),
        "total_priority_slug_count": len(all_slugs),
        "thresholds": {
            "max_tick_age_sec": int(max_tick_age_sec),
            "max_model_age_sec": int(max_model_age_sec),
            "max_fact_age_sec": int(max_fact_age_sec),
        },
        "rows": rows,
        "blockers": blockers,
        "warnings": warnings,
        "next_action": (
            "recommended_pilot_gap_refresh_ok"
            if not blockers
            else "refresh_recommended_pilot_gap_priority_slugs"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only freshness health for recommended pilot priority gap slugs."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--plan", default=str(DEFAULT_PLAN))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--max-tick-age-sec", type=int, default=900)
    parser.add_argument("--max-model-age-sec", type=int, default=21600)
    parser.add_argument("--max-fact-age-sec", type=int, default=21600)
    parser.add_argument("--topn", type=int, default=DEFAULT_TOPN)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(
        Path(args.db),
        plan_path=Path(args.plan),
        max_tick_age_sec=int(args.max_tick_age_sec),
        max_model_age_sec=int(args.max_model_age_sec),
        max_fact_age_sec=int(args.max_fact_age_sec),
        topn=max(0, int(args.topn or 0)),
    )
    out_path = Path(args.out) if str(args.out or "").strip() else None
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    print(json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=not args.json))
    return 0 if report.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
