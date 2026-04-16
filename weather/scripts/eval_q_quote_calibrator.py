#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from edge_calibration_runtime import predict_q_quote
from scripts.calibration_dataset_utils import days_to_event as dataset_days_to_event
from scripts.calibration_dataset_utils import load_joined_open_market_dataset


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _days_to_event(event_date: str, created_at: str) -> Optional[float]:
    return dataset_days_to_event(event_date, created_at)


def _load_bundle(path: Path) -> Dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return raw if isinstance(raw, dict) else {}


def _ensure_eval_table(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS q_quote_calibration_eval_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_version TEXT,
            bundle_path TEXT,
            sample_n INTEGER,
            mae REAL,
            rmse REAL,
            top1_open_hit_rate REAL,
            champion INTEGER,
            promoted INTEGER,
            status TEXT,
            notes_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _load_open_first_rows(
    conn: sqlite3.Connection,
    since_iso: str,
    limit_events: int,
) -> List[Dict[str, Any]]:
    samples: List[Dict[str, Any]] = []
    events = load_joined_open_market_dataset(conn, since_iso, int(limit_events))
    for event in events:
        slug = str(event.get("slug") or "")
        event_date = str(event.get("event_date") or "")
        created_at = str(event.get("created_at") or "")
        dte = _days_to_event(event_date, created_at)
        rows = event.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            outcome = str(row.get("outcome") or "").strip()
            q_raw = _to_float(row.get("q_raw"))
            if q_raw is None:
                q_raw = _to_float(row.get("q_round"))
            ask = _to_float(row.get("best_ask"))
            if not outcome or q_raw is None or ask is None:
                continue
            if not (math.isfinite(q_raw) and math.isfinite(ask)):
                continue
            samples.append(
                {
                    "slug": slug,
                    "event_date": event_date,
                    "created_at": created_at,
                    "days_to_event": dte,
                    "outcome": outcome,
                    "q_raw": float(max(1e-6, min(1.0 - 1e-6, q_raw))),
                    "best_ask": float(max(1e-6, min(1.0 - 1e-6, ask))),
                    "spread": _to_float(row.get("spread")),
                    "depth_ratio": _to_float(row.get("depth_ratio_yes")) or _to_float(row.get("depth_ratio")),
                    "tick_age_sec": _to_float(row.get("tick_age_sec")),
                }
            )
    return samples


def _error_metrics(rows: List[Tuple[float, float]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "mae": None, "rmse": None}
    abs_sum = 0.0
    sq_sum = 0.0
    for pred, target in rows:
        err = float(pred) - float(target)
        abs_sum += abs(err)
        sq_sum += err * err
    n = float(len(rows))
    return {
        "n": int(n),
        "mae": abs_sum / n,
        "rmse": math.sqrt(max(0.0, sq_sum / n)),
    }


def _top1_open_by_prediction(samples: List[Dict[str, Any]], pred_key: str) -> Dict[str, Any]:
    by_event: Dict[Tuple[str, str], List[Dict[str, Any]]] = {}
    for item in samples:
        key = (str(item.get("slug") or ""), str(item.get("event_date") or ""))
        if not key[0] or not key[1]:
            continue
        by_event.setdefault(key, []).append(item)
    if not by_event:
        return {"n": 0, "hit_rate": None}
    hits = 0
    total = 0
    for rows in by_event.values():
        usable = [row for row in rows if _to_float(row.get(pred_key)) is not None]
        if not usable:
            continue
        pred_top = max(usable, key=lambda x: float(x.get(pred_key) or 0.0))
        ask_top = max(usable, key=lambda x: float(x.get("best_ask") or 0.0))
        total += 1
        if str(pred_top.get("outcome") or "") == str(ask_top.get("outcome") or ""):
            hits += 1
    if total <= 0:
        return {"n": 0, "hit_rate": None}
    return {"n": int(total), "hit_rate": float(hits) / float(total)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate q_quote calibration bundle vs champion.")
    parser.add_argument("--db", default=str(ROOT_DIR / "data" / "archive.db"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--open-limit-events", type=int, default=2000)
    parser.add_argument(
        "--candidate",
        default=str(ROOT_DIR / "data" / "q_quote_calibration_bundle.candidate.json"),
    )
    parser.add_argument(
        "--champion",
        default=str(ROOT_DIR / "data" / "q_quote_calibration_bundle.json"),
    )
    parser.add_argument("--write-db", action="store_true", default=False)
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    cand_path = Path(args.candidate).expanduser().resolve()
    champ_path = Path(args.champion).expanduser().resolve()
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"db_missing:{db_path}"}, ensure_ascii=False))
        raise SystemExit(1)
    if not cand_path.exists():
        print(json.dumps({"ok": False, "error": f"candidate_missing:{cand_path}"}, ensure_ascii=False))
        raise SystemExit(2)

    candidate_bundle = _load_bundle(cand_path)
    champion_bundle = _load_bundle(champ_path) if champ_path.exists() else {}
    since_iso = (datetime.now(timezone.utc) - timedelta(days=max(1, int(args.days)))).isoformat(
        timespec="seconds"
    )

    conn = sqlite3.connect(str(db_path))
    _ensure_eval_table(conn)
    samples = _load_open_first_rows(conn, since_iso, int(args.open_limit_events))
    if not samples:
        print(json.dumps({"ok": False, "error": "no_open_first_snapshot_samples"}, ensure_ascii=False))
        raise SystemExit(3)

    cand_rows: List[Tuple[float, float]] = []
    champ_rows: List[Tuple[float, float]] = []
    for sample in samples:
        kwargs = {
            "q_raw": sample.get("q_raw"),
            "spread": sample.get("spread"),
            "depth_ratio": sample.get("depth_ratio"),
            "tick_age_sec": sample.get("tick_age_sec"),
            "days_to_event": sample.get("days_to_event"),
            "decision_ts": sample.get("created_at"),
        }
        cand_pred = predict_q_quote(bundle=candidate_bundle, **kwargs)
        champ_pred = predict_q_quote(bundle=champion_bundle, **kwargs)
        cand_p = _to_float(cand_pred.get("q_quote_mean"))
        champ_p = _to_float(champ_pred.get("q_quote_mean"))
        q_raw = _to_float(sample.get("q_raw"))
        if cand_p is None:
            cand_p = q_raw
        if champ_p is None:
            champ_p = q_raw
        if cand_p is None or champ_p is None:
            continue
        target = float(sample.get("best_ask") or 0.0)
        sample["cand_pred"] = cand_p
        sample["champ_pred"] = champ_p
        cand_rows.append((cand_p, target))
        champ_rows.append((champ_p, target))

    if not cand_rows:
        print(json.dumps({"ok": False, "error": "no_prediction_rows"}, ensure_ascii=False))
        raise SystemExit(4)

    cand_metrics = _error_metrics(cand_rows)
    champ_metrics = _error_metrics(champ_rows)
    cand_top1 = _top1_open_by_prediction(samples, "cand_pred")
    champ_top1 = _top1_open_by_prediction(samples, "champ_pred")

    mae_ok = (
        cand_metrics.get("mae") is not None
        and (
            champ_metrics.get("mae") is None
            or float(cand_metrics["mae"]) <= float(champ_metrics["mae"]) + 1e-12
        )
    )
    rmse_ok = (
        cand_metrics.get("rmse") is not None
        and (
            champ_metrics.get("rmse") is None
            or float(cand_metrics["rmse"]) <= float(champ_metrics["rmse"]) + 1e-12
        )
    )
    top1_open_ok = (
        cand_top1.get("hit_rate") is not None
        and (
            champ_top1.get("hit_rate") is None
            or float(cand_top1["hit_rate"]) >= float(champ_top1["hit_rate"]) - 1e-12
        )
    )
    recommend_promote = bool(mae_ok and rmse_ok and top1_open_ok)

    notes = {
        "candidate_bundle": str(cand_path),
        "champion_bundle": str(champ_path) if champ_path.exists() else "",
        "candidate_metrics": cand_metrics,
        "champion_metrics": champ_metrics,
        "candidate_top1_open": cand_top1,
        "champion_top1_open": champ_top1,
        "datasets": {
            "open_first_snapshot_n": int(cand_metrics.get("n") or 0),
            "since": since_iso,
        },
        "rules": {
            "mae_ok": bool(mae_ok),
            "rmse_ok": bool(rmse_ok),
            "top1_open_ok": bool(top1_open_ok),
        },
        "recommend_promote": bool(recommend_promote),
        "days": int(args.days),
    }

    eval_id = None
    if bool(args.write_db):
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO q_quote_calibration_eval_runs(
                model_version, bundle_path, sample_n, mae, rmse, top1_open_hit_rate,
                champion, promoted, status, notes_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(candidate_bundle.get("version") or ""),
                str(cand_path),
                int(cand_metrics.get("n") or 0),
                _to_float(cand_metrics.get("mae")),
                _to_float(cand_metrics.get("rmse")),
                _to_float(cand_top1.get("hit_rate")),
                1 if recommend_promote else 0,
                0,
                "promote_recommended" if recommend_promote else "hold",
                json.dumps(notes, ensure_ascii=False),
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        eval_id = int(cur.lastrowid or 0)
        conn.commit()
    conn.close()

    out = {
        "ok": True,
        "recommend_promote": bool(recommend_promote),
        "candidate": cand_metrics,
        "champion": champ_metrics,
        "candidate_top1_open": cand_top1,
        "champion_top1_open": champ_top1,
        "rules": notes.get("rules"),
        "eval_id": eval_id,
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
