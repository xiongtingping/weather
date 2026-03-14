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


def _days_to_event(event_date: str, created_at: str) -> Optional[float]:
    ts = _parse_ts(created_at)
    if ts is None:
        return None
    try:
        ev_day = datetime.strptime(str(event_date or "")[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    return float((ev_day - ts.date()).days)


def _clamp_prob(value: float, eps: float = 1e-6) -> float:
    return min(max(float(value), eps), 1.0 - eps)


def _safe_logit(prob: float) -> float:
    p = _clamp_prob(prob)
    return math.log(p / (1.0 - p))


def _safe_sigmoid(z: float) -> float:
    if z >= 0:
        exp_neg = math.exp(-z)
        return 1.0 / (1.0 + exp_neg)
    exp_pos = math.exp(z)
    return exp_pos / (1.0 + exp_pos)


def _predict_platt(q_raw: float, a: float, b: float) -> float:
    return _safe_sigmoid(float(a) * _safe_logit(float(q_raw)) + float(b))


def _fit_logit_linear(
    samples: List[Tuple[float, float, float]],
    *,
    l2: float,
) -> Optional[Tuple[float, float]]:
    if not samples:
        return None
    reg = max(0.0, float(l2))
    sw = reg
    sx = 0.0
    sy = 0.0
    sxx = reg
    sxy = 0.0
    for q_raw, y, w in samples:
        weight = float(w)
        if weight <= 0:
            continue
        x = _safe_logit(_clamp_prob(float(q_raw)))
        t = _safe_logit(_clamp_prob(float(y)))
        sw += weight
        sx += weight * x
        sy += weight * t
        sxx += weight * x * x
        sxy += weight * x * t
    det = sxx * sw - sx * sx
    if not math.isfinite(det) or abs(det) < 1e-12:
        return None
    a = (sxy * sw - sy * sx) / det
    b = (sy * sxx - sxy * sx) / det
    if not (math.isfinite(a) and math.isfinite(b)):
        return None
    return float(a), float(b)


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
) -> Tuple[float, float]:
    info = platt_by_dte.get(bucket) if isinstance(platt_by_dte, dict) else None
    if isinstance(info, dict):
        a_val = _to_float(info.get("a"))
        b_val = _to_float(info.get("b"))
        if a_val is not None and b_val is not None:
            return float(a_val), float(b_val)
    return float(fallback[0]), float(fallback[1])


def _error_metrics(samples: List[Tuple[float, float]]) -> Dict[str, Any]:
    if not samples:
        return {"n": 0, "mae": None, "rmse": None}
    abs_sum = 0.0
    sq_sum = 0.0
    for pred, target in samples:
        err = float(pred) - float(target)
        abs_sum += abs(err)
        sq_sum += err * err
    n = float(len(samples))
    return {
        "n": int(n),
        "mae": abs_sum / n,
        "rmse": math.sqrt(max(0.0, sq_sum / n)),
    }


def _top1_open_metric(samples: List[Dict[str, Any]]) -> Dict[str, Any]:
    by_event: Dict[Tuple[str, str], List[Dict[str, Any]]] = defaultdict(list)
    for item in samples:
        key = (str(item.get("slug") or ""), str(item.get("event_date") or ""))
        if not key[0] or not key[1]:
            continue
        by_event[key].append(item)
    if not by_event:
        return {"n": 0, "hit_rate": None}
    hits = 0
    total = 0
    for rows in by_event.values():
        if not rows:
            continue
        pred_top = max(rows, key=lambda x: float(x.get("pred") or 0.0))
        ask_top = max(rows, key=lambda x: float(x.get("best_ask") or 0.0))
        total += 1
        if str(pred_top.get("outcome") or "") == str(ask_top.get("outcome") or ""):
            hits += 1
    if total <= 0:
        return {"n": 0, "hit_rate": None}
    return {"n": int(total), "hit_rate": float(hits) / float(total)}


def _select_blend_alpha(samples: List[Dict[str, Any]]) -> Tuple[float, Dict[str, Any]]:
    if not samples:
        return 1.0, {"n": 0, "mae": None, "rmse": None, "top1_open": {"n": 0, "hit_rate": None}}
    best_alpha = 1.0
    best_metrics: Dict[str, Any] = {}
    best_tuple: Optional[Tuple[float, float, float]] = None
    candidates = [i / 20.0 for i in range(0, 21)]
    for alpha in candidates:
        metric_rows: List[Tuple[float, float]] = []
        for sample in samples:
            pred_cal = _to_float(sample.get("pred_calibrated"))
            q_raw = _to_float(sample.get("q_raw"))
            target = _to_float(sample.get("best_ask"))
            if pred_cal is None or q_raw is None or target is None:
                continue
            pred = max(1e-9, min(1.0 - 1e-9, alpha * float(pred_cal) + (1.0 - alpha) * float(q_raw)))
            sample[f"pred_blend_{alpha:.2f}"] = pred
            metric_rows.append((pred, float(target)))
        metrics = _error_metrics(metric_rows)
        top1 = _top1_open_metric(
            [
                {
                    **sample,
                    "pred": sample.get(f"pred_blend_{alpha:.2f}"),
                }
                for sample in samples
            ]
        )
        mae = _to_float(metrics.get("mae"))
        rmse = _to_float(metrics.get("rmse"))
        top1_hit = _to_float(top1.get("hit_rate"))
        if mae is None:
            continue
        cmp_tuple = (
            float(mae),
            float(rmse) if rmse is not None else 999.0,
            -(float(top1_hit) if top1_hit is not None else 0.0),
        )
        if best_tuple is None or cmp_tuple < best_tuple:
            best_tuple = cmp_tuple
            best_alpha = float(alpha)
            best_metrics = {
                "n": int(metrics.get("n") or 0),
                "mae": mae,
                "rmse": rmse,
                "top1_open": top1,
            }
    return best_alpha, best_metrics


def _load_open_first_rows(
    conn: sqlite3.Connection,
    since_iso: str,
    limit_events: int,
) -> List[Dict[str, Any]]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, slug, event_date, created_at, payload_json
        FROM event_runs
        WHERE payload_json IS NOT NULL
          AND payload_json != ''
          AND datetime(created_at) >= datetime(?)
        ORDER BY datetime(created_at) ASC, id ASC
        """,
        (since_iso,),
    )
    first_by_event: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in cur.fetchall():
        slug = str(row[1] or "").strip()
        event_date = str(row[2] or "").strip()
        created_at = str(row[3] or "").strip()
        if not slug or not event_date or not created_at:
            continue
        key = (slug, event_date)
        if key in first_by_event:
            continue
        try:
            payload = json.loads(str(row[4] or ""))
        except Exception:
            payload = {}
        if not isinstance(payload, dict):
            payload = {}
        rows = payload.get("rows") or payload.get("distribution") or []
        if not isinstance(rows, list) or not rows:
            continue
        first_by_event[key] = {
            "slug": slug,
            "event_date": event_date,
            "created_at": created_at,
            "rows": rows,
        }
        if len(first_by_event) >= max(1, int(limit_events)):
            break
    samples: List[Dict[str, Any]] = []
    for event in first_by_event.values():
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
            best_ask = _to_float(row.get("best_ask"))
            if not outcome or q_raw is None or best_ask is None:
                continue
            if not (math.isfinite(q_raw) and math.isfinite(best_ask)):
                continue
            spread = _to_float(row.get("spread"))
            depth_ratio = _to_float(row.get("depth_ratio_yes"))
            if depth_ratio is None:
                depth_ratio = _to_float(row.get("depth_ratio"))
            tick_age = _to_float(row.get("tick_age_sec"))
            segment_key_full, seg_ctx = build_segment_key(
                days_to_event=dte,
                spread=spread,
                depth_ratio=depth_ratio,
                tick_age_sec=tick_age,
                decision_ts=created_at,
            )
            samples.append(
                {
                    "slug": slug,
                    "event_date": event_date,
                    "created_at": created_at,
                    "outcome": outcome,
                    "q_raw": _clamp_prob(q_raw),
                    "best_ask": _clamp_prob(best_ask),
                    "spread": spread,
                    "depth_ratio": depth_ratio,
                    "tick_age_sec": tick_age,
                    "days_to_event": dte,
                    "dte_bucket": _dte_bucket(dte),
                    "segment_key": _dte_bucket_key(seg_ctx.regime, dte),
                    "segment_key_full": segment_key_full,
                }
            )
    return samples


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Train q_quote automatic bias calibration bundle (open best_ask target)."
    )
    parser.add_argument("--db", default=str(ROOT_DIR / "data" / "archive.db"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--limit-events", type=int, default=2000)
    parser.add_argument("--l2", type=float, default=0.1)
    parser.add_argument("--z", type=float, default=1.64)
    parser.add_argument("--min-label-samples", type=int, default=300)
    parser.add_argument("--min-dte-label-samples", type=int, default=80)
    parser.add_argument("--min-segment-samples", type=int, default=80)
    parser.add_argument(
        "--out",
        default=str(ROOT_DIR / "data" / "q_quote_calibration_bundle.candidate.json"),
    )
    args = parser.parse_args()

    db_path = Path(args.db).expanduser().resolve()
    if not db_path.exists():
        print(json.dumps({"ok": False, "error": f"db_missing:{db_path}"}, ensure_ascii=False))
        raise SystemExit(1)
    since_iso = (datetime.now(timezone.utc) - timedelta(days=max(1, int(args.days)))).isoformat(
        timespec="seconds"
    )
    conn = sqlite3.connect(str(db_path))
    samples = _load_open_first_rows(conn, since_iso, int(args.limit_events))
    conn.close()

    label_n = len(samples)
    if label_n < max(1, int(args.min_label_samples)):
        out = {
            "ok": False,
            "error": "insufficient_labels",
            "label_n": int(label_n),
            "min_label_samples": int(args.min_label_samples),
            "since": since_iso,
        }
        print(json.dumps(out, ensure_ascii=False))
        raise SystemExit(2)

    labeled: List[Tuple[float, float, float]] = []
    labeled_by_dte: Dict[str, List[Tuple[float, float, float]]] = defaultdict(list)
    for sample in samples:
        pair = (float(sample["q_raw"]), float(sample["best_ask"]), 1.0)
        labeled.append(pair)
        labeled_by_dte[str(sample.get("dte_bucket") or "du")].append(pair)

    fit_global = _fit_logit_linear(labeled, l2=float(args.l2))
    if fit_global is None:
        print(json.dumps({"ok": False, "error": "platt_fit_failed"}, ensure_ascii=False))
        raise SystemExit(3)
    a_global, b_global = fit_global

    min_dte_label_samples = max(1, int(args.min_dte_label_samples))
    platt_by_dte: Dict[str, Dict[str, Any]] = {}
    for bucket, bucket_samples in labeled_by_dte.items():
        if len(bucket_samples) < min_dte_label_samples:
            continue
        fit_bucket = _fit_logit_linear(bucket_samples, l2=float(args.l2))
        if fit_bucket is None:
            continue
        a_bucket, b_bucket = fit_bucket
        platt_by_dte[str(bucket)] = {
            "a": float(a_bucket),
            "b": float(b_bucket),
            "sample_n": int(len(bucket_samples)),
        }

    residuals: List[float] = []
    residual_by_dte: Dict[str, List[float]] = defaultdict(list)
    residual_by_segment: Dict[str, List[float]] = defaultdict(list)
    for sample in samples:
        bucket = str(sample.get("dte_bucket") or "du")
        a_use, b_use = _select_platt_by_bucket(platt_by_dte, bucket, (a_global, b_global))
        pred_base = _predict_platt(float(sample["q_raw"]), a_use, b_use)
        residual = float(sample["best_ask"]) - pred_base
        sample["pred_base"] = pred_base
        residuals.append(residual)
        residual_by_dte[bucket].append(residual)
        residual_by_segment[str(sample.get("segment_key") or "default")].append(residual)

    if not residuals:
        print(json.dumps({"ok": False, "error": "no_residuals"}, ensure_ascii=False))
        raise SystemExit(4)

    global_residual_mean = sum(residuals) / float(len(residuals))
    global_residual_var = sum((x - global_residual_mean) ** 2 for x in residuals) / float(
        max(1, len(residuals) - 1)
    )
    global_sigma = math.sqrt(max(1e-9, global_residual_var))

    dte_segments: Dict[str, Dict[str, Any]] = {}
    for bucket, values in residual_by_dte.items():
        n = len(values)
        if n < max(1, int(args.min_segment_samples)):
            continue
        mean = sum(values) / float(n)
        var = sum((x - mean) ** 2 for x in values) / float(max(1, n - 1))
        dte_segments[str(bucket)] = {
            "sample_n": int(n),
            "residual_mean": float(mean),
            "residual_sigma": float(math.sqrt(max(1e-9, var))),
        }

    segments: Dict[str, Dict[str, Any]] = {}
    for segment_key, values in residual_by_segment.items():
        n = len(values)
        if n < max(1, int(args.min_segment_samples)):
            continue
        mean = sum(values) / float(n)
        var = sum((x - mean) ** 2 for x in values) / float(max(1, n - 1))
        segments[segment_key] = {
            "sample_n": int(n),
            "residual_mean": float(mean),
            "residual_sigma": float(math.sqrt(max(1e-9, var))),
        }

    metric_rows_raw: List[Tuple[float, float]] = []
    metric_rows_calibrated: List[Tuple[float, float]] = []
    metric_rows_by_dte_raw: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    metric_rows_by_dte_calibrated: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for sample in samples:
        pred_base = _to_float(sample.get("pred_base"))
        if pred_base is None:
            continue
        bucket = str(sample.get("dte_bucket") or "du")
        seg = dte_segments.get(bucket) or {}
        res_mean = _to_float(seg.get("residual_mean"))
        if res_mean is None:
            res_mean = float(global_residual_mean)
        pred = _clamp_prob(pred_base + res_mean, eps=1e-9)
        sample["pred_calibrated"] = pred
        target = float(sample["best_ask"])
        q_raw = float(sample["q_raw"])
        metric_rows_raw.append((q_raw, target))
        metric_rows_calibrated.append((pred, target))
        metric_rows_by_dte_raw[bucket].append((q_raw, target))
        metric_rows_by_dte_calibrated[bucket].append((pred, target))

    blend_alpha, blend_metrics = _select_blend_alpha(samples)
    metric_rows_final: List[Tuple[float, float]] = []
    metric_rows_by_dte_final: Dict[str, List[Tuple[float, float]]] = defaultdict(list)
    for sample in samples:
        pred_cal = _to_float(sample.get("pred_calibrated"))
        q_raw = _to_float(sample.get("q_raw"))
        target = _to_float(sample.get("best_ask"))
        if pred_cal is None or q_raw is None or target is None:
            continue
        pred_final = _clamp_prob(
            float(blend_alpha) * float(pred_cal) + (1.0 - float(blend_alpha)) * float(q_raw),
            eps=1e-9,
        )
        sample["pred"] = pred_final
        bucket = str(sample.get("dte_bucket") or "du")
        metric_rows_final.append((pred_final, float(target)))
        metric_rows_by_dte_final[bucket].append((pred_final, float(target)))

    metrics = _error_metrics(metric_rows_final)
    metrics_by_dte = {
        str(bucket): _error_metrics(rows_bucket)
        for bucket, rows_bucket in metric_rows_by_dte_final.items()
        if rows_bucket
    }
    top1_open = _top1_open_metric(samples)
    metrics_raw = _error_metrics(metric_rows_raw)
    metrics_calibrated_no_blend = _error_metrics(metric_rows_calibrated)
    metrics_by_dte_raw = {
        str(bucket): _error_metrics(rows_bucket)
        for bucket, rows_bucket in metric_rows_by_dte_raw.items()
        if rows_bucket
    }
    metrics_by_dte_calibrated = {
        str(bucket): _error_metrics(rows_bucket)
        for bucket, rows_bucket in metric_rows_by_dte_calibrated.items()
        if rows_bucket
    }

    ts_now = datetime.now(timezone.utc).replace(microsecond=0)
    version = f"qquote_v1_{ts_now.strftime('%Y%m%dT%H%M%SZ')}"
    bundle = {
        "enabled": True,
        "version": version,
        "generated_at": ts_now.isoformat(),
        "source": {
            "db_path": str(db_path),
            "since": since_iso,
            "days": int(args.days),
            "limit_events": int(args.limit_events),
        },
        "sample_n": int(len(samples)),
        "label_n": int(label_n),
        "label_definition": "open_first_snapshot_best_ask_yes",
        "target": "market_quote_alignment",
        "z": float(args.z),
        "min_segment_n": int(args.min_segment_samples),
        "min_dte_label_samples": int(min_dte_label_samples),
        "segment_mode": "dte_d0_d1_d2p",
        "blend_alpha": float(blend_alpha),
        "global_platt": {
            "a": float(a_global),
            "b": float(b_global),
            "sample_n": int(label_n),
        },
        "platt_by_dte": platt_by_dte,
        "global_residual_mean": float(global_residual_mean),
        "global_sigma": float(global_sigma),
        "dte_segments": dte_segments,
        "segments": segments,
        "metrics": metrics,
        "metrics_raw": metrics_raw,
        "metrics_calibrated_no_blend": metrics_calibrated_no_blend,
        "metrics_by_dte": metrics_by_dte,
        "metrics_by_dte_raw": metrics_by_dte_raw,
        "metrics_by_dte_calibrated_no_blend": metrics_by_dte_calibrated,
        "blend_search_best": blend_metrics,
        "top1_open": top1_open,
    }

    out_path = Path(args.out).expanduser().resolve()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")

    print(
        json.dumps(
            {
                "ok": True,
                "bundle_path": str(out_path),
                "version": version,
                "sample_n": int(len(samples)),
                "label_n": int(label_n),
                "blend_alpha": float(blend_alpha),
                "metrics": metrics,
                "metrics_raw": metrics_raw,
                "metrics_calibrated_no_blend": metrics_calibrated_no_blend,
                "top1_open": top1_open,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
