#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import settlement_actual_repair_plan  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_REPAIR_PLAN = ROOT / "data" / "reports" / "settlement_actual_repair_plan_latest.json"
DEFAULT_OUT = ROOT / "data" / "reports" / "settlement_actual_apply_plan_latest.json"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
APPLY_CONFIRM_PHRASE = "APPLY_SETTLEMENT_ACTUALS"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
    backup_path = backup_dir / f"archive.before-settlement-actual-apply.{stamp}.db"
    suffix = 1
    while backup_path.exists():
        backup_path = backup_dir / f"archive.before-settlement-actual-apply.{stamp}.{suffix}.db"
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
        "reason": "created_before_direct_actual_apply",
    }


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


def _latest_event_row(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
) -> dict[str, Any] | None:
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT *
        FROM event_runs
        WHERE slug = ?
          AND (? = '' OR event_date = ?)
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (slug, event_date, event_date),
    ).fetchone()
    return dict(row) if row is not None else None


def _existing_settlement_actual(
    conn: sqlite3.Connection,
    *,
    slug: str,
    event_date: str,
) -> float | None:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """
        SELECT settlement_actual
        FROM event_runs
        WHERE slug = ?
          AND (? = '' OR event_date = ?)
          AND settlement_actual IS NOT NULL
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """,
        (slug, event_date, event_date),
    ).fetchall()
    for row in rows:
        actual = _to_float(row["settlement_actual"])
        if actual is not None:
            return actual
    return None


def _candidate_actual(row: Mapping[str, Any]) -> float | None:
    preview = row.get("fetch_preview")
    if not isinstance(preview, Mapping):
        return None
    if not bool(preview.get("ok")):
        return None
    return _to_float(preview.get("candidate_settlement_actual"))


def _eligible_repairs(plan: Mapping[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for item in plan.get("rows") or []:
        if not isinstance(item, Mapping):
            continue
        actual = _candidate_actual(item)
        row = dict(item)
        row["candidate_settlement_actual"] = actual
        blockers = list(row.get("blockers") or [])
        if not bool(row.get("metadata_ok_for_settlement_fetch")):
            blockers.append("metadata_not_ok_for_settlement_fetch")
        if actual is None:
            blockers.append("candidate_settlement_actual_missing")
        row["apply_eligible"] = not blockers
        row["apply_blockers"] = blockers
        rows.append(row)
    return rows


def _review_item(row: Mapping[str, Any]) -> dict[str, Any]:
    preview = row.get("fetch_preview")
    if not isinstance(preview, Mapping):
        preview = {}
    actual = row.get("candidate_settlement_actual")
    source = preview.get("source") or row.get("resolution_source") or ""
    station = preview.get("station_id") or row.get("station_id") or row.get("source_station_id") or ""
    station_source_match = row.get("station_source_match")
    if station_source_match is None and station:
        station_source_match = str(row.get("source_station_id") or station).strip().upper() == str(station).strip().upper()
    slug = str(row.get("slug") or "").strip()
    event_date = str(row.get("event_date") or "").split("T", 1)[0]
    basis = {
        "slug": slug,
        "event_date": event_date,
        "actual": actual,
        "source": source,
        "station_id": station,
    }
    checksum = hashlib.sha256(
        json.dumps(basis, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:16]
    return {
        "city": row.get("city"),
        "slug": slug,
        "event_date": event_date,
        "title": row.get("title"),
        "actual": actual,
        "market_unit": row.get("market_unit") or preview.get("market_unit"),
        "resolution_source": row.get("resolution_source"),
        "source": source,
        "station_id": station,
        "source_station_id": row.get("source_station_id"),
        "station_source_match": bool(station_source_match),
        "event_run_count": row.get("event_run_count"),
        "latest_event_run_id": row.get("latest_event_run_id"),
        "latest_event_run_at": row.get("latest_event_run_at"),
        "apply_eligible": bool(row.get("apply_eligible")),
        "blockers": list(row.get("apply_blockers") or []),
        "review_checksum": checksum,
    }


def _review_packet(rows: list[Mapping[str, Any]], *, ready_count: int) -> dict[str, Any]:
    items = [_review_item(row) for row in rows]
    return {
        "required_confirm_apply": APPLY_CONFIRM_PHRASE,
        "ready_count": int(ready_count),
        "candidate_count": len(rows),
        "would_insert_event_run_rows": int(ready_count),
        "write_policy": "append_only_event_runs_settlement_actual_rows",
        "requires_manual_review": int(ready_count) > 0,
        "items": items,
        "confirm_command": (
            ".venv/bin/python scripts/apply_settlement_validation_flow.py "
            "--since-days 42 --market-scope highest_temperature "
            "--apply-actuals --confirm-actuals APPLY_SETTLEMENT_ACTUALS"
            if int(ready_count) > 0
            else ""
        ),
    }


def _insert_settlement_row(
    conn: sqlite3.Connection,
    *,
    plan_row: Mapping[str, Any],
    created_at: str,
) -> dict[str, Any]:
    slug = str(plan_row.get("slug") or "").strip()
    event_date = str(plan_row.get("event_date") or "").split("T", 1)[0]
    actual = _to_float(plan_row.get("candidate_settlement_actual"))
    if not slug or not event_date:
        return {"ok": False, "status": "missing_slug_or_event_date", "actual": None}
    if actual is None:
        return {"ok": False, "status": "candidate_settlement_actual_missing", "actual": None}
    existing = _existing_settlement_actual(conn, slug=slug, event_date=event_date)
    if existing is not None:
        return {"ok": True, "status": "already_settled", "actual": existing}
    latest = _latest_event_row(conn, slug=slug, event_date=event_date)
    if latest is None:
        return {"ok": False, "status": "event_run_missing", "actual": None}
    cols = [col for col in _columns(conn, "event_runs") if col != "id"]
    if "settlement_actual" not in cols:
        return {"ok": False, "status": "settlement_actual_column_missing", "actual": None}
    next_row = {col: latest.get(col) for col in cols}
    for key in (
        "slug",
        "event_date",
        "title",
        "event_url",
        "resolution_source",
        "station_id",
        "market_var",
        "market_unit",
        "event_end_utc",
    ):
        value = plan_row.get(key)
        if key in next_row and value not in (None, ""):
            next_row[key] = value
    if "run_date" in next_row:
        next_row["run_date"] = event_date
    if "market_closed" in next_row:
        next_row["market_closed"] = 1
    if "settlement_actual" in next_row:
        next_row["settlement_actual"] = float(actual)
    if "created_at" in next_row:
        next_row["created_at"] = created_at
    placeholders = ", ".join("?" for _ in cols)
    quoted_cols = ", ".join(cols)
    conn.execute(
        f"INSERT INTO event_runs ({quoted_cols}) VALUES ({placeholders})",
        [next_row.get(col) for col in cols],
    )
    return {"ok": True, "status": "settlement_actual_inserted", "actual": float(actual)}


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    repair_plan_path: Path = DEFAULT_REPAIR_PLAN,
    refresh_preview: bool = False,
    apply: bool = False,
    confirm_apply: str = "",
    backup_before_apply: bool = True,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    require_pre_apply_backup: bool = True,
) -> dict[str, Any]:
    if refresh_preview:
        plan = settlement_actual_repair_plan.build_report(db_path, fetch_preview=True)
    else:
        plan = _load_json(repair_plan_path)
    rows = _eligible_repairs(plan)
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
            "reason": "handled_by_parent_apply_flow",
        }
    apply_allowed = bool(apply_requested and confirmation_ok and backup_gate_allows_apply)
    results: list[dict[str, Any]] = []
    created_at = _utc_now()
    inserted_count = 0
    if apply_requested and confirmation_ok and not backup_gate_allows_apply:
        results = [
            {
                "city": row.get("city"),
                "slug": row.get("slug"),
                "event_date": row.get("event_date"),
                "ok": False,
                "status": "pre_apply_backup_not_created",
                "actual": row.get("candidate_settlement_actual"),
                "blockers": ["pre_apply_backup_not_created"],
            }
            for row in rows
        ]
    elif apply_requested and not confirmation_ok:
        results = [
            {
                "city": row.get("city"),
                "slug": row.get("slug"),
                "event_date": row.get("event_date"),
                "ok": False,
                "status": "missing_apply_confirmation",
                "actual": row.get("candidate_settlement_actual"),
            }
            for row in rows
        ]
    elif apply_allowed:
        conn = sqlite3.connect(db_path, timeout=5.0)
        try:
            for row in rows:
                if not row.get("apply_eligible"):
                    result = {
                        "ok": False,
                        "status": "not_apply_eligible",
                        "actual": row.get("candidate_settlement_actual"),
                        "blockers": row.get("apply_blockers"),
                    }
                else:
                    result = _insert_settlement_row(conn, plan_row=row, created_at=created_at)
                    if result.get("status") == "settlement_actual_inserted":
                        inserted_count += 1
                results.append(
                    {
                        "city": row.get("city"),
                        "slug": row.get("slug"),
                        "event_date": row.get("event_date"),
                        **result,
                    }
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        results = [
            {
                "city": row.get("city"),
                "slug": row.get("slug"),
                "event_date": row.get("event_date"),
                "ok": bool(row.get("apply_eligible")),
                "status": "dry_run_ready" if row.get("apply_eligible") else "dry_run_blocked",
                "actual": row.get("candidate_settlement_actual"),
                "blockers": row.get("apply_blockers"),
            }
            for row in rows
        ]
    blocked = [row for row in results if not bool(row.get("ok"))]
    ready_count = sum(1 for row in rows if row.get("apply_eligible"))
    return {
        "ok": not blocked,
        "read_only": not apply_allowed,
        "generated_at": created_at,
        "db_path": str(db_path),
        "repair_plan_path": str(repair_plan_path),
        "repair_plan_loaded": bool(plan),
        "refresh_preview_requested": bool(refresh_preview),
        "apply_requested": apply_requested,
        "apply_confirmation_ok": confirmation_ok,
        "apply_allowed": apply_allowed,
        "pre_apply_backup": pre_apply_backup,
        "required_confirm_apply": APPLY_CONFIRM_PHRASE,
        "candidate_count": len(rows),
        "ready_count": ready_count,
        "inserted_count": inserted_count,
        "results": results,
        "review_packet": _review_packet(rows, ready_count=ready_count),
        "next_action": (
            "rebuild_settlement_calibration_labels"
            if inserted_count
            else (
                "rerun_with_confirm_apply_after_review"
                if rows and not apply_allowed
                else "no_settlement_actual_repairs_ready"
            )
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply reviewed settlement_actual repair plan rows. Default is dry-run."
    )
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--repair-plan", default=str(DEFAULT_REPAIR_PLAN))
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--refresh-preview", action="store_true")
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
        repair_plan_path=Path(args.repair_plan),
        refresh_preview=bool(args.refresh_preview),
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
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
