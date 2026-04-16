#!/usr/bin/env python3
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
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

from edge_calibration_runtime import predict_q_calibrated
from scripts.calibration_dataset_utils import days_to_event as dataset_days_to_event
from scripts.calibration_dataset_utils import load_first_q_settled_dataset
from scripts.calibration_dataset_utils import load_joined_open_market_dataset


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


def _days_to_event(event_date: str, created_at: str) -> Optional[float]:
    return dataset_days_to_event(event_date, created_at)


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
        lo = min(float(spec.k), float(spec.k2))
        hi = max(float(spec.k), float(spec.k2))
        return lo <= value <= hi
    return None


def _safe_logloss(p: float, y: int) -> float:
    eps = 1e-12
    p2 = min(max(float(p), eps), 1.0 - eps)
    y2 = float(y)
    return -(y2 * math.log(p2) + (1.0 - y2) * math.log(1.0 - p2))


def _metric_summary(rows: List[Tuple[float, int]]) -> Dict[str, Any]:
    if not rows:
        return {"n": 0, "brier": None, "logloss": None, "ece": None}
    brier_sum = 0.0
    logloss_sum = 0.0
    bins: Dict[int, Dict[str, float]] = {i: {"n": 0.0, "p": 0.0, "y": 0.0} for i in range(10)}
    for p, y in rows:
        p2 = min(max(float(p), 1e-9), 1.0 - 1e-9)
        y2 = int(y)
        brier_sum += (p2 - float(y2)) ** 2
        logloss_sum += _safe_logloss(p2, y2)
        idx = min(9, max(0, int(p2 * 10.0)))
        bins[idx]["n"] += 1.0
        bins[idx]["p"] += p2
        bins[idx]["y"] += float(y2)
    total = float(len(rows))
    ece = 0.0
    for entry in bins.values():
        n = entry["n"]
        if n <= 0:
            continue
        p_mean = entry["p"] / n
        y_mean = entry["y"] / n
        ece += (n / total) * abs(p_mean - y_mean)
    return {
        "n": int(total),
        "brier": brier_sum / total,
        "logloss": logloss_sum / total,
        "ece": ece,
    }


def _population_stability_index(base_vals: List[float], cand_vals: List[float], bins: int = 10) -> Optional[float]:
    if not base_vals or not cand_vals:
        return None
    bins_n = max(5, int(bins))
    cuts = [i / float(bins_n) for i in range(bins_n + 1)]

    def _hist(vals: List[float]) -> List[float]:
        h = [0.0 for _ in range(bins_n)]
        for v in vals:
            x = min(max(float(v), 0.0), 1.0)
            idx = min(bins_n - 1, int(x * bins_n))
            h[idx] += 1.0
        total = sum(h)
        if total <= 0:
            return [0.0 for _ in range(bins_n)]
        return [max(1e-9, c / total) for c in h]

    p_base = _hist(base_vals)
    p_cand = _hist(cand_vals)
    psi = 0.0
    for pb, pc in zip(p_base, p_cand):
        psi += (pc - pb) * math.log(pc / pb)
    return psi


def _roi_stats(pairs: List[Tuple[float, float]], threshold: float = 0.5) -> Dict[str, Any]:
    selected = [float(pnl) for p, pnl in pairs if p >= threshold and math.isfinite(pnl)]
    n = len(selected)
    if n <= 0:
        return {"selected_n": 0, "roi_mean": None, "roi_lcb": None}
    mean = sum(selected) / float(n)
    if n == 1:
        return {"selected_n": 1, "roi_mean": mean, "roi_lcb": mean}
    var = sum((x - mean) ** 2 for x in selected) / float(n - 1)
    std = math.sqrt(max(0.0, var))
    lcb = mean - 1.64 * std / math.sqrt(float(n))
    return {"selected_n": n, "roi_mean": mean, "roi_lcb": lcb}


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


def _load_labeled_samples(conn: sqlite3.Connection, since_iso: str, limit: int) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            s.decision_id,
            s.q_raw,
            s.spread,
            s.depth_ratio,
            s.tick_age_sec,
            s.days_to_event,
            s.created_at,
            l.label,
            l.realized_pnl
        FROM calibration_samples s
        JOIN calibration_labels l ON l.decision_id = s.decision_id
        WHERE datetime(s.created_at) >= datetime(?)
        ORDER BY s.created_at DESC
        LIMIT ?
        """,
        (since_iso, max(1, int(limit))),
    )
    out: List[Dict[str, Any]] = []
    for row in cur.fetchall():
        out.append(
            {
                "decision_id": row[0],
                "q_raw": _to_float(row[1]),
                "spread": _to_float(row[2]),
                "depth_ratio": _to_float(row[3]),
                "tick_age_sec": _to_float(row[4]),
                "days_to_event": _to_float(row[5]),
                "created_at": str(row[6] or ""),
                "label": int(row[7]) if row[7] is not None else None,
                "realized_pnl": _to_float(row[8]),
            }
        )
    return out


def _load_open_first_snapshots(conn: sqlite3.Connection, since_iso: str, limit_events: int) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            id,
            slug,
            event_date,
            created_at,
            settlement_actual,
            rounding_rule,
            payload_json
        FROM event_runs
        WHERE settlement_actual IS NOT NULL
          AND payload_json IS NOT NULL
          AND payload_json != ''
          AND datetime(created_at) >= datetime(?)
        ORDER BY datetime(created_at) ASC, id ASC
        """,
        (since_iso,),
    )
    first_by_event: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in cur.fetchall():
        slug = str(row[1] or "")
        event_date = str(row[2] or "")
        if not slug or not event_date:
            continue
        key = (slug, event_date)
        if key in first_by_event:
            continue
        payload_text = str(row[6] or "")
        try:
            payload = json.loads(payload_text)
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        rows = payload.get("rows") or payload.get("distribution") or []
        if not isinstance(rows, list) or not rows:
            continue
        first_by_event[key] = {
            "id": int(row[0] or 0),
            "slug": slug,
            "event_date": event_date,
            "created_at": str(row[3] or ""),
            "settlement_actual": _to_float(row[4]),
            "rounding_rule": str(row[5] or ""),
            "rows": rows,
        }
        if len(first_by_event) >= max(1, int(limit_events)):
            break
    events = list(first_by_event.values())
    events.sort(key=lambda x: (str(x.get("event_date") or ""), str(x.get("slug") or "")))
    return events


def _resolve_actual_label(
    rows: List[Dict[str, Any]],
    actual: Optional[float],
    rounding_rule: Optional[str],
) -> Optional[str]:
    if actual is None or not math.isfinite(actual):
        return None
    for row in rows:
        if not isinstance(row, dict):
            continue
        outcome = str(row.get("outcome") or "").strip()
        if not outcome:
            continue
        hit = _bucket_hit(float(actual), outcome, rounding_rule)
        if hit is True:
            return outcome
    return None


def _top1_from_open_snapshot(
    rows: List[Dict[str, Any]],
    *,
    bundle: Dict[str, Any],
    created_at: str,
    event_date: str,
) -> Optional[str]:
    best_label: Optional[str] = None
    best_prob = -1.0
    dte = _days_to_event(event_date, created_at)
    for row in rows:
        if not isinstance(row, dict):
            continue
        outcome = str(row.get("outcome") or "").strip()
        if not outcome:
            continue
        q_raw = _to_float(row.get("q_raw"))
        if q_raw is None:
            q_raw = _to_float(row.get("q_round"))
        if q_raw is None:
            continue
        pred = predict_q_calibrated(
            bundle=bundle,
            q_raw=q_raw,
            spread=_to_float(row.get("spread")),
            depth_ratio=_to_float(row.get("depth_ratio_yes")) or _to_float(row.get("depth_ratio")),
            tick_age_sec=_to_float(row.get("tick_age_sec")),
            days_to_event=dte,
            decision_ts=created_at,
        )
        p = _to_float(pred.get("q_calibrated_mean"))
        if p is None:
            p = q_raw
        if p > best_prob:
            best_prob = float(p)
            best_label = outcome
    return best_label


def _market_top1_from_open_snapshot(rows: List[Dict[str, Any]]) -> Optional[str]:
    best_label: Optional[str] = None
    best_ask = -1.0
    for row in rows:
        if not isinstance(row, dict):
            continue
        outcome = str(row.get("outcome") or "").strip()
        ask = _to_float(row.get("best_ask"))
        if not outcome or ask is None or not math.isfinite(ask):
            continue
        if ask > best_ask:
            best_ask = float(ask)
            best_label = outcome
    return best_label


def _top1_metric_summary(hits: List[int]) -> Dict[str, Any]:
    n = len(hits)
    if n <= 0:
        return {"n": 0, "hit_rate": None}
    hit_rate = sum(1 for v in hits if int(v) == 1) / float(n)
    return {"n": int(n), "hit_rate": float(hit_rate)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate q calibration candidate bundle vs champion.")
    parser.add_argument("--db", default=str(Path(__file__).resolve().parents[1] / "data" / "archive.db"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=5000)
    parser.add_argument("--open-limit-events", type=int, default=2000)
    parser.add_argument("--candidate", default=str(Path(__file__).resolve().parents[1] / "data" / "q_calibration_bundle.candidate.json"))
    parser.add_argument("--champion", default=str(Path(__file__).resolve().parents[1] / "data" / "q_calibration_bundle.json"))
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
    since_iso = (datetime.now(timezone.utc) - timedelta(days=max(1, int(args.days)))).isoformat(timespec="seconds")

    conn = sqlite3.connect(str(db_path))
    _ensure_eval_table(conn)
    settled_events = load_first_q_settled_dataset(conn, since_iso, int(args.limit))
    if not settled_events:
        out = {"ok": False, "error": "no_settled_event_dataset", "since": since_iso}
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(3)

    cand_metric_rows: List[Tuple[float, int]] = []
    champ_metric_rows: List[Tuple[float, int]] = []
    cand_roi_pairs: List[Tuple[float, float]] = []
    champ_roi_pairs: List[Tuple[float, float]] = []
    cand_probs: List[float] = []
    champ_probs: List[float] = []

    for event in settled_events:
        rows_event = event.get("rows")
        if not isinstance(rows_event, list) or not rows_event:
            continue
        actual_label = _resolve_actual_label(
            rows_event,
            _to_float(event.get("settlement_actual")),
            str(event.get("rounding_rule") or ""),
        )
        if not actual_label:
            continue
        created_at = str(event.get("created_at") or "")
        event_date = str(event.get("event_date") or "")
        dte = _days_to_event(event_date, created_at)
        for row in rows_event:
            if not isinstance(row, dict):
                continue
            outcome = str(row.get("outcome") or "").strip()
            q_raw = _to_float(row.get("q_raw"))
            if q_raw is None:
                q_raw = _to_float(row.get("q_round"))
            if not outcome or q_raw is None:
                continue
            label = 1 if outcome == actual_label else 0
            kwargs = {
                "q_raw": q_raw,
                "spread": _to_float(row.get("spread")),
                "depth_ratio": _to_float(row.get("depth_ratio_yes")) or _to_float(row.get("depth_ratio")),
                "tick_age_sec": _to_float(row.get("tick_age_sec")),
                "days_to_event": dte,
                "decision_ts": created_at,
            }
            cand_pred = predict_q_calibrated(bundle=candidate_bundle, **kwargs)
            champ_pred = predict_q_calibrated(bundle=champion_bundle, **kwargs)
            cand_p = _to_float(cand_pred.get("q_calibrated_mean"))
            champ_p = _to_float(champ_pred.get("q_calibrated_mean"))
            if cand_p is None:
                cand_p = q_raw
            if champ_p is None:
                champ_p = q_raw
            cand_metric_rows.append((cand_p, int(label)))
            champ_metric_rows.append((champ_p, int(label)))
            cand_probs.append(cand_p)
            champ_probs.append(champ_p)

    if not cand_metric_rows:
        out = {"ok": False, "error": "settled_dataset_empty_after_labeling", "since": since_iso}
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(4)

    cand_metrics = _metric_summary(cand_metric_rows)
    champ_metrics = _metric_summary(champ_metric_rows)
    cand_roi = _roi_stats(cand_roi_pairs)
    champ_roi = _roi_stats(champ_roi_pairs)
    psi = _population_stability_index(champ_probs, cand_probs, bins=10)

    open_events = load_joined_open_market_dataset(
        conn,
        since_iso,
        int(args.open_limit_events),
        require_settlement=True,
    )
    if not open_events:
        out = {"ok": False, "error": "no_open_first_snapshot_dataset", "since": since_iso}
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(5)
    cand_top1_hits: List[int] = []
    champ_top1_hits: List[int] = []
    market_top1_hits: List[int] = []
    top1_event_n = 0
    top1_skipped = 0
    for event in open_events:
        rows_event = event.get("rows")
        if not isinstance(rows_event, list) or not rows_event:
            top1_skipped += 1
            continue
        actual_label = _resolve_actual_label(
            rows_event,
            _to_float(event.get("settlement_actual")),
            str(event.get("rounding_rule") or ""),
        )
        if not actual_label:
            top1_skipped += 1
            continue
        created_at = str(event.get("created_at") or "")
        event_date = str(event.get("event_date") or "")
        cand_top = _top1_from_open_snapshot(
            rows_event,
            bundle=candidate_bundle,
            created_at=created_at,
            event_date=event_date,
        )
        champ_top = _top1_from_open_snapshot(
            rows_event,
            bundle=champion_bundle,
            created_at=created_at,
            event_date=event_date,
        )
        market_top = _market_top1_from_open_snapshot(rows_event)
        if not cand_top or not champ_top:
            top1_skipped += 1
            continue
        cand_top1_hits.append(1 if cand_top == actual_label else 0)
        champ_top1_hits.append(1 if champ_top == actual_label else 0)
        if market_top:
            market_top1_hits.append(1 if market_top == actual_label else 0)
        top1_event_n += 1

    if top1_event_n <= 0:
        out = {"ok": False, "error": "open_top1_dataset_empty", "since": since_iso}
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(6)

    cand_top1 = _top1_metric_summary(cand_top1_hits)
    champ_top1 = _top1_metric_summary(champ_top1_hits)
    market_top1 = _top1_metric_summary(market_top1_hits)

    brier_ok = (
        cand_metrics.get("brier") is not None
        and champ_metrics.get("brier") is not None
        and float(cand_metrics["brier"]) <= float(champ_metrics["brier"]) + 1e-12
    )
    logloss_ok = (
        cand_metrics.get("logloss") is not None
        and champ_metrics.get("logloss") is not None
        and float(cand_metrics["logloss"]) <= float(champ_metrics["logloss"]) + 1e-12
    )
    ece_ok = (
        cand_metrics.get("ece") is not None
        and champ_metrics.get("ece") is not None
        and float(cand_metrics["ece"]) <= float(champ_metrics["ece"]) + 1e-12
    )
    roi_ok = (
        cand_roi.get("roi_lcb") is not None
        and champ_roi.get("roi_lcb") is not None
        and float(cand_roi["roi_lcb"]) >= float(champ_roi["roi_lcb"]) - 1e-12
    ) or (champ_roi.get("roi_lcb") is None and cand_roi.get("roi_lcb") is not None)
    top1_open_ok = (
        cand_top1.get("hit_rate") is not None
        and champ_top1.get("hit_rate") is not None
        and float(cand_top1["hit_rate"]) > float(champ_top1["hit_rate"]) + 1e-12
    )
    recommend_promote = bool(brier_ok and logloss_ok and top1_open_ok)

    notes = {
        "candidate_bundle": str(cand_path),
        "champion_bundle": str(champ_path) if champ_path.exists() else "",
        "candidate_metrics": cand_metrics,
        "champion_metrics": champ_metrics,
        "candidate_top1_open": cand_top1,
        "champion_top1_open": champ_top1,
        "market_top1_open": market_top1,
        "candidate_roi": cand_roi,
        "champion_roi": champ_roi,
        "datasets": {
            "labeled_sample_n": int(cand_metrics.get("n") or 0),
            "open_first_snapshot_n": int(top1_event_n),
            "open_first_snapshot_skipped": int(top1_skipped),
            "since": since_iso,
        },
        "rules": {
            "brier_ok": bool(brier_ok),
            "logloss_ok": bool(logloss_ok),
            "ece_ok": bool(ece_ok),
            "roi_ok": bool(roi_ok),
            "top1_open_ok": bool(top1_open_ok),
        },
        "recommend_promote": bool(recommend_promote),
        "sample_n": int(cand_metrics.get("n") or 0),
        "days": int(args.days),
    }

    eval_id = None
    if bool(args.write_db):
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO calibration_eval_runs(
                model_version, bundle_path, sample_n, label_n, ece, brier, logloss, psi,
                roi_mean, roi_lcb, champion, promoted, status, notes_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(candidate_bundle.get("version") or ""),
                str(cand_path),
                int(cand_metrics.get("n") or 0),
                int(cand_metrics.get("n") or 0),
                _to_float(cand_metrics.get("ece")),
                _to_float(cand_metrics.get("brier")),
                _to_float(cand_metrics.get("logloss")),
                _to_float(psi),
                _to_float(cand_roi.get("roi_mean")),
                _to_float(cand_roi.get("roi_lcb")),
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
        "market_top1_open": market_top1,
        "candidate_roi": cand_roi,
        "champion_roi": champ_roi,
        "rules": notes.get("rules"),
        "psi": psi,
        "eval_id": eval_id,
    }
    print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
