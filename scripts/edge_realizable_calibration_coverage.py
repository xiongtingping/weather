#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional


ROOT = Path(__file__).resolve().parent.parent
DB_DEFAULT = ROOT / "data" / "archive.db"
CALIBRATION_DEFAULT = ROOT / "data" / "edge_realizable_calibration.json"
OUT_DEFAULT = ROOT / "data" / "reports" / "edge_realizable_calibration_coverage_latest.json"

COMPONENTS = {
    "p_converge": {
        "table": "p_converge_buckets",
        "status": "edge_realizable_p_converge_evidence_status",
        "samples": "edge_realizable_p_converge_samples",
        "bucket": "edge_realizable_p_converge_bucket_key",
        "used_bucket": "edge_realizable_p_converge_bucket_used_key",
    },
    "p_fill_entry": {
        "table": "p_fill_entry_buckets",
        "status": "edge_realizable_p_fill_entry_evidence_status",
        "samples": "edge_realizable_p_fill_entry_samples",
        "bucket": "edge_realizable_p_fill_entry_bucket_key",
        "used_bucket": "edge_realizable_p_fill_entry_bucket_used_key",
    },
    "p_fill_exit": {
        "table": "p_fill_exit_buckets",
        "status": "edge_realizable_p_fill_exit_evidence_status",
        "samples": "edge_realizable_p_fill_exit_samples",
        "bucket": "edge_realizable_p_fill_exit_bucket_key",
        "used_bucket": "edge_realizable_p_fill_exit_bucket_used_key",
    },
}


def _to_float(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
    except Exception:
        return None
    if not math.isfinite(parsed):
        return None
    return parsed


def _boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return bool(value)
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _normalize_status(value: Any) -> str:
    status = str(value or "").strip().lower()
    return "default_only" if status == "default" else status


def _load_json_dict(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse_details(text: str) -> Dict[str, Any]:
    try:
        parsed = json.loads(text or "{}")
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _strategy_family(details: Mapping[str, Any]) -> str:
    existing = str(details.get("entry_strategy_family") or "").strip().lower()
    if existing:
        if existing == "lcb_global_main":
            return "path_probability_follow"
        return existing
    strategy = str(details.get("strategy") or "").strip().lower()
    variant = str(details.get("strategy_variant") or "").strip().lower()
    exit_plan = str(details.get("weather_strategy_exit_plan") or details.get("entry_edge_exit_plan") or "").strip().lower()
    if (
        _boolish(details.get("edge_tradeability_lcb_canary_override"))
        or _boolish(details.get("edge_tradeability_lcb_canary_matched"))
        or _boolish(details.get("edge_tradeability_deferred_to_lcb_canary"))
        or _boolish(details.get("edge_tradeability_canary_sampled"))
    ):
        return "exploration_canary"
    if variant == "consensus_yes_top" or _boolish(details.get("lcb_consensus_yes_top_eligible")):
        return "consensus_top_regional"
    if "repricing" in exit_plan or _boolish(details.get("weather_repricing_exit_target_price")):
        return "repricing_bucket"
    if strategy in {"", "main", "lcb_global", "hard_source_direct"}:
        return "path_probability_follow"
    return strategy


def _is_repricing_candidate(details: Mapping[str, Any], strategy: str) -> bool:
    exit_plan = str(
        details.get("entry_edge_exit_plan")
        or details.get("weather_strategy_exit_plan")
        or details.get("entry_exit_plan")
        or details.get("exit_plan")
        or ""
    ).strip().lower()
    return bool(
        strategy == "repricing_bucket"
        or _boolish(details.get("entry_edge_exit_path_required"))
        or "repricing" in exit_plan
        or "target_exit" in exit_plan
        or "exit_target" in exit_plan
        or str(details.get("entry_edge_repricing_exit_evidence_status") or "").strip()
        or str(details.get("edge_realizable_p_realize_evidence_status") or "").strip()
    )


def _top(counter: Mapping[str, int], limit: int = 12) -> list[Dict[str, Any]]:
    return [
        {"key": str(key), "count": int(count)}
        for key, count in Counter(counter).most_common(max(1, int(limit)))
    ]


def _table_entry_total(entry: Any) -> Optional[int]:
    if not isinstance(entry, dict):
        return None
    total = entry.get("total")
    try:
        parsed = int(total)
    except Exception:
        return None
    return parsed if parsed >= 0 else None


def _table_summary(table: Any, min_samples: int) -> Dict[str, Any]:
    if not isinstance(table, dict):
        table = {}
    totals = [
        total
        for total in (_table_entry_total(entry) for entry in table.values())
        if total is not None
    ]
    return {
        "bucket_count": len(table),
        "sample_total": int(sum(totals)) if totals else 0,
        "min_total": int(min(totals)) if totals else None,
        "max_total": int(max(totals)) if totals else None,
        "below_min_samples_count": int(sum(1 for total in totals if total < int(min_samples))),
    }


def _new_component_stats() -> Dict[str, Any]:
    return {
        "candidate_count": 0,
        "bucket_key_present_count": 0,
        "bucket_key_missing_count": 0,
        "bucket_lookup_missing_count": 0,
        "missing_count": 0,
        "default_only_count": 0,
        "low_sample_count": 0,
        "bucket_evidence_count": 0,
        "samples_min_values": [],
        "status_counts": Counter(),
        "bucket_counts": Counter(),
        "missing_bucket_counts": Counter(),
        "low_sample_bucket_counts": Counter(),
    }


def _new_scope_stats() -> Dict[str, Any]:
    return {
        "raw_decision_count": 0,
        "repricing_candidate_count": 0,
        "missing_count": 0,
        "default_only_count": 0,
        "low_sample_count": 0,
        "bucket_evidence_count": 0,
        "probability_too_low_count": 0,
        "status_counts": Counter(),
        "missing_table_counts": Counter(),
        "default_table_counts": Counter(),
        "low_sample_table_counts": Counter(),
        "component_stats": {name: _new_component_stats() for name in COMPONENTS},
    }


def _split_tables(text: Any) -> Iterable[str]:
    for item in str(text or "").split(","):
        item = item.strip()
        if item:
            yield item


def _add_component(
    stats: Dict[str, Any],
    details: Mapping[str, Any],
    calibration: Mapping[str, Any],
    min_samples: int,
) -> None:
    for name, spec in COMPONENTS.items():
        comp = stats["component_stats"][name]
        comp["candidate_count"] += 1
        status = str(details.get(spec["status"]) or "unknown").strip().lower() or "unknown"
        comp["status_counts"][status] += 1
        if status == "bucket_evidence":
            comp["bucket_evidence_count"] += 1
        elif status == "low_sample":
            comp["low_sample_count"] += 1
        elif status == "default_only":
            comp["default_only_count"] += 1
            comp["missing_count"] += 1
        elif status == "missing":
            comp["missing_count"] += 1

        samples = _to_float(details.get(spec["samples"]))
        if samples is not None:
            comp["samples_min_values"].append(float(samples))
            if samples < int(min_samples) and status not in {"missing", "default_only", "low_sample"}:
                comp["low_sample_count"] += 1

        raw_bucket = str(details.get(spec["bucket"]) or "").strip()
        used_bucket = str(details.get(spec["used_bucket"]) or "").strip() or raw_bucket
        if used_bucket:
            comp["bucket_key_present_count"] += 1
            comp["bucket_counts"][used_bucket] += 1
        else:
            comp["bucket_key_missing_count"] += 1
            used_bucket = "__bucket_key_missing__"

        table = calibration.get(spec["table"])
        table = table if isinstance(table, dict) else {}
        if used_bucket != "__bucket_key_missing__" and used_bucket not in table:
            comp["bucket_lookup_missing_count"] += 1
        if status in {"missing", "default_only"}:
            comp["missing_bucket_counts"][used_bucket] += 1
        if status == "low_sample" or (samples is not None and samples < int(min_samples)):
            comp["low_sample_bucket_counts"][used_bucket] += 1


def _add_candidate(
    stats: Dict[str, Any],
    details: Mapping[str, Any],
    calibration: Mapping[str, Any],
    min_samples: int,
) -> None:
    stats["repricing_candidate_count"] += 1
    status = _normalize_status(
        details.get("entry_edge_repricing_exit_evidence_status")
        or details.get("edge_realizable_p_realize_evidence_status")
    )
    if not status:
        if _boolish(details.get("edge_realizable_p_realize_uses_default")):
            status = "default_only"
        elif _boolish(details.get("edge_realizable_p_realize_missing")):
            status = "missing"
        elif _boolish(details.get("edge_realizable_p_realize_low_sample")):
            status = "low_sample"
        else:
            status = "unknown"
    stats["status_counts"][status] += 1
    missing = bool(
        _boolish(details.get("entry_edge_repricing_exit_evidence_missing"))
        or _boolish(details.get("edge_realizable_p_realize_missing"))
        or _boolish(details.get("edge_realizable_p_realize_uses_default"))
        or status in {"missing", "default_only"}
    )
    low_sample = bool(
        _boolish(details.get("entry_edge_repricing_exit_evidence_low_sample"))
        or _boolish(details.get("edge_realizable_p_realize_low_sample"))
        or status == "low_sample"
    )
    if status == "default_only":
        stats["default_only_count"] += 1
    if status == "bucket_evidence":
        stats["bucket_evidence_count"] += 1
    if missing:
        stats["missing_count"] += 1
    if low_sample and not missing:
        stats["low_sample_count"] += 1
    false_type = str(details.get("entry_edge_false_edge_type") or "").strip()
    if false_type == "target_exit_execution_probability_too_low":
        stats["probability_too_low_count"] += 1
    for table in _split_tables(
        details.get("entry_edge_repricing_exit_evidence_missing_tables")
        or details.get("edge_realizable_p_realize_missing_tables")
    ):
        stats["missing_table_counts"][table] += 1
    for table in _split_tables(
        details.get("entry_edge_repricing_exit_evidence_default_tables")
        or details.get("edge_realizable_p_realize_default_tables")
    ):
        stats["default_table_counts"][table] += 1
    for table in _split_tables(
        details.get("entry_edge_repricing_exit_evidence_low_sample_tables")
        or details.get("edge_realizable_p_realize_low_sample_tables")
    ):
        stats["low_sample_table_counts"][table] += 1
    _add_component(stats, details, calibration, min_samples)


def _rate(num: int, den: int) -> Optional[float]:
    if den <= 0:
        return None
    return float(num) / float(den)


def _finalize_component(stats: Mapping[str, Any]) -> Dict[str, Any]:
    count = int(stats.get("candidate_count") or 0)
    samples = [float(x) for x in stats.get("samples_min_values") or []]
    return {
        "candidate_count": count,
        "bucket_key_present_count": int(stats.get("bucket_key_present_count") or 0),
        "bucket_key_missing_count": int(stats.get("bucket_key_missing_count") or 0),
        "bucket_lookup_missing_count": int(stats.get("bucket_lookup_missing_count") or 0),
        "missing_count": int(stats.get("missing_count") or 0),
        "missing_rate": _rate(int(stats.get("missing_count") or 0), count),
        "default_only_count": int(stats.get("default_only_count") or 0),
        "low_sample_count": int(stats.get("low_sample_count") or 0),
        "low_sample_rate": _rate(int(stats.get("low_sample_count") or 0), count),
        "bucket_evidence_count": int(stats.get("bucket_evidence_count") or 0),
        "bucket_evidence_rate": _rate(int(stats.get("bucket_evidence_count") or 0), count),
        "min_samples_observed": min(samples) if samples else None,
        "avg_samples_observed": (sum(samples) / len(samples)) if samples else None,
        "status_top": _top(stats.get("status_counts") or {}),
        "bucket_top": _top(stats.get("bucket_counts") or {}),
        "missing_bucket_top": _top(stats.get("missing_bucket_counts") or {}),
        "low_sample_bucket_top": _top(stats.get("low_sample_bucket_counts") or {}),
    }


def _finalize_scope(stats: Mapping[str, Any]) -> Dict[str, Any]:
    count = int(stats.get("repricing_candidate_count") or 0)
    return {
        "raw_decision_count": int(stats.get("raw_decision_count") or 0),
        "repricing_candidate_count": count,
        "missing_count": int(stats.get("missing_count") or 0),
        "missing_rate": _rate(int(stats.get("missing_count") or 0), count),
        "default_only_count": int(stats.get("default_only_count") or 0),
        "low_sample_count": int(stats.get("low_sample_count") or 0),
        "low_sample_rate": _rate(int(stats.get("low_sample_count") or 0), count),
        "bucket_evidence_count": int(stats.get("bucket_evidence_count") or 0),
        "bucket_evidence_rate": _rate(int(stats.get("bucket_evidence_count") or 0), count),
        "probability_too_low_count": int(stats.get("probability_too_low_count") or 0),
        "probability_too_low_rate": _rate(int(stats.get("probability_too_low_count") or 0), count),
        "status_top": _top(stats.get("status_counts") or {}),
        "missing_table_top": _top(stats.get("missing_table_counts") or {}),
        "default_table_top": _top(stats.get("default_table_counts") or {}),
        "low_sample_table_top": _top(stats.get("low_sample_table_counts") or {}),
        "components": {
            name: _finalize_component(value)
            for name, value in (stats.get("component_stats") or {}).items()
        },
    }


def _recommendations(overall: Mapping[str, Any], min_samples: int) -> list[str]:
    recs: list[str] = []
    components = overall.get("components") if isinstance(overall.get("components"), dict) else {}
    missing_bucket_keys = sum(
        int(comp.get("bucket_key_missing_count") or 0)
        for comp in components.values()
        if isinstance(comp, Mapping)
    )
    if missing_bucket_keys > 0:
        recs.append(
            "Run a fresh guarded decision pass after this build so component bucket keys are recorded for precise gap diagnosis."
        )
    for name, comp in components.items():
        if not isinstance(comp, Mapping):
            continue
        missing_rate = comp.get("missing_rate")
        low_rate = comp.get("low_sample_rate")
        if missing_rate is not None and float(missing_rate) >= 0.25:
            recs.append(
                f"Prioritize {name} coverage: missing/default-only rate is {float(missing_rate):.1%}."
            )
        if low_rate is not None and float(low_rate) >= 0.15:
            recs.append(
                f"Increase tick calibration sample coverage for {name}; low-sample rate is {float(low_rate):.1%} below min_samples={int(min_samples)}."
            )
    if not recs:
        recs.append("Calibration bucket coverage is acceptable for the inspected window; keep monitoring after new runs.")
    return recs


def _latest_entry_created_at(conn: sqlite3.Connection) -> Optional[str]:
    row = conn.execute(
        """
        SELECT created_at
        FROM auto_trade_decisions
        WHERE action = 'entry' AND created_at IS NOT NULL AND created_at != ''
        ORDER BY created_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    return str(row[0] or "").strip() or None


def build_report(
    *,
    db_path: Path,
    calibration_path: Path,
    days: int,
    min_samples: int,
    limit: int,
    run_id: Optional[int] = None,
    latest_run: bool = False,
) -> Dict[str, Any]:
    calibration = _load_json_dict(calibration_path)
    since_dt = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    since_iso = since_dt.isoformat(timespec="seconds")
    overall_stats = _new_scope_stats()
    by_strategy_stats: Dict[str, Dict[str, Any]] = defaultdict(_new_scope_stats)
    selected_run_id = int(run_id) if run_id is not None and int(run_id) > 0 else None

    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        sample_since_iso = since_iso
        if selected_run_id is None and latest_run:
            row_latest = conn.execute(
                """
                SELECT run_id
                FROM auto_trade_decisions
                WHERE action = 'entry' AND run_id IS NOT NULL
                ORDER BY run_id DESC, created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
            if row_latest and row_latest["run_id"] is not None:
                selected_run_id = int(row_latest["run_id"])
        if selected_run_id is not None:
            rows = conn.execute(
                """
                SELECT id, details_json, created_at
                FROM auto_trade_decisions
                WHERE action = 'entry' AND run_id = ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (int(selected_run_id), max(1, int(limit))),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, details_json, created_at
                FROM auto_trade_decisions
                WHERE action = 'entry' AND created_at >= ?
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                (sample_since_iso, max(1, int(limit))),
            ).fetchall()
            if not rows:
                latest_entry_created_at = _latest_entry_created_at(conn)
                if latest_entry_created_at:
                    try:
                        anchor_dt = datetime.fromisoformat(
                            latest_entry_created_at.replace("Z", "+00:00")
                        )
                        if anchor_dt.tzinfo is None:
                            anchor_dt = anchor_dt.replace(tzinfo=timezone.utc)
                        sample_since_iso = (
                            anchor_dt - timedelta(days=max(1, int(days)))
                        ).isoformat(timespec="seconds")
                        rows = conn.execute(
                            """
                            SELECT id, details_json, created_at
                            FROM auto_trade_decisions
                            WHERE action = 'entry' AND created_at >= ?
                            ORDER BY created_at DESC, id DESC
                            LIMIT ?
                            """,
                            (sample_since_iso, max(1, int(limit))),
                        ).fetchall()
                    except ValueError:
                        sample_since_iso = since_iso
        for row in rows:
            details = _parse_details(str(row["details_json"] or ""))
            strategy = _strategy_family(details)
            overall_stats["raw_decision_count"] += 1
            by_strategy_stats[strategy]["raw_decision_count"] += 1
            if not _is_repricing_candidate(details, strategy):
                continue
            _add_candidate(overall_stats, details, calibration, min_samples)
            _add_candidate(by_strategy_stats[strategy], details, calibration, min_samples)

    overall = _finalize_scope(overall_stats)
    by_strategy = {
        key: _finalize_scope(value)
        for key, value in sorted(by_strategy_stats.items(), key=lambda item: item[0])
    }
    calibration_tables = {
        spec["table"]: _table_summary(calibration.get(spec["table"]), min_samples)
        for spec in COMPONENTS.values()
    }
    return {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "db_path": str(db_path),
        "calibration_path": str(calibration_path),
        "window": {
            "days": int(days),
            "since_utc": sample_since_iso if selected_run_id is None else since_iso,
            "limit": int(limit),
            "run_id": selected_run_id,
            "latest_run": bool(latest_run),
        },
        "min_samples": int(min_samples),
        "calibration": {
            "generated_at": calibration.get("generated_at"),
            "fill_mode": calibration.get("fill_mode"),
            "tables": calibration_tables,
        },
        "overall": overall,
        "by_strategy": by_strategy,
        "recommendations": _recommendations(overall, min_samples),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(DB_DEFAULT))
    parser.add_argument("--calibration", default=str(CALIBRATION_DEFAULT))
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--min-samples", type=int, default=int(os.environ.get("PM_EDGE_REALIZABLE_MIN_SAMPLES", "20") or 20))
    parser.add_argument("--limit", type=int, default=20000)
    parser.add_argument("--run-id", type=int, default=0)
    parser.add_argument("--latest-run", action="store_true")
    parser.add_argument("--out", default=str(OUT_DEFAULT))
    args = parser.parse_args()

    db_path = Path(args.db)
    calibration_path = Path(args.calibration)
    if not db_path.exists():
        raise SystemExit(f"db_missing: {db_path}")
    report = build_report(
        db_path=db_path,
        calibration_path=calibration_path,
        days=max(1, int(args.days)),
        min_samples=max(1, int(args.min_samples)),
        limit=max(1, int(args.limit)),
        run_id=int(args.run_id) if int(args.run_id or 0) > 0 else None,
        latest_run=bool(args.latest_run),
    )
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
