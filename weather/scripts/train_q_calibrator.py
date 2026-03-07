#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
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

from edge_calibration_runtime import build_segment_key


def _to_float(value: Any) -> Optional[float]:
    try:
        num = float(value)
    except Exception:
        return None
    if not math.isfinite(num):
        return None
    return float(num)


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _parse_ts(value: Any) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _safe_logit(prob: float, eps: float = 1e-6) -> float:
    p = min(max(float(prob), eps), 1.0 - eps)
    return math.log(p / (1.0 - p))


def _safe_sigmoid(z: float) -> float:
    if z >= 0:
        exp_neg = math.exp(-z)
        return 1.0 / (1.0 + exp_neg)
    exp_pos = math.exp(z)
    return exp_pos / (1.0 + exp_pos)


def _platt_predict(q_raw: float, a: float, b: float) -> float:
    return _safe_sigmoid(float(a) * _safe_logit(float(q_raw)) + float(b))


def _fit_platt(
    samples: List[Tuple[float, int, float]],
    *,
    l2: float,
    max_iter: int = 60,
    tol: float = 1e-8,
) -> Optional[Tuple[float, float]]:
    if not samples:
        return None
    reg = max(0.0, float(l2))

    def _loss(a_val: float, b_val: float) -> float:
        eps = 1e-12
        total = 0.0
        for q_raw, y, w in samples:
            if w <= 0:
                continue
            p = _platt_predict(q_raw, a_val, b_val)
            p = min(max(p, eps), 1.0 - eps)
            total += float(w) * (-(float(y) * math.log(p) + (1.0 - float(y)) * math.log(1.0 - p)))
        total += reg * (a_val * a_val + b_val * b_val)
        return total

    a = 1.0
    b = 0.0
    for _ in range(max_iter):
        g_a = 0.0
        g_b = 0.0
        h_aa = 0.0
        h_ab = 0.0
        h_bb = 0.0
        cur_loss = _loss(a, b)
        for q_raw, y, w in samples:
            if w <= 0:
                continue
            x = _safe_logit(float(q_raw))
            p = _safe_sigmoid(a * x + b)
            err = (p - float(y)) * float(w)
            g_a += err * x
            g_b += err
            var = p * (1.0 - p) * float(w)
            h_aa += var * x * x
            h_ab += var * x
            h_bb += var
        g_a += 2.0 * reg * a
        g_b += 2.0 * reg * b
        h_aa += 2.0 * reg
        h_bb += 2.0 * reg
        det = h_aa * h_bb - h_ab * h_ab
        if not math.isfinite(det) or abs(det) < 1e-18:
            h_aa += 1e-6
            h_bb += 1e-6
            det = h_aa * h_bb - h_ab * h_ab
        if not math.isfinite(det) or abs(det) < 1e-18:
            return None
        d_a = (g_a * h_bb - g_b * h_ab) / det
        d_b = (g_b * h_aa - g_a * h_ab) / det
        if not math.isfinite(d_a) or not math.isfinite(d_b):
            return None
        if max(abs(d_a), abs(d_b)) < tol:
            break
        step = 1.0
        best = (a, b, cur_loss)
        for _ in range(12):
            a_try = a - step * d_a
            b_try = b - step * d_b
            trial = _loss(a_try, b_try)
            if math.isfinite(trial) and trial <= best[2]:
                best = (a_try, b_try, trial)
                break
            step *= 0.5
        if best[0] == a and best[1] == b:
            break
        a = best[0]
        b = best[1]
    if not (math.isfinite(a) and math.isfinite(b)):
        return None
    return float(a), float(b)


def _ensure_tables(conn: sqlite3.Connection) -> None:
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS calibration_samples (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            decision_id INTEGER,
            run_date TEXT,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            strategy TEXT,
            q_raw REAL,
            q_calibrated REAL,
            q_calibrated_lcb REAL,
            q_calibrated_ucb REAL,
            q_calibrated_sigma REAL,
            spread REAL,
            depth_ratio REAL,
            tick_age_sec REAL,
            days_to_event REAL,
            segment_key TEXT,
            bundle_version TEXT,
            details_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_samples_decision
        ON calibration_samples(decision_id)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS calibration_labels (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sample_id INTEGER,
            decision_id INTEGER,
            slug TEXT,
            event_date TEXT,
            outcome TEXT,
            side TEXT,
            label INTEGER,
            label_source TEXT,
            realized_pnl REAL,
            path_hit INTEGER,
            path_timeout INTEGER,
            path_adverse INTEGER,
            state TEXT,
            resolved_at TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_labels_sample
        ON calibration_labels(sample_id)
        """
    )
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_labels_decision
        ON calibration_labels(decision_id)
        """
    )
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS calibration_eval_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            model_version TEXT,
            bundle_path TEXT,
            sample_n INTEGER,
            label_n INTEGER,
            ece REAL,
            brier REAL,
            logloss REAL,
            psi REAL,
            roi_mean REAL,
            roi_lcb REAL,
            champion INTEGER,
            promoted INTEGER,
            status TEXT,
            notes_json TEXT,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()


def _load_rows(conn: sqlite3.Connection, since_iso: str) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            d.id AS decision_id,
            d.run_id,
            d.run_date,
            d.slug,
            d.event_date,
            d.outcome,
            d.side,
            d.created_at,
            d.details_json,
            t.state,
            t.position_size,
            t.realized_pnl,
            t.updated_at
        FROM auto_trade_decisions d
        LEFT JOIN edge_trades t
          ON t.slug = d.slug
         AND t.event_date = d.event_date
         AND t.outcome = d.outcome
         AND t.side = d.side
        WHERE d.action = 'entry'
          AND d.will_order = 1
          AND datetime(d.created_at) >= datetime(?)
        ORDER BY d.id ASC
        """,
        (since_iso,),
    )
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        out.append(
            {
                "decision_id": row[0],
                "run_id": row[1],
                "run_date": str(row[2] or ""),
                "slug": str(row[3] or ""),
                "event_date": str(row[4] or ""),
                "outcome": str(row[5] or ""),
                "side": str(row[6] or ""),
                "created_at": str(row[7] or ""),
                "details_json": str(row[8] or ""),
                "state": str(row[9] or ""),
                "position_size": _to_float(row[10]),
                "realized_pnl": _to_float(row[11]),
                "resolved_at": str(row[12] or ""),
            }
        )
    return out


def _label_from_trade(state: str, position_size: Optional[float], realized_pnl: Optional[float]) -> Tuple[Optional[int], str]:
    st = str(state or "").strip().lower()
    pos = position_size if position_size is not None else None
    closed = st in {"exit", "closed", "settled", "resolved", "no_position", "pos_sync_clear"}
    if pos is not None and pos <= 1e-9:
        closed = True
    if not closed:
        return None, "open_position"
    if realized_pnl is None:
        return None, "closed_no_realized_pnl"
    if realized_pnl > 1e-9:
        return 1, "realized_pnl_positive"
    if realized_pnl < -1e-9:
        return 0, "realized_pnl_negative"
    return None, "realized_pnl_zero"


def _metrics(samples: List[Tuple[float, int]]) -> Dict[str, Any]:
    if not samples:
        return {"brier": None, "logloss": None, "ece": None}
    brier_sum = 0.0
    ll_sum = 0.0
    eps = 1e-12
    bins: Dict[int, Dict[str, float]] = {i: {"n": 0.0, "p_sum": 0.0, "y_sum": 0.0} for i in range(10)}
    for p, y in samples:
        p_clamped = min(max(float(p), eps), 1.0 - eps)
        y_f = float(y)
        brier_sum += (p_clamped - y_f) ** 2
        ll_sum += -(y_f * math.log(p_clamped) + (1.0 - y_f) * math.log(1.0 - p_clamped))
        idx = min(9, max(0, int(p_clamped * 10.0)))
        bins[idx]["n"] += 1.0
        bins[idx]["p_sum"] += p_clamped
        bins[idx]["y_sum"] += y_f
    total_n = float(len(samples))
    ece_sum = 0.0
    for entry in bins.values():
        n = entry["n"]
        if n <= 0:
            continue
        p_mean = entry["p_sum"] / n
        y_mean = entry["y_sum"] / n
        ece_sum += (n / total_n) * abs(p_mean - y_mean)
    return {
        "brier": brier_sum / total_n,
        "logloss": ll_sum / total_n,
        "ece": ece_sum,
    }


def _days_to_event(event_date: str, created_at: str) -> Optional[float]:
    ts = _parse_ts(created_at)
    if ts is None:
        return None
    try:
        ev_day = datetime.strptime(str(event_date or "")[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    return float((ev_day - ts.date()).days)


def main() -> None:
    parser = argparse.ArgumentParser(description="Train q_raw->q_calibrated bundle (V1 segmented residual).")
    parser.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "data" / "archive.db"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--l2", type=float, default=0.2)
    parser.add_argument("--z", type=float, default=1.64)
    parser.add_argument("--min-label-samples", type=int, default=30)
    parser.add_argument("--min-segment-samples", type=int, default=20)
    parser.add_argument("--out", default=str(Path(__file__).resolve().parents[1] / "data" / "q_calibration_bundle.candidate.json"))
    parser.add_argument("--write-ssot", action="store_true", default=False)
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"db_missing:{db_path}"}, ensure_ascii=False))
        raise SystemExit(1)
    since_iso = (datetime.now(timezone.utc) - timedelta(days=max(1, int(args.days)))).isoformat(timespec="seconds")

    conn = sqlite3.connect(str(db_path))
    _ensure_tables(conn)
    rows = _load_rows(conn, since_iso)
    samples: List[Dict[str, Any]] = []
    labeled_platt: List[Tuple[float, int, float]] = []

    for row in rows:
        details: Dict[str, Any] = {}
        try:
            payload = json.loads(str(row.get("details_json") or ""))
            if isinstance(payload, dict):
                details = payload
        except Exception:
            details = {}
        q_raw = _to_float(details.get("q_raw"))
        if q_raw is None:
            q_raw = _to_float(details.get("q_entry"))
        if q_raw is None:
            continue
        q_raw = min(max(float(q_raw), 1e-6), 1.0 - 1e-6)
        spread = _to_float(details.get("spread"))
        depth_ratio = _to_float(details.get("depth_ratio"))
        tick_age = _to_float(details.get("tick_age_sec"))
        days_to_event = _days_to_event(str(row.get("event_date") or ""), str(row.get("created_at") or ""))
        segment_key, _ = build_segment_key(
            days_to_event=days_to_event,
            spread=spread,
            depth_ratio=depth_ratio,
            tick_age_sec=tick_age,
            decision_ts=row.get("created_at"),
        )
        label, label_source = _label_from_trade(
            str(row.get("state") or ""),
            _to_float(row.get("position_size")),
            _to_float(row.get("realized_pnl")),
        )
        sample = {
            "decision_id": _to_int(row.get("decision_id")),
            "run_id": _to_int(row.get("run_id")),
            "run_date": str(row.get("run_date") or ""),
            "slug": str(row.get("slug") or ""),
            "event_date": str(row.get("event_date") or ""),
            "outcome": str(row.get("outcome") or ""),
            "side": str(row.get("side") or "").upper(),
            "strategy": str(details.get("strategy") or ""),
            "q_raw": q_raw,
            "q_calibrated": _to_float(details.get("q_calibrated")),
            "q_calibrated_lcb": _to_float(details.get("q_calibrated_lcb")),
            "q_calibrated_ucb": _to_float(details.get("q_calibrated_ucb")),
            "q_calibrated_sigma": _to_float(details.get("q_calibrated_sigma")),
            "spread": spread,
            "depth_ratio": depth_ratio,
            "tick_age_sec": tick_age,
            "days_to_event": days_to_event,
            "segment_key": segment_key,
            "created_at": str(row.get("created_at") or ""),
            "label": label,
            "label_source": label_source,
            "realized_pnl": _to_float(row.get("realized_pnl")),
            "resolved_at": str(row.get("resolved_at") or ""),
            "details_json": details,
        }
        samples.append(sample)
        if label in {0, 1}:
            labeled_platt.append((float(q_raw), int(label), 1.0))

    label_n = len(labeled_platt)
    if label_n < max(1, int(args.min_label_samples)):
        out = {
            "ok": False,
            "error": "insufficient_labels",
            "sample_n": len(samples),
            "label_n": label_n,
            "min_label_samples": int(args.min_label_samples),
            "since": since_iso,
        }
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(2)

    fit = _fit_platt(labeled_platt, l2=float(args.l2))
    if fit is None:
        print(json.dumps({"ok": False, "error": "platt_fit_failed", "label_n": label_n}, ensure_ascii=False))
        raise SystemExit(3)
    a, b = fit

    metrics_rows: List[Tuple[float, int]] = []
    residuals: List[float] = []
    residual_by_segment: Dict[str, List[float]] = defaultdict(list)
    for sample in samples:
        label = sample.get("label")
        if label not in {0, 1}:
            continue
        q_raw = float(sample["q_raw"])
        p = _platt_predict(q_raw, a, b)
        metrics_rows.append((p, int(label)))
        residual = float(label) - p
        residuals.append(residual)
        residual_by_segment[str(sample.get("segment_key") or "default")].append(residual)

    if not residuals:
        print(json.dumps({"ok": False, "error": "no_residuals"}, ensure_ascii=False))
        raise SystemExit(4)

    global_residual_mean = sum(residuals) / float(len(residuals))
    global_residual_var = sum((x - global_residual_mean) ** 2 for x in residuals) / float(max(1, len(residuals) - 1))
    global_sigma = math.sqrt(max(1e-9, global_residual_var))

    segment_payload: Dict[str, Dict[str, Any]] = {}
    for key, vals in residual_by_segment.items():
        n = len(vals)
        if n < max(1, int(args.min_segment_samples)):
            continue
        mean = sum(vals) / float(n)
        var = sum((x - mean) ** 2 for x in vals) / float(max(1, n - 1))
        segment_payload[key] = {
            "sample_n": n,
            "residual_mean": mean,
            "residual_sigma": math.sqrt(max(1e-9, var)),
        }

    metrics = _metrics(metrics_rows)
    ts_now = datetime.now(timezone.utc).replace(microsecond=0)
    version = f"qcal_v1_{ts_now.strftime('%Y%m%dT%H%M%SZ')}"
    bundle = {
        "enabled": True,
        "version": version,
        "generated_at": ts_now.isoformat(),
        "source": {
            "db_path": str(db_path),
            "since": since_iso,
            "days": int(args.days),
        },
        "sample_n": len(samples),
        "label_n": label_n,
        "z": float(args.z),
        "min_segment_n": int(args.min_segment_samples),
        "global_platt": {
            "a": float(a),
            "b": float(b),
            "sample_n": label_n,
        },
        "global_residual_mean": float(global_residual_mean),
        "global_sigma": float(global_sigma),
        "segments": segment_payload,
        "metrics": metrics,
    }

    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    if bool(args.write_ssot):
        cur = conn.cursor()
        for sample in samples:
            decision_id = _to_int(sample.get("decision_id"))
            if decision_id is None:
                continue
            cur.execute("DELETE FROM calibration_samples WHERE decision_id = ?", (decision_id,))
            cur.execute(
                """
                INSERT INTO calibration_samples(
                    run_id, decision_id, run_date, slug, event_date, outcome, side, strategy,
                    q_raw, q_calibrated, q_calibrated_lcb, q_calibrated_ucb, q_calibrated_sigma,
                    spread, depth_ratio, tick_age_sec, days_to_event, segment_key, bundle_version, details_json, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample.get("run_id"),
                    decision_id,
                    sample.get("run_date"),
                    sample.get("slug"),
                    sample.get("event_date"),
                    sample.get("outcome"),
                    sample.get("side"),
                    sample.get("strategy"),
                    sample.get("q_raw"),
                    sample.get("q_calibrated"),
                    sample.get("q_calibrated_lcb"),
                    sample.get("q_calibrated_ucb"),
                    sample.get("q_calibrated_sigma"),
                    sample.get("spread"),
                    sample.get("depth_ratio"),
                    sample.get("tick_age_sec"),
                    sample.get("days_to_event"),
                    sample.get("segment_key"),
                    version,
                    json.dumps(sample.get("details_json") or {}, ensure_ascii=False),
                    sample.get("created_at") or ts_now.isoformat(),
                ),
            )
            label = sample.get("label")
            cur.execute("DELETE FROM calibration_labels WHERE decision_id = ?", (decision_id,))
            if label in {0, 1}:
                cur.execute(
                    """
                    INSERT INTO calibration_labels(
                        sample_id, decision_id, slug, event_date, outcome, side,
                        label, label_source, realized_pnl, path_hit, path_timeout, path_adverse,
                        state, resolved_at, created_at
                    )
                    VALUES (
                        (SELECT id FROM calibration_samples WHERE decision_id = ?),
                        ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                    )
                    """,
                    (
                        decision_id,
                        decision_id,
                        sample.get("slug"),
                        sample.get("event_date"),
                        sample.get("outcome"),
                        sample.get("side"),
                        int(label),
                        sample.get("label_source"),
                        sample.get("realized_pnl"),
                        1 if int(label) == 1 else 0,
                        0,
                        1 if int(label) == 0 else 0,
                        str(sample.get("label_source") or ""),
                        sample.get("resolved_at") or "",
                        ts_now.isoformat(),
                    ),
                )
        conn.commit()

    conn.close()
    print(
        json.dumps(
            {
                "ok": True,
                "bundle_path": str(out_path),
                "version": version,
                "sample_n": len(samples),
                "label_n": label_n,
                "segment_n": len(segment_payload),
                "metrics": metrics,
                "write_ssot": bool(args.write_ssot),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
