#!/usr/bin/env python3
"""Promote existing final-lock evidence into fact top-level fields.

Default mode is read-only. Apply mode appends a new event_fact_history row and
points event_fact_latest at it; it does not overwrite existing history rows.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui import server  # noqa: E402

DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
CONFIRM_PHRASE = "PROMOTE_FINAL_BUCKET_LOCK"
COMPOSER_VERSION = "final_bucket_lock_promote_v1"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_obj(raw: Any) -> Dict[str, Any]:
    try:
        parsed = json.loads(str(raw or ""))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y", "on"}


def _to_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:
        return None
    return out


def _metric(row: Mapping[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _to_float(row.get(key))
        if value is not None:
            return float(value)
    return None


def _meta(payload: Mapping[str, Any]) -> Dict[str, Any]:
    for key in ("hard_source_posterior_override_meta", "hard_source_posterior_override"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _features(payload: Mapping[str, Any]) -> Dict[str, Any]:
    value = payload.get("weather_likelihood_features")
    return dict(value) if isinstance(value, Mapping) else {}


def _distribution_final_locked(payload: Mapping[str, Any], target_bucket: str) -> Tuple[bool, str]:
    target = str(target_bucket or "").strip()
    if not target:
        return False, "target_bucket_missing"
    rows = payload.get("distribution")
    if not isinstance(rows, list):
        rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return False, "distribution_missing"
    target_seen = False
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        label = str(row.get("outcome") or row.get("label") or "").strip()
        if not label:
            continue
        q = _metric(
            row,
            "q_bayes_posterior",
            "q_temperature_bucket_posterior",
            "q_raw",
            "q_round",
        )
        if q is None:
            continue
        if label == target:
            target_seen = True
            if float(q) < 0.999:
                return False, "target_probability_not_locked"
        elif float(q) > 0.001:
            return False, "non_target_probability_not_zero"
    if not target_seen:
        return False, "target_bucket_not_in_distribution"
    return True, ""


def _needs_promotion(payload: Mapping[str, Any], meta: Mapping[str, Any]) -> bool:
    features = _features(payload)
    return not (
        _truthy(payload.get("final_bucket_lock"))
        and _truthy(features.get("final_bucket_lock"))
        and str(payload.get("final_bucket") or "").strip()
    )


def _promote_payload(payload: Dict[str, Any], meta: Mapping[str, Any]) -> Tuple[Dict[str, Any], List[str]]:
    before = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    promoted = dict(payload)
    target = str(meta.get("target_bucket") or promoted.get("target_bucket") or "").strip()
    server._promote_hard_source_final_lock_fields(
        promoted,
        meta,
        target_bucket=target,
        features=_features(promoted),
    )
    after = json.dumps(promoted, sort_keys=True, ensure_ascii=False)
    changed: List[str] = []
    if before != after:
        for key in (
            "final_bucket_lock",
            "final_bucket",
            "hard_source_final_bucket_lock",
            "hard_source_final_lock_reason",
            "settlement_actual",
            "settlement_actual_c",
            "market_closed",
            "weather_likelihood_features",
            "weather_likelihood_features_compact",
        ):
            if payload.get(key) != promoted.get(key):
                changed.append(key)
    return promoted, changed


def _candidate_from_row(row: sqlite3.Row) -> Dict[str, Any]:
    payload = _json_obj(row["fact_json"])
    if not payload:
        return {
            "eligible": False,
            "reason": "fact_json_invalid",
            "slug": str(row["slug"] or ""),
            "event_date": str(row["event_date"] or ""),
            "fact_id": int(row["id"] or 0),
        }
    meta = _meta(payload)
    features = _features(payload)
    slug = str(row["slug"] or "")
    event_date = str(row["event_date"] or "")
    fact_id = int(row["id"] or 0)
    target = str(meta.get("target_bucket") or payload.get("target_bucket") or "").strip()
    meta_final_lock = bool(_truthy(meta.get("final_bucket_lock")))
    hard_source_value_c = _to_float(meta.get("value_c"))
    settlement_actual = _to_float(
        meta.get("value_c")
        if meta_final_lock and meta.get("value_c") is not None
        else payload.get("settlement_actual")
        if payload.get("settlement_actual") is not None
        else features.get("settlement_actual")
        if features.get("settlement_actual") is not None
        else features.get("settlement_actual_c")
    )
    base = {
        "slug": slug,
        "event_date": event_date,
        "fact_id": fact_id,
        "fact_as_of": str(row["fact_as_of"] or ""),
        "run_date": str(row["run_date"] or ""),
        "target_bucket": target,
        "settlement_actual": settlement_actual,
        "hard_source_value_c": hard_source_value_c,
        "top_final_bucket_lock": bool(_truthy(payload.get("final_bucket_lock"))),
        "feature_final_bucket_lock": bool(_truthy(features.get("final_bucket_lock"))),
        "meta_final_bucket_lock": meta_final_lock,
    }
    if not meta_final_lock:
        return {**base, "eligible": False, "reason": "meta_final_bucket_lock_missing"}
    if _truthy(payload.get("settlement_actual_boundary_ambiguous")) or _truthy(
        features.get("settlement_actual_boundary_ambiguous")
    ):
        return {**base, "eligible": False, "reason": "settlement_boundary_ambiguous"}
    locked, lock_reason = _distribution_final_locked(payload, target)
    if not locked:
        return {**base, "eligible": False, "reason": lock_reason}
    if not _needs_promotion(payload, meta):
        return {**base, "eligible": False, "reason": "already_promoted"}
    promoted, changed = _promote_payload(payload, meta)
    if not changed:
        return {**base, "eligible": False, "reason": "no_field_changes"}
    return {
        **base,
        "eligible": True,
        "reason": "needs_final_lock_promotion",
        "changed_fields": changed,
        "_payload": promoted,
    }


def _candidate_rows(
    conn: sqlite3.Connection,
    *,
    slug: str = "",
    event_date: str = "",
    limit: int = 100,
) -> List[sqlite3.Row]:
    clauses: List[str] = []
    params: List[Any] = []
    if slug:
        clauses.append("l.slug = ?")
        params.append(slug)
    if event_date:
        clauses.append("l.event_date = ?")
        params.append(event_date)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"""
        SELECT
          h.id, h.run_date, h.slug, h.event_date, h.fact_as_of,
          h.model_snapshot_id, h.market_snapshot_id, h.composer_version,
          h.market_updated_at, h.model_updated_at, h.market_stale_sec,
          h.model_cache_age_sec, h.best_side, h.best_outcome, h.best_edge_score,
          h.best_signal_edge_entry, h.fact_json
        FROM event_fact_latest l
        JOIN event_fact_history h ON h.id = l.fact_id
        {where}
        ORDER BY h.event_date DESC, h.fact_as_of DESC, h.id DESC
        LIMIT ?
        """,
        (*params, max(1, int(limit or 100))),
    ).fetchall()
    return list(rows)


def build_plan(
    db_path: Path,
    *,
    slug: str = "",
    event_date: str = "",
    limit: int = 100,
) -> Dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only=ON")
        rows = _candidate_rows(conn, slug=slug, event_date=event_date, limit=limit)
        candidates = [_candidate_from_row(row) for row in rows]
    finally:
        conn.close()
    public_candidates = [{k: v for k, v in c.items() if not k.startswith("_")} for c in candidates]
    return {
        "ok": True,
        "db": str(db_path),
        "generated_at": _utc_now(),
        "confirm_phrase": CONFIRM_PHRASE,
        "scanned": len(rows),
        "eligible_count": sum(1 for c in candidates if c.get("eligible")),
        "candidates": public_candidates,
    }


def _sqlite_backup(db_path: Path, backup_dir: Path) -> Dict[str, Any]:
    if not db_path.exists():
        return {"created": False, "reason": "source_db_missing", "path": ""}
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"archive.before-final-lock-promote.{stamp}.db"
    suffix = 1
    while backup_path.exists():
        backup_path = backup_dir / f"archive.before-final-lock-promote.{stamp}.{suffix}.db"
        suffix += 1
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        dst = sqlite3.connect(str(backup_path), timeout=30)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return {"created": True, "path": str(backup_path), "reason": ""}


def apply_plan(
    db_path: Path,
    *,
    slug: str = "",
    event_date: str = "",
    limit: int = 100,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
) -> Dict[str, Any]:
    backup = _sqlite_backup(db_path, backup_dir)
    if not backup.get("created"):
        return {"ok": False, "backup": backup, "applied": 0, "reason": "backup_failed"}
    conn = sqlite3.connect(str(db_path), timeout=30)
    conn.row_factory = sqlite3.Row
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    try:
        server.ensure_fact_tables(conn)
        for row in _candidate_rows(conn, slug=slug, event_date=event_date, limit=limit):
            candidate = _candidate_from_row(row)
            public = {k: v for k, v in candidate.items() if not k.startswith("_")}
            if not candidate.get("eligible"):
                skipped.append(public)
                continue
            payload = candidate["_payload"]
            fact_as_of = _utc_now()
            fact_id = server._insert_event_fact_history(
                conn,
                run_date=str(row["run_date"] or candidate.get("event_date") or ""),
                slug=str(row["slug"] or ""),
                event_date=str(row["event_date"] or ""),
                fact_as_of=fact_as_of,
                model_snapshot_id=int(row["model_snapshot_id"]) if row["model_snapshot_id"] is not None else None,
                market_snapshot_id=int(row["market_snapshot_id"]) if row["market_snapshot_id"] is not None else None,
                composer_version=COMPOSER_VERSION,
                item=payload,
            )
            updated_latest = server._upsert_event_fact_latest(
                conn,
                str(row["slug"] or ""),
                str(row["event_date"] or ""),
                int(fact_id),
            )
            applied.append({**public, "new_fact_id": int(fact_id), "updated_latest": bool(updated_latest)})
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {
        "ok": True,
        "backup": backup,
        "applied": len(applied),
        "applied_items": applied,
        "skipped": skipped,
    }


def main(argv: Optional[Iterable[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--slug", default="")
    parser.add_argument("--event-date", default="")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--confirm-apply", default="")
    parser.add_argument("--backup-dir", default=str(DEFAULT_BACKUP_DIR))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    db_path = Path(args.db)
    if not args.apply:
        payload = build_plan(db_path, slug=args.slug, event_date=args.event_date, limit=args.limit)
        print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json else None))
        return 0
    if str(args.confirm_apply or "") != CONFIRM_PHRASE:
        payload = {
            "ok": False,
            "apply_result": {
                "blocked": True,
                "block_reason": "missing_apply_confirmation",
                "required_confirm_phrase": CONFIRM_PHRASE,
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2 if args.json else None))
        return 2
    result = apply_plan(
        db_path,
        slug=args.slug,
        event_date=args.event_date,
        limit=args.limit,
        backup_dir=Path(args.backup_dir),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2 if args.json else None))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
