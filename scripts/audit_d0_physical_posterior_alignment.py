#!/usr/bin/env python3
"""Audit whether D0 physical posterior impact agrees with observed trajectory.

Read-only by design. The script uses persisted event_runs payloads as SSOT and
does not reconstruct weather evidence from external sources.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "archive.db"
DEFAULT_CHENGDU_SLUG = "highest-temperature-in-chengdu-on-june-13-2026"


def _to_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    return num if math.isfinite(num) else None


def _date_key(value: Any) -> str:
    return str(value or "").split("T", 1)[0]


def _label_center_c(label: Any) -> float | None:
    text = str(label or "").strip()
    if not text:
        return None
    cleaned = text.replace("°C", "").replace("C", "").strip()
    lower = cleaned.lower()
    import re

    nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", cleaned)]
    if not nums:
        return None
    if "or below" in lower or "below" in lower:
        return nums[0] - 0.5
    if "or above" in lower or "above" in lower:
        return nums[0] + 0.5
    if len(nums) >= 2:
        return (nums[0] + nums[1]) / 2.0
    return nums[0]


def _normalize(values: Sequence[float | None]) -> list[float]:
    nums = [max(0.0, float(v)) if v is not None and math.isfinite(float(v)) else 0.0 for v in values]
    total = sum(nums)
    if total <= 0:
        n = len(nums)
        return [1.0 / n for _ in nums] if n else []
    return [v / total for v in nums]


def _weighted_mean(labels: Sequence[str], probs: Sequence[float]) -> float | None:
    total = 0.0
    weighted = 0.0
    for label, prob in zip(labels, probs):
        center = _label_center_c(label)
        if center is None:
            continue
        total += float(prob)
        weighted += center * float(prob)
    return weighted / total if total > 0 else None


def _score_component_probs(rows: Sequence[Mapping[str, Any]], *, include_physical: bool) -> list[float]:
    scores: list[float | None] = []
    for row in rows:
        components = row.get("q_temperature_bucket_score_components")
        if not isinstance(components, Mapping):
            scores.append(None)
            continue
        total = 0.0
        seen = False
        for key, value in components.items():
            if key == "independent_physics_residual" and not include_physical:
                continue
            num = _to_float(value)
            if num is None:
                continue
            total += max(0.0, num)
            seen = True
        scores.append(total if seen else None)
    return _normalize(scores) if any(v is not None for v in scores) else []


def _posterior_without_physical(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    score_probs = _score_component_probs(rows, include_physical=False)
    if score_probs:
        return score_probs
    return _normalize([_to_float(row.get("q_bayes_posterior")) for row in rows])


def _posterior_with_physical(rows: Sequence[Mapping[str, Any]]) -> list[float]:
    score_probs = _score_component_probs(rows, include_physical=True)
    if score_probs:
        return score_probs
    probs = [_to_float(row.get("q_temperature_bucket_posterior")) for row in rows]
    if any(v is not None for v in probs):
        return _normalize(probs)
    return _normalize([_to_float(row.get("q_bayes_posterior")) for row in rows])


def _physical_evidence(row: Mapping[str, Any]) -> float:
    return float(_to_float(row.get("q_bayes_physical_likelihood_evidence")) or 0.0)


def _physical_enabled(rows: Sequence[Mapping[str, Any]]) -> bool:
    return any(bool(row.get("q_bayes_physical_likelihood_enabled")) for row in rows)


def _trajectory_direction(features: Mapping[str, Any]) -> tuple[str, str]:
    observed = _to_float(features.get("observed_temp_c") or features.get("observed_temp"))
    observed_max = _to_float(features.get("observed_today_max_c") or features.get("daily_max_observed_c"))
    rise_rate = _to_float(features.get("temperature_rise_rate_c_per_hour"))
    momentum_1h = _to_float(features.get("temperature_momentum_1h_c_per_hour"))
    momentum_2h = _to_float(features.get("temperature_momentum_2h_c_per_hour"))
    momentum_3h = _to_float(features.get("temperature_momentum_3h_c_per_hour"))
    momentum_rates = [v for v in (momentum_1h, momentum_2h, momentum_3h) if v is not None]
    momentum_effective = _to_float(features.get("temperature_momentum_effective_c_per_hour"))
    if momentum_effective is None and momentum_rates:
        weights = [0.50, 0.30, 0.20]
        pairs = [(rate, weights[idx]) for idx, rate in enumerate((momentum_1h, momentum_2h, momentum_3h)) if rate is not None]
        denom = sum(weight for _, weight in pairs) or 1.0
        momentum_effective = sum(rate * weight for rate, weight in pairs) / denom
    momentum_state = str(features.get("temperature_momentum_realization_state") or "").strip()
    forecast_peak = _to_float(
        features.get("settlement_source_forecast_peak_c")
        or features.get("wu_forecast_peak_c")
        or features.get("taf_forecast_peak_c")
        or features.get("forecast_peak_temp_c")
        or features.get("forecast_peak_temp")
    )
    hour = _to_float(features.get("observation_hour_local") or features.get("hour_local"))
    parts = []
    if observed is not None:
        parts.append(f"obs={observed:.1f}C")
    if observed_max is not None:
        parts.append(f"max={observed_max:.1f}C")
    if rise_rate is not None:
        parts.append(f"rise={rise_rate:+.2f}C/h")
    if momentum_1h is not None:
        parts.append(f"m1h={momentum_1h:+.2f}C/h")
    if momentum_2h is not None:
        parts.append(f"m2h={momentum_2h:+.2f}C/h")
    if momentum_3h is not None:
        parts.append(f"m3h={momentum_3h:+.2f}C/h")
    if momentum_state:
        parts.append(f"momentum={momentum_state}")
    if forecast_peak is not None:
        parts.append(f"fcst_peak={forecast_peak:.1f}C")
    if hour is not None:
        parts.append(f"hour={hour:.1f}")
    if observed is not None and forecast_peak is not None:
        gap = forecast_peak - observed
        parts.append(f"soft_gap={gap:+.1f}C")
        if gap > 2.0 and rise_rate is not None and rise_rate <= 0.0:
            parts.append("physical_potential_not_realized")
            return "weak_or_unrealized_warming", ", ".join(parts)
        if gap > 2.0 and len(momentum_rates) >= 2 and momentum_effective is not None and momentum_effective <= 0.15:
            parts.append("temperature_momentum_not_sustained")
            return "weak_or_unrealized_warming", ", ".join(parts)

    if len(momentum_rates) >= 2 and momentum_effective is not None:
        if momentum_state == "sustained_warming" or (
            momentum_1h is not None and momentum_1h >= 0.35 and momentum_effective >= 0.25
        ):
            return "warming", ", ".join(parts)
        if momentum_effective <= -0.15:
            return "cooling", ", ".join(parts)
    if rise_rate is not None:
        if rise_rate >= 0.35:
            return "warming", ", ".join(parts)
        if rise_rate <= -0.20:
            return "cooling", ", ".join(parts)
    if observed is not None and observed_max is not None and observed <= observed_max - 0.4:
        return "cooling", ", ".join(parts)
    if observed is not None and forecast_peak is not None and observed <= forecast_peak - 1.0 and (hour is None or hour < 15.5):
        return "warming", ", ".join(parts)
    return "neutral_or_unknown", ", ".join(parts) or "trajectory_fields_missing"


def _impact_direction(delta_mean_c: float | None, high_mass_pp: float, low_mass_pp: float) -> str:
    if delta_mean_c is not None:
        if delta_mean_c >= 0.05:
            return "warming"
        if delta_mean_c <= -0.05:
            return "cooling"
    if high_mass_pp - low_mass_pp >= 0.5:
        return "warming"
    if low_mass_pp - high_mass_pp >= 0.5:
        return "cooling"
    return "neutral"


def _audit_payload(row: sqlite3.Row) -> dict[str, Any] | None:
    try:
        payload = json.loads(str(row["payload_json"] or "{}"))
    except json.JSONDecodeError:
        return None
    rows_raw = payload.get("rows") or payload.get("distribution") or []
    rows = [item for item in rows_raw if isinstance(item, Mapping) and str(item.get("outcome") or item.get("label") or "").strip()]
    if len(rows) < 2:
        return None
    labels = [str(item.get("outcome") or item.get("label") or "") for item in rows]
    posterior = _posterior_with_physical(rows)
    no_phys = _posterior_without_physical(rows)
    if len(posterior) != len(no_phys) or not posterior:
        return None
    deltas = [(p - b) * 100.0 for p, b in zip(posterior, no_phys)]
    centers = [_label_center_c(label) for label in labels]
    base_mean = _weighted_mean(labels, no_phys)
    post_mean = _weighted_mean(labels, posterior)
    delta_mean = (post_mean - base_mean) if base_mean is not None and post_mean is not None else None
    split = base_mean if base_mean is not None else (sum(c for c in centers if c is not None) / max(1, sum(1 for c in centers if c is not None)))
    high_mass_pp = sum(delta for delta, center in zip(deltas, centers) if center is not None and center > split)
    low_mass_pp = sum(delta for delta, center in zip(deltas, centers) if center is not None and center < split)
    max_up_idx = max(range(len(deltas)), key=lambda i: deltas[i])
    max_down_idx = min(range(len(deltas)), key=lambda i: deltas[i])

    features = payload.get("weather_likelihood_features")
    if not isinstance(features, Mapping):
        features = {}
    traj_direction, traj_detail = _trajectory_direction(features)
    impact_direction = _impact_direction(delta_mean, high_mass_pp, low_mass_pp)
    enabled = _physical_enabled(rows)
    evidence = max(_physical_evidence(item) for item in rows)
    if evidence <= 0 or not enabled:
        consistency = "not_applicable_s_phys_gated"
    elif traj_direction in {"neutral_or_unknown"} or impact_direction == "neutral":
        consistency = "unknown_or_neutral"
    elif traj_direction == "weak_or_unrealized_warming" and impact_direction == "warming":
        consistency = "diverged_physical_potential_without_trajectory_realization"
    elif traj_direction == "weak_or_unrealized_warming" and impact_direction in {"cooling", "neutral"}:
        consistency = "aligned"
    elif traj_direction == impact_direction:
        consistency = "aligned"
    else:
        consistency = "diverged"

    first_expl = next(
        (item.get("q_bayes_physical_d0_explanation") for item in rows if isinstance(item.get("q_bayes_physical_d0_explanation"), Mapping)),
        {},
    )
    combo = first_expl.get("combination") if isinstance(first_expl, Mapping) and isinstance(first_expl.get("combination"), Mapping) else {}
    return {
        "id": row["id"],
        "created_at": str(row["created_at"] or ""),
        "slug": str(row["slug"] or ""),
        "event_date": _date_key(row["event_date"]),
        "run_date": _date_key(row["run_date"]),
        "s_phys_enabled": enabled,
        "s_phys_evidence": evidence,
        "posterior_scale": first_expl.get("posterior_scale") if isinstance(first_expl, Mapping) else None,
        "combo_label": combo.get("label") if isinstance(combo, Mapping) else None,
        "temperature_trajectory_direction": traj_direction,
        "temperature_trajectory_detail": traj_detail,
        "physical_impact_direction": impact_direction,
        "physical_delta_mean_c": delta_mean,
        "high_bucket_mass_delta_pp": high_mass_pp,
        "low_bucket_mass_delta_pp": low_mass_pp,
        "max_up_bucket": labels[max_up_idx],
        "max_up_pp": deltas[max_up_idx],
        "max_down_bucket": labels[max_down_idx],
        "max_down_pp": deltas[max_down_idx],
        "consistency": consistency,
    }


def _summarize(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    enabled = [row for row in rows if row.get("s_phys_enabled") and (_to_float(row.get("s_phys_evidence")) or 0.0) > 0]
    aligned = sum(1 for row in enabled if row.get("consistency") == "aligned")
    diverged = sum(1 for row in enabled if str(row.get("consistency") or "").startswith("diverged"))
    gated = sum(1 for row in rows if row.get("consistency") == "not_applicable_s_phys_gated")
    status = "ok"
    if not enabled:
        status = "waiting_for_s_phys_to_enter_posterior"
    elif diverged > 0:
        status = "needs_review_divergence_detected"
    return {
        "status": status,
        "sample_count": len(rows),
        "enabled_count": len(enabled),
        "gated_count": gated,
        "aligned_count": aligned,
        "diverged_count": diverged,
        "alignment_rate_enabled": (aligned / len(enabled)) if enabled else None,
    }


def _latest_chengdu_d0_key(conn: sqlite3.Connection) -> tuple[str, str, str] | None:
    rows = conn.execute(
        """
        SELECT slug, event_date, run_date, MAX(created_at) AS latest_created_at
        FROM event_runs
        WHERE lower(slug) LIKE '%chengdu%'
          AND event_date IS NOT NULL
          AND run_date IS NOT NULL
          AND substr(event_date, 1, 10) = substr(run_date, 1, 10)
        GROUP BY slug, event_date, run_date
        ORDER BY latest_created_at DESC
        LIMIT 1
        """
    ).fetchall()
    if not rows:
        return None
    row = rows[0]
    return str(row["slug"] or ""), _date_key(row["event_date"]), _date_key(row["run_date"])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DEFAULT_DB))
    parser.add_argument("--slug", default=DEFAULT_CHENGDU_SLUG)
    parser.add_argument("--event-date", default="2026-06-13")
    parser.add_argument("--run-date", default="")
    parser.add_argument("--limit", type=int, default=60)
    parser.add_argument("--latest-chengdu-d0", action="store_true", help="Audit the latest Chengdu D0 event_runs key.")
    parser.add_argument("--output", default="", help="Optional JSON report path. Writes a read-only audit artifact, not DB state.")
    parser.add_argument("--require-enabled", action="store_true", help="Exit 2 until at least one S_phys-enabled sample exists.")
    parser.add_argument("--fail-on-divergence", action="store_true", help="Exit 3 when enabled S_phys samples diverge from trajectory.")
    parser.add_argument("--json", action="store_true", help="Emit JSON only.")
    args = parser.parse_args()

    limit = max(1, min(int(args.limit or 60), 500))
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    slug = str(args.slug or "").strip()
    event_date = _date_key(args.event_date)
    run_date = _date_key(args.run_date) if args.run_date else ""
    if args.latest_chengdu_d0:
        latest_key = _latest_chengdu_d0_key(conn)
        if latest_key is None:
            payload = {
                "ok": False,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "error": "latest_chengdu_d0_not_found",
            }
            print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else "latest_chengdu_d0_not_found")
            return 1
        slug, event_date, run_date = latest_key
    params: list[Any] = [slug, event_date]
    run_filter = ""
    if run_date:
        run_filter = "AND run_date = ?"
        params.append(run_date)
    params.append(limit)
    rows = conn.execute(
        f"""
        SELECT id, slug, event_date, run_date, created_at, payload_json
        FROM event_runs
        WHERE slug = ? AND event_date = ?
          {run_filter}
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    audits = [item for item in (_audit_payload(row) for row in rows) if item is not None]
    payload = {
        "ok": True,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "slug": slug,
        "event_date": event_date,
        "run_date": run_date,
        "selection": "latest_chengdu_d0" if args.latest_chengdu_d0 else "explicit",
        "summary": _summarize(audits),
        "latest": audits[:10],
    }
    output_path = str(args.output or "").strip()
    if output_path:
        path = Path(output_path)
        if not path.is_absolute():
            path = ROOT / path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        payload["report_path"] = str(path)
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        if args.require_enabled and int(payload["summary"].get("enabled_count") or 0) <= 0:
            return 2
        if args.fail_on_divergence and int(payload["summary"].get("diverged_count") or 0) > 0:
            return 3
        return 0

    summary = payload["summary"]
    print(
        "status={status} samples={sample_count} enabled={enabled_count} "
        "gated={gated_count} aligned={aligned_count} diverged={diverged_count}".format(**summary)
    )
    for item in audits[:10]:
        delta_mean = _to_float(item.get("physical_delta_mean_c"))
        delta_text = f"{delta_mean:+.3f}C" if delta_mean is not None else "NA"
        print(
            f"{item['created_at']} id={item['id']} S={item['s_phys_evidence']:.2f} "
            f"{item['temperature_trajectory_direction']} -> {item['physical_impact_direction']} "
            f"mean_delta={delta_text} up={item['max_up_bucket']} {item['max_up_pp']:+.2f}pp "
            f"down={item['max_down_bucket']} {item['max_down_pp']:+.2f}pp "
            f"consistency={item['consistency']} detail={item['temperature_trajectory_detail']}"
        )
    if args.require_enabled and int(summary.get("enabled_count") or 0) <= 0:
        return 2
    if args.fail_on_divergence and int(summary.get("diverged_count") or 0) > 0:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
