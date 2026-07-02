#!/usr/bin/env python3
"""Apply reviewed physical backtest snapshot plan rows.

Default mode is dry-run. Confirmed apply appends event_runs rows only; it never
updates existing rows or overwrites existing settlement_actual values.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_PLAN = ROOT / "data" / "reports" / "physical_backtest_snapshot_apply_plan_latest.json"
DEFAULT_OUT = ROOT / "data" / "reports" / "physical_backtest_snapshot_apply_result_latest.json"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
APPLY_CONFIRM_PHRASE = "APPLY_PHYSICAL_BACKTEST_SNAPSHOTS"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _plan_body(plan: Mapping[str, Any]) -> Mapping[str, Any]:
    candidate_projection = plan.get("candidate_projection")
    if isinstance(candidate_projection, Mapping):
        body = candidate_projection.get("physical_backtest_apply_plan")
        if isinstance(body, Mapping):
            return body
    body = plan.get("physical_backtest_apply_plan")
    if isinstance(body, Mapping):
        return body
    return plan


def _plan_rows(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    body = _plan_body(plan)
    rows = body.get("rows")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, Mapping)]


def _sqlite_backup(
    db_path: Path,
    *,
    backup_before_apply: bool,
    backup_dir: Path,
) -> dict[str, Any]:
    if not backup_before_apply:
        return {
            "requested": True,
            "created": False,
            "path": "",
            "reason": "disabled_by_no_backup_before_apply",
        }
    if not db_path.exists():
        return {
            "requested": True,
            "created": False,
            "path": "",
            "reason": "source_db_missing",
        }
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"archive.before-physical-backtest-snapshots.{stamp}.db"
    suffix = 1
    while backup_path.exists():
        backup_path = backup_dir / f"archive.before-physical-backtest-snapshots.{stamp}.{suffix}.db"
        suffix += 1
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(backup_path), timeout=30)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
    finally:
        src.close()
    return {
        "requested": True,
        "created": True,
        "path": str(backup_path),
        "reason": "created_before_physical_backtest_snapshot_apply",
    }


def _existing_settlement_actual(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
) -> float | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT settlement_actual
        FROM event_runs
        WHERE slug = ?
          AND event_date = ?
          AND settlement_actual IS NOT NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (slug, event_date),
    ).fetchone()
    if row is None:
        return None
    return _to_float(row["settlement_actual"])


def _latest_event_row(conn: sqlite3.Connection, *, slug: str, event_date: str) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT *
        FROM event_runs
        WHERE slug = ?
          AND event_date = ?
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (slug, event_date),
    ).fetchone()
    return dict(row) if row is not None else None


def _event_end_utc(event_date: str) -> str:
    return f"{event_date}T12:00:00Z" if event_date else ""


def _candidate_actual(row: Mapping[str, Any], existing_actual: float | None) -> tuple[float | None, str]:
    if existing_actual is not None:
        return float(existing_actual), "existing_db"
    actual = _to_float(row.get("candidate_settlement_actual"))
    if actual is None:
        return None, "candidate_settlement_actual_missing"
    return float(actual), str(row.get("settlement_actual_source") or "candidate_preview")


def _payload_for_row(row: Mapping[str, Any], *, actual: float, applied_at: str) -> dict[str, Any] | None:
    payload = row.get("snapshot_payload")
    if not isinstance(payload, Mapping):
        return None
    out = dict(payload)
    rows = out.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    features = out.get("weather_likelihood_features")
    if not isinstance(features, Mapping) or not features:
        return None
    out["market_unit"] = row.get("market_unit") or out.get("market_unit") or ""
    out["market_var"] = row.get("market_var") or out.get("market_var") or "temperature_2m_max"
    out["settlement_actual"] = float(actual)
    out["weather_likelihood_feature_quality"] = (
        row.get("feature_quality") or out.get("weather_likelihood_feature_quality") or ""
    )
    out["weather_likelihood_feature_digest"] = (
        row.get("feature_digest") or out.get("weather_likelihood_feature_digest") or ""
    )
    out["physical_backtest_snapshot_meta"] = {
        "applied_at": applied_at,
        "source": "apply_physical_backtest_snapshot_plan",
        "review_checksum": row.get("review_checksum") or "",
        "snapshot_payload_digest": row.get("snapshot_payload_digest") or "",
        "settlement_actual_source": row.get("settlement_actual_source") or "",
        "write_policy": "append_only_event_runs_candidate_settlement_and_physical_feature_snapshot_rows",
    }
    return out


def _insert_snapshot_row(
    conn: sqlite3.Connection,
    *,
    row: Mapping[str, Any],
    actual: float,
    applied_at: str,
) -> dict[str, Any]:
    slug = str(row.get("slug") or "").strip()
    event_date = str(row.get("event_date") or "").split("T", 1)[0]
    if not slug or not event_date:
        return {"ok": False, "status": "missing_slug_or_event_date"}
    payload = _payload_for_row(row, actual=actual, applied_at=applied_at)
    if payload is None:
        return {"ok": False, "status": "snapshot_payload_missing_or_invalid"}
    cols = [col for col in _columns(conn, "event_runs") if col != "id"]
    required = {"slug", "event_date", "settlement_actual", "payload_json", "created_at"}
    missing_cols = sorted(required - set(cols))
    if missing_cols:
        return {"ok": False, "status": "event_runs_required_columns_missing", "missing_columns": missing_cols}
    latest = _latest_event_row(conn, slug=slug, event_date=event_date) or {}
    next_row = {col: latest.get(col) for col in cols}
    fill = {
        "run_date": event_date,
        "event_date": event_date,
        "slug": slug,
        "event_url": str(latest.get("event_url") or f"https://polymarket.com/event/{slug}"),
        "title": row.get("title") or latest.get("title") or slug,
        "city": latest.get("city") or row.get("station_id") or "",
        "lat": row.get("lat") if row.get("lat") is not None else latest.get("lat"),
        "lon": row.get("lon") if row.get("lon") is not None else latest.get("lon"),
        "tz": row.get("tz") or latest.get("tz") or "UTC",
        "location_basis": latest.get("location_basis") or "station",
        "resolution_source": row.get("resolution_source") or latest.get("resolution_source") or "",
        "station_id": row.get("station_id") or latest.get("station_id") or "",
        "rounding_rule": latest.get("rounding_rule") or "whole_degrees",
        "market_var": row.get("market_var") or latest.get("market_var") or "temperature_2m_max",
        "market_unit": row.get("market_unit") or latest.get("market_unit") or payload.get("market_unit") or "",
        "settlement_actual": float(actual),
        "payload_json": json.dumps(payload, ensure_ascii=False, sort_keys=True),
        "suggestions_json": latest.get("suggestions_json") or "{}",
        "created_at": applied_at,
        "event_end_utc": latest.get("event_end_utc") or _event_end_utc(event_date),
        "market_closed": 1,
    }
    for key, value in fill.items():
        if key in next_row:
            next_row[key] = value
    placeholders = ", ".join("?" for _ in cols)
    quoted_cols = ", ".join(cols)
    conn.execute(
        f"INSERT INTO event_runs ({quoted_cols}) VALUES ({placeholders})",
        [next_row.get(col) for col in cols],
    )
    return {"ok": True, "status": "snapshot_row_inserted", "actual": float(actual)}


def _review_row(conn: sqlite3.Connection, row: Mapping[str, Any]) -> dict[str, Any]:
    slug = str(row.get("slug") or "").strip()
    event_date = str(row.get("event_date") or "").split("T", 1)[0]
    existing_actual = _existing_settlement_actual(conn, slug=slug, event_date=event_date) if slug and event_date else None
    actual, actual_source = _candidate_actual(row, existing_actual)
    blockers: list[str] = []
    if not slug:
        blockers.append("slug_missing")
    if not event_date:
        blockers.append("event_date_missing")
    if actual is None:
        blockers.append(actual_source)
    if not isinstance(row.get("snapshot_payload"), Mapping):
        blockers.append("snapshot_payload_missing")
    if str(row.get("feature_quality") or "") != "complete":
        blockers.append("feature_quality_not_complete")
    if int(row.get("yes_price_count") or 0) < 2:
        blockers.append("probability_snapshot_missing")
    return {
        "slug": slug,
        "event_date": event_date,
        "actual": actual,
        "actual_source": actual_source,
        "review_checksum": row.get("review_checksum") or "",
        "snapshot_payload_digest": row.get("snapshot_payload_digest") or "",
        "apply_eligible": not blockers,
        "blockers": blockers,
    }


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    plan_path: Path = DEFAULT_PLAN,
    apply: bool = False,
    confirm_apply: str = "",
    backup_before_apply: bool = True,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    require_pre_apply_backup: bool = True,
) -> dict[str, Any]:
    plan = _load_json(plan_path)
    body = _plan_body(plan)
    rows = _plan_rows(plan)
    apply_requested = bool(apply)
    confirmation_ok = str(confirm_apply or "").strip() == APPLY_CONFIRM_PHRASE
    pre_apply_backup = {
        "requested": False,
        "created": False,
        "path": "",
        "reason": "no_confirmed_apply_requested",
    }
    backup_gate_allows_apply = True
    if apply_requested and confirmation_ok and bool(require_pre_apply_backup):
        pre_apply_backup = _sqlite_backup(
            db_path,
            backup_before_apply=bool(backup_before_apply),
            backup_dir=Path(backup_dir),
        )
        backup_gate_allows_apply = bool(pre_apply_backup.get("created"))
    elif apply_requested and confirmation_ok:
        pre_apply_backup = {
            "requested": False,
            "created": False,
            "path": "",
            "reason": "disabled_for_test_or_parent_backup",
        }
    apply_allowed = bool(apply_requested and confirmation_ok and backup_gate_allows_apply)
    reviewed: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    inserted_count = 0
    applied_at = _utc_now()
    conn_uri = str(db_path)
    if not apply_allowed:
        conn_uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(conn_uri, uri=not apply_allowed, timeout=30)
    try:
        for row in rows:
            review = _review_row(conn, row)
            reviewed.append(review)
            result_base = {
                "slug": review["slug"],
                "event_date": review["event_date"],
                "actual": review["actual"],
                "actual_source": review["actual_source"],
                "review_checksum": review["review_checksum"],
            }
            if apply_requested and not confirmation_ok:
                results.append({**result_base, "ok": False, "status": "missing_apply_confirmation"})
            elif apply_requested and confirmation_ok and not backup_gate_allows_apply:
                results.append({**result_base, "ok": False, "status": "pre_apply_backup_not_created"})
            elif not review.get("apply_eligible"):
                results.append({**result_base, "ok": False, "status": "not_apply_eligible", "blockers": review["blockers"]})
            elif apply_allowed:
                inserted = _insert_snapshot_row(conn, row=row, actual=float(review["actual"]), applied_at=applied_at)
                if inserted.get("status") == "snapshot_row_inserted":
                    inserted_count += 1
                results.append({**result_base, **inserted})
            else:
                results.append({**result_base, "ok": True, "status": "dry_run_ready"})
        if apply_allowed:
            conn.commit()
    except Exception:
        if apply_allowed:
            conn.rollback()
        raise
    finally:
        conn.close()
    ready_count = sum(1 for row in reviewed if row.get("apply_eligible"))
    blocked_count = len(reviewed) - ready_count
    return {
        "ok": blocked_count == 0 and bool(plan),
        "read_only": not apply_allowed,
        "writes_performed": bool(apply_allowed and inserted_count > 0),
        "generated_at": applied_at,
        "db_path": str(db_path),
        "plan_path": str(plan_path),
        "plan_loaded": bool(plan),
        "plan_write_policy": body.get("write_policy") or "",
        "required_confirm_apply": APPLY_CONFIRM_PHRASE,
        "apply_requested": apply_requested,
        "apply_confirmation_ok": confirmation_ok,
        "apply_allowed": apply_allowed,
        "pre_apply_backup": pre_apply_backup,
        "candidate_count": len(rows),
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "inserted_count": inserted_count,
        "reviewed": reviewed,
        "results": results,
        "next_action": (
            "rerun_backtest_physical_likelihood"
            if inserted_count
            else (
                "rerun_with_confirm_apply_after_review"
                if ready_count and not apply_allowed
                else "repair_plan_rows_before_apply"
            )
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--plan", default=str(DEFAULT_PLAN))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--confirm-apply",
        default="",
        help=f"Required with --apply. Must equal {APPLY_CONFIRM_PHRASE}.",
    )
    parser.add_argument(
        "--no-backup-before-apply",
        action="store_true",
        help="Disable direct-apply backup; confirmed apply will be blocked.",
    )
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = build_report(
        Path(args.db),
        plan_path=Path(args.plan),
        apply=bool(args.apply),
        confirm_apply=str(args.confirm_apply or ""),
        backup_before_apply=not bool(args.no_backup_before_apply),
        backup_dir=Path(args.backup_dir),
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
