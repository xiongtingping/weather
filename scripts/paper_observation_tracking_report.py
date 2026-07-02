#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import pilot_observation_review  # noqa: E402
from scripts.train_q_calibrator import _label_from_settlement  # noqa: E402


DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_OUT = ROOT / "data" / "reports" / "paper_observation_tracking_latest.json"
DEFAULT_MARKET_SCOPE = "highest_temperature"
DEFAULT_SINCE_DAYS = 42


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except Exception:
        return None
    return out if math.isfinite(out) else None


def _row_key(row: Mapping[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("slug") or ""),
        str(row.get("event_date") or "").split("T", 1)[0],
        str(row.get("outcome") or ""),
        str(row.get("side") or "").upper(),
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table,),
    ).fetchone()
    return bool(row)


def _load_calibration_labels(
    db_path: Path,
    keys: Sequence[tuple[str, str, str, str]],
) -> dict[tuple[str, str, str, str], dict[str, Any]]:
    if not db_path.exists() or not keys:
        return {}
    out: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if not _table_exists(conn, "calibration_labels"):
            return {}
        for key in keys:
            slug, event_date, outcome, side = key
            row = conn.execute(
                """
                SELECT label, label_source, resolved_at, created_at, state
                FROM calibration_labels
                WHERE slug = ?
                  AND date(event_date) = date(?)
                  AND outcome = ?
                  AND UPPER(side) = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (slug, event_date, outcome, side),
            ).fetchone()
            if row:
                out[key] = {
                    "label": int(row["label"]) if row["label"] is not None else None,
                    "label_source": str(row["label_source"] or ""),
                    "resolved_at": str(row["resolved_at"] or ""),
                    "created_at": str(row["created_at"] or ""),
                    "state": str(row["state"] or ""),
                }
    except sqlite3.Error:
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def _load_settlements(
    db_path: Path,
    keys: Sequence[tuple[str, str, str, str]],
) -> dict[tuple[str, str], dict[str, Any]]:
    if not db_path.exists() or not keys:
        return {}
    event_keys = sorted({(slug, event_date) for slug, event_date, _, _ in keys})
    out: dict[tuple[str, str], dict[str, Any]] = {}
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=ON")
        if not _table_exists(conn, "event_runs"):
            return {}
        for slug, event_date in event_keys:
            row = conn.execute(
                """
                SELECT settlement_actual, rounding_rule, market_unit, created_at
                FROM event_runs
                WHERE slug = ?
                  AND date(event_date) = date(?)
                  AND settlement_actual IS NOT NULL
                ORDER BY id DESC
                LIMIT 1
                """,
                (slug, event_date),
            ).fetchone()
            if row:
                out[(slug, event_date)] = {
                    "settlement_actual": _to_float(row["settlement_actual"]),
                    "rounding_rule": str(row["rounding_rule"] or ""),
                    "market_unit": str(row["market_unit"] or ""),
                    "resolved_at": str(row["created_at"] or ""),
                }
    except sqlite3.Error:
        return out
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out


def _attach_result(
    row: Mapping[str, Any],
    *,
    labels: Mapping[tuple[str, str, str, str], Mapping[str, Any]],
    settlements: Mapping[tuple[str, str], Mapping[str, Any]],
) -> dict[str, Any]:
    out = dict(row)
    key = _row_key(row)
    label = labels.get(key)
    if label and label.get("label") in {0, 1}:
        out.update(
            {
                "tracking_status": "labeled",
                "label": int(label.get("label")),
                "label_source": label.get("label_source") or "calibration_labels",
                "settlement_actual": None,
                "resolved_at": label.get("resolved_at") or label.get("created_at") or "",
            }
        )
        return out

    settlement = settlements.get((key[0], key[1]))
    if settlement:
        label_value, label_source = _label_from_settlement(
            side=key[3],
            outcome=key[2],
            settlement_actual=_to_float(settlement.get("settlement_actual")),
            rounding_rule=str(settlement.get("rounding_rule") or ""),
        )
        if label_value in {0, 1}:
            out.update(
                {
                    "tracking_status": "resolved_from_settlement",
                    "label": int(label_value),
                    "label_source": label_source,
                    "settlement_actual": settlement.get("settlement_actual"),
                    "resolved_at": settlement.get("resolved_at") or "",
                }
            )
            return out

    out.update(
        {
            "tracking_status": "pending_settlement_label",
            "label": None,
            "label_source": "pending_settlement_label",
            "settlement_actual": None,
            "resolved_at": "",
        }
    )
    return out


def _tracking_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    by_status: Counter[str] = Counter()
    by_strategy: dict[str, Counter[str]] = {}
    resolved = 0
    wins = 0
    for row in rows:
        status = str(row.get("tracking_status") or "")
        strategy = str(row.get("candidate_strategy") or "")
        by_status[status] += 1
        by_strategy.setdefault(strategy, Counter())[status] += 1
        label = row.get("label")
        if label in {0, 1}:
            resolved += 1
            wins += int(label)
    return {
        "candidate_rows": len(rows),
        "resolved_rows": resolved,
        "pending_rows": int(by_status.get("pending_settlement_label") or 0),
        "wins": wins,
        "losses": max(0, resolved - wins),
        "hit_rate": (wins / resolved) if resolved else None,
        "by_tracking_status": dict(sorted(by_status.items())),
        "by_candidate_strategy": {
            strategy: dict(sorted(counter.items()))
            for strategy, counter in sorted(by_strategy.items())
        },
    }


def attach_tracking_results(
    db_path: Path,
    rows: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    candidate_rows = [dict(row) for row in rows if isinstance(row, Mapping)]
    keys = [_row_key(row) for row in candidate_rows]
    labels = _load_calibration_labels(db_path, keys)
    settlements = _load_settlements(db_path, keys)
    tracked_rows = [
        _attach_result(row, labels=labels, settlements=settlements)
        for row in candidate_rows
    ]
    summary = _tracking_summary(tracked_rows)
    return {
        "read_only": True,
        "candidate_rows": len(candidate_rows),
        "summary": summary,
        "rows": tracked_rows,
        "next_mainline_task": (
            "wait_for_settlement_labels_to_score_paper_observations"
            if int(summary.get("pending_rows") or 0) > 0
            else "review_paper_observation_hit_rate_before_small_capital"
        ),
    }


def build_report(
    db_path: Path = DEFAULT_DB,
    *,
    pilot_cities: Sequence[str],
    since_days: int = DEFAULT_SINCE_DAYS,
    market_scope: str = DEFAULT_MARKET_SCOPE,
) -> dict[str, Any]:
    observation = pilot_observation_review.build_report(
        db_path,
        pilot_cities=pilot_cities,
        since_days=max(1, int(since_days)),
        market_scope=str(market_scope or DEFAULT_MARKET_SCOPE),
    )
    rows = [row for row in observation.get("rows") or [] if isinstance(row, Mapping)]
    tracking = attach_tracking_results(db_path, rows)
    tracked_rows = [
        row for row in tracking.get("rows") or [] if isinstance(row, Mapping)
    ]
    summary = (
        tracking.get("summary") if isinstance(tracking.get("summary"), Mapping) else {}
    )

    return {
        "ok": bool(observation.get("ok")),
        "read_only": True,
        "generated_at": _utc_now(),
        "db": str(db_path),
        "since_days": max(1, int(since_days)),
        "market_scope": str(market_scope or DEFAULT_MARKET_SCOPE),
        "pilot_scope": observation.get("pilot_scope") or {},
        "blockers": observation.get("blockers") or [],
        "paper_observation_allowed": bool(observation.get("paper_observation_allowed")),
        "small_capital_candidate_ready": bool(observation.get("small_capital_candidate_ready")),
        "summary": dict(summary),
        "rows": tracked_rows,
        "next_mainline_task": (
            "wait_for_settlement_labels_to_score_paper_observations"
            if int(summary.get("pending_rows") or 0) > 0
            else "review_paper_observation_hit_rate_before_small_capital"
        ),
        "next_actions": [
            "wait_for_settlement_actual_then_rebuild_calibration_label_plan",
            "rerun_paper_observation_tracking_after_labels_update",
        ]
        if int(summary.get("pending_rows") or 0) > 0
        else [
            "review_resolved_paper_observation_hit_rate",
            "keep_small_capital_blocked_until_paper_validation_passes",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only tracking report for paper observation candidates.")
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--pilot-city", action="append", default=[], help="repeatable or comma-separated")
    parser.add_argument("--since-days", type=int, default=DEFAULT_SINCE_DAYS)
    parser.add_argument("--market-scope", default=DEFAULT_MARKET_SCOPE)
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    cities = pilot_observation_review.small_capital_validation_review._csv_values(args.pilot_city)
    report = build_report(
        Path(args.db),
        pilot_cities=cities,
        since_days=int(args.since_days),
        market_scope=str(args.market_scope or DEFAULT_MARKET_SCOPE),
    )
    text = json.dumps(report, ensure_ascii=False, indent=None if args.json else 2, sort_keys=True)
    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
