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
from archive_db_write_queue import connect_archive_db_rw  # noqa: E402

DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_BACKUP_DIR = ROOT / "data" / "backups"
CONFIRM_PHRASE = "PROMOTE_FINAL_BUCKET_LOCK"
COMPOSER_VERSION = "final_bucket_lock_promote_v1"
FINAL_LOCK_FLAG_KEYS = (
    "final_bucket_lock",
    "hard_source_final_bucket_lock",
    "observed_today_max_final",
    "hard_source_final_daily_max",
    "hard_source_settlement_final",
    "temperature_observation_final_daily_max",
    "official_final_result",
    "final_daily_max_bucket_lock",
)


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


def _has_final_lock_flag(value: Mapping[str, Any]) -> bool:
    return any(_truthy(value.get(key)) for key in FINAL_LOCK_FLAG_KEYS)


def _rows(payload: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    rows = payload.get("distribution")
    if not isinstance(rows, list):
        rows = payload.get("rows")
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _target_bucket_from_value(payload: Mapping[str, Any], value_c: Optional[float]) -> str:
    if value_c is None:
        return ""
    rounding_rule = str(payload.get("rounding_rule") or "whole_degrees")
    market_var = str(payload.get("market_var") or "temperature_2m_max")
    for row in _rows(payload):
        label = str(row.get("outcome") or row.get("label") or row.get("bucket") or "").strip()
        if not label:
            continue
        try:
            inside = server.bucket_contains(float(value_c), label, rounding_rule, market_var)
        except Exception:
            inside = None
        if inside is True:
            return label
    return ""


def _final_lock_evidence(payload: Mapping[str, Any], meta: Mapping[str, Any]) -> Dict[str, Any]:
    features = _features(payload)
    meta_final_lock = bool(_truthy(meta.get("final_bucket_lock")))
    feature_final_lock = bool(
        _has_final_lock_flag(features)
        or _truthy(payload.get("final_bucket_lock"))
        or _truthy(payload.get("hard_source_final_bucket_lock"))
        or _truthy(payload.get("hard_source_final_daily_max"))
        or _truthy(payload.get("official_final_result"))
    )
    value_c = _to_float(meta.get("value_c")) if meta.get("value_c") is not None else None
    if value_c is None:
        for value in (
            payload.get("settlement_actual"),
            payload.get("settlement_actual_c"),
            features.get("settlement_actual"),
            features.get("settlement_actual_c"),
            features.get("observed_today_max_c"),
            features.get("zuuu_metar_raw_awc_today_max_c"),
        ):
            value_c = _to_float(value)
            if value_c is not None:
                break
    target = str(
        meta.get("target_bucket")
        or payload.get("final_bucket")
        or payload.get("target_bucket")
        or ""
    ).strip()
    if value_c is not None:
        mapped_target = _target_bucket_from_value(payload, value_c)
        if mapped_target:
            target = mapped_target
    if not bool(meta_final_lock or feature_final_lock):
        reason = "hard_source_lower_bound_only"
    else:
        reason = str(
            meta.get("final_lock_reason")
            or features.get("hard_source_final_lock_reason")
            or (
                "hard_source_final_daily_max"
                if feature_final_lock and not meta_final_lock
                else "settlement_actual_known"
            )
        )
    source = str(
        meta.get("source")
        or features.get("settlement_actual_source")
        or features.get("observed_today_max_source")
        or payload.get("settlement_actual_source")
        or "final_bucket_lock_promote"
    )
    return {
        "meta_final_lock": meta_final_lock,
        "feature_final_lock": feature_final_lock,
        "final_lock": bool(meta_final_lock or feature_final_lock),
        "value_c": value_c,
        "target_bucket": target,
        "final_lock_reason": reason,
        "source": source,
    }


def _distribution_final_locked(payload: Mapping[str, Any], target_bucket: str) -> Tuple[bool, str]:
    target = str(target_bucket or "").strip()
    if not target:
        return False, "target_bucket_missing"
    rows = _rows(payload)
    if not rows:
        return False, "distribution_missing"
    target_seen = False
    for row in rows:
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
    evidence = _final_lock_evidence(promoted, meta)
    target = str(evidence.get("target_bucket") or "").strip()
    promotion_meta = dict(meta)
    promotion_meta["final_bucket_lock"] = True
    promotion_meta["target_bucket"] = target
    promotion_meta["value_c"] = evidence.get("value_c")
    promotion_meta["final_lock_reason"] = evidence.get("final_lock_reason")
    server._promote_hard_source_final_lock_fields(
        promoted,
        promotion_meta,
        target_bucket=target,
        features=_features(promoted),
    )
    features = _features(promoted)
    if evidence.get("source") and features.get("settlement_actual_source") in (None, ""):
        features["settlement_actual_source"] = evidence.get("source")
    if evidence.get("value_c") is not None:
        features.setdefault("observed_today_max_c", evidence.get("value_c"))
    promoted["weather_likelihood_features"] = features
    promoted["weather_likelihood_features_compact"] = server._compact_weather_likelihood_features_for_ui(features)
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
    evidence = _final_lock_evidence(payload, meta)
    slug = str(row["slug"] or "")
    event_date = str(row["event_date"] or "")
    fact_id = int(row["id"] or 0)
    target = str(evidence.get("target_bucket") or "").strip()
    meta_final_lock = bool(evidence.get("meta_final_lock"))
    feature_final_lock = bool(evidence.get("feature_final_lock"))
    hard_source_value_c = _to_float(evidence.get("value_c"))
    explicit_settlement_actual = _to_float(
        payload.get("settlement_actual")
        if payload.get("settlement_actual") is not None
        else payload.get("settlement_actual_c")
        if payload.get("settlement_actual_c") is not None
        else features.get("settlement_actual")
        if features.get("settlement_actual") is not None
        else features.get("settlement_actual_c")
    )
    settlement_actual = (
        explicit_settlement_actual
        if explicit_settlement_actual is not None
        else _to_float(evidence.get("value_c"))
        if bool(evidence.get("final_lock"))
        else None
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
        "feature_final_bucket_lock": feature_final_lock,
        "meta_final_bucket_lock": meta_final_lock,
        "final_lock_source": evidence.get("source"),
        "final_lock_reason": evidence.get("final_lock_reason"),
    }
    if not bool(evidence.get("final_lock")):
        return {**base, "eligible": False, "reason": "final_lock_evidence_missing"}
    if not target:
        return {**base, "eligible": False, "reason": "target_bucket_missing"}
    if settlement_actual is None:
        return {**base, "eligible": False, "reason": "final_lock_value_missing"}
    if _truthy(payload.get("settlement_actual_boundary_ambiguous")) or _truthy(
        features.get("settlement_actual_boundary_ambiguous")
    ):
        return {**base, "eligible": False, "reason": "settlement_boundary_ambiguous"}
    if meta_final_lock and not feature_final_lock:
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


def build_final_bucket_lock_fact_snapshot_writer_apply_plan(
    row: sqlite3.Row,
    *,
    payload: Mapping[str, Any],
    fact_as_of: str,
    prior_history_db_path: Path | str | None = None,
) -> Dict[str, Any]:
    from weather_fact_snapshot_writer import build_fact_snapshot_write_plan
    from weather_source_acquisition_manager import build_source_acquisition_trace

    event_date = str(row["event_date"] or payload.get("event_date") or "")
    slug = str(row["slug"] or payload.get("slug") or "")
    run_date = str(row["run_date"] or payload.get("run_date") or event_date)
    features = _features(payload)
    meta = _meta(payload)
    value_c = (
        _to_float(meta.get("value_c"))
        if meta.get("value_c") is not None
        else _to_float(payload.get("settlement_actual"))
        if payload.get("settlement_actual") is not None
        else _to_float(features.get("settlement_actual"))
    )
    valid_time = str(
        features.get("settlement_actual_at")
        or features.get("settlement_final_at")
        or payload.get("settlement_signal_created_at")
        or fact_as_of
    )
    source_trace = build_source_acquisition_trace(
        sources=[
            {
                "source_id": "settlement_actual",
                "station_id": str(payload.get("station_id") or features.get("station_id") or "ZUUU"),
                "event_date": event_date,
                "valid_time": valid_time,
                "fetched_at": fact_as_of,
                "response_completed_at": fact_as_of,
                "temperature_c": value_c,
                "observed_today_max_c": value_c,
                "raw_ref": str(
                    features.get("settlement_actual_source")
                    or payload.get("settlement_actual_source")
                    or "final_bucket_lock_promote"
                ),
                "raw_payload": {
                    "target_bucket": meta.get("target_bucket") or payload.get("final_bucket") or "",
                    "value_c": value_c,
                    "final_bucket_lock": True,
                },
            }
        ],
        station_id=str(payload.get("station_id") or features.get("station_id") or "ZUUU"),
        event_date=event_date,
        fetched_at=fact_as_of,
        include_payload=False,
    )
    incoming = dict(payload)
    incoming.setdefault("slug", slug)
    incoming.setdefault("event_date", event_date)
    incoming.setdefault("run_date", run_date)
    incoming.setdefault("composer_version", COMPOSER_VERSION)
    return build_fact_snapshot_write_plan(
        slug=slug,
        event_date=event_date,
        run_date=run_date,
        fact_as_of=fact_as_of,
        incoming_payload=incoming,
        current_fact_id=int(row["id"] or 0),
        current_fact_as_of=str(row["fact_as_of"] or ""),
        current_payload=_json_obj(row["fact_json"]),
        source_acquisition_trace=source_trace,
        composer_version=COMPOSER_VERSION,
        write_reason="final_bucket_lock_promote",
        include_payload=True,
        enable_apply_contract=True,
        write_latest=True,
        prior_history_db_path=prior_history_db_path,
    )


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
    gate_cm = connect_archive_db_rw(
        db_path,
        owner="promote_final_bucket_lock_facts.apply",
        timeout_sec=30.0,
        busy_timeout_ms=30000,
        row_factory=True,
    )
    conn = gate_cm.__enter__()
    conn.row_factory = sqlite3.Row
    applied: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    try:
        server.ensure_fact_tables(conn)
        from weather_fact_snapshot_writer import (
            apply_fact_snapshot_write_plan,
            compact_fact_snapshot_writer_apply_result_for_lineage,
        )

        for row in _candidate_rows(conn, slug=slug, event_date=event_date, limit=limit):
            candidate = _candidate_from_row(row)
            public = {k: v for k, v in candidate.items() if not k.startswith("_")}
            if not candidate.get("eligible"):
                skipped.append(public)
                continue
            payload = candidate["_payload"]
            fact_as_of = _utc_now()
            plan = build_final_bucket_lock_fact_snapshot_writer_apply_plan(
                row,
                payload=payload,
                fact_as_of=fact_as_of,
                prior_history_db_path=db_path,
            )
            result = apply_fact_snapshot_write_plan(
                conn,
                plan,
                allow_apply=True,
                commit=False,
                created_at=fact_as_of,
            )
            result_summary = compact_fact_snapshot_writer_apply_result_for_lineage(result)
            if not bool(result.get("ok")) or not bool(result.get("applied")):
                skipped.append(
                    {
                        **public,
                        "writer_apply_block_reason": result.get("block_reason") or "writer_apply_failed",
                        "writer_apply_result_summary": result_summary,
                    }
                )
                continue
            applied.append(
                {
                    **public,
                    "new_fact_id": int(result.get("fact_id") or 0),
                    "updated_latest": bool(result.get("latest_repointed")),
                    "writer_apply_result_schema": result.get("schema"),
                    "write_owner": result.get("write_owner"),
                    "post_apply_persisted_verification_ok": bool(
                        result_summary.get("post_apply_persisted_verification_ok")
                    ),
                    "writer_apply_result_summary": result_summary,
                }
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        try:
            conn.close()
        finally:
            gate_cm.__exit__(None, None, None)
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
