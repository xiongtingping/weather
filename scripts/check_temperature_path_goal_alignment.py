#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
UI_SERVER = ROOT / "ui" / "server.py"


def _to_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _same_prob(left: Any, right: Any, *, tol: float = 1e-9) -> bool:
    l_val = _to_float(left)
    r_val = _to_float(right)
    if l_val is None or r_val is None:
        return l_val is None and r_val is None
    return abs(l_val - r_val) <= tol


def _leg_by_role(plan: Mapping[str, Any], role: str) -> Mapping[str, Any]:
    legs = plan.get("legs")
    if not isinstance(legs, list):
        return {}
    for leg in legs:
        if isinstance(leg, Mapping) and str(leg.get("role") or "") == role:
            return leg
    return {}


def _zeroish(value: Any, *, tol: float = 1e-9) -> bool:
    val = _to_float(value)
    return val is not None and abs(val) <= tol


def _bucket_temp(label: Any) -> float | None:
    match = re.search(r"-?\d+(?:\.\d+)?", str(label or ""))
    if not match:
        return None
    return _to_float(match.group(0))


def _is_lower_boundary_bucket(label: Any) -> bool:
    text = str(label or "").strip().lower()
    return bool(text and ("or below" in text or "below" in text or text.startswith("<=")))


def _bucket_below_observed_floor(label: Any, observed_max: Any) -> bool:
    bucket_temp = _bucket_temp(label)
    observed = _to_float(observed_max)
    if bucket_temp is None or observed is None:
        return False
    if _is_lower_boundary_bucket(label):
        return float(observed) > float(bucket_temp) + 1e-9
    text = str(label or "").strip().lower()
    if "or higher" in text or "higher" in text or "above" in text or text.startswith(">="):
        return False
    return float(bucket_temp) < float(observed) - 1e-9


def _observed_max_floor(item: Mapping[str, Any], path: Mapping[str, Any], plan: Mapping[str, Any], risk: Mapping[str, Any]) -> float | None:
    outlook = item.get("target_bucket_outlook")
    outlook = outlook if isinstance(outlook, Mapping) else {}
    hard_direct = outlook.get("hard_source_direct")
    hard_direct = hard_direct if isinstance(hard_direct, Mapping) else {}
    path_hard = path.get("hard_source_state")
    path_hard = path_hard if isinstance(path_hard, Mapping) else {}
    risk_hard = risk.get("hard_source_state")
    risk_hard = risk_hard if isinstance(risk_hard, Mapping) else {}
    candidates: list[Any] = [
        path.get("observed_today_max_c"),
        path.get("observed_path_temp_c"),
        path_hard.get("observed_max_c"),
        risk_hard.get("observed_max_c"),
        outlook.get("observed_today_max_c"),
        hard_direct.get("observed_max_c"),
    ]
    for leg in plan.get("legs") if isinstance(plan.get("legs"), list) else []:
        if isinstance(leg, Mapping):
            candidates.append(leg.get("target_realization_observed_max_c"))
    vals = [val for val in (_to_float(raw) for raw in candidates) if val is not None]
    return max(vals) if vals else None


def _fetch_runtime_snapshot(url: str, timeout: float) -> Mapping[str, Any]:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _snapshot_rebuild_in_flight(payload: Mapping[str, Any]) -> bool:
    if not isinstance(payload, Mapping):
        return False
    facts = payload.get("facts")
    facts = facts if isinstance(facts, Mapping) else {}
    return bool(
        payload.get("busy") is True
        or payload.get("cache_rebuild_in_flight") is True
        or str(facts.get("source") or payload.get("snapshot_source") or "") == "ui_snapshot_rebuild_in_flight"
    )


def _fetch_runtime_snapshot_with_retries(
    url: str,
    timeout: float,
    *,
    rebuild_retries: int,
    rebuild_retry_delay_sec: float,
) -> Mapping[str, Any]:
    last_payload: Mapping[str, Any] = {}
    attempts = max(1, int(rebuild_retries) + 1)
    for index in range(attempts):
        payload = _fetch_runtime_snapshot(url, timeout)
        last_payload = payload
        if not _snapshot_rebuild_in_flight(payload):
            return payload
        if index < attempts - 1:
            time.sleep(max(0.0, float(rebuild_retry_delay_sec)))
    return last_payload


def _fetch_source_snapshot(run_date: str, limit: int) -> Mapping[str, Any]:
    from ui import server

    return server.ui_snapshot_summary(run_date, limit)


def _items_from_snapshot(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    facts = payload.get("facts")
    if isinstance(facts, Mapping) and isinstance(facts.get("items"), list):
        return [item for item in facts.get("items") or [] if isinstance(item, Mapping)]
    if isinstance(payload.get("items"), list):
        return [item for item in payload.get("items") or [] if isinstance(item, Mapping)]
    return []


def _check_item(item: Mapping[str, Any], index: int, *, run_date: str = "") -> dict[str, Any]:
    dist = item.get("temperature_path_distribution")
    plan = item.get("target_realization_capital_plan")
    if not isinstance(dist, Mapping):
        return {"index": index, "skipped": "temperature_path_distribution_missing"}
    blockers: list[str] = []
    warnings: list[str] = []
    if not isinstance(plan, Mapping):
        return {
            "index": index,
            "slug": item.get("slug"),
            "event_date": item.get("event_date"),
            "ok": False,
            "blockers": ["target_realization_capital_plan_missing"],
        }

    target_leg = _leg_by_role(plan, "target_yes")
    second_leg = _leg_by_role(plan, "second_target_yes")
    risk = plan.get("ui_risk_explanation")
    if not isinstance(risk, Mapping):
        risk = {}
    outlook = item.get("target_bucket_outlook")
    if not isinstance(outlook, Mapping):
        outlook = {}

    checks = {
        "target_bucket": str(plan.get("target_bucket") or "") == str(dist.get("target_bucket") or ""),
        "second_target_bucket": str(plan.get("second_target_bucket") or "") == str(dist.get("second_target_bucket") or ""),
        "target_posterior": _same_prob(plan.get("target_posterior"), dist.get("target_bucket_posterior")),
        "second_target_posterior": _same_prob(plan.get("second_target_posterior"), dist.get("second_target_bucket_posterior")),
        "target_path_probability": _same_prob(
            target_leg.get("target_realization_path_probability"),
            dist.get("target_bucket_path_probability"),
        ),
        "second_target_path_probability": _same_prob(
            second_leg.get("target_realization_path_probability"),
            dist.get("second_target_bucket_path_probability"),
        ),
        "risk_target_posterior": _same_prob(risk.get("target_posterior"), dist.get("target_bucket_posterior")),
        "risk_second_target_posterior": _same_prob(risk.get("second_target_posterior"), dist.get("second_target_bucket_posterior")),
        "risk_target_realization_probability": _same_prob(
            risk.get("target_realization_probability"),
            dist.get("target_bucket_path_probability"),
        ),
        "risk_second_realization_probability": _same_prob(
            risk.get("second_target_realization_probability"),
            dist.get("second_target_bucket_path_probability"),
        ),
    }
    for key, passed in checks.items():
        if not passed:
            blockers.append(f"{key}_mismatch")

    required_dist_fields = [
        "target_bucket_posterior",
        "second_target_bucket_posterior",
        "freshness",
        "reliability",
        "error_band",
        "risk_explanation",
        "trade_follow",
    ]
    for key in required_dist_fields:
        value = dist.get(key)
        if value in (None, "", {}, []):
            blockers.append(f"{key}_missing")
    bucket_distribution = dist.get("bucket_distribution")
    event_date = str(item.get("event_date") or "").strip().split("T", 1)[0]
    bucket_distribution_required = bool(run_date and event_date == str(run_date or "").strip())
    if bucket_distribution in (None, "", {}, []):
        if bucket_distribution_required:
            blockers.append("bucket_distribution_missing")
        else:
            warnings.append("bucket_distribution_missing_legacy_or_non_d0")
    if isinstance(bucket_distribution, list) and bucket_distribution:
        by_bucket = {
            str(item.get("bucket") or "").strip(): item
            for item in bucket_distribution
            if isinstance(item, Mapping) and str(item.get("bucket") or "").strip()
        }
        for bucket_key, label in (
            (str(dist.get("target_bucket") or "").strip(), "target"),
            (str(dist.get("second_target_bucket") or "").strip(), "second_target"),
        ):
            if not bucket_key:
                continue
            bucket_row = by_bucket.get(bucket_key)
            if not isinstance(bucket_row, Mapping):
                blockers.append(f"{label}_missing_from_bucket_distribution")
                continue
            dist_posterior_key = (
                "target_bucket_posterior" if label == "target" else "second_target_bucket_posterior"
            )
            dist_path_key = (
                "target_bucket_path_probability" if label == "target" else "second_target_bucket_path_probability"
            )
            if not _same_prob(bucket_row.get("posterior"), dist.get(dist_posterior_key)):
                blockers.append(f"{label}_bucket_distribution_posterior_mismatch")
            if not _same_prob(bucket_row.get("path_probability"), dist.get(dist_path_key)):
                blockers.append(f"{label}_bucket_distribution_path_probability_mismatch")
            if bucket_row.get("path_probability_available") is True and not str(
                bucket_row.get("path_probability_source") or ""
            ).strip():
                blockers.append(f"{label}_bucket_distribution_path_source_missing")
    elif bucket_distribution not in (None, "", {}, []):
        blockers.append("bucket_distribution_not_list")
    if not isinstance(dist.get("path_state"), Mapping):
        warnings.append("path_state_missing")

    trade_follow = dist.get("trade_follow")
    if isinstance(trade_follow, Mapping):
        allowed = trade_follow.get("allowed")
        target_lcb = _to_float(dist.get("target_bucket_path_probability_lcb"))
        if target_lcb is None and isinstance(dist.get("target"), Mapping):
            target_lcb = _to_float((dist.get("target") or {}).get("path_probability_lcb"))
        min_lcb_effective = _to_float(trade_follow.get("min_target_path_lcb_effective"))
        if min_lcb_effective is None:
            min_lcb_effective = _to_float(trade_follow.get("min_target_path_lcb"))
        min_posterior = _to_float(trade_follow.get("min_target_posterior"))
        target_posterior = _to_float(dist.get("target_bucket_posterior"))
        if allowed is False and plan.get("temperature_path_trade_follow_block_yes") is not True:
            blockers.append("trade_follow_block_not_reflected_in_plan")
        if allowed is True and plan.get("temperature_path_trade_follow_allowed") is not True:
            blockers.append("trade_follow_allow_not_reflected_in_plan")
        if allowed is True and min_lcb_effective is not None:
            if target_lcb is None:
                blockers.append("trade_follow_allowed_without_target_lcb")
            elif target_lcb + 1e-9 < min_lcb_effective:
                blockers.append("trade_follow_allowed_below_effective_lcb")
        if allowed is True and min_posterior is not None:
            if target_posterior is None:
                blockers.append("trade_follow_allowed_without_target_posterior")
            elif target_posterior + 1e-9 < min_posterior:
                blockers.append("trade_follow_allowed_below_min_posterior")
        adjustment = trade_follow.get("calibration_risk_adjustment")
        if isinstance(adjustment, Mapping) and adjustment.get("applied") is True:
            base_lcb = _to_float(
                adjustment.get("base_min_target_path_lcb")
                if adjustment.get("base_min_target_path_lcb") is not None
                else trade_follow.get("base_min_target_path_lcb")
            )
            if min_lcb_effective is None or base_lcb is None:
                blockers.append("calibration_adjustment_lcb_threshold_missing")
            elif min_lcb_effective <= base_lcb + 1e-9:
                blockers.append("calibration_adjustment_did_not_raise_lcb")
        if allowed is False:
            for role in ("target_yes", "second_target_yes"):
                leg = target_leg if role == "target_yes" else second_leg
                if not isinstance(leg, Mapping) or not leg:
                    continue
                action = str(leg.get("action") or "").strip()
                block_reason = str(leg.get("block_reason") or "").strip()
                if action in {"add", "increase", "buy", "rebalance_add"}:
                    blockers.append(f"{role}_allows_add_when_trade_follow_blocked")
                if block_reason != "temperature_path_trade_follow_not_allowed":
                    blockers.append(f"{role}_missing_temperature_path_block_reason")
                if not (_zeroish(leg.get("delta_notional")) or _zeroish(leg.get("target_notional"))):
                    blockers.append(f"{role}_has_positive_add_when_trade_follow_blocked")
    else:
        blockers.append("trade_follow_not_object")

    observed_floor = _observed_max_floor(item, dist, plan, risk)
    if observed_floor is not None:
        floor_checks = (
            ("temperature_path_distribution.target_bucket", dist.get("target_bucket")),
            ("target_realization_capital_plan.target_bucket", plan.get("target_bucket")),
            ("target_realization_capital_plan.ui_risk_explanation.target_bucket", risk.get("target_bucket")),
            ("target_bucket_outlook.target_bucket", outlook.get("target_bucket")),
            ("target_bucket_outlook.final_bucket", outlook.get("final_bucket")),
            ("target_bucket_outlook.posterior_final_bucket", outlook.get("posterior_final_bucket")),
            ("target_bucket_outlook.cumulative_posterior_final_bucket", outlook.get("cumulative_posterior_final_bucket")),
        )
        for field, label in floor_checks:
            if _bucket_below_observed_floor(label, observed_floor):
                blockers.append(f"{field}_below_observed_max_floor")

    return {
        "index": index,
        "slug": item.get("slug"),
        "event_date": item.get("event_date"),
        "ok": not blockers,
        "blockers": blockers,
        "warnings": warnings,
        "target": dist.get("target_bucket"),
        "target_posterior": dist.get("target_bucket_posterior"),
        "target_path_probability": dist.get("target_bucket_path_probability"),
        "second_target": dist.get("second_target_bucket"),
        "second_target_posterior": dist.get("second_target_bucket_posterior"),
        "second_target_path_probability": dist.get("second_target_bucket_path_probability"),
        "reliability": dist.get("reliability"),
        "freshness": dist.get("freshness"),
        "trade_follow": dist.get("trade_follow"),
    }


def _check_snapshot(payload: Mapping[str, Any], source: str) -> dict[str, Any]:
    facts = payload.get("facts") if isinstance(payload.get("facts"), Mapping) else {}
    if _snapshot_rebuild_in_flight(payload):
        return {
            "ok": False,
            "source": source,
            "snapshot_source": facts.get("source") or payload.get("source"),
            "temporary": True,
            "blockers": ["ui_snapshot_rebuild_in_flight"],
            "item_count": 0,
            "checked_count": 0,
            "items": [],
        }
    items = _items_from_snapshot(payload)
    run_date = str(payload.get("run_date") or facts.get("run_date") or "").strip().split("T", 1)[0]
    checked = [_check_item(item, index, run_date=run_date) for index, item in enumerate(items)]
    relevant = [entry for entry in checked if not entry.get("skipped")]
    blockers: list[str] = []
    if not relevant:
        blockers.append("no_temperature_path_distribution_items")
    for entry in relevant:
        for blocker in entry.get("blockers") or []:
            blockers.append(f"item_{entry.get('index')}:{blocker}")
    return {
        "ok": not blockers,
        "source": source,
        "snapshot_source": facts.get("source") or payload.get("source"),
        "item_count": len(items),
        "checked_count": len(relevant),
        "blockers": blockers,
        "items": checked,
    }


def _runtime_process_diagnostics() -> dict[str, Any]:
    diag: dict[str, Any] = {
        "ui_server_mtime": UI_SERVER.stat().st_mtime if UI_SERVER.exists() else None,
        "processes": [],
        "stale_process_detected": False,
    }
    try:
        proc = subprocess.run(
            ["ps", "-axo", "pid,lstart,command"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception as exc:
        diag["error"] = f"{exc.__class__.__name__}: {exc}"
        return diag
    if proc.returncode != 0:
        diag["error"] = proc.stderr.strip() or f"ps_exit_{proc.returncode}"
        return diag
    mtime = _to_float(diag.get("ui_server_mtime"))
    for line in proc.stdout.splitlines()[1:]:
        if str(UI_SERVER) not in line:
            continue
        parts = line.split(None, 6)
        if len(parts) < 7:
            continue
        pid = parts[0]
        lstart = " ".join(parts[1:6])
        command = parts[6]
        started_ts = None
        try:
            import time

            started_ts = time.mktime(time.strptime(lstart, "%a %b %d %H:%M:%S %Y"))
        except Exception:
            started_ts = None
        stale = bool(mtime is not None and started_ts is not None and started_ts + 1e-6 < float(mtime))
        if stale:
            diag["stale_process_detected"] = True
        diag["processes"].append(
            {
                "pid": pid,
                "started_at": lstart,
                "started_ts": started_ts,
                "command": command,
                "started_before_ui_server_mtime": stale,
            }
        )
    return diag


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only temperature path goal alignment audit.")
    parser.add_argument("--runtime", action="store_true", help="Check the running 3002 API instead of importing current source.")
    parser.add_argument("--url", default="http://127.0.0.1:3002/api/ui_snapshot_summary?limit=24")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--rebuild-retries", type=int, default=8)
    parser.add_argument("--rebuild-retry-delay-sec", type=float, default=1.5)
    parser.add_argument("--run-date", default="")
    parser.add_argument("--limit", type=int, default=24)
    args = parser.parse_args()

    if args.runtime:
        payload = _fetch_runtime_snapshot_with_retries(
            args.url,
            args.timeout,
            rebuild_retries=args.rebuild_retries,
            rebuild_retry_delay_sec=args.rebuild_retry_delay_sec,
        )
        result = _check_snapshot(payload, "runtime_api")
        result["runtime_process"] = _runtime_process_diagnostics()
    else:
        payload = _fetch_source_snapshot(args.run_date, args.limit)
        result = _check_snapshot(payload, "current_source_import")
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
