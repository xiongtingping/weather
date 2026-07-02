#!/usr/bin/env python3
"""Read-only repair/merge for physical backtest snapshot apply plans."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_PLAN = ROOT / "data" / "reports" / "physical_backtest_snapshot_apply_plan_latest.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import dry_run_physical_feature_reconstruction as dry_run  # noqa: E402

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw or ""))
    except Exception:
        return {}


def _plan_rows(plan_doc: Mapping[str, Any]) -> list[dict[str, Any]]:
    projection = plan_doc.get("candidate_projection")
    apply_plan = (
        projection.get("physical_backtest_apply_plan")
        if isinstance(projection, Mapping)
        else None
    )
    rows = apply_plan.get("rows") if isinstance(apply_plan, Mapping) else None
    return [dict(row) for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _row_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (
        str(row.get("slug") or "").strip(),
        str(row.get("event_date") or "").split("T", 1)[0],
    )


def _merge_plan_rows(existing: Sequence[Mapping[str, Any]], new_rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    merged: dict[tuple[str, str], dict[str, Any]] = {}
    for row in existing:
        key = _row_key(row)
        if key[0] and key[1]:
            merged[key] = dict(row)
    for row in new_rows:
        key = _row_key(row)
        if key[0] and key[1]:
            merged[key] = dict(row)
    return sorted(merged.values(), key=lambda row: (str(row.get("event_date") or ""), str(row.get("slug") or "")))


def _plan_row_from_projection(projection: Mapping[str, Any]) -> dict[str, Any] | None:
    plan = dry_run._candidate_physical_backtest_apply_plan(  # type: ignore[attr-defined]
        [projection],
        max_rows=0,
        include_payload=True,
    )
    rows = plan.get("rows") if isinstance(plan, Mapping) else None
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
        return None
    return dict(rows[0])


def _refresh_weather_com_temperature(row: Mapping[str, Any], *, timeout: float) -> dict[str, Any] | None:
    payload = row.get("snapshot_payload")
    if not isinstance(payload, Mapping):
        return None
    preview = dry_run._fetch_wu_official_settlement_preview(  # type: ignore[attr-defined]
        row.get("resolution_source") or "",
        str(row.get("event_date") or ""),
        str(row.get("market_unit") or payload.get("market_unit") or ""),
        timeout=float(timeout),
    )
    temp_features = preview.get("temperature_features") if isinstance(preview, Mapping) else None
    if not isinstance(temp_features, Mapping) or not temp_features:
        return None
    out = dict(row)
    next_payload = dict(payload)
    features = dict(next_payload.get("weather_likelihood_features") or {})
    dry_run._merge_stronger_temperature_trajectory(features, temp_features)  # type: ignore[attr-defined]
    next_payload["weather_likelihood_features"] = features
    next_payload["weather_likelihood_feature_groups"] = dry_run._derived_feature_group_status(features)
    next_payload["weather_likelihood_feature_quality"] = dry_run._feature_quality(features)
    next_payload["weather_likelihood_feature_digest"] = dry_run._stable_digest(  # type: ignore[attr-defined]
        {
            key: features.get(key)
            for key in sorted(features)
            if key not in {"raw_response", "source_url"}
        }
    )
    out["snapshot_payload"] = next_payload
    out["snapshot_payload_digest"] = dry_run._stable_digest(next_payload)  # type: ignore[attr-defined]
    out["feature_quality"] = next_payload["weather_likelihood_feature_quality"]
    out["feature_digest"] = next_payload["weather_likelihood_feature_digest"]
    out["feature_key_count"] = len(features)
    out["weather_com_temperature_refreshed"] = True
    return out


def _update_plan_doc(
    plan_doc: Mapping[str, Any],
    *,
    merged_rows: Sequence[Mapping[str, Any]],
    added_rows: Sequence[Mapping[str, Any]],
    repair_meta: Mapping[str, Any],
) -> dict[str, Any]:
    out = dict(plan_doc)
    projection = dict(out.get("candidate_projection") or {})
    apply_plan = dict(projection.get("physical_backtest_apply_plan") or {})
    repair_out = {
        "read_only": True,
        "writes_performed": False,
        **dict(repair_meta),
    }
    apply_plan.update(
        {
            "ok": len(merged_rows) >= 100,
            "read_only": True,
            "writes_performed": False,
            "apply_allowed": False,
            "requires_confirmation": True,
            "required_confirm_apply": dry_run.PHYSICAL_BACKTEST_SNAPSHOT_CONFIRM_PHRASE,
            "ready_row_count": len(merged_rows),
            "row_sample_count": len(merged_rows),
            "rows_include_payload": True,
            "rows": [dict(row) for row in merged_rows],
            "repaired_added_row_count": len(added_rows),
            "repaired_at": repair_meta.get("generated_at"),
        }
    )
    projection["physical_backtest_apply_plan"] = apply_plan
    projection["physical_backtest_apply_plan_repair"] = repair_out
    out["candidate_projection"] = projection
    out["mode"] = "dry_run_read_only_repaired_apply_plan"
    out["writes_performed"] = False
    return out


def build_repair_report(
    *,
    db_path: Path,
    plan_path: Path,
    out_path: Path,
    days: int,
    max_attempts: int,
    target_ready_rows: int,
    timeout: float,
    include_current_day: bool,
    refresh_existing_temperature: bool = False,
) -> dict[str, Any]:
    generated_at = datetime.now(timezone.utc).isoformat()
    plan_doc = _safe_json(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
    if not isinstance(plan_doc, Mapping):
        plan_doc = {}
    existing_rows = _plan_rows(plan_doc)
    existing_keys = {_row_key(row) for row in existing_rows}
    tz = ZoneInfo("America/New_York") if ZoneInfo is not None else timezone(timedelta(hours=-5))
    today_et = datetime.now(timezone.utc).astimezone(tz).date()
    end_day = today_et if include_current_day else today_et - timedelta(days=1)
    start_date = (end_day - timedelta(days=max(1, int(days)) - 1)).isoformat()
    end_date = end_day.isoformat()
    attempts: list[dict[str, Any]] = []
    added_rows: list[dict[str, Any]] = []
    blockers: list[str] = []
    if not db_path.exists():
        blockers.append("archive_db_missing")
    else:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=20)
        conn.row_factory = sqlite3.Row
        try:
            combined = dry_run._combined_candidate_records(conn, start_date, end_date, [])  # type: ignore[attr-defined]
        finally:
            conn.close()
        attempts_budget = max(0, int(max_attempts))
        if refresh_existing_temperature and attempts_budget > 0:
            refresh_candidates = [
                row
                for row in existing_rows
                if start_date <= str(row.get("event_date") or "") <= end_date
                and dry_run._resolution_source_kind(row.get("resolution_source") or "") == "wunderground_history"  # type: ignore[attr-defined]
            ]
            for row in refresh_candidates[:attempts_budget]:
                refreshed = _refresh_weather_com_temperature(row, timeout=float(timeout))
                attempt = {
                    "slug": row.get("slug") or "",
                    "event_date": row.get("event_date") or "",
                    "refresh_existing_temperature": True,
                    "added": refreshed is not None,
                    "weather_com_temperature_refreshed": refreshed is not None,
                }
                if refreshed is None:
                    attempt["blocker"] = "weather_com_temperature_features_missing"
                else:
                    added_rows.append(refreshed)
                attempts.append(attempt)
            attempts_budget = max(0, attempts_budget - len(attempts))

        candidates = [
            item
            for key, item in sorted(combined.items(), key=lambda kv: (kv[0][1], kv[0][0]))
            if key not in existing_keys
            and dry_run._resolution_source_kind(item.get("resolution_source") or "") == "wunderground_history"  # type: ignore[attr-defined]
        ]
        completed_existing = [
            row for row in existing_rows if start_date <= str(row.get("event_date") or "") <= end_date
        ]
        target_total = max(0, int(target_ready_rows))
        for item in candidates[:attempts_budget]:
            if len(completed_existing) + len(added_rows) >= target_total:
                break
            projection = dry_run._candidate_projection_record(  # type: ignore[attr-defined]
                item,
                fetch_open_meteo_archive=True,
                fetch_open_meteo_historical_forecast_fallback=True,
                fetch_settlement_preview=True,
                timeout=float(timeout),
            )
            attempt = {
                "slug": item.get("slug") or "",
                "event_date": item.get("event_date") or "",
                "projected_complete": bool(projection.get("projected_complete")),
                "settlement_preview_ready": bool(projection.get("settlement_preview_ready")),
                "backtest_row_ready_preview": bool(projection.get("backtest_row_ready_preview")),
                "source_attempts": projection.get("source_attempts") or {},
                "settlement_preview_status": (
                    projection.get("settlement_preview", {}).get("status")
                    if isinstance(projection.get("settlement_preview"), Mapping)
                    else ""
                ),
            }
            if projection.get("backtest_row_ready_preview"):
                row = _plan_row_from_projection(projection)
                if row is not None:
                    added_rows.append(row)
                    attempt["added"] = True
                else:
                    attempt["added"] = False
                    attempt["blocker"] = "plan_row_build_failed"
            else:
                attempt["added"] = False
            attempts.append(attempt)
    merged_rows = _merge_plan_rows(existing_rows, added_rows)
    repair_meta = {
        "generated_at": generated_at,
        "read_only": True,
        "writes_performed": False,
        "start_date_et": start_date,
        "end_date_et": end_date,
        "include_current_day": bool(include_current_day),
        "existing_row_count": len(existing_rows),
        "added_row_count": len(added_rows),
        "merged_row_count": len(merged_rows),
        "attempt_count": len(attempts),
        "target_ready_rows": int(target_ready_rows),
        "max_attempts": int(max_attempts),
        "refresh_existing_temperature": bool(refresh_existing_temperature),
    }
    repaired_doc = _update_plan_doc(
        plan_doc,
        merged_rows=merged_rows,
        added_rows=added_rows,
        repair_meta=repair_meta,
    )
    if not blockers:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(repaired_doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return {
        "ok": not blockers,
        "read_only": True,
        "writes_performed": False,
        "db": str(db_path),
        "plan": str(plan_path),
        "out": str(out_path),
        "blockers": blockers,
        "repair": repair_meta,
        "attempts": attempts,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--plan", default=str(DEFAULT_PLAN))
    ap.add_argument("--out", default=str(DEFAULT_PLAN))
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--max-attempts", type=int, default=30)
    ap.add_argument("--target-ready-rows", type=int, default=105)
    ap.add_argument("--timeout-sec", type=float, default=12.0)
    ap.add_argument("--include-current-day", action="store_true")
    ap.add_argument("--refresh-existing-temperature", action="store_true")
    args = ap.parse_args()
    report = build_repair_report(
        db_path=Path(args.db),
        plan_path=Path(args.plan),
        out_path=Path(args.out),
        days=int(args.days),
        max_attempts=int(args.max_attempts),
        target_ready_rows=int(args.target_ready_rows),
        timeout=float(args.timeout_sec),
        include_current_day=bool(args.include_current_day),
        refresh_existing_temperature=bool(args.refresh_existing_temperature),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
