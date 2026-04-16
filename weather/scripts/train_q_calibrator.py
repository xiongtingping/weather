#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Tuple

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from edge_calibration_runtime import build_segment_key
from scripts.calibration_dataset_utils import days_to_event as dataset_days_to_event
from scripts.calibration_dataset_utils import load_first_q_settled_dataset


@dataclass
class OutcomeSpec:
    raw: str
    kind: str
    k: float
    k2: Optional[float]
    unit: Optional[str]
    precision: int


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


def _normalize_unit(unit: Optional[str]) -> Optional[str]:
    if not unit:
        return None
    text = str(unit).strip().lower().replace("°", "")
    if text in {"c", "celsius"}:
        return "C"
    if text in {"f", "fahrenheit"}:
        return "F"
    return str(unit).strip()


def _decimal_places(text: str) -> int:
    if "." in text:
        return len(text.split(".")[1])
    return 0


def _parse_outcome_label(label: str) -> OutcomeSpec:
    raw = str(label or "").strip()
    if not raw:
        raise ValueError("empty_label")
    normalized = raw.replace("–", "-")
    num = r"(-?\d+(?:\.\d+)?)"
    unit = r"([A-Za-z°/%]+)?"

    m = re.match(rf"^{num}\s*-\s*{num}\s*{unit}$", normalized)
    if m:
        p1 = _decimal_places(m.group(1))
        p2 = _decimal_places(m.group(2))
        return OutcomeSpec(
            raw=raw,
            kind="range",
            k=float(m.group(1)),
            k2=float(m.group(2)),
            unit=_normalize_unit(m.group(3)),
            precision=max(p1, p2),
        )

    m = re.match(rf"^{num}\s+to\s+{num}\s*{unit}$", normalized, flags=re.I)
    if m:
        p1 = _decimal_places(m.group(1))
        p2 = _decimal_places(m.group(2))
        return OutcomeSpec(
            raw=raw,
            kind="range",
            k=float(m.group(1)),
            k2=float(m.group(2)),
            unit=_normalize_unit(m.group(3)),
            precision=max(p1, p2),
        )

    m = re.match(rf"^{num}\s*{unit}\s+or\s+below$", normalized, flags=re.I)
    if m:
        return OutcomeSpec(
            raw=raw,
            kind="le",
            k=float(m.group(1)),
            k2=None,
            unit=_normalize_unit(m.group(2)),
            precision=_decimal_places(m.group(1)),
        )

    m = re.match(rf"^{num}\s*{unit}\s+or\s+(higher|above|more)$", normalized, flags=re.I)
    if m:
        return OutcomeSpec(
            raw=raw,
            kind="ge",
            k=float(m.group(1)),
            k2=None,
            unit=_normalize_unit(m.group(2)),
            precision=_decimal_places(m.group(1)),
        )

    m = re.match(rf"^{num}\s*{unit}$", normalized)
    if m:
        return OutcomeSpec(
            raw=raw,
            kind="eq",
            k=float(m.group(1)),
            k2=None,
            unit=_normalize_unit(m.group(2)),
            precision=_decimal_places(m.group(1)),
        )

    raise ValueError(f"unsupported_outcome_label:{label}")


def _apply_rounding(actual: float, rounding_rule: Optional[str], unit: Optional[str]) -> float:
    if str(rounding_rule or "").strip().lower() == "whole_degrees" and str(unit or "").upper() in {"C", "F"}:
        return float(math.floor(float(actual)))
    return float(actual)


def _bucket_hit(actual: float, label: str, rounding_rule: Optional[str]) -> Optional[bool]:
    try:
        spec = _parse_outcome_label(label)
    except Exception:
        return None
    value = _apply_rounding(float(actual), rounding_rule, spec.unit)
    if spec.kind == "eq":
        return abs(value - spec.k) <= 1e-9
    if spec.kind == "le":
        return value <= spec.k
    if spec.kind == "ge":
        return value >= spec.k
    if spec.kind == "range" and spec.k2 is not None:
        low = min(float(spec.k), float(spec.k2))
        high = max(float(spec.k), float(spec.k2))
        return low <= value <= high
    return None


def _bucket_label_for_value(
    rows: List[Dict[str, Any]],
    actual: Optional[float],
    rounding_rule: Optional[str],
) -> str:
    if actual is None or not math.isfinite(actual):
        return ""
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        outcome = str(row.get("outcome") or "").strip()
        if not outcome:
            continue
        hit = _bucket_hit(float(actual), outcome, rounding_rule)
        if hit is True:
            return outcome
    return ""


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
    trade_by_key: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    cur.execute(
        """
        SELECT
            t.slug,
            t.event_date,
            t.outcome,
            t.side,
            t.state,
            t.position_size,
            t.realized_pnl,
            t.updated_at
        FROM edge_trades t
        JOIN (
            SELECT slug, event_date, outcome, side, MAX(id) AS max_id
            FROM edge_trades
            GROUP BY slug, event_date, outcome, side
        ) latest
          ON latest.max_id = t.id
        """
    )
    for row in cur.fetchall():
        key = (
            str(row[0] or ""),
            str(row[1] or ""),
            str(row[2] or ""),
            str(row[3] or "").upper(),
        )
        trade_by_key[key] = {
            "state": str(row[4] or ""),
            "position_size": _to_float(row[5]),
            "realized_pnl": _to_float(row[6]),
            "resolved_at": str(row[7] or ""),
        }

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
            d.details_json
        FROM auto_trade_decisions d
        WHERE d.action = 'entry'
          AND d.will_order = 1
          AND datetime(d.created_at) >= datetime(?)
        ORDER BY d.id ASC
        """,
        (since_iso,),
    )
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        item = {
            "decision_id": row[0],
            "run_id": row[1],
            "run_date": str(row[2] or ""),
            "slug": str(row[3] or ""),
            "event_date": str(row[4] or ""),
            "outcome": str(row[5] or ""),
            "side": str(row[6] or ""),
            "created_at": str(row[7] or ""),
            "details_json": str(row[8] or ""),
        }
        trade_key = (
            item["slug"],
            item["event_date"],
            item["outcome"],
            str(item["side"] or "").upper(),
        )
        trade = trade_by_key.get(trade_key) or {}
        item["state"] = str(trade.get("state") or "")
        item["position_size"] = _to_float(trade.get("position_size"))
        item["realized_pnl"] = _to_float(trade.get("realized_pnl"))
        item["resolved_at"] = str(trade.get("resolved_at") or "")
        out.append(item)
    return out


def _load_settlement_map(conn: sqlite3.Connection) -> Dict[Tuple[str, str], Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            e.slug,
            e.event_date,
            e.settlement_actual,
            e.rounding_rule,
            e.market_unit,
            e.created_at
        FROM event_runs e
        JOIN (
            SELECT slug, event_date, MAX(id) AS max_id
            FROM event_runs
            WHERE settlement_actual IS NOT NULL
            GROUP BY slug, event_date
        ) latest
          ON latest.max_id = e.id
        WHERE e.settlement_actual IS NOT NULL
        """
    )
    mapping: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in cur.fetchall():
        key = (str(row[0] or ""), str(row[1] or ""))
        mapping[key] = {
            "settlement_actual": _to_float(row[2]),
            "rounding_rule": str(row[3] or ""),
            "market_unit": str(row[4] or ""),
            "resolved_at": str(row[5] or ""),
        }
    return mapping


def _label_from_settlement(
    *,
    side: str,
    outcome: str,
    settlement_actual: Optional[float],
    rounding_rule: Optional[str],
) -> Tuple[Optional[int], str]:
    if settlement_actual is None or not math.isfinite(settlement_actual):
        return None, "settlement_missing"
    side_u = str(side or "").strip().upper()
    if side_u not in {"YES", "NO"}:
        return None, "side_invalid"
    hit = _bucket_hit(float(settlement_actual), str(outcome or ""), rounding_rule)
    if hit is None:
        return None, "outcome_parse_failed"
    if side_u == "YES":
        return (1 if hit else 0), "settlement_actual_yes_hit"
    return (0 if hit else 1), "settlement_actual_no_hit"


def _dte_bucket(days_to_event: Optional[float]) -> str:
    if days_to_event is None or not math.isfinite(days_to_event):
        return "du"
    d = int(math.floor(float(days_to_event)))
    if d <= 0:
        return "d0"
    if d == 1:
        return "d1"
    return "d2p"


def _dte_bucket_key(regime: str, days_to_event: Optional[float]) -> str:
    bucket = _dte_bucket(days_to_event)
    d_part = {"d0": "d:0", "d1": "d:1", "d2p": "d:2p", "du": "d:u"}.get(bucket, "d:u")
    reg = str(regime or "").strip().lower() or "default"
    return f"reg:{reg}|{d_part}|s:u|r:u|t:u|sess:u"


def _select_platt_by_bucket(
    platt_by_dte: Dict[str, Dict[str, Any]],
    bucket: str,
    fallback: Tuple[float, float],
) -> Tuple[float, float, str]:
    info = platt_by_dte.get(bucket) if isinstance(platt_by_dte, dict) else None
    if isinstance(info, dict):
        a_val = _to_float(info.get("a"))
        b_val = _to_float(info.get("b"))
        if a_val is not None and b_val is not None:
            return float(a_val), float(b_val), str(bucket)
    return float(fallback[0]), float(fallback[1]), "global"


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
    return dataset_days_to_event(event_date, created_at)


def main() -> None:
    parser = argparse.ArgumentParser(description="Train q_raw->q_calibrated bundle (D0/D1/D2+ segmented platt).")
    parser.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "data" / "archive.db"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit-events", type=int, default=5000)
    parser.add_argument("--l2", type=float, default=0.2)
    parser.add_argument("--z", type=float, default=1.64)
    parser.add_argument("--min-label-samples", type=int, default=30)
    parser.add_argument("--min-dte-label-samples", type=int, default=20)
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
    events = load_first_q_settled_dataset(conn, since_iso, int(args.limit_events))
    samples: List[Dict[str, Any]] = []
    labeled_platt: List[Tuple[float, int, float]] = []
    labeled_platt_by_dte: Dict[str, List[Tuple[float, int, float]]] = defaultdict(list)

    for event in events:
        rows_event = event.get("rows")
        if not isinstance(rows_event, list) or not rows_event:
            continue
        settlement_actual = _to_float(event.get("settlement_actual"))
        rounding_rule = str(event.get("rounding_rule") or "")
        actual_label = _bucket_label_for_value(rows_event, settlement_actual, rounding_rule)
        if not actual_label:
            continue
        created_at = str(event.get("created_at") or "")
        event_date = str(event.get("event_date") or "")
        days_to_event = _days_to_event(event_date, created_at)
        base_decision_id = -max(1, _to_int(event.get("anchor_snapshot_id")))
        for idx, row in enumerate(rows_event, start=1):
            if not isinstance(row, dict):
                continue
            outcome = str(row.get("outcome") or "").strip()
            q_raw = _to_float(row.get("q_raw"))
            if q_raw is None:
                q_raw = _to_float(row.get("q_round"))
            if not outcome or q_raw is None:
                continue
            q_raw = min(max(float(q_raw), 1e-6), 1.0 - 1e-6)
            spread = _to_float(row.get("spread"))
            depth_ratio = _to_float(row.get("depth_ratio_yes"))
            if depth_ratio is None:
                depth_ratio = _to_float(row.get("depth_ratio"))
            tick_age = _to_float(row.get("tick_age_sec"))
            segment_key_full, seg_ctx = build_segment_key(
                days_to_event=days_to_event,
                spread=spread,
                depth_ratio=depth_ratio,
                tick_age_sec=tick_age,
                decision_ts=created_at,
            )
            dte_bucket = _dte_bucket(days_to_event)
            segment_key = _dte_bucket_key(seg_ctx.regime, days_to_event)
            label = 1 if outcome == actual_label else 0
            sample = {
                "decision_id": base_decision_id * 1000 - idx,
                "run_id": None,
                "run_date": "",
                "slug": str(event.get("slug") or ""),
                "event_date": event_date,
                "outcome": outcome,
                "side": "YES",
                "strategy": "q_settle_event_run",
                "q_raw": q_raw,
                "q_calibrated": _to_float(row.get("q_calibrated")),
                "q_calibrated_lcb": _to_float(row.get("q_calibrated_lcb")),
                "q_calibrated_ucb": _to_float(row.get("q_calibrated_ucb")),
                "q_calibrated_sigma": _to_float(row.get("q_calibrated_sigma")),
                "spread": spread,
                "depth_ratio": depth_ratio,
                "tick_age_sec": tick_age,
                "days_to_event": days_to_event,
                "dte_bucket": dte_bucket,
                "segment_key": segment_key,
                "segment_key_full": segment_key_full,
                "created_at": created_at,
                "label": label,
                "label_source": "settlement_actual_bucket_hit",
                "settlement_actual": settlement_actual,
                "rounding_rule": rounding_rule,
                "realized_pnl": None,
                "resolved_at": created_at,
                "details_json": row,
            }
            samples.append(sample)
            item = (float(q_raw), int(label), 1.0)
            labeled_platt.append(item)
            labeled_platt_by_dte[dte_bucket].append(item)

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
    a_global, b_global = fit
    min_dte_label_samples = max(1, int(args.min_dte_label_samples))
    platt_by_dte: Dict[str, Dict[str, Any]] = {}
    for bucket_key, bucket_samples in labeled_platt_by_dte.items():
        if len(bucket_samples) < min_dte_label_samples:
            continue
        fit_bucket = _fit_platt(bucket_samples, l2=float(args.l2))
        if fit_bucket is None:
            continue
        a_bucket, b_bucket = fit_bucket
        platt_by_dte[str(bucket_key)] = {
            "a": float(a_bucket),
            "b": float(b_bucket),
            "sample_n": int(len(bucket_samples)),
        }

    metrics_rows: List[Tuple[float, int]] = []
    metrics_rows_by_dte: Dict[str, List[Tuple[float, int]]] = defaultdict(list)
    residuals: List[float] = []
    residual_by_dte: Dict[str, List[float]] = defaultdict(list)
    residual_by_segment: Dict[str, List[float]] = defaultdict(list)
    for sample in samples:
        label = sample.get("label")
        if label not in {0, 1}:
            continue
        q_raw = float(sample["q_raw"])
        dte_bucket = str(sample.get("dte_bucket") or "du")
        a_use, b_use, platt_source = _select_platt_by_bucket(
            platt_by_dte,
            dte_bucket,
            (a_global, b_global),
        )
        p = _platt_predict(q_raw, a_use, b_use)
        sample["platt_source"] = platt_source
        metrics_rows.append((p, int(label)))
        metrics_rows_by_dte[dte_bucket].append((p, int(label)))
        residual = float(label) - p
        residuals.append(residual)
        residual_by_dte[dte_bucket].append(residual)
        residual_by_segment[str(sample.get("segment_key") or "default")].append(residual)

    if not residuals:
        print(json.dumps({"ok": False, "error": "no_residuals"}, ensure_ascii=False))
        raise SystemExit(4)

    global_residual_mean = sum(residuals) / float(len(residuals))
    global_residual_var = sum((x - global_residual_mean) ** 2 for x in residuals) / float(max(1, len(residuals) - 1))
    global_sigma = math.sqrt(max(1e-9, global_residual_var))

    dte_segment_payload: Dict[str, Dict[str, Any]] = {}
    for bucket_key, vals in residual_by_dte.items():
        n = len(vals)
        if n < max(1, int(args.min_segment_samples)):
            continue
        mean = sum(vals) / float(n)
        var = sum((x - mean) ** 2 for x in vals) / float(max(1, n - 1))
        dte_segment_payload[str(bucket_key)] = {
            "sample_n": int(n),
            "residual_mean": float(mean),
            "residual_sigma": float(math.sqrt(max(1e-9, var))),
        }

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
    metrics_by_dte = {
        str(bucket_key): _metrics(bucket_rows)
        for bucket_key, bucket_rows in metrics_rows_by_dte.items()
        if bucket_rows
    }
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
        "label_definition": "settlement_actual_bucket_hit",
        "z": float(args.z),
        "min_segment_n": int(args.min_segment_samples),
        "min_dte_label_samples": int(min_dte_label_samples),
        "segment_mode": "dte_d0_d1_d2p",
        "global_platt": {
            "a": float(a_global),
            "b": float(b_global),
            "sample_n": label_n,
        },
        "platt_by_dte": platt_by_dte,
        "global_residual_mean": float(global_residual_mean),
        "global_sigma": float(global_sigma),
        "dte_segments": dte_segment_payload,
        "segments": segment_payload,
        "metrics": metrics,
        "metrics_by_dte": metrics_by_dte,
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
                "dte_segment_n": len(dte_segment_payload),
                "platt_by_dte_n": len(platt_by_dte),
                "metrics": metrics,
                "metrics_by_dte": metrics_by_dte,
                "write_ssot": bool(args.write_ssot),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
