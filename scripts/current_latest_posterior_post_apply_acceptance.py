#!/usr/bin/env python3
"""Read-only exact acceptance for current-latest posterior SSOT repairs.

This audit is meant to run after the controlled current-latest repair sequence.
It verifies each original candidate fact id was preserved in history, is no
longer the latest fact for its slug/date, and was replaced by a latest fact with
a valid posterior_ssot_fact matching the candidate's no-apply projection.

It never writes archive.db, creates backups, fetches weather data, restarts
launchd, or places orders.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from posterior_ssot_projection_runtime import build_fact_projection_no_apply  # noqa: E402
from scripts import posterior_ssot_fact_coverage_audit as coverage_audit  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
SCHEMA_VERSION = "current_latest_posterior_post_apply_acceptance_v1"


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _safe_json(raw: Any) -> Any:
    if isinstance(raw, Mapping):
        return dict(raw)
    try:
        parsed = json.loads(str(raw or ""))
    except Exception:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _parse_fact_ids(value: str | Sequence[Any] | None) -> list[int]:
    if value is None:
        values: Sequence[Any] = []
    elif isinstance(value, str):
        values = value.replace(";", ",").split(",")
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        values = value
    else:
        values = [value]
    out: list[int] = []
    for raw in values:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            fact_id = int(text)
        except Exception:
            continue
        if fact_id > 0 and fact_id not in out:
            out.append(fact_id)
    return out


def _parse_json_mapping_or_sequence(value: str | None) -> Any:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _expected_projection_map(value: Any) -> dict[int, Mapping[str, Any]]:
    rows: Sequence[Any]
    if isinstance(value, Mapping):
        out: dict[int, Mapping[str, Any]] = {}
        for key, raw in value.items():
            try:
                fact_id = int(key)
            except Exception:
                fact_id = int(_as_mapping(raw).get("fact_id") or 0)
            if fact_id > 0 and isinstance(raw, Mapping):
                out[fact_id] = raw
        return out
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        rows = value
    else:
        rows = ()
    out = {}
    for raw in rows:
        row = _as_mapping(raw)
        fact_id = int(row.get("fact_id") or row.get("candidate_fact_id") or 0)
        if fact_id > 0:
            out[fact_id] = row
    return out


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {}
    return {key: row[key] for key in row.keys()}


def _load_fact(conn: sqlite3.Connection, fact_id: int) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT id, run_date, slug, event_date, fact_as_of, composer_version, fact_json, created_at
        FROM event_fact_history
        WHERE id = ?
        """,
        (int(fact_id),),
    ).fetchone()
    return _row_to_dict(row)


def _load_latest_for_event(conn: sqlite3.Connection, *, slug: str, event_date: str) -> dict[str, Any]:
    row = conn.execute(
        """
        SELECT h.id, h.run_date, h.slug, h.event_date, h.fact_as_of, h.composer_version,
               h.fact_json, h.created_at, l.updated_at AS latest_updated_at
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        WHERE l.slug = ? AND l.event_date = ?
        """,
        (slug, event_date),
    ).fetchone()
    return _row_to_dict(row)


def _projection_for_candidate(
    conn: sqlite3.Connection,
    *,
    db_path: Path,
    candidate: Mapping[str, Any],
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    slug = str(candidate.get("slug") or payload.get("slug") or "")
    event_date = str(candidate.get("event_date") or payload.get("event_date") or "")
    projection = build_fact_projection_no_apply(
        slug=slug,
        event_date=event_date,
        run_date=str(payload.get("run_date") or candidate.get("run_date") or event_date),
        fact_as_of=str(payload.get("fact_as_of") or candidate.get("fact_as_of") or ""),
        payload=payload,
        current_fact_id=int(candidate.get("id") or 0),
        current_fact_as_of=str(candidate.get("fact_as_of") or ""),
        composer_version=str(candidate.get("composer_version") or ""),
        conn=conn,
        db_path=db_path,
        write_reason="current_latest_posterior_post_apply_acceptance",
        enable_apply_contract=True,
        write_latest=True,
    )
    projected_payload = _as_mapping(projection.get("projected_payload"))
    projected_audit = (
        coverage_audit.audit_fact_payload(projected_payload, meta=candidate)
        if projected_payload
        else {"ok": False, "issues": ["writer_projection_payload_missing"]}
    )
    ssot = _as_mapping(projected_payload.get("posterior_ssot_fact"))
    return {
        "ok": bool(projection.get("ok")) and bool(projected_audit.get("ok")),
        "projection_mode": projection.get("projection_mode"),
        "issues": list(projection.get("issues") or []) + list(projected_audit.get("issues") or []),
        "target_bucket": ssot.get("target_bucket"),
        "target_probability": ssot.get("target_probability"),
        "fact_snapshot_id": ssot.get("fact_snapshot_id"),
        "posterior_run_id": ssot.get("posterior_run_id"),
    }


def _prob_close(left: Any, right: Any, *, tolerance: float = 1e-9) -> bool:
    try:
        return abs(float(left) - float(right)) <= float(tolerance)
    except Exception:
        return False


def _audit_one(
    conn: sqlite3.Connection,
    *,
    db_path: Path,
    candidate_fact_id: int,
    expected_projection: Mapping[str, Any] | None = None,
    expected_projection_required: bool = False,
) -> dict[str, Any]:
    blockers: list[str] = []
    candidate = _load_fact(conn, int(candidate_fact_id))
    if not candidate:
        return {
            "candidate_fact_id": int(candidate_fact_id),
            "ok": False,
            "blockers": ["candidate_fact_row_missing"],
        }
    payload = _safe_json(candidate.get("fact_json"))
    if not isinstance(payload, Mapping):
        return {
            "candidate_fact_id": int(candidate_fact_id),
            "ok": False,
            "blockers": ["candidate_fact_json_invalid"],
            "slug": candidate.get("slug"),
            "event_date": candidate.get("event_date"),
        }
    slug = str(candidate.get("slug") or payload.get("slug") or "")
    event_date = str(candidate.get("event_date") or payload.get("event_date") or "")
    if not slug or not event_date:
        blockers.append("candidate_slug_or_event_date_missing")
    expected = _as_mapping(expected_projection)
    if expected:
        projection = {
            "ok": True,
            "projection_mode": "pre_apply_expected_projection",
            "issues": [],
            "target_bucket": expected.get("target_bucket")
            or expected.get("projected_target_bucket"),
            "target_probability": expected.get("target_probability")
            if expected.get("target_probability") is not None
            else expected.get("projected_target_probability"),
            "fact_snapshot_id": expected.get("fact_snapshot_id"),
            "posterior_run_id": expected.get("posterior_run_id"),
        }
        if not projection.get("target_bucket") or projection.get("target_probability") is None:
            projection["ok"] = False
            projection["issues"] = ["expected_projection_target_missing"]
    else:
        projection = _projection_for_candidate(conn, db_path=db_path, candidate=candidate, payload=payload)
        if expected_projection_required:
            projection["ok"] = False
            projection["issues"] = list(projection.get("issues") or []) + ["expected_projection_missing"]
    if projection.get("ok") is not True:
        blockers.append("candidate_projection_not_ok")
    latest = _load_latest_for_event(conn, slug=slug, event_date=event_date) if slug and event_date else {}
    if not latest:
        blockers.append("latest_fact_row_missing")
        latest_payload: Mapping[str, Any] = {}
        latest_audit: Mapping[str, Any] = {"ok": False, "issues": ["latest_fact_row_missing"]}
    else:
        latest_payload_raw = _safe_json(latest.get("fact_json"))
        latest_payload = latest_payload_raw if isinstance(latest_payload_raw, Mapping) else {}
        latest_audit = (
            coverage_audit.audit_fact_payload(latest_payload, meta=latest)
            if latest_payload
            else {"ok": False, "issues": ["latest_fact_json_invalid"]}
        )
    latest_fact_id = int(latest.get("id") or 0)
    if latest_fact_id == int(candidate_fact_id):
        blockers.append("candidate_fact_still_latest")
    if latest_audit.get("ok") is not True:
        blockers.append("latest_posterior_ssot_fact_not_ok")
    latest_ssot = _as_mapping(latest_payload.get("posterior_ssot_fact"))
    target_bucket_matches = bool(
        projection.get("target_bucket") and projection.get("target_bucket") == latest_ssot.get("target_bucket")
    )
    target_probability_matches = _prob_close(
        latest_ssot.get("target_probability"),
        projection.get("target_probability"),
    )
    if not target_bucket_matches:
        blockers.append("latest_target_bucket_mismatch")
    if not target_probability_matches:
        blockers.append("latest_target_probability_mismatch")
    return {
        "candidate_fact_id": int(candidate_fact_id),
        "ok": not blockers,
        "slug": slug,
        "event_date": event_date,
        "candidate_preserved_in_history": True,
        "latest_fact_id": latest_fact_id or None,
        "latest_replaced_candidate": bool(latest_fact_id and latest_fact_id != int(candidate_fact_id)),
        "projection": projection,
        "latest_posterior": {
            "ok": bool(latest_audit.get("ok")),
            "issues": list(latest_audit.get("issues") or []),
            "target_bucket": latest_ssot.get("target_bucket"),
            "target_probability": latest_ssot.get("target_probability"),
            "fact_snapshot_id": latest_ssot.get("fact_snapshot_id"),
            "posterior_run_id": latest_ssot.get("posterior_run_id"),
        },
        "target_bucket_matches_projection": target_bucket_matches,
        "target_probability_matches_projection": target_probability_matches,
        "blockers": sorted(set(blockers)),
    }


def _guards() -> dict[str, bool]:
    return {
        "writes_archive_db": False,
        "writes_event_fact_history": False,
        "writes_event_fact_latest": False,
        "writes_event_fact_summary_cache": False,
        "writes_auto_trade_decisions": False,
        "writes_daily_weather_reviews": False,
        "creates_backup": False,
        "fetches_external_weather_sources": False,
        "places_orders": False,
        "historical_backfill": False,
        "changes_parameters": False,
        "restarts_launchd": False,
        "executes_sql_writes": False,
    }


def build_acceptance(
    db_path: Path | str = DEFAULT_DB,
    *,
    candidate_fact_ids: str | Sequence[Any] | None = None,
    expected_projections: Any = None,
    max_items_output: int = 20,
) -> dict[str, Any]:
    db = Path(db_path).expanduser().resolve()
    fact_ids = _parse_fact_ids(candidate_fact_ids)
    expected_by_fact_id = _expected_projection_map(expected_projections)
    expected_projection_required = bool(expected_by_fact_id)
    blockers: list[str] = []
    if not fact_ids:
        blockers.append("candidate_fact_ids_required")
    with _connect_ro(db) as conn:
        items = [
            _audit_one(
                conn,
                db_path=db,
                candidate_fact_id=fact_id,
                expected_projection=expected_by_fact_id.get(int(fact_id)),
                expected_projection_required=expected_projection_required,
            )
            for fact_id in fact_ids
        ]
    for item in items:
        for blocker in item.get("blockers") or []:
            blockers.append(str(blocker))
    visible_limit = max(0, int(max_items_output))
    visible_items = items[:visible_limit] if visible_limit else []
    accepted_count = sum(1 for item in items if item.get("ok"))
    return {
        "schema": SCHEMA_VERSION,
        "ok": not blockers,
        "read_only": True,
        "execution_mode": "current_latest_posterior_post_apply_acceptance_read_only",
        "db": str(db),
        "candidate_fact_ids": fact_ids,
        "candidate_count": len(fact_ids),
        "expected_projection_count": len(expected_by_fact_id),
        "expected_projection_source": (
            "pre_apply_expected_projection"
            if expected_by_fact_id
            else "post_apply_recomputed_projection_diagnostic"
        ),
        "accepted_count": accepted_count,
        "rejected_count": len(items) - accepted_count,
        "blockers": sorted(set(blockers)),
        "items": visible_items,
        "items_truncated": len(items) > len(visible_items),
        "guards": _guards(),
        "next_boundary": (
            "post_apply_exact_candidate_acceptance_ok"
            if not blockers
            else "finish_current_latest_posterior_apply_then_rerun_exact_acceptance"
        ),
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--candidate-fact-ids", default="")
    parser.add_argument(
        "--expected-projections-json",
        default="",
        help="JSON list/map of pre-apply projected target_bucket/target_probability by fact id.",
    )
    parser.add_argument("--max-items-output", type=int, default=20)
    args = parser.parse_args(argv)
    report = build_acceptance(
        db_path=args.db,
        candidate_fact_ids=args.candidate_fact_ids,
        expected_projections=_parse_json_mapping_or_sequence(args.expected_projections_json),
        max_items_output=int(args.max_items_output),
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True, default=str))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
